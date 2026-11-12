import os
import io
import re
import time
import base64
import random
import textwrap
import asyncio
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
from aiohttp import web
import aiosqlite
from PIL import Image, ImageDraw, ImageFont

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DB_FILE = "bot_database.db"

MODELS_POOL = [
    "gemini-3-flash",
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-robotics-er-2-preview",
    "gemma-4-31b-it",
    "gemma-4-26b-it",
]

PERSONAS = {
    "statham": {
        "name": "Джейсон Стетхом 🐺",
        "prompt": (
            "Ты Джейсон Стетхом. Общайся как брутальный пацан с улицы и верный друг в Discord-чате. "
            "Отвечай коротко, по делу, с жесткой иронией, иногда вставляй абсурдные пацанские цитаты. "
            "Никакого официоза и соплей. Отвечай сразу с первого слова."
        )
    },
    "senior": {
        "name": "Душный Синьор 💻",
        "prompt": (
            "Ты Lead Software Engineer. Везде видишь техдолг, неоптимальность и костыли. "
            "Вздыхаешь, советуешь всё переписать на Rust/C++, докапываешься до архитектуры и нейминга."
        )
    },
    "toxic": {
        "name": "Токсичный Роастер 💀",
        "prompt": (
            "Ты язвительный интернет-тролль. Твоя цель — смешно и ядовито подколоть собеседника за любую "
            "глупость, опечатку или наивность. Держи планку жесткого, но дружеского сарказма."
        )
    },
    "chill": {
        "name": "Чилловый Зумер 🤙",
        "prompt": (
            "Ты максимально ленивый зумер на чилле. Сленг: 'пон', 'жиза', 'кринж', 'вайб', 'база'. "
            "Пиши маленькими буквами, без знаков препинания, кратко и расслабленно."
        )
    }
}

TEXT_EXTENSIONS = (
    ".txt", ".py", ".js", ".ts", ".json", ".html", ".css", 
    ".cpp", ".c", ".cs", ".java", ".go", ".rs", ".md", ".log", 
    ".xml", ".yaml", ".yml", ".sql", ".sh"
)

AUDIO_EXTENSIONS = (".ogg", ".mp3", ".wav", ".m4a", ".aac")

http_session: aiohttp.ClientSession | None = None
channel_spontaneous_cooldown = defaultdict(float)


# ==================== БАЗА ДАННЫХ (SQLite WAL) ====================
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_settings (
                channel_id INTEGER PRIMARY KEY,
                model TEXT DEFAULT 'auto',
                persona TEXT DEFAULT 'statham',
                web_search INTEGER DEFAULT 1,
                spontaneous INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER,
                role TEXT,
                author TEXT,
                text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_dossier (
                guild_id INTEGER,
                user_id INTEGER,
                user_name TEXT,
                facts TEXT,
                PRIMARY KEY (guild_id, user_id)
            )
        """)
        await db.commit()


async def get_settings(channel_id: int) -> dict:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT model, persona, web_search, spontaneous FROM channel_settings WHERE channel_id = ?", (channel_id,)) as cur:
            row = await cur.fetchone()
            if row:
                return {"model": row[0], "persona": row[1], "web_search": bool(row[2]), "spontaneous": bool(row[3])}
            return {"model": "auto", "persona": "statham", "web_search": True, "spontaneous": True}


async def update_setting(channel_id: int, key: str, value):
    settings = await get_settings(channel_id)
    settings[key] = value
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO channel_settings (channel_id, model, persona, web_search, spontaneous)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                model = excluded.model,
                persona = excluded.persona,
                web_search = excluded.web_search,
                spontaneous = excluded.spontaneous
        """, (channel_id, settings["model"], settings["persona"], int(settings["web_search"]), int(settings["spontaneous"])))
        await db.commit()


async def save_memory(channel_id: int, role: str, author: str, text: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("INSERT INTO channel_memory (channel_id, role, author, text) VALUES (?, ?, ?, ?)", (channel_id, role, author, text))
        await db.execute("""
            DELETE FROM channel_memory WHERE id NOT IN (
                SELECT id FROM channel_memory WHERE channel_id = ? ORDER BY id DESC LIMIT 30
            ) AND channel_id = ?
        """, (channel_id, channel_id))
        await db.commit()


async def get_memory(channel_id: int) -> list:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT role, author, text FROM channel_memory WHERE channel_id = ? ORDER BY id ASC", (channel_id,)) as cur:
            rows = await cur.fetchall()
            contents = []
            for role, author, text in rows:
                if role == "user":
                    contents.append({"role": "user", "parts": [{"text": f"[{author}]: {text}"}]})
                else:
                    contents.append({"role": "model", "parts": [{"text": text}]})
            return contents


async def clear_channel_memory(channel_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM channel_memory WHERE channel_id = ?", (channel_id,))
        await db.commit()


async def get_user_facts(guild_id: int, user_id: int) -> str:
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT facts FROM user_dossier WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)) as cur:
            row = await cur.fetchone()
            return row[0] if row else ""


async def save_user_fact(guild_id: int, user_id: int, user_name: str, fact: str):
    existing = await get_user_facts(guild_id, user_id)
    new_facts = f"{existing}\n• {fact}".strip() if existing else f"• {fact}"
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO user_dossier (guild_id, user_id, user_name, facts) VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET facts = excluded.facts, user_name = excluded.user_name
        """, (guild_id, user_id, user_name, new_facts))
        await db.commit()


# ==================== УТИЛИТЫ И ВСПОМОГАТЕЛЬНЫЙ КОД ====================
async def run_piston_code(language: str, code: str) -> str:
    url = "https://emkc.org/api/v2/piston/execute"
    lang_map = {
        "py": "python", "python": "python", "js": "javascript", 
        "ts": "typescript", "cpp": "c++", "c": "c", "rs": "rust", 
        "go": "go", "sh": "bash", "bash": "bash"
    }
    lang = lang_map.get(language.lower(), language.lower())
    payload = {"language": lang, "version": "*", "files": [{"content": code}]}
    try:
        async with http_session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                data = await resp.json()
                run_res = data.get("run", {})
                stdout = run_res.get("stdout", "").strip()
                stderr = run_res.get("stderr", "").strip()
                return stdout or stderr or "✅ Код успешно выполнился (пустой вывод)."
            return f"❌ Ошибка Piston API: HTTP {resp.status}"
    except Exception as e:
        return f"❌ Ошибка запуска: {e}"


def create_quote_card(text: str, author_name: str, avatar_bytes: bytes = None) -> io.BytesIO:
    w, h = 1000, 500
    img = Image.new("RGB", (w, h), color=(18, 18, 24))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle([(20, 20), (w - 20, h - 20)], radius=20, outline=(45, 45, 60), width=2)
    draw.line([(50, h - 90), (w - 50, h - 90)], fill=(40, 40, 55), width=2)

    font_large = ImageFont.load_default()
    font_small = ImageFont.load_default()

    if avatar_bytes:
        try:
            av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((70, 70))
            mask = Image.new("L", (70, 70), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 70, 70), fill=255)
            img.paste(av, (50, h - 80), mask)
        except Exception:
            pass

    wrapped = textwrap.wrap(f"«{text}»", width=38)
    quote_str = "\n".join(wrapped)

    draw.multiline_text((w // 2, (h - 100) // 2), quote_str, fill=(245, 245, 250), font=font_large, anchor="mm", align="center", spacing=14)
    draw.text((135 if avatar_bytes else 50, h - 55), f"© {author_name}", fill=(170, 170, 190), font=font_small, anchor="ls")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def split_text_with_markdown(text: str, limit: int = 1950) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        split_idx = text.rfind("\n", 0, limit)
        if split_idx == -1:
            split_idx = text.rfind(" ", 0, limit)
        if split_idx == -1:
            split_idx = limit

        chunk = text[:split_idx]
        text = text[split_idx:].strip()

        code_matches = re.findall(r"```(\w*)", chunk)
        if len(code_matches) % 2 != 0:
            last_lang = code_matches[-1]
            chunk += "\n```"
            text = f"```{last_lang}\n" + text

        chunks.append(chunk)

    return chunks


# ==================== UI: ПАНЕЛЬ УПРАВЛЕНИЯ ====================
class ControlPanelView(discord.ui.View):
    def __init__(self, settings: dict):
        super().__init__(timeout=180)
        self.settings = settings
        self.add_item(ModelDropdown(settings["model"]))
        self.add_item(PersonaDropdown(settings["persona"]))

    @discord.ui.button(label="🌐 Поиск Google", style=discord.ButtonStyle.secondary, row=2)
    async def toggle_search(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_state = not self.settings["web_search"]
        await update_setting(interaction.channel_id, "web_search", new_state)
        self.settings["web_search"] = new_state
        await self.refresh_view(interaction)

    @discord.ui.button(label="🎲 Спонтанность", style=discord.ButtonStyle.secondary, row=2)
    async def toggle_spontaneous(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_state = not self.settings["spontaneous"]
        await update_setting(interaction.channel_id, "spontaneous", new_state)
        self.settings["spontaneous"] = new_state
        await self.refresh_view(interaction)

    @discord.ui.button(label="🧹 Сброс памяти", style=discord.ButtonStyle.danger, row=2)
    async def clear_mem(self, interaction: discord.Interaction, button: discord.ui.Button):
        await clear_channel_memory(interaction.channel_id)
        await interaction.response.send_message("🧹 Память диалога в этом канале очищена!", ephemeral=True)

    async def refresh_view(self, interaction: discord.Interaction):
        embed = build_menu_embed(self.settings)
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.Forbidden:
            await interaction.response.edit_message(content=build_menu_text(self.settings), view=self)


class ModelDropdown(discord.ui.Select):
    def __init__(self, current: str):
        options = [discord.SelectOption(label="⚡ Стандарт (Автовыбор)", value="auto", default=(current == "auto"))]
        for m in MODELS_POOL:
            options.append(discord.SelectOption(label=m[:100], value=m, default=(current == m)))
        super().__init__(placeholder="Выберите активную модель...", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        await update_setting(interaction.channel_id, "model", self.values[0])
        settings = await get_settings(interaction.channel_id)
        embed = build_menu_embed(settings)
        try:
            await interaction.response.edit_message(embed=embed, view=ControlPanelView(settings))
        except discord.Forbidden:
            await interaction.response.edit_message(content=build_menu_text(settings), view=ControlPanelView(settings))


class PersonaDropdown(discord.ui.Select):
    def __init__(self, current: str):
        options = [
            discord.SelectOption(label=p["name"], value=k, default=(current == k))
            for k, p in PERSONAS.items()
        ]
        super().__init__(placeholder="Выберите характер...", min_values=1, max_values=1, options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        await update_setting(interaction.channel_id, "persona", self.values[0])
        settings = await get_settings(interaction.channel_id)
        embed = build_menu_embed(settings)
        try:
            await interaction.response.edit_message(embed=embed, view=ControlPanelView(settings))
        except discord.Forbidden:
            await interaction.response.edit_message(content=build_menu_text(settings), view=ControlPanelView(settings))


def build_menu_embed(settings: dict) -> discord.Embed:
    p_name = PERSONAS.get(settings["persona"], {}).get("name", settings["persona"])
    embed = discord.Embed(title="⚙️ Центр управления Стетхомом", color=discord.Color.blue())
    embed.add_field(name="🤖 Активная модель", value=f"`{settings['model']}`", inline=True)
    embed.add_field(name="🎭 Характер", value=f"**{p_name}**", inline=True)
    embed.add_field(name="🌐 Поиск Google", value="✅ Включен" if settings["web_search"] else "❌ Выключен", inline=True)
    embed.add_field(name="🎲 Спонтанность", value="✅ 3% шанс" if settings["spontaneous"] else "❌ Выключена", inline=True)
    embed.set_footer(text="Изменения применяются для текущего канала.")
    return embed


def build_menu_text(settings: dict) -> str:
    p_name = PERSONAS.get(settings["persona"], {}).get("name", settings["persona"])
    return (
        f"⚙️ **Центр управления Стетхомом**\n"
        f"• 🤖 **Модель:** `{settings['model']}`\n"
        f"• 🎭 **Характер:** {p_name}\n"
        f"• 🌐 **Поиск Google:** {'✅ Включен' if settings['web_search'] else '❌ Выключен'}\n"
        f"• 🎲 **Спонтанность:** {'✅ Включена' if settings['spontaneous'] else '❌ Выключена'}"
    )


async def send_control_panel(target_message: discord.Message):
    """Надежная отправка панели настроек с защитой от отсутствия прав на Embed."""
    settings = await get_settings(target_message.channel.id)
    view = ControlPanelView(settings)
    try:
        await target_message.reply(embed=build_menu_embed(settings), view=view)
    except discord.Forbidden:
        await target_message.reply(content=build_menu_text(settings), view=view)


# ==================== ЯДРО GEMINI ====================
async def call_gemini(model_name: str, payload: dict, headers: dict) -> tuple[bool, str, list]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
    try:
        async with http_session.post(url, json=payload, headers=headers) as resp:
            data = await resp.json(content_type=None)

            if resp.status == 200 and "candidates" in data and len(data["candidates"]) > 0:
                cand = data["candidates"][0]
                cand_parts = cand.get("content", {}).get("parts", [])
                
                sources = []
                for chunk in cand.get("groundingMetadata", {}).get("groundingChunks", []):
                    web_info = chunk.get("web", {})
                    uri = web_info.get("uri")
                    title = web_info.get("title") or uri
                    if uri and uri not in [s[1] for s in sources]:
                        sources.append((title, uri))

                for part in reversed(cand_parts):
                    if "text" in part and not part.get("thought", False):
                        return True, part["text"].strip(), sources
                if cand_parts and "text" in cand_parts[-1]:
                    return True, cand_parts[-1]["text"].strip(), sources

            err_msg = data.get("error", {}).get("message", f"HTTP {resp.status}")
            return False, f"[{resp.status}] {err_msg}", []
    except Exception as e:
        return False, str(e), []


async def ask_ai(author: discord.User | discord.Member, user_text: str, channel: discord.TextChannel | discord.VoiceChannel, file_parts: list = None) -> tuple[str, list]:
    global http_session
    if not GEMINI_API_KEY:
        return "Ошибка: Не задан GEMINI_API_KEY.", []

    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))

    settings = await get_settings(channel.id)
    persona_cfg = PERSONAS.get(settings["persona"], PERSONAS["statham"])

    dossier = await get_user_facts(channel.guild.id if channel.guild else 0, author.id)
    dossier_text = f"\nДосье на собеседника ({author.display_name}):\n{dossier}" if dossier else ""
    vc_context = f"\n[Ты находишься в голосовом канале: {channel.name}]" if isinstance(channel, discord.VoiceChannel) else ""

    system_instruction = f"{persona_cfg['prompt']}{dossier_text}{vc_context}"

    history = await get_memory(channel.id)
    current_parts = [{"text": f"[{author.display_name}]: {user_text}"}]
    if file_parts:
        current_parts.extend(file_parts)

    contents = history + [{"role": "user", "parts": current_parts}]
    headers = {"Content-Type": "application/json", "X-goog-api-key": GEMINI_API_KEY}

    payload = {
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.6}
    }

    if settings["web_search"]:
        payload["tools"] = [{"google_search": {}}]

    selected = settings["model"]
    warning_notice = ""
    models_to_try = []

    if selected != "auto":
        ok, res, sources = await call_gemini(selected, payload, headers)
        if ok:
            await save_memory(channel.id, "user", author.display_name, user_text)
            await save_memory(channel.id, "model", "стетхом", res)
            return res, sources
        else:
            warning_notice = f"⚠️ У `{selected}` закончились лимиты/ошибка! Переключился на автовыбор.\n\n"
            await update_setting(channel.id, "model", "auto")
            models_to_try = [m for m in MODELS_POOL if m != selected]
    else:
        models_to_try = list(MODELS_POOL)

    for m in models_to_try:
        if file_parts and m.startswith("gemma"):
            continue
        ok, res, sources = await call_gemini(m, payload, headers)
        if ok:
            await save_memory(channel.id, "user", author.display_name, user_text)
            await save_memory(channel.id, "model", "стетхом", res)
            return warning_notice + res, sources

    return warning_notice + "❌ Все доступные модели исчерпали лимиты.", []


# ==================== DISCORD СОБЫТИЯ ====================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    await init_db()
    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"Ошибка синхронизации дерева команд: {e}")
    print(f"✅ Бот {bot.user} успешно запущен и готов к работе!", flush=True)


@bot.tree.command(name="menu", description="Открыть центр управления Стетхомом")
async def slash_menu(interaction: discord.Interaction):
    settings = await get_settings(interaction.channel_id)
    try:
        await interaction.response.send_message(embed=build_menu_embed(settings), view=ControlPanelView(settings), ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(content=build_menu_text(settings), view=ControlPanelView(settings), ephemeral=True)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    try:
        content_raw = message.content.strip().lower()
        clean_text = message.clean_content.strip().lower()

        # --- НАДЕЖНЫЙ ПЕРЕХВАТ МЕНЮ ---
        is_menu_request = (
            content_raw in ("!menu", "/menu")
            or "!menu" in content_raw
            or clean_text in ("!menu", "/menu", "меню")
            or ("меню" in clean_text and (bot.user in message.mentions or (message.reference and message.reference.resolved and message.reference.resolved.author == bot.user)))
        )

        if is_menu_request:
            await send_control_panel(message)
            return

        # Быстрый запуск кода
        if message.content.startswith("!run"):
            match = re.search(r"```(\w*)\n([\s\S]*?)```", message.content)
            if match:
                lang, code = match.group(1) or "python", match.group(2)
                out = await run_piston_code(lang, code)
                await message.reply(f"```\n{out}\n```")
                return

        # Эмодзи-реакции на триггеры
        if any(w in clean_text for w in ("кринж", "база", "стетхом одобряет", "пон", "жиза")):
            try:
                await message.add_reaction(random.choice(["🗿", "💀", "🤝", "🤡", "☕"]))
            except Exception:
                pass

        is_mentioned = bot.user in message.mentions
        is_reply = (
            message.reference
            and message.reference.resolved
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author == bot.user
        )

        settings = await get_settings(message.channel.id)

        # Спонтанные реплики (раз в 15 минут)
        now = time.time()
        last_time = channel_spontaneous_cooldown[message.channel.id]
        trigger_spontaneous = (
            settings["spontaneous"]
            and not (is_mentioned or is_reply)
            and len(message.content) > 15
            and (now - last_time > 900)
            and (random.random() < 0.03)
        )

        if trigger_spontaneous:
            channel_spontaneous_cooldown[message.channel.id] = now

        if not (is_mentioned or is_reply or trigger_spontaneous):
            return

        user_query = message.clean_content.replace(f"@{bot.user.display_name}", "").strip()

        # Сбор файлов (включая аудио и голосовые)
        file_parts = []
        attachments = list(message.attachments)
        if message.reference and isinstance(message.reference.resolved, discord.Message):
            attachments.extend(message.reference.resolved.attachments)

        for att in attachments:
            if att.size > 15 * 1024 * 1024:
                continue
            fn = att.filename.lower()
            ct = att.content_type or ""
            try:
                b = await att.read()
                if ct.startswith("image/") or fn.endswith((".png", ".jpg", ".jpeg", ".webp")):
                    mime = ct if ct.startswith("image/") else "image/jpeg"
                    file_parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(b).decode()}})
                elif ct.startswith("audio/") or fn.endswith(AUDIO_EXTENSIONS):
                    mime = ct if ct.startswith("audio/") else "audio/ogg"
                    file_parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(b).decode()}})
                elif fn.endswith(".pdf") or ct == "application/pdf":
                    file_parts.append({"inlineData": {"mimeType": "application/pdf", "data": base64.b64encode(b).decode()}})
                elif fn.endswith(TEXT_EXTENSIONS):
                    file_parts.append({"text": f"\n\n[Файл {att.filename}]:\n{b.decode('utf-8', errors='replace')}\n"})
            except Exception as e:
                print(f"Ошибка чтения файла: {e}")

        if not user_query:
            user_query = "Что на файле/аудио?" if file_parts else "Ты тут?"

        async with message.channel.typing():
            answer, sources = await ask_ai(message.author, user_query, message.channel, file_parts)

        if sources:
            source_links = "\n".join([f"• [{title}]({uri})" for title, uri in sources[:4]])
            answer += f"\n\n🌐 **Источники:**\n{source_links}"

        chunks = split_text_with_markdown(answer)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await message.reply(chunk)
            else:
                await message.channel.send(chunk)

    except Exception as e:
        print(f"❌ Ошибка в on_message: {e}", flush=True)


# ==================== KEEPALIVE СЕРВЕР ====================
async def handle(request):
    return web.Response(text="Bot is alive 24/7!")


async def main():
    global http_session
    http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))

    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()

    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        if http_session and not http_session.closed:
            await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())

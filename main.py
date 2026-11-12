import os
import io
import re
import time
import base64
import random
import textwrap
import asyncio
from collections import defaultdict
from urllib.parse import urlparse

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
            "Ты максимально ленивый зумер на чилле. Сленг: 'пон', 'жиза', 'кринж', 'вайб', 'тянет на базу'. "
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


async def wipe_user_dossier(guild_id: int, user_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM user_dossier WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        await db.commit()


# ==================== УТИЛИТЫ: WEB-ПАРСИНГ, PISTON, PILLOW ====================
async def fetch_web_page(url: str) -> str:
    """Извлекает текстовое содержание страницы для скармливания модели."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with http_session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200 and "text/html" in (resp.content_type or ""):
                html = await resp.text()
                # Удаляем скрипты и стили
                clean = re.sub(r"<(script|style).*?>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
                # Удаляем HTML-теги
                clean = re.sub(r"<[^>]+>", " ", clean)
                clean = " ".join(clean.split())
                return clean[:3000]
    except Exception:
        pass
    return ""


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
    """Генерирует стильный постер с цитатой, аватаркой и подписью."""
    w, h = 1000, 500
    img = Image.new("RGB", (w, h), color=(18, 18, 24))
    draw = ImageDraw.Draw(img)

    # Рисуем рамку и акцентную линию
    draw.rounded_rectangle([(20, 20), (w - 20, h - 20)], radius=20, outline=(45, 45, 60), width=2)
    draw.line([(50, h - 90), (w - 50, h - 90)], fill=(40, 40, 55), width=2)

    # Загружаем шрифт или стандартный
    font_large = ImageFont.load_default()
    font_small = ImageFont.load_default()

    # Если есть аватарка, вставляем кружочком
    if avatar_bytes:
        try:
            av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((70, 70))
            mask = Image.new("L", (70, 70), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 70, 70), fill=255)
            img.paste(av, (50, h - 80), mask)
        except Exception:
            pass

    # Оформляем текст цитаты
    wrapped = textwrap.wrap(f"«{text}»", width=38)
    quote_str = "\n".join(wrapped)

    draw.multiline_text((w // 2, (h - 100) // 2), quote_str, fill=(245, 245, 250), font=font_large, anchor="mm", align="center", spacing=14)
    draw.text((135 if avatar_bytes else 50, h - 55), f"© {author_name}", fill=(170, 170, 190), font=font_small, anchor="ls")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def split_text_with_markdown(text: str, limit: int = 1950) -> list[str]:
    """Умное разбиение текста с сохранением подсветки Markdown (```)."""
    if len(text) <= limit:
        return [text]

    chunks = []
    in_code_block = False
    code_lang = ""

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

        # Проверка на незакрытый кодовый блок
        code_matches = re.findall(r"```(\w*)", chunk)
        if len(code_matches) % 2 != 0:
            last_lang = code_matches[-1]
            chunk += "\n```"
            text = f"```{last_lang}\n" + text

        chunks.append(chunk)

    return chunks


# ==================== UI: ИНТЕРАКТИВНЫЕ КНОПКИ И МЕНЮ ====================
class RunCodeButtonView(discord.ui.View):
    def __init__(self, language: str, code: str):
        super().__init__(timeout=300)
        self.language = language
        self.code = code

    @discord.ui.button(label="▶️ Запустить код", style=discord.ButtonStyle.success)
    async def run_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        out = await run_piston_code(self.language, self.code)
        if len(out) > 1850:
            out = out[:1850] + "\n... (хвост обрезан)"
        await interaction.followup.send(f"💻 **Результат запуска ({self.language}):**\n```\n{out}\n```")


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
        await interaction.response.edit_message(embed=build_menu_embed(self.settings), view=self)

    @discord.ui.button(label="🎲 Спонтанность", style=discord.ButtonStyle.secondary, row=2)
    async def toggle_spontaneous(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_state = not self.settings["spontaneous"]
        await update_setting(interaction.channel_id, "spontaneous", new_state)
        self.settings["spontaneous"] = new_state
        await interaction.response.edit_message(embed=build_menu_embed(self.settings), view=self)

    @discord.ui.button(label="🧹 Сброс памяти", style=discord.ButtonStyle.danger, row=2)
    async def clear_mem(self, interaction: discord.Interaction, button: discord.ui.Button):
        await clear_channel_memory(interaction.channel_id)
        await interaction.response.send_message("🧹 Память канала полностью сброшена!", ephemeral=True)


class ModelDropdown(discord.ui.Select):
    def __init__(self, current: str):
        options = [discord.SelectOption(label="⚡ Стандарт (Автовыбор)", value="auto", default=(current == "auto"))]
        for m in MODELS_POOL:
            options.append(discord.SelectOption(label=m[:100], value=m, default=(current == m)))
        super().__init__(placeholder="Выберите активную модель...", min_values=1, max_values=1, options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        await update_setting(interaction.channel_id, "model", self.values[0])
        settings = await get_settings(interaction.channel_id)
        await interaction.response.edit_message(embed=build_menu_embed(settings), view=ControlPanelView(settings))


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
        await interaction.response.edit_message(embed=build_menu_embed(settings), view=ControlPanelView(settings))


def build_menu_embed(settings: dict) -> discord.Embed:
    p_name = PERSONAS.get(settings["persona"], {}).get("name", settings["persona"])
    embed = discord.Embed(title="⚙️ Центр управления Стетхомом", color=discord.Color.blue())
    embed.add_field(name="🤖 Активная модель", value=f"`{settings['model']}`", inline=True)
    embed.add_field(name="🎭 Характер", value=f"**{p_name}**", inline=True)
    embed.add_field(name="🌐 Поиск в сети", value="✅ Активен" if settings["web_search"] else "❌ Выключен", inline=True)
    embed.add_field(name="🎲 Спонтанные реплики", value="✅ 3% (с кулдауном)" if settings["spontaneous"] else "❌ Выкл", inline=True)
    embed.set_footer(text="Все настройки сохраняются в базу данных канала.")
    return embed


# ==================== ЯДРО GEMINI С GROUNDING ====================
async def call_gemini(model_name: str, payload: dict, headers: dict) -> tuple[bool, str, list]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
    try:
        async with http_session.post(url, json=payload, headers=headers) as resp:
            data = await resp.json(content_type=None)

            if resp.status == 400 and "thinkingConfig" in payload.get("generationConfig", {}):
                clean_payload = dict(payload)
                clean_payload["generationConfig"] = {"temperature": 0.6}
                async with http_session.post(url, json=clean_payload, headers=headers) as retry_resp:
                    data = await retry_resp.json(content_type=None)
                    resp = retry_resp

            if resp.status == 200 and "candidates" in data and len(data["candidates"]) > 0:
                cand = data["candidates"][0]
                cand_parts = cand.get("content", {}).get("parts", [])
                
                # Достаем источники Google Search (Grounding)
                sources = []
                grounding_chunks = cand.get("groundingMetadata", {}).get("groundingChunks", [])
                for chunk in grounding_chunks:
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
    dossier_text = f"\nТвое досье на собеседника ({author.display_name}):\n{dossier}" if dossier else ""
    vc_context = f"\n[Контекст: ты общаешься в текстовом чате голосового канала: {channel.name}]" if isinstance(channel, discord.VoiceChannel) else ""

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
        "generationConfig": {"temperature": 0.6, "thinkingConfig": {"thinkingBudget": 1024}}
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
            warning_notice = f"⚠️ У `{selected}` закончились лимиты/ошибка! Переключился на автоперебор.\n\n"
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


# ==================== DISCORD BOT И КОМАНДЫ ====================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    await init_db()
    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"Ошибка дерева команд: {e}")
    print(f"✅ Бот {bot.user} запущен и готов разносить чат!", flush=True)


@bot.tree.command(name="menu", description="Панель настроек моделей, характера и поиска")
async def cmd_menu(interaction: discord.Interaction):
    settings = await get_settings(interaction.channel_id)
    await interaction.response.send_message(embed=build_menu_embed(settings), view=ControlPanelView(settings), ephemeral=True)


@bot.tree.command(name="quote", description="Сделать мем-цитату (можно ответить на сообщение или передать текст)")
@app_commands.describe(text="Текст цитаты (если пусто — придумает сам)")
async def cmd_quote(interaction: discord.Interaction, text: str = None):
    await interaction.response.defer()
    author_name = "Джейсон Стетхом"
    av_bytes = None

    if not text:
        text, _ = await ask_ai(interaction.user, "Выдай одну абсурдную пацанскую цитату в одно короткое предложение.", interaction.channel)

    buf = create_quote_card(text.replace("«", "").replace("»", ""), author_name, av_bytes)
    await interaction.followup.send(file=discord.File(buf, filename="quote.png"))


@bot.tree.command(name="dossier", description="Посмотреть досье на участника")
@app_commands.describe(user="Пользователь")
async def cmd_dossier(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    facts = await get_user_facts(interaction.guild_id, target.id)
    if not facts:
        await interaction.response.send_message(f"📁 Досье на **{target.display_name}** пока пустое.", ephemeral=True)
    else:
        embed = discord.Embed(title=f"📁 Досье: {target.display_name}", description=facts, color=discord.Color.dark_gold())
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="remember", description="Записать факт в досье")
@app_commands.describe(user="Пользователь", fact="Что запомнить?")
async def cmd_remember(interaction: discord.Interaction, user: discord.Member, fact: str):
    await save_user_fact(interaction.guild_id, user.id, user.display_name, fact)
    await interaction.response.send_message(f"📝 Записал в досье на **{user.display_name}**: *{fact}*", ephemeral=True)


@bot.tree.command(name="forget", description="Очистить досье на пользователя")
@app_commands.describe(user="Пользователь")
async def cmd_forget(interaction: discord.Interaction, user: discord.Member):
    await wipe_user_dossier(interaction.guild_id, user.id)
    await interaction.response.send_message(f"🗑️ Досье на **{user.display_name}** очищено.", ephemeral=True)


@bot.tree.command(name="run", description="Запустить код в песочнице")
@app_commands.describe(language="Язык (python, js, cpp, rust, bash)", code="Код")
async def cmd_run(interaction: discord.Interaction, language: str, code: str):
    await interaction.response.defer()
    out = await run_piston_code(language, code)
    if len(out) > 1850:
        out = out[:1850] + "\n..."
    await interaction.followup.send(f"💻 **Результат ({language}):**\n```\n{out}\n```")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Текстовый вызов панели
    if message.content.strip().lower() in ("!menu", "/menu", "стетхом меню"):
        settings = await get_settings(message.channel.id)
        await message.reply(embed=build_menu_embed(settings), view=ControlPanelView(settings))
        return

    # Цитата по реплаю на чужое сообщение: "!quote" в ответ
    if message.content.strip().lower() == "!quote" and message.reference and message.reference.resolved:
        orig = message.reference.resolved
        if isinstance(orig, discord.Message) and orig.content:
            av_data = await orig.author.display_avatar.read() if orig.author.display_avatar else None
            buf = create_quote_card(orig.content, orig.author.display_name, av_data)
            await message.reply(file=discord.File(buf, filename="quote.png"))
            return

    # Быстрый запуск кода через markdown-блоки
    if message.content.startswith("!run"):
        match = re.search(r"```(\w*)\n([\s\S]*?)```", message.content)
        if match:
            lang, code = match.group(1) or "python", match.group(2)
            out = await run_piston_code(lang, code)
            await message.reply(f"```\n{out}\n```")
            return

    # Эмодзи-реакции на триггеры
    lowered = message.content.lower()
    if any(w in lowered for w in ("кринж", "база", "стетхом одобряет", "пон", "жиза")):
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

    # Спонтанные реплики с кулдауном 15 минут
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

    clean_text = message.clean_content.replace(f"@{bot.user.display_name}", "").strip()

    # Сбор и обработка вложений (Изображения, PDF, Код, Голосовые/Аудио)
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
            # Картинки
            if ct.startswith("image/") or fn.endswith((".png", ".jpg", ".jpeg", ".webp")):
                mime = ct if ct.startswith("image/") else "image/jpeg"
                file_parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(b).decode()}})
            # Голосовые сообщения и аудио
            elif ct.startswith("audio/") or fn.endswith(AUDIO_EXTENSIONS):
                mime = ct if ct.startswith("audio/") else "audio/ogg"
                file_parts.append({"inlineData": {"mimeType": mime, "data": base64.b64encode(b).decode()}})
            # PDF
            elif fn.endswith(".pdf") or ct == "application/pdf":
                file_parts.append({"inlineData": {"mimeType": "application/pdf", "data": base64.b64encode(b).decode()}})
            # Текстовые файлы и код
            elif fn.endswith(TEXT_EXTENSIONS):
                file_parts.append({"text": f"\n\n[Файл {att.filename}]:\n{b.decode('utf-8', errors='replace')}\n"})
        except Exception as e:
            print(f"Ошибка чтения вложения: {e}")

    # Парсинг ссылок из текста
    urls = re.findall(r"https?://[^\s]+", clean_text)
    for u in urls[:2]:
        page_text = await fetch_web_page(u)
        if page_text:
            file_parts.append({"text": f"\n\n[Содержимое страницы {u}]:\n{page_text}\n"})

    if not clean_text:
        clean_text = "Что на прикрепленном файле/аудио?" if file_parts else "Ты тут?"

    async with message.channel.typing():
        answer, sources = await ask_ai(message.author, clean_text, message.channel, file_parts)

    # Прикрепляем кликабельные источники Google Search
    if sources:
        source_links = "\n".join([f"• [{title}]({uri})" for title, uri in sources[:4]])
        answer += f"\n\n🌐 **Источники:**\n{source_links}"

    # Проверяем, есть ли в ответе сгенерированный код, чтобы добавить кнопку запуска
    code_match = re.search(r"```(\w+)\n([\s\S]*?)```", answer)
    run_view = RunCodeButtonView(code_match.group(1), code_match.group(2)) if code_match else None

    chunks = split_text_with_markdown(answer)
    for i, chunk in enumerate(chunks):
        view_to_attach = run_view if (i == len(chunks) - 1 and run_view) else None
        if i == 0:
            await message.reply(chunk, view=view_to_attach)
        else:
            await message.channel.send(chunk, view=view_to_attach)


# ==================== KEEP-ALIVE ВЕБ-СЕРВЕР ====================
async def handle(request):
    return web.Response(text="Bot is running smoothly 24/7!")


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

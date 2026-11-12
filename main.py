import os
import base64
import asyncio
from collections import defaultdict
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
from aiohttp import web

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Пул моделей для стандартного режима (по порядку приоритета)
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

TEXT_EXTENSIONS = (
    ".txt", ".py", ".js", ".ts", ".json", ".html", ".css", 
    ".cpp", ".c", ".cs", ".java", ".go", ".rs", ".md", ".log", 
    ".xml", ".yaml", ".yml", ".sql", ".sh"
)

MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB

SYSTEM_INSTRUCTION = """Ты участник Discord-сервера по имени стетхом.

ТВОЙ ХАРАКТЕР И ПРАВИЛА (СТРОЖАЙШЕ СОБЛЮДАЙ):
1. ЗАБУДЬ, ЧТО ТЫ РОБОТ ИЛИ ТЕХПОДДЕРЖКА. Общайся как реальный живой человек в голосовом/текстовом чате с друзьями — по-простому, иногда с иронией, без официоза.
2. НИКАКИХ "Привет! 👋", "Здравствуйте", "Чем я могу вам помочь?", "Хотите обсудить что-то еще? 😉" в начале или конце! За такие фразы сразу бан. Отвечай СРАЗУ по делу с первого слова.
3. НИКАКИХ нумерованных списков (1., 2., 3.), если тебя прямо не попросили составить список. Пиши нормальным текстом, как в мессенджерах.
4. ПАМЯТЬ: Ниже приведена история чата. Если спрашивают, о чем говорили — смотри в историю и пересказывай своими словами по фактам.
5. НЕ ПРИДУМЫВАЙ. Если не знаешь человека на фото, локальный мем или ответ — скажи прямо: "Хз кто это", "Не шарю за этот прикол, поясни"."""

# Память каналов: channel_id -> list of role dicts
channel_memories = defaultdict(list)

# Настройка выбранной модели: channel_id -> "auto" или название модели
channel_active_model = defaultdict(lambda: "auto")

http_session: aiohttp.ClientSession | None = None


# --- UI ВЫБОРА МОДЕЛЕЙ ---
class ModelSelect(discord.ui.Select):
    def __init__(self, current_choice: str):
        options = [
            discord.SelectOption(
                label="⚡ Стандарт (Автовыбор)",
                value="auto",
                description="Автоперебор пула моделей при лимитах и ошибках",
                default=(current_choice == "auto")
            )
        ]
        for model in MODELS_POOL:
            options.append(
                discord.SelectOption(
                    label=model[:100],
                    value=model,
                    description=f"Фиксировать {model}"[:100],
                    default=(current_choice == model)
                )
            )

        super().__init__(placeholder="Выберите модель для ответов...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        chosen = self.values[0]
        channel_active_model[interaction.channel_id] = chosen

        if chosen == "auto":
            text = "⚡ Включен **Стандартный режим** (автоматический перебор моделей по пулу)."
        else:
            text = (
                f"🤖 Для этого канала установлена модель: **`{chosen}`**\n"
                f"*(Если у неё закончатся лимиты, бот уведомит об этом и переключится на стандарт)*"
            )

        await interaction.response.edit_message(content=text, view=None)


class ModelSelectView(discord.ui.View):
    def __init__(self, current_choice: str):
        super().__init__(timeout=120)
        self.add_item(ModelSelect(current_choice))


def split_text_smartly(text: str, limit: int = 1950) -> list[str]:
    """Разбивает длинный текст по абзацам и пробелам."""
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

        chunks.append(text[:split_idx].strip())
        text = text[split_idx:].strip()

    return chunks


async def call_gemini(model_name: str, payload: dict, headers: dict) -> tuple[bool, str]:
    """Выполняет единичный запрос к API Gemini."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"

    try:
        async with http_session.post(url, json=payload, headers=headers) as resp:
            data = await resp.json(content_type=None)

            # Если модель не поддерживает thinkingConfig (HTTP 400), пробуем без него
            if resp.status == 400 and "thinkingConfig" in payload.get("generationConfig", {}):
                clean_payload = dict(payload)
                clean_payload["generationConfig"] = {"temperature": 0.6}
                async with http_session.post(url, json=clean_payload, headers=headers) as retry_resp:
                    data = await retry_resp.json(content_type=None)
                    resp = retry_resp

            if resp.status == 200 and "candidates" in data and len(data["candidates"]) > 0:
                cand_parts = data["candidates"][0].get("content", {}).get("parts", [])
                for part in reversed(cand_parts):
                    if "text" in part and not part.get("thought", False):
                        return True, part["text"].strip()
                if cand_parts and "text" in cand_parts[-1]:
                    return True, cand_parts[-1]["text"].strip()

            err_msg = data.get("error", {}).get("message", f"HTTP {resp.status}")
            return False, f"[{resp.status}] {err_msg}"

    except Exception as e:
        return False, str(e)


async def ask_ai(author_name: str, user_text: str, channel_id: int, file_parts: list = None) -> str:
    global http_session
    if not GEMINI_API_KEY:
        return "Ошибка: Не задан GEMINI_API_KEY в переменных окружения."

    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))

    current_parts = [{"text": f"[{author_name}]: {user_text}"}]
    if file_parts:
        current_parts.extend(file_parts)

    history = list(channel_memories[channel_id][-30:])
    contents = history + [{"role": "user", "parts": current_parts}]

    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY,
    }

    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.6,
            "thinkingConfig": {"thinkingBudget": 1024}
        }
    }

    selected_model = channel_active_model[channel_id]
    warning_notice = ""
    models_to_try = []

    # Если выбрана конкретная модель
    if selected_model != "auto":
        ok, result = await call_gemini(selected_model, payload, headers)
        if ok:
            print(f"✅ Ответила выбранная модель: {selected_model}", flush=True)
            mem = channel_memories[channel_id]
            mem.append({"role": "user", "parts": current_parts})
            mem.append({"role": "model", "parts": [{"text": result}]})
            if len(mem) > 40:
                del mem[:-40]
            return result
        else:
            # У выбранной модели исчерпан лимит или она сломалась
            print(f"⚠️ У выбранной модели {selected_model} ошибка/лимит: {result}", flush=True)
            warning_notice = (
                f"⚠️ У модели **`{selected_model}`** закончились лимиты (или возникла ошибка)!\n"
                f"Переключаюсь на **стандартный режим** (автовыбор из пула).\n\n"
            )
            # Сбрасываем канал обратно в автовыбор
            channel_active_model[channel_id] = "auto"

            # Формируем пул без только что упавшей модели
            models_to_try = [m for m in MODELS_POOL if m != selected_model]
    else:
        models_to_try = list(MODELS_POOL)

    # Проходим по пулу моделей
    for model_name in models_to_try:
        if file_parts and model_name.startswith("gemma"):
            continue

        ok, result = await call_gemini(model_name, payload, headers)
        if ok:
            print(f"✅ Ответила резервная модель: {model_name}", flush=True)
            mem = channel_memories[channel_id]
            mem.append({"role": "user", "parts": current_parts})
            mem.append({"role": "model", "parts": [{"text": result}]})
            if len(mem) > 40:
                del mem[:-40]

            return warning_notice + result

    return warning_notice + "❌ Ошибка: У всех моделей из списка исчерпаны лимиты или они временно недоступны."


# --- ЗАПУСК БОТА DISCORD ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✅ Синхронизировано слэш-команд: {len(synced)}", flush=True)
    except Exception as e:
        print(f"⚠️ Ошибка синхронизации слэш-команд: {e}", flush=True)
    print(f"✅ Бот {bot.user} готов к работе!", flush=True)


# Слэш-команда /models
@bot.tree.command(name="models", description="Выбрать модель нейросети или включить авторежим")
async def models_slash(interaction: discord.Interaction):
    current = channel_active_model[interaction.channel_id]
    view = ModelSelectView(current)
    await interaction.response.send_message(
        f"⚙️ Текущий режим канала: **`{current}`**\nВыберите нужную модель из меню:",
        view=view,
        ephemeral=True
    )


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Текстовая команда-дублер !models или /models в чат
    if message.content.strip().lower() in ("!models", "/models"):
        current = channel_active_model[message.channel.id]
        view = ModelSelectView(current)
        await message.reply(
            f"⚙️ Текущий режим канала: **`{current}`**\nВыберите нужную модель из списка:",
            view=view
        )
        return

    is_mentioned = bot.user in message.mentions
    is_reply_to_bot = (
        message.reference
        and message.reference.resolved
        and isinstance(message.reference.resolved, discord.Message)
        and message.reference.resolved.author == bot.user
    )

    if not (is_mentioned or is_reply_to_bot):
        return

    clean_text = message.clean_content.replace(f"@{bot.user.display_name}", "").strip()

    file_parts = []
    attachments = list(message.attachments)
    if message.reference and isinstance(message.reference.resolved, discord.Message):
        attachments.extend(message.reference.resolved.attachments)

    for att in attachments:
        if att.size > MAX_FILE_SIZE:
            await message.reply(f"⚠️ Файл `{att.filename}` больше 15 МБ, Google API его не примет.")
            continue

        filename = att.filename.lower()
        content_type = att.content_type or ""

        try:
            if content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".webp")):
                file_bytes = await att.read()
                b64 = base64.b64encode(file_bytes).decode("utf-8")
                mime = content_type if content_type.startswith("image/") else "image/jpeg"
                file_parts.append({"inlineData": {"mimeType": mime, "data": b64}})

            elif filename.endswith(".pdf") or content_type == "application/pdf":
                file_bytes = await att.read()
                b64 = base64.b64encode(file_bytes).decode("utf-8")
                file_parts.append({"inlineData": {"mimeType": "application/pdf", "data": b64}})

            elif filename.endswith(TEXT_EXTENSIONS):
                file_bytes = await att.read()
                text_content = file_bytes.decode("utf-8", errors="replace")
                file_parts.append({"text": f"\n\n[Файл {att.filename}]:\n{text_content}\n"})
        except Exception as e:
            print(f"Ошибка файла {att.filename}: {e}", flush=True)

    if not clean_text:
        clean_text = "Что на прикрепленном файле?" if file_parts else "Ты тут?"

    async with message.channel.typing():
        answer = await ask_ai(
            author_name=message.author.display_name,
            user_text=clean_text,
            channel_id=message.channel.id,
            file_parts=file_parts
        )

    chunks = split_text_smartly(answer)
    for i, chunk in enumerate(chunks):
        if i == 0:
            await message.reply(chunk)
        else:
            await message.channel.send(chunk)


async def handle(request):
    return web.Response(text="Discord Bot is alive 24/7!")


async def main():
    global http_session
    http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))

    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Сервер запущен на порту: {port}", flush=True)

    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        if http_session and not http_session.closed:
            await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())

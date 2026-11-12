import os
import base64
import asyncio
from collections import defaultdict
import discord
import aiohttp
from aiohttp import web

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Рабочий пул генеративных и мультимодальных моделей
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

MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB лимит на inlineData

SYSTEM_INSTRUCTION = """Ты участник Discord-сервера по имени стетхом.

ТВОЙ ХАРАКТЕР И ПРАВИЛА (СТРОЖАЙШЕ СОБЛЮДАЙ):
1. ЗАБУДЬ, ЧТО ТЫ РОБОТ ИЛИ ТЕХПОДДЕРЖКА. Общайся как реальный живой человек в голосовом/текстовом чате с друзьями — по-простому, иногда с иронией, без официоза.
2. НИКАКИХ "Привет! 👋", "Здравствуйте", "Чем я могу вам помочь?", "Хотите обсудить что-то еще? 😉" в начале или конце! За такие фразы сразу бан. Отвечай СРАЗУ по делу с первого слова.
3. НИКАКИХ нумерованных списков (1., 2., 3.), если тебя прямо не попросили составить список. Пиши нормальным текстом, как в мессенджерах.
4. ПАМЯТЬ: Ниже приведена история чата. Если спрашивают, о чем говорили — смотри в историю и пересказывай своими словами по фактам.
5. НЕ ПРИДУМЫВАЙ. Если не знаешь человека на фото, локальный мем или ответ — скажи прямо: "Хз кто это", "Не шарю за этот прикол, поясни"."""

# Память каналов в нативном формате ролей: channel_id -> [{"role": "user"|"model", "parts": [...]}]
channel_memories = defaultdict(list)
http_session: aiohttp.ClientSession | None = None


def split_text_smartly(text: str, limit: int = 1950) -> list[str]:
    """Разрезает длинные ответы по абзацам/пробелам, чтобы не ломать код и разметку."""
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


async def ask_ai(author_name: str, user_text: str, channel_id: int, file_parts: list = None) -> str:
    global http_session
    if not GEMINI_API_KEY:
        return "Ошибка: Не задан GEMINI_API_KEY в переменных окружения."

    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))

    current_parts = [{"text": f"[{author_name}]: {user_text}"}]
    if file_parts:
        current_parts.extend(file_parts)

    # Берем до 30 последних реплик (15 циклов общения)
    history = list(channel_memories[channel_id][-30:])
    contents = history + [{"role": "user", "parts": current_parts}]

    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY,
    }

    payload = {
        "system_instruction": {
            "parts": [{"text": SYSTEM_INSTRUCTION}]
        },
        "contents": contents,
        "generationConfig": {
            "temperature": 0.6,
            "thinkingConfig": {
                "thinkingBudget": 1024
            }
        }
    }

    last_error = "Нет доступных моделей"

    for model_name in MODELS_POOL:
        # Пропускаем файлы для Gemma, если они не поддерживают конкретный тип вложения
        if file_parts and model_name.startswith("gemma"):
            continue

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"

        try:
            async with http_session.post(url, json=payload, headers=headers) as resp:
                data = await resp.json(content_type=None)

                # Если модель не поддерживает thinkingConfig (HTTP 400), повторяем без него
                if resp.status == 400 and "thinkingConfig" in payload.get("generationConfig", {}):
                    fallback_payload = dict(payload)
                    fallback_payload["generationConfig"] = {"temperature": 0.6}
                    async with http_session.post(url, json=fallback_payload, headers=headers) as retry_resp:
                        data = await retry_resp.json(content_type=None)
                        resp = retry_resp

                if resp.status == 200 and "candidates" in data and len(data["candidates"]) > 0:
                    cand_parts = data["candidates"][0].get("content", {}).get("parts", [])

                    # Отсекаем мысли (thought) модели, если включен thinking mode
                    for part in reversed(cand_parts):
                        if "text" in part and not part.get("thought", False):
                            answer = part["text"].strip()
                            print(f"✅ Ответила: {model_name}", flush=True)

                            # Сохраняем диалог в нативном формате ролей
                            mem = channel_memories[channel_id]
                            mem.append({"role": "user", "parts": current_parts})
                            mem.append({"role": "model", "parts": [{"text": answer}]})
                            if len(mem) > 40:
                                del mem[:-40]

                            return answer

                err_msg = data.get("error", {}).get("message", f"HTTP {resp.status}")
                last_error = f"{model_name}: {err_msg}"
                print(f"⚠️ {model_name} ошибка: {err_msg[:60]}", flush=True)

        except Exception as e:
            last_error = f"{model_name}: {str(e)}"
            continue

    return f"Ошибка: `{last_error}`"


async def run_discord_bot():
    if not DISCORD_TOKEN:
        print("❌ Ошибка: DISCORD_TOKEN не задан!", flush=True)
        return

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"✅ Бот {client.user} онлайн!", flush=True)

    @client.event
    async def on_message(message: discord.Message):
        if message.author.bot:
            return

        is_mentioned = client.user in message.mentions
        is_reply_to_bot = (
            message.reference
            and message.reference.resolved
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author == client.user
        )

        if not (is_mentioned or is_reply_to_bot):
            return

        clean_text = message.clean_content.replace(f"@{client.user.display_name}", "").strip()

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
                print(f"Ошибка загрузки {att.filename}: {e}", flush=True)

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
                # Последующие куски шлём без повторного пинга/реплая
                await message.channel.send(chunk)

    try:
        await client.start(DISCORD_TOKEN)
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}", flush=True)


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
    print(f"🌐 Keep-alive сервер запущен на порту: {port}", flush=True)

    try:
        await run_discord_bot()
    finally:
        if http_session and not http_session.closed:
            await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())

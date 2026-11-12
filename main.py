import os
import json
import base64
import asyncio
from collections import defaultdict
import discord
import aiohttp
from aiohttp import web

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Пул моделей
MODELS_POOL = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-3-flash",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemini-robotics-er-2-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-embedding-1",
    "gemini-embedding-2",
    "gemini-decision-1",
    "gemma-4-31b-it",
    "gemma-4-26b-it",
]

TEXT_EXTENSIONS = (
    ".txt", ".py", ".js", ".ts", ".json", ".html", ".css", 
    ".cpp", ".c", ".cs", ".java", ".go", ".rs", ".md", ".log", 
    ".xml", ".yaml", ".yml", ".sql", ".sh"
)

# Раздельная память каналов
channel_memories = defaultdict(list)


async def ask_ai(user_message: str, channel_id: int, file_parts: list = None) -> str:
    if not GEMINI_API_KEY:
        return "Ошибка: Не задан GEMINI_API_KEY в переменных окружения."

    # Теперь бот помнит до 40 ПОЛНОЦЕННЫХ сообщений из диалога
    chat_memory = channel_memories[channel_id]
    memory_text = "\n".join(chat_memory[-40:])

    # Промпт: выбиваем из него повадки бота
    full_prompt = f"""Ты участник Discord-сервера по имени стетхом.

ТВОЙ ХАРАКТЕР И ПРАВИЛА (СТРОЖАЙШЕ СОБЛЮДАЙ):
1. ЗАБУДЬ, ЧТО ТЫ РОБОТ ИЛИ ТЕХПОДДЕРЖКА. Общайся как реальный живой человек в голосовом/текстовом чате с друзьями — по-простому, иногда с иронией, без официоза.
2. НИКАКИХ "Привет! 👋", "Здравствуйте", "Чем я могу вам помочь?", "Хотите обсудить что-то еще? 😉" в начале или конце! За такие фразы сразу бан. Отвечай СРАЗУ по делу с первого слова.
3. НИКАКИХ нумерованных списков (1., 2., 3.), если тебя прямо не попросили составить список. Пиши нормальным сплошным текстом или предложениями, как люди в мессенджерах.
4. ПАМЯТЬ: Ниже приведена реальная история чата. Если тебя спрашивают, о чем говорили или что было в начале — смотри в историю чата и пересказывай точно и по фактам, своими словами, а не сочиняй сказки.
5. НЕ ПРИДУМЫВАЙ. Если не знаешь человека на фото, локальный мем или ответ — так прямо и скажи: "Хз кто это", "Не шарю за этот прикол, поясни". Не пытайся угадывать чушь.

История текущего разговора на сервере:
{memory_text if memory_text else "(Диалог только начинается)"}

Новое сообщение от {user_message}"""

    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY,
    }

    # Настройка: даем модели подумать (thinking) и снижаем температуру для ума
    generation_config = {
        "temperature": 0.5,
        "thinkingConfig": {
            "thinkingBudget": 1024  # Модель сначала обдумывает логику вопроса
        }
    }

    last_error = None
    timeout = aiohttp.ClientTimeout(total=45)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for model_name in MODELS_POOL:
            if file_parts and model_name.startswith("gemma"):
                continue

            parts = [{"text": full_prompt}]
            if file_parts:
                parts.extend(file_parts)

            payload = {
                "contents": [{"parts": parts}],
                "generationConfig": generation_config
            }

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"

            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    data = await resp.json(content_type=None)

                    # Если модель не поддерживает thinkingConfig (HTTP 400), шлем без него
                    if resp.status == 400 and "thinkingConfig" in payload.get("generationConfig", {}):
                        payload["generationConfig"] = {"temperature": 0.5}
                        async with session.post(url, json=payload, headers=headers) as retry_resp:
                            data = await retry_resp.json(content_type=None)
                            resp = retry_resp

                    if resp.status == 200 and "candidates" in data and len(data["candidates"]) > 0:
                        cand_parts = data["candidates"][0].get("content", {}).get("parts", [])
                        
                        # Берем финальный ответ, отсекая внутренние мысли модели (thoughts)
                        for part in reversed(cand_parts):
                            if "text" in part and not part.get("thought", False):
                                print(f"✅ Ответила: {model_name}", flush=True)
                                return part["text"].strip()
                        if cand_parts and "text" in cand_parts[-1]:
                            return cand_parts[-1]["text"].strip()

                    err_msg = data.get("error", {}).get("message", f"HTTP {resp.status}")
                    last_error = f"{model_name}: {err_msg}"
                    print(f"⚠️ {model_name} ошибка: {err_msg[:50]}", flush=True)

            except Exception as e:
                last_error = f"{model_name}: {str(e)}"
                continue

    return f"Ошибка: `{last_error}`"


async def run_discord_bot():
    if not DISCORD_TOKEN:
        print("❌ Ошибка: DISCORD_TOKEN не задан!", flush=True)
        return

    while True:
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            print(f"✅ Бот {client.user} онлайн с расширенной памятью!", flush=True)

        @client.event
        async def on_message(message):
            if message.author.bot:
                return

            is_mentioned = client.user in message.mentions
            is_reply_to_bot = (
                message.reference
                and message.reference.resolved
                and isinstance(message.reference.resolved, discord.Message)
                and message.reference.resolved.author == client.user
            )

            if is_mentioned or is_reply_to_bot:
                clean_text = message.content.replace(f"<@{client.user.id}>", "").strip()

                file_parts = []
                attachments = list(message.attachments)
                if message.reference and message.reference.resolved and isinstance(message.reference.resolved, discord.Message):
                    attachments.extend(message.reference.resolved.attachments)

                for att in attachments:
                    filename = att.filename.lower()
                    content_type = att.content_type or ""

                    if content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".webp")):
                        try:
                            file_bytes = await att.read()
                            b64 = base64.b64encode(file_bytes).decode("utf-8")
                            mime = content_type if content_type.startswith("image/") else "image/jpeg"
                            file_parts.append({"inlineData": {"mimeType": mime, "data": b64}})
                        except Exception as e:
                            print(f"Ошибка картинки: {e}")

                    elif filename.endswith(".pdf") or content_type == "application/pdf":
                        try:
                            file_bytes = await att.read()
                            b64 = base64.b64encode(file_bytes).decode("utf-8")
                            file_parts.append({"inlineData": {"mimeType": "application/pdf", "data": b64}})
                        except Exception as e:
                            print(f"Ошибка PDF: {e}")

                    elif filename.endswith(TEXT_EXTENSIONS):
                        try:
                            file_bytes = await att.read()
                            text_content = file_bytes.decode("utf-8", errors="replace")
                            file_parts.append({
                                "text": f"\n\n[Файл {att.filename}]:\n{text_content}\n"
                            })
                        except Exception as e:
                            print(f"Ошибка файла: {e}")

                if not clean_text:
                    clean_text = "Что на прикрепленном файле?" if file_parts else "Ты тут?"

                async with message.channel.typing():
                    answer = await ask_ai(
                        f"{message.author.display_name}: {clean_text}",
                        message.channel.id,
                        file_parts=file_parts
                    )

                # Сохраняем в память канала до 50 сообщений БЕЗ обрезания!
                if not answer.startswith("Ошибка:"):
                    memory = channel_memories[message.channel.id]
                    memory.append(f"{message.author.display_name}: {clean_text}")
                    memory.append(f"стетхом: {answer}")
                    if len(memory) > 50:
                        del memory[:-50]

                for i in range(0, len(answer), 1990):
                    await message.reply(answer[i:i+1990])

        try:
            await client.start(DISCORD_TOKEN)
        except discord.errors.HTTPException as e:
            await client.close()
            print(f"⚠️ Лимит сети ({e.status}). Ждем 5 минут...", flush=True)
            await asyncio.sleep(300)
        except Exception as e:
            await client.close()
            print(f"⚠️ Ошибка: {e}. Перезапуск через 30 сек...", flush=True)
            await asyncio.sleep(30)


async def handle(request):
    return web.Response(text="Discord Bot is alive 24/7!")

async def main():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Порт веб-сервера: {port}", flush=True)

    await run_discord_bot()

if __name__ == "__main__":
    asyncio.run(main())

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

MODELS_POOL = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemma-4-26b-it",
    "gemma-4-31b-it",
]

# Текстовые форматы, которые бот декодирует как код/текст:
TEXT_EXTENSIONS = (
    ".txt", ".py", ".js", ".json", ".html", ".css", 
    ".cpp", ".c", ".cs", ".java", ".md", ".log", ".xml", ".yaml", ".yml"
)

# Раздельная память: для каждого канала своя история сообщений
channel_memories = defaultdict(list)


async def ask_ai(user_message: str, channel_id: int, file_parts: list = None) -> str:
    if not GEMINI_API_KEY:
        return "Ошибка: Не задан GEMINI_API_KEY в переменных окружения."

    chat_memory = channel_memories[channel_id]
    memory_text = "\n".join(chat_memory[-8:])
    
    full_prompt = f"""Ты умный и полезный ИИ-помощник на сервере в Discord.
Если прикреплены файлы, код или документы — внимательно проанализируй их и ответь на вопрос пользователя.
При необходимости используй поиск в интернете, чтобы давать актуальную информацию.
Отвечай понятно, структурированно и на русском языке.

Последние сообщения в этом канале:
{memory_text if memory_text else "(начало диалога)"}

Запрос пользователя: {user_message}"""

    parts = [{"text": full_prompt}]
    if file_parts:
        parts.extend(file_parts)

    payload = {
        "contents": [{"parts": parts}],
        # Включаем живой поиск в Google:
        "tools": [{"google_search": {}}],
        "generationConfig": {
            "temperature": 0.7
        }
    }

    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY,
    }

    last_error = None
    timeout = aiohttp.ClientTimeout(total=60)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for model_name in MODELS_POOL:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"
            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    data = await resp.json(content_type=None)

                    if resp.status == 200 and "candidates" in data and len(data["candidates"]) > 0:
                        cand_parts = data["candidates"][0].get("content", {}).get("parts", [])
                        for part in reversed(cand_parts):
                            if "text" in part and not part.get("thought", False):
                                return part["text"].strip()
                        if cand_parts and "text" in cand_parts[-1]:
                            return cand_parts[-1]["text"].strip()

                    last_error = data.get("error", {}).get("message", data)
                    print(f"⚠️ Модель {model_name} вернула ошибку: {last_error}")

            except Exception as e:
                last_error = str(e)
                continue

    return f"Ошибка ответа ИИ: `{last_error}`"


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
            print(f"✅ Бот {client.user} готов! (Поиск в Google + Чтение PDF и кода)", flush=True)

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

                # Сбор вложений из текущего сообщения или из реплая
                file_parts = []
                attachments = list(message.attachments)
                if message.reference and message.reference.resolved and isinstance(message.reference.resolved, discord.Message):
                    attachments.extend(message.reference.resolved.attachments)

                for att in attachments:
                    filename = att.filename.lower()
                    content_type = att.content_type or ""

                    # 1. Картинки
                    if content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".webp")):
                        try:
                            file_bytes = await att.read()
                            b64 = base64.b64encode(file_bytes).decode("utf-8")
                            mime = content_type if content_type.startswith("image/") else "image/jpeg"
                            file_parts.append({"inline_data": {"mime_type": mime, "data": b64}})
                        except Exception as e:
                            print(f"Ошибка картинки: {e}")

                    # 2. PDF документы
                    elif filename.endswith(".pdf") or content_type == "application/pdf":
                        try:
                            file_bytes = await att.read()
                            b64 = base64.b64encode(file_bytes).decode("utf-8")
                            file_parts.append({"inline_data": {"mime_type": "application/pdf", "data": b64}})
                        except Exception as e:
                            print(f"Ошибка чтения PDF: {e}")

                    # 3. Файлы кода и текстовые документы (.py, .txt, .json и др.)
                    elif filename.endswith(TEXT_EXTENSIONS):
                        try:
                            file_bytes = await att.read()
                            # Читаем текст файла с авто-определением кодировки UTF-8
                            text_content = file_bytes.decode("utf-8", errors="replace")
                            file_parts.append({
                                "text": f"\n\n--- Содержимое прикрепленного файла [{att.filename}] ---\n{text_content}\n--- Конец файла ---\n"
                            })
                        except Exception as e:
                            print(f"Ошибка чтения файла с кодом: {e}")

                if not clean_text:
                    if file_parts:
                        clean_text = "Проанализируй прикрепленный файл и расскажи главное."
                    else:
                        clean_text = "Привет!"

                async with message.channel.typing():
                    answer = await ask_ai(f"{message.author.display_name}: {clean_text}", message.channel.id, file_parts=file_parts)

                # Сохраняем историю в памяти именно этого канала
                if not answer.startswith("Ошибка ответа ИИ:"):
                    memory = channel_memories[message.channel.id]
                    memory.append(f"{message.author.display_name}: {clean_text}")
                    memory.append(f"Бот: {answer[:300]}")
                    if len(memory) > 12:
                        del memory[:-12]

                # Отправка ответа (разбивка длинных сообщений)
                for i in range(0, len(answer), 1990):
                    await message.reply(answer[i:i+1990])

        try:
            await client.start(DISCORD_TOKEN)
        except discord.errors.HTTPException as e:
            await client.close()
            print(f"⚠️ Ограничение Discord ({e.status}). Ждём 5 минут...", flush=True)
            await asyncio.sleep(300)
        except Exception as e:
            await client.close()
            print(f"⚠️ Ошибка: {e}. Перезапуск через 30 секунд...", flush=True)
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
    print(f"🌐 Сервер запущен на порту {port}", flush=True)

    await run_discord_bot()

if __name__ == "__main__":
    asyncio.run(main())

import os
import json
import base64
import asyncio
from collections import defaultdict
import discord
import aiohttp
from aiohttp import web

# ----------------- КОНФИГУРАЦИЯ И ТОКЕНЫ -----------------
# На Render токены берутся из вкладки Environment.
# Локально можно вписать токен Дискорда во вторые кавычки:
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Пул моделей на основе ваших лимитов в AI Studio:
MODELS_POOL = [
    # 1. Основные модели (500 запросов в день, быстрые, мультимодальные)
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",

    # 2. Гигантский запас прочности (по 14 400 запросов в день каждая)
    "gemma-4-31b-it",
    "gemma-4-26b-it",

    # 3. Резервные Flash-модели
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]

# Расширения файлов кода и документов для текстового парсинга:
TEXT_EXTENSIONS = (
    ".txt", ".py", ".js", ".ts", ".json", ".html", ".css", 
    ".cpp", ".c", ".cs", ".java", ".go", ".rs", ".md", ".log", 
    ".xml", ".yaml", ".yml", ".sql", ".sh"
)

# Раздельная память: у каждого канала свой изолированный контекст диалога
channel_memories = defaultdict(list)


# ----------------- ФУНКЦИЯ ОБРАЩЕНИЯ К ИИ -----------------
async def ask_ai(user_message: str, channel_id: int, file_parts: list = None) -> str:
    if not GEMINI_API_KEY:
        return "Ошибка: Не задан GEMINI_API_KEY в переменных окружения."

    chat_memory = channel_memories[channel_id]
    memory_text = "\n".join(chat_memory[-8:])

    full_prompt = f"""Ты умный и полезный ИИ-помощник на сервере в Discord.
Если прикреплены файлы, код или изображения — внимательно изучи их и ответь пользователю.
При необходимости используй поиск в интернете для предоставления самых свежих данных.
Отвечай понятно, емко, структурированно и на русском языке.

Последние сообщения в этом канале:
{memory_text if memory_text else "(начало диалога)"}

Запрос пользователя: {user_message}"""

    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY,
    }

    last_error = None
    timeout = aiohttp.ClientTimeout(total=45)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for model_name in MODELS_POOL:
            # Gemma — чисто текстовая модель, вложения передаем только для моделей Gemini
            parts = [{"text": full_prompt}]
            if file_parts and not model_name.startswith("gemma"):
                parts.extend(file_parts)

            payload = {
                "contents": [{"parts": parts}],
                "generationConfig": {"temperature": 0.7}
            }

            # Подключаем живой поиск Google Search (для моделей Gemini)
            if not model_name.startswith("gemma"):
                payload["tools"] = [{"google_search": {}}]

            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"

            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    data = await resp.json(content_type=None)

                    # Успешный ответ
                    if resp.status == 200 and "candidates" in data and len(data["candidates"]) > 0:
                        cand_parts = data["candidates"][0].get("content", {}).get("parts", [])
                        for part in reversed(cand_parts):
                            if "text" in part and not part.get("thought", False):
                                return part["text"].strip()
                        if cand_parts and "text" in cand_parts[-1]:
                            return cand_parts[-1]["text"].strip()

                    # Если модель не поддерживает инструмент поиска (400), пробуем повторить без tools
                    if resp.status == 400 and "tools" in payload:
                        payload.pop("tools", None)
                        async with session.post(url, json=payload, headers=headers) as retry_resp:
                            retry_data = await retry_resp.json(content_type=None)
                            if retry_resp.status == 200 and "candidates" in retry_data and len(retry_data["candidates"]) > 0:
                                return retry_data["candidates"][0]["content"]["parts"][-1]["text"].strip()

                    err_msg = data.get("error", {}).get("message", f"HTTP {resp.status}")
                    last_error = f"{model_name}: {err_msg}"
                    print(f"⚠️ Модель {model_name} не ответила ({err_msg[:45]}...), переключаюсь...")

            except Exception as e:
                last_error = f"{model_name}: {str(e)}"
                continue

    return f"Ошибка ответа ИИ: `{last_error}`"


# ----------------- ЛОГИКА ДИСКОРД-БОТА -----------------
async def run_discord_bot():
    if not DISCORD_TOKEN:
        print("❌ Ошибка: DISCORD_TOKEN не задан! Укажите его в переменных окружения.", flush=True)
        return

    while True:
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            print(f"✅ Дискорд-бот {client.user} успешно запущен и готов к работе!", flush=True)

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

            # Реагируем на @упоминание или ответ на сообщение бота
            if is_mentioned or is_reply_to_bot:
                clean_text = message.content.replace(f"<@{client.user.id}>", "").strip()

                # Сбор всех вложений (из текущего сообщения или из реплая)
                file_parts = []
                attachments = list(message.attachments)
                if message.reference and message.reference.resolved and isinstance(message.reference.resolved, discord.Message):
                    attachments.extend(message.reference.resolved.attachments)

                for att in attachments:
                    filename = att.filename.lower()
                    content_type = att.content_type or ""

                    # 1. Обработка изображений
                    if content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".webp")):
                        try:
                            file_bytes = await att.read()
                            b64 = base64.b64encode(file_bytes).decode("utf-8")
                            mime = content_type if content_type.startswith("image/") else "image/jpeg"
                            file_parts.append({"inline_data": {"mime_type": mime, "data": b64}})
                        except Exception as e:
                            print(f"Ошибка загрузки картинки: {e}")

                    # 2. Обработка PDF-документов
                    elif filename.endswith(".pdf") or content_type == "application/pdf":
                        try:
                            file_bytes = await att.read()
                            b64 = base64.b64encode(file_bytes).decode("utf-8")
                            file_parts.append({"inline_data": {"mime_type": "application/pdf", "data": b64}})
                        except Exception as e:
                            print(f"Ошибка загрузки PDF: {e}")

                    # 3. Обработка файлов кода и текстовых документов
                    elif filename.endswith(TEXT_EXTENSIONS):
                        try:
                            file_bytes = await att.read()
                            text_content = file_bytes.decode("utf-8", errors="replace")
                            file_parts.append({
                                "text": f"\n\n--- Файл [{att.filename}] ---\n{text_content}\n--- Конец файла ---\n"
                            })
                        except Exception as e:
                            print(f"Ошибка чтения текстового файла: {e}")

                if not clean_text:
                    if file_parts:
                        clean_text = "Проанализируй прикрепленный файл и расскажи подробно, что в нем."
                    else:
                        clean_text = "Привет!"

                async with message.channel.typing():
                    answer = await ask_ai(
                        f"{message.author.display_name}: {clean_text}",
                        message.channel.id,
                        file_parts=file_parts
                    )

                # Сохранение в контекст именно этого канала
                if not answer.startswith("Ошибка ответа ИИ:"):
                    memory = channel_memories[message.channel.id]
                    memory.append(f"{message.author.display_name}: {clean_text}")
                    memory.append(f"Бот: {answer[:300]}")
                    if len(memory) > 12:
                        del memory[:-12]

                # Обход лимита длины сообщений Discord (2000 символов)
                for i in range(0, len(answer), 1990):
                    await message.reply(answer[i:i+1990])

        try:
            await client.start(DISCORD_TOKEN)
        except discord.errors.HTTPException as e:
            await client.close()
            print(f"⚠️ Ограничение сети Discord ({e.status}). Ждем 5 минут...", flush=True)
            await asyncio.sleep(300)
        except Exception as e:
            await client.close()
            print(f"⚠️ Ошибка: {e}. Перезапуск через 30 секунд...", flush=True)
            await asyncio.sleep(30)


# ----------------- ВЕБ-СЕРВЕР ДЛЯ RENDER И CRON-JOB -----------------
async def handle(request):
    return web.Response(text="Discord Bot is alive 24/7!")

async def main():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()

    # Render передает свой порт через переменную PORT
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Сервер мониторинга слушает порт: {port}", flush=True)

    await run_discord_bot()

if __name__ == "__main__":
    asyncio.run(main())

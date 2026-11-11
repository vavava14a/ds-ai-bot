import os
import json
import base64
import asyncio
import discord
import aiohttp
from aiohttp import web

# Токены:
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Рабочий пул моделей:
MODELS_POOL = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemma-4-26b-it",
    "gemma-4-31b-it",
]

chat_memory = []

async def ask_ai(user_message: str, image_parts: list = None) -> str:
    if not GEMINI_API_KEY:
        return "Ошибка: Не задан GEMINI_API_KEY в переменных окружения."

    memory_text = "\n".join(chat_memory[-8:])
    full_prompt = f"""Ты умный и полезный ИИ-помощник на сервере в Discord. 
Если к сообщению прикреплено изображение — внимательно изучи его и ответь на вопрос пользователя.
Отвечай понятно, по делу и на русском языке.

Последние сообщения в чате:
{memory_text if memory_text else "(начало диалога)"}

Запрос пользователя: {user_message}"""

    # Собираем части запроса: сначала текст, затем прикрепленные изображения
    parts = [{"text": full_prompt}]
    if image_parts:
        parts.extend(image_parts)

    payload = {
        "contents": [
            {
                "parts": parts
            }
        ],
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
                    print(f"⚠️ Модель {model_name} вернула ошибку, переключаюсь: {last_error}")

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
            print(f"✅ Дискорд-бот {client.user} в сети и готов видеть картинки!", flush=True)

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
                if not clean_text:
                    clean_text = "Что изображено на картинке?"

                # 1. Поиск прикрепленных картинок (в текущем сообщении или в цитате)
                image_parts = []
                attachments_to_check = list(message.attachments)

                # Если пользователь ответил на сообщение с картинкой:
                if message.reference and message.reference.resolved and isinstance(message.reference.resolved, discord.Message):
                    attachments_to_check.extend(message.reference.resolved.attachments)

                for att in attachments_to_check:
                    # Проверяем, что файл — изображение
                    content_type = att.content_type or ""
                    if content_type.startswith("image/") or att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                        try:
                            img_data = await att.read()
                            b64_image = base64.b64encode(img_data).decode("utf-8")
                            mime = content_type if content_type.startswith("image/") else "image/jpeg"
                            image_parts.append({
                                "inline_data": {
                                    "mime_type": mime,
                                    "data": b64_image
                                }
                            })
                        except Exception as err:
                            print(f"Не удалось скачать картинку: {err}")

                # 2. Индикация набора текста и запрос к ИИ
                async with message.channel.typing():
                    answer = await ask_ai(f"{message.author.display_name}: {clean_text}", image_parts=image_parts)

                if not answer.startswith("Ошибка ответа ИИ:"):
                    chat_memory.append(f"{message.author.display_name}: {clean_text}")
                    chat_memory.append(f"Бот: {answer[:300]}")
                    if len(chat_memory) > 12:
                        del chat_memory[:-12]

                # Отправка ответа (с разделением, если текст длинный)
                for i in range(0, len(answer), 1990):
                    await message.reply(answer[i:i+1990])

        try:
            await client.start(DISCORD_TOKEN)
        except discord.errors.HTTPException as e:
            await client.close()
            print(f"⚠️ Ограничение сети Discord ({e.status}). Ожидание 5 минут...", flush=True)
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

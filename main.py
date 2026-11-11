import os
import json
import discord
import aiohttp
from aiohttp import web

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Рабочие настройки прямиком из твоего бота для Твича:
GOOGLE_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbxmxYQjk8Msrtpv5MwzhKzZBmWGqQy4rkgY6sRCYa34Z7hGWOsEcx3NA-Q8i_SHtBEA/exec"
DEFAULT_KEY = "AQ.Ab8RN6JOmjCIa_EV6ZbldIgELj-6qOvB5nFpKN10XJs3-hHlRQ"

# Если на Render в GEMINI_KEY указан ключ на AIzaSy..., берём его, иначе берём ключ из Твич-бота
env_key = os.getenv("GEMINI_KEY", "").strip()
GEMINI_API_KEY = env_key if env_key.startswith("AIza") else DEFAULT_KEY

# Пул моделей (если одна занята или недоступна, мгновенно ответит следующая)
MODELS_POOL = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemma-3-27b-it",
    "gemma-4-31b-it",
]

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Память последних сообщений, чтобы бот помнил контекст разговора
chat_memory = []

async def ask_ai(user_message: str) -> str:
    memory_text = "\n".join(chat_memory[-10:])
    full_prompt = f"""Ты умный и полезный ИИ-помощник в Discord. Отвечай понятно, по делу и на русском языке.

Контекст предыдущих сообщений:
{memory_text if memory_text else "(начало диалога)"}

Запрос пользователя: {user_message}"""

    payload = {
        "contents": [{"role": "user", "parts": [{"text": full_prompt}]}],
        "generationConfig": {"temperature": 0.7},
    }
    safe_json_bytes = json.dumps(payload, ensure_ascii=True).encode("ascii")

    last_error = None
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for model_name in MODELS_POOL:
            try:
                url = f"{GOOGLE_SCRIPT_URL.strip()}?key={GEMINI_API_KEY.strip()}&model={model_name}"
                async with session.post(
                    url,
                    data=safe_json_bytes,
                    headers={"Content-Type": "application/json"},
                    allow_redirects=True
                ) as resp:
                    data = await resp.json(content_type=None)

                    if "candidates" in data and len(data["candidates"]) > 0:
                        parts = (
                            data["candidates"][0]
                            .get("content", {})
                            .get("parts", [])
                        )
                        for part in reversed(parts):
                            if "text" in part and not part.get("thought", False):
                                return part["text"].strip()
                        if parts and "text" in parts[-1]:
                            return parts[-1]["text"].strip()
                    else:
                        last_error = data
            except Exception as e:
                last_error = str(e)
                continue

    return f"Не удалось получить ответ от моделей. Последняя ошибка: `{last_error}`"

@client.event
async def on_ready():
    print(f"Бот {client.user} успешно запущен и готов отвечать!")

@client.event
async def on_message(message):
    if message.author.bot:
        return

    is_mentioned = client.user in message.mentions
    is_reply_to_bot = (
        message.reference 
        and message.reference.resolved 
        and message.reference.resolved.author == client.user
    )

    if is_mentioned or is_reply_to_bot:
        clean_text = message.content.replace(f"<@{client.user.id}>", "").strip()
        if not clean_text:
            clean_text = "Привет!"

        async with message.channel.typing():
            answer = await ask_ai(f"{message.author.display_name}: {clean_text}")

        # Сохраняем в память только успешные ответы
        if not answer.startswith("Не удалось получить ответ"):
            chat_memory.append(f"{message.author.display_name}: {clean_text}")
            chat_memory.append(f"Бот: {answer[:300]}")
            if len(chat_memory) > 12:
                del chat_memory[:-12]

        for i in range(0, len(answer), 1990):
            await message.reply(answer[i:i+1990])

# Веб-сервер для работы 24/7 на Render
async def handle(request):
    return web.Response(text="Bot is alive 24/7!")

async def main():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    await client.start(DISCORD_TOKEN)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

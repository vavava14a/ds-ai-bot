import os
import discord
import aiohttp
from aiohttp import web

# На Render эти два ключа возьмутся из настроек Environment Variables
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY") # Твой ключ, который начинается на AIzaSy...

# Твоя готовая ссылка из Google Apps Script уже вшита:
GAS_URL = "https://script.google.com/macros/s/AKfycbwzGIV3Y5GkaKU6CyfXiSL_RIU9NR1q2XnJuOQq70ZzSvt4TWW_iuQvSO-Axoxf8ksC/exec"
MODEL = "gemini-2.5-flash-lite"

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

async def ask_ai(prompt: str) -> str:
    url = f"{GAS_URL}?model={MODEL}&key={GEMINI_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}]
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            data = await resp.json(content_type=None)
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except Exception:
                return f"Ошибка от ИИ: `{data}`"

@client.event
async def on_ready():
    print(f"Бот {client.user} в сети и готов к работе!")

@client.event
async def on_message(message):
    if message.author.bot:
        return

    # Отвечает, когда ты тегаешь бота (@Бот) или отвечаешь (Reply) на его сообщение
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
            answer = await ask_ai(clean_text)

        for i in range(0, len(answer), 1990):
            await message.reply(answer[i:i+1990])

# Веб-сервер для поддержки работы 24/7 на Render
async def handle(request):
    return web.Response(text="Bot is running 24/7!")

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

import os
import json
import asyncio
import discord
import aiohttp
from aiohttp import web

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

# Твой проверенный рабочий ключ:
GEMINI_API_KEY = "AQ.Ab8RN6JfTYbuFm6ZNWr7TNWmZNA81FAT42Kgx81w4B3bxj4W6A"

# Твой проверенный рабочий скрипт:
GOOGLE_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbxmxYQjk8Msrtpv5MwzhKzZBmWGqQy4rkgY6sRCYa34Z7hGWOsEcx3NA-Q8i_SHtBEA/exec"

# Все модели, где у тебя на скриншоте большие лимиты (500 и 14 400 в день):
MODELS_POOL = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemma-4-26b-it",
    "gemma-4-31b-it",
]

chat_memory = []

async def ask_ai(user_message: str) -> str:
    memory_text = "\n".join(chat_memory[-10:])
    full_prompt = f"""Ты умный и полезный ИИ-помощник на сервере в Discord. Отвечай понятно, по делу и на русском языке.

Последние сообщения в чате:
{memory_text if memory_text else "(начало диалога)"}

Запрос пользователя: {user_message}"""

    payload = {
        "contents": [{"role": "user", "parts": [{"text": full_prompt}]}],
        "generationConfig": {"temperature": 0.8},
    }
    safe_json_bytes = json.dumps(payload, ensure_ascii=True).encode("ascii")

    last_error = None
    timeout = aiohttp.ClientTimeout(total=90)

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

                    if data and "candidates" in data and len(data["candidates"]) > 0:
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

    return f"Ошибка ответа ИИ: `{last_error}`"


async def run_discord_bot():
    while True:
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            print(f"✅ Дискорд-бот {client.user} в сети и готов к работе!", flush=True)

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

                if not answer.startswith("Ошибка ответа ИИ:"):
                    chat_memory.append(f"{message.author.display_name}: {clean_text}")
                    chat_memory.append(f"Бот: {answer[:300]}")
                    if len(chat_memory) > 12:
                        del chat_memory[:-12]

                for i in range(0, len(answer), 1990):
                    await message.reply(answer[i:i+1990])

        try:
            await client.start(DISCORD_TOKEN)
        except discord.errors.HTTPException as e:
            await client.close()
            print(f"⚠️ Временный лимит IP от Discord ({e.status}). Ждём 5 минут...", flush=True)
            await asyncio.sleep(300)
        except Exception as e:
            await client.close()
            print(f"⚠️ Ошибка: {e}. Перезапуск через 60 секунд...", flush=True)
            await asyncio.sleep(60)


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
    print(f"🌐 Веб-сервер запущен на порту {port}", flush=True)

    await run_discord_bot()

if __name__ == "__main__":
    asyncio.run(main())

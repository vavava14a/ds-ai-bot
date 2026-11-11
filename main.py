import os
import json
import asyncio
import discord
import aiohttp
from aiohttp import web

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

# Твоя новая ссылка из скриншота Google Apps Script уже вставлена сюда:
GOOGLE_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbzKhZluY1Tjgh6meYJvtygVcdZ-GRLgFcTYyq03S7pZ8Fi8ecT7F7L3JcEN-p3NcnFk/exec"
GEMINI_API_KEY = os.getenv("GEMINI_KEY", "AQ.Ab8RN6JOmjCIa_EV6ZbldIgELj-6qOvB5nFpKN10XJs3-hHlRQ").strip()

MODELS_POOL = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemma-3-27b-it",
]

chat_memory = []

async def ask_ai(user_message: str) -> str:
    memory_text = "\n".join(chat_memory[-10:])
    system_prompt = "Ты умный, чёткий и полезный ИИ-помощник в Discord. Отвечай понятно, по делу и на русском языке."
    full_prompt = f"""Контекст предыдущих сообщений:
{memory_text if memory_text else "(начало диалога)"}

Запрос пользователя: {user_message}"""

    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # 1. Пробуем через твой скрипт Google Apps Script
        payload = {
            "contents": [{"role": "user", "parts": [{"text": f"{system_prompt}\n\n{full_prompt}"}]}],
            "generationConfig": {"temperature": 0.7},
        }
        safe_json_bytes = json.dumps(payload, ensure_ascii=True).encode("ascii")

        for model_name in MODELS_POOL:
            try:
                url = f"{GOOGLE_SCRIPT_URL}?key={GEMINI_API_KEY}&model={model_name}"
                async with session.post(
                    url,
                    data=safe_json_bytes,
                    headers={"Content-Type": "application/json"},
                    allow_redirects=True
                ) as resp:
                    data = await resp.json(content_type=None)
                    if "candidates" in data and len(data["candidates"]) > 0:
                        parts = data["candidates"][0].get("content", {}).get("parts", [])
                        for part in reversed(parts):
                            if "text" in part and not part.get("thought", False):
                                return part["text"].strip()
                    # Если ошибка в ключе (400/401), не ждём остальные модели, сразу переходим к резервному ИИ
                    if "error" in data and data["error"].get("code") in (400, 401, 403):
                        break
            except Exception:
                break

        # 2. Встроенный бесплатный ИИ без ключей (срабатывает автоматически!)
        try:
            free_payload = {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": full_prompt}
                ],
                "model": "openai"
            }
            async with session.post(
                "https://text.pollinations.ai/",
                json=free_payload,
                headers={"Content-Type": "application/json"}
            ) as resp:
                if resp.status == 200:
                    text = (await resp.text()).strip()
                    if text.startswith("{") and "choices" in text:
                        parsed = json.loads(text)
                        return parsed["choices"][0]["message"]["content"].strip()
                    if text:
                        return text
        except Exception as e:
            return f"Ошибка соединения с ИИ: `{e}`"

    return "Не удалось получить ответ от ИИ, попробуй ещё раз через пару секунд."


async def run_discord_bot():
    while True:
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            print(f"✅ Бот {client.user} успешно подключился к Discord и готов отвечать!", flush=True)

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
            print(f"⚠️ Временный лимит IP от Discord Cloudflare ({e.status}). Ждём 5 минут...", flush=True)
            await asyncio.sleep(300)
        except Exception as e:
            await client.close()
            print(f"⚠️ Ошибка: {e}. Перезапуск через 60 секунд...", flush=True)
            await asyncio.sleep(60)


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
    print(f"🌐 Веб-сервер запущен на порту {port}", flush=True)

    await run_discord_bot()

if __name__ == "__main__":
    asyncio.run(main())

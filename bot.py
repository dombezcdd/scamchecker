import asyncio
import os

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message, MenuButtonWebApp, WebAppInfo
import uvicorn

from db import ensure_user, init_db

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
PORT = int(os.getenv("PORT", "3000"))

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения Bothost.")

if not WEBAPP_URL.startswith("https://"):
    raise RuntimeError("WEBAPP_URL должен быть HTTPS-адресом.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def start_silent(message: Message):
    # Регистрирует пользователя, не отправляя текстовое сообщение.
    if message.from_user:
        ensure_user(message.from_user.id, message.from_user.username)


async def configure_menu():
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="Открыть WALLET",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )
    )


async def run_web():
    config = uvicorn.Config(
        "app:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    init_db()
    await configure_menu()

    web_task = asyncio.create_task(run_web(), name="web")
    bot_task = asyncio.create_task(dp.start_polling(bot), name="telegram-bot")
    tasks = {web_task, bot_task}

    try:
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_EXCEPTION,
        )

        for task in done:
            exception = task.exception()
            if exception is not None:
                raise exception

        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

import asyncio
import math
import os

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, MenuButtonWebApp, WebAppInfo
import uvicorn

from db import ensure_user, init_db, credit, ASSETS

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()
PORT = int(os.getenv("PORT", "3000"))

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в переменных окружения Bothost.")
if not WEBAPP_URL.startswith("https://"):
    raise RuntimeError("WEBAPP_URL должен быть HTTPS-адресом.")
if not ADMIN_ID.isdigit():
    raise RuntimeError("Добавь ADMIN_ID — свой числовой Telegram ID — в переменные Bothost.")

ADMIN_ID = int(ADMIN_ID)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def start_silent(message: Message):
    if message.from_user:
        ensure_user(message.from_user.id, message.from_user.username)


@dp.message(Command("give"))
async def give_demo_balance(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("У тебя нет доступа к этой команде.")
        return

    parts = (message.text or "").split()
    if len(parts) != 4:
        await message.answer(
            "Формат: /give ID ВАЛЮТА СУММА\n"
            "Пример: /give 123456789 USDT 1000"
        )
        return

    try:
        target_id = int(parts[1])
        asset = parts[2].upper()
        amount = float(parts[3])
    except (ValueError, TypeError):
        await message.answer("Проверь ID и сумму. Сумма должна быть числом.")
        return

    if target_id <= 0:
        await message.answer("Telegram ID должен быть положительным числом.")
        return
    if asset not in ASSETS:
        await message.answer("Неизвестная валюта. Доступно: " + ", ".join(ASSETS))
        return
    if not math.isfinite(amount) or amount <= 0 or amount > 1_000_000_000:
        await message.answer("Сумма должна быть больше 0 и не превышать 1 000 000 000.")
        return

    ensure_user(target_id)
    credit(target_id, asset, amount, note=f"DEMO начисление администратором {ADMIN_ID}")
    await message.answer(
        f"Готово: пользователю {target_id} начислено {amount:g} DEMO {asset}."
    )


async def configure_menu():
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="Открыть WALLET",
            web_app=WebAppInfo(url=WEBAPP_URL),
        )
    )


async def run_web():
    config = uvicorn.Config("app:app", host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    init_db()
    await configure_menu()
    web_task = asyncio.create_task(run_web(), name="web")
    bot_task = asyncio.create_task(dp.start_polling(bot), name="telegram-bot")
    tasks = {web_task, bot_task}

    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
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

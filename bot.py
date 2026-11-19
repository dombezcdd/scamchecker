import asyncio
import os
import secrets

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

from db import ASSETS, init_db, ensure_user, get_balances, credit, create_check, claim_check

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "memgriefhelp").strip().lstrip("@").lower()

def is_admin(user):
    # Рекомендуется указать числовой Telegram ID в ADMIN_IDS.
    return user.id in ADMIN_IDS or (user.username or "").lower() == ADMIN_USERNAME

if not TOKEN:
    raise RuntimeError("Укажите BOT_TOKEN в .env")

bot = Bot(TOKEN)
dp = Dispatcher()

def menu():
    rows = []
    if WEBAPP_URL.startswith("https://"):
        rows.append([InlineKeyboardButton(
            text="💎 Открыть WALLET",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )])
    rows.extend([
        [InlineKeyboardButton(text="💰 Мой баланс", callback_data="balance")],
        [InlineKeyboardButton(text="📜 История", callback_data="history")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@dp.message(CommandStart())
async def start(message: Message):
    ensure_user(message.from_user.id, message.from_user.username)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("check_"):
        check_id = parts[1][6:]
        try:
            asset, amount = claim_check(check_id, message.from_user.id)
            await message.answer(
                f"🎁 Чек активирован! Начислено {amount:g} {asset} DEMO.\n"
                "Это внутренние тестовые единицы, не реальные криптоактивы."
            )
        except ValueError as e:
            await message.answer(str(e))
        return
    await message.answer(
        "💎 <b>WALLET BOT</b>\n\n"
        "Добро пожаловать! Здесь доступны внутренние демонстрационные балансы, "
        "чеки и обмен тестовых единиц. Реальные депозиты и выводы не поддерживаются.",
        reply_markup=menu(),
        parse_mode="HTML",
    )

@dp.message(Command("id"))
async def show_id(message: Message):
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>", parse_mode="HTML")

@dp.message(Command("credit"))
async def admin_credit(message: Message):
    if not is_admin(message.from_user):
        await message.answer("⛔ Команда доступна только владельцу.")
        return
    parts = (message.text or "").split()
    if len(parts) != 4:
        await message.answer("Формат: /credit USER_ID ASSET AMOUNT\nПример: /credit 123456789 TON 25")
        return
    try:
        user_id = int(parts[1])
        asset = parts[2].upper()
        amount = float(parts[3].replace(",", "."))
        if asset not in ASSETS or amount <= 0 or amount > 1_000_000:
            raise ValueError()
        credit(user_id, asset, amount, f"Начислено администратором {message.from_user.id}")
        await message.answer(f"✅ Начислено {amount:g} {asset} DEMO пользователю {user_id}.")
        try:
            await bot.send_message(
                user_id,
                f"🎁 Вам начислено <b>{amount:g} {asset} DEMO</b> владельцем бота.\n"
                "Это внутренние тестовые единицы, не реальные криптоактивы.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    except (ValueError, TypeError):
        await message.answer("Проверьте ID, валюту и сумму. Поддерживаются: " + ", ".join(ASSETS))

@dp.message(Command("check"))
async def admin_check(message: Message):
    if not is_admin(message.from_user):
        await message.answer("⛔ Команда доступна только владельцу.")
        return
    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Формат: /check ASSET AMOUNT\nПример: /check TON 5")
        return
    try:
        asset = parts[1].upper()
        amount = float(parts[2].replace(",", "."))
        if asset not in ASSETS or amount <= 0 or amount > 1_000_000:
            raise ValueError()
        check_id = secrets.token_urlsafe(9)
        create_check(check_id, message.from_user.id, asset, amount)
        if not BOT_USERNAME:
            await message.answer("Добавьте BOT_USERNAME в .env, чтобы сформировать ссылку на чек.")
            return
        url = f"https://t.me/{BOT_USERNAME}?start=check_{check_id}"
        await message.answer(
            f"🎁 Демонстрационный чек создан: <b>{amount:g} {asset} DEMO</b>\n"
            f"Ссылка: {url}\n"
            "Активировать можно один раз. Это не перевод реальной криптовалюты.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except (ValueError, TypeError):
        await message.answer("Проверьте валюту и сумму. Поддерживаются: " + ", ".join(ASSETS))

@dp.callback_query(F.data == "balance")
async def balance(call):
    ensure_user(call.from_user.id, call.from_user.username)
    balances = get_balances(call.from_user.id)
    lines = ["💰 <b>Ваш DEMO-баланс</b>"]
    lines += [f"• {a}: <code>{balances.get(a, 0):.8f}</code>" for a in ASSETS]
    lines.append("\nЭто внутренние тестовые единицы, не реальные криптоактивы.")
    await call.message.answer("\n".join(lines), parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "history")
async def history(call):
    await call.message.answer("История операций доступна в Mini App.")
    await call.answer()

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

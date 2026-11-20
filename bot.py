"""
Shop Bot — магазин в Telegram с товарами, которые добавляет сам админ.

Что умеет:
- Админ добавляет/редактирует/удаляет товары прямо в чате с ботом (без правки кода).
- У каждого товара: название, цена (в Telegram Stars), описание, остаток,
  и "контент выдачи" — текст/ссылка/код, который покупатель получает после оплаты.
- Покупатель смотрит каталог, оплачивает звёздами (официальный платёжный метод
  Telegram, деньги поступают на баланс бота в Fragment/Stars, вывод — через
  официальный интерфейс Telegram), получает товар автоматически.
- Ведётся история покупок.

Установка зависимостей:
    pip install aiogram

Перед запуском:
    1. Получи токен бота у @BotFather.
    2. Узнай свой Telegram user_id (например, у @userinfobot).
    3. Впиши их ниже в BOT_TOKEN и ADMIN_IDS.

Запуск:
    python shop_bot.py
"""

import asyncio
import logging
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

BOT_TOKEN = "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER"
ADMIN_IDS = {8684468454}  # твой Telegram user_id (можно добавить ещё через запятую)
DB_PATH = "shop.db"

logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------

db = sqlite3.connect(DB_PATH)
db.execute("""
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    price_stars INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    stock INTEGER NOT NULL DEFAULT 0,
    delivery_content TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1
)
""")
db.execute("""
CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    product_name TEXT NOT NULL,
    price_stars INTEGER NOT NULL,
    created_at TEXT NOT NULL
)
""")
db.commit()


def db_add_product(name: str, price: int, description: str, stock: int, content: str) -> int:
    cur = db.execute(
        "INSERT INTO products (name, price_stars, description, stock, delivery_content) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, price, description, stock, content),
    )
    db.commit()
    return cur.lastrowid


def db_list_products(active_only: bool = True):
    q = "SELECT id, name, price_stars, description, stock FROM products"
    if active_only:
        q += " WHERE is_active = 1 AND stock > 0"
    return db.execute(q).fetchall()


def db_get_product(product_id: int):
    return db.execute(
        "SELECT id, name, price_stars, description, stock, delivery_content FROM products WHERE id = ?",
        (product_id,),
    ).fetchone()


def db_delete_product(product_id: int):
    db.execute("DELETE FROM products WHERE id = ?", (product_id,))
    db.commit()


def db_set_stock(product_id: int, stock: int):
    db.execute("UPDATE products SET stock = ? WHERE id = ?", (stock, product_id))
    db.commit()


def db_decrement_stock(product_id: int):
    db.execute("UPDATE products SET stock = stock - 1 WHERE id = ?", (product_id,))
    db.commit()


def db_record_purchase(user_id: int, name: str, price: int):
    db.execute(
        "INSERT INTO purchases (user_id, product_name, price_stars, created_at) VALUES (?, ?, ?, ?)",
        (user_id, name, price, datetime.now().strftime("%d.%m.%Y %H:%M")),
    )
    db.commit()


def db_user_purchases(user_id: int):
    return db.execute(
        "SELECT product_name, price_stars, created_at FROM purchases WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def main_menu_kb(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🛍 Каталог")],
        [KeyboardButton(text="📜 Мои покупки"), KeyboardButton(text="🆘 Помощь")],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def catalog_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{name} — {price} ⭐️", callback_data=f"product_{pid}")]
        for pid, name, price, _desc, _stock in db_list_products()
    ]
    if not rows:
        rows = [[InlineKeyboardButton(text="Каталог пуст", callback_data="noop")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def product_card_kb(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Купить", callback_data=f"buy_{product_id}")],
        [InlineKeyboardButton(text="⬅️ К каталогу", callback_data="catalog")],
    ])


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="admin_add")],
        [InlineKeyboardButton(text="📋 Список товаров", callback_data="admin_list")],
    ])


async def safe_edit(message: Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


# ---------------------------------------------------------------------------
# FSM: добавление товара пошагово
# ---------------------------------------------------------------------------

class AddProduct(StatesGroup):
    name = State()
    price = State()
    description = State()
    stock = State()
    content = State()


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ---------------------------------------------------------------------------
# Пользовательские хендлеры
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Добро пожаловать! Здесь можно посмотреть каталог и купить товары за Telegram Stars.",
        reply_markup=main_menu_kb(message.from_user.id),
    )


@dp.message(F.text == "🛍 Каталог")
async def show_catalog(message: Message):
    await message.answer("Каталог товаров:", reply_markup=catalog_kb())


@dp.message(F.text == "📜 Мои покупки")
async def show_purchases(message: Message):
    rows = db_user_purchases(message.from_user.id)
    if not rows:
        await message.answer("У вас пока нет покупок.")
        return
    text = "📜 <b>Ваши покупки:</b>\n\n" + "\n".join(
        f"▪️ {name} — {price} ⭐️ ({stamp})" for name, price, stamp in rows
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(F.text == "🆘 Помощь")
async def show_help(message: Message):
    await message.answer("По любым вопросам пишите в поддержку: @your_support_username")


@dp.callback_query(F.data == "catalog")
async def cb_catalog(callback: types.CallbackQuery):
    await safe_edit(callback.message, "Каталог товаров:", catalog_kb())
    await callback.answer()


@dp.callback_query(F.data.startswith("product_"))
async def cb_product_card(callback: types.CallbackQuery):
    product_id = int(callback.data.split("_", 1)[1])
    product = db_get_product(product_id)
    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    _id, name, price, description, stock, _content = product
    text = f"<b>{name}</b>\n\n{description}\n\n💰 {price} ⭐️\n📦 Осталось: {stock}"
    await safe_edit(callback.message, text, product_card_kb(product_id))
    await callback.answer()


@dp.callback_query(F.data.startswith("buy_"))
async def cb_buy(callback: types.CallbackQuery):
    product_id = int(callback.data.split("_", 1)[1])
    product = db_get_product(product_id)
    if not product or product[4] <= 0:
        await callback.answer("Товар недоступен.", show_alert=True)
        return
    _id, name, price, description, _stock, _content = product

    # Telegram Stars: currency "XTR", provider_token не нужен
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=name,
        description=description or name,
        payload=f"product_{product_id}",
        currency="XTR",
        prices=[LabeledPrice(label=name, amount=price)],
    )
    await callback.answer()


@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout: PreCheckoutQuery):
    await pre_checkout.answer(ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    if not payload.startswith("product_"):
        return
    product_id = int(payload.split("_", 1)[1])
    product = db_get_product(product_id)
    if not product or product[4] <= 0:
        await message.answer("Оплата прошла, но товар закончился. Напишите в поддержку.")
        return

    _id, name, price, _description, _stock, content = product
    db_decrement_stock(product_id)
    db_record_purchase(message.from_user.id, name, price)

    await message.answer(
        f"✅ Оплата получена!\n\n<b>{name}</b>\n\nВаш товар:\n<code>{content}</code>",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Админ-хендлеры
# ---------------------------------------------------------------------------

@dp.message(F.text == "⚙️ Админ-панель")
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Админ-панель:", reply_markup=admin_kb())


@dp.callback_query(F.data == "admin_list")
async def cb_admin_list(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    rows = db_list_products(active_only=False)
    if not rows:
        await callback.message.answer("Товаров пока нет.")
    else:
        lines = [
            f"#{pid} <b>{name}</b> — {price}⭐️ | остаток: {stock}\n{desc}\n"
            for pid, name, price, desc, stock in rows
        ]
        await callback.message.answer(
            "\n".join(lines) + "\nУдалить: /delete ID\nИзменить остаток: /setstock ID количество",
            parse_mode="HTML",
        )
    await callback.answer()


@dp.callback_query(F.data == "admin_add")
async def cb_admin_add(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.answer("Введите название товара:")
    await state.set_state(AddProduct.name)
    await callback.answer()


@dp.message(AddProduct.name)
async def add_product_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Введите цену в Telegram Stars (целое число):")
    await state.set_state(AddProduct.price)


@dp.message(AddProduct.price)
async def add_product_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Цена должна быть числом. Введите ещё раз:")
        return
    await state.update_data(price=int(message.text))
    await message.answer("Введите описание товара:")
    await state.set_state(AddProduct.description)


@dp.message(AddProduct.description)
async def add_product_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await message.answer("Введите количество на складе (целое число):")
    await state.set_state(AddProduct.stock)


@dp.message(AddProduct.stock)
async def add_product_stock(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите число:")
        return
    await state.update_data(stock=int(message.text))
    await message.answer(
        "Введите контент, который получит покупатель после оплаты "
        "(текст, ссылка, код активации и т.п.):"
    )
    await state.set_state(AddProduct.content)


@dp.message(AddProduct.content)
async def add_product_content(message: Message, state: FSMContext):
    data = await state.get_data()
    product_id = db_add_product(
        name=data["name"],
        price=data["price"],
        description=data["description"],
        stock=data["stock"],
        content=message.text.strip(),
    )
    await state.clear()
    await message.answer(
        f"✅ Товар добавлен (#{product_id}): {data['name']} — {data['price']} ⭐️, "
        f"остаток {data['stock']}.",
        reply_markup=main_menu_kb(message.from_user.id),
    )


@dp.message(Command("delete"))
async def cmd_delete(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Используйте: /delete ID")
        return
    db_delete_product(int(parts[1]))
    await message.answer("Товар удалён.")


@dp.message(Command("setstock"))
async def cmd_setstock(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Используйте: /setstock ID количество")
        return
    db_set_stock(int(parts[1]), int(parts[2]))
    await message.answer("Остаток обновлён.")


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: types.CallbackQuery):
    await callback.answer()


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

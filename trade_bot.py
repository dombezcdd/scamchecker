import asyncio
import json
import logging
import os
import re
import sqlite3
from html import escape

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_ТОКЕН")
ADMIN_ID = 1749483152
CHANNEL = "@CTD_chanel"          # канал для обязательной подписки (ОП)
DB_PATH = "units.db"
PAGE_SIZE = 10                   # юнитов/трейдов на страницу
MAX_QTY = 999                    # максимум штук одного юнита в трейде

RARITIES = {
    "m": "🔮 Мифическая",
    "g": "👑 Годли",
    "l": "💎 Лимит",
}
DEFAULT_UNITS = {
    "m": ["Палач"],
    "g": [],
    "l": [],
}
# =====================================================

logging.basicConfig(level=logging.INFO)

# ---------------------- БАЗА ДАННЫХ ----------------------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row

def init_db():
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            created TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS units (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE COLLATE NOCASE,
            rarity TEXT DEFAULT 'm'
        );
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            kind TEXT,                    -- give / seek / trade
            unit TEXT,                    -- give/seek: один юнит
            want TEXT,                    -- (старые трейды)
            qty INTEGER DEFAULT 1,        -- (старые трейды)
            want_qty INTEGER DEFAULT 1,   -- (старые трейды)
            items TEXT,                   -- трейд: JSON {"a": {юнит: шт}, "b": {юнит: шт}}
            status TEXT DEFAULT 'active', -- active / done / cancelled
            created TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    ucols = [r["name"] for r in db.execute("PRAGMA table_info(units)").fetchall()]
    if "rarity" not in ucols:
        db.execute("ALTER TABLE units ADD COLUMN rarity TEXT DEFAULT 'm'")
    rcols = [r["name"] for r in db.execute("PRAGMA table_info(requests)").fetchall()]
    for col, ddl in (("qty", "INTEGER DEFAULT 1"), ("want_qty", "INTEGER DEFAULT 1"), ("items", "TEXT")):
        if col not in rcols:
            db.execute(f"ALTER TABLE requests ADD COLUMN {col} {ddl}")
    for rk, names in DEFAULT_UNITS.items():
        for name in names:
            db.execute("INSERT OR IGNORE INTO units(name, rarity) VALUES (?,?)", (name, rk))
    db.commit()

def save_user(u):
    db.execute(
        "INSERT INTO users(user_id, username, full_name) VALUES (?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name",
        (u.id, u.username, u.full_name),
    )
    db.commit()

def get_units(rarity: str):
    return db.execute(
        "SELECT id, name FROM units WHERE rarity=? ORDER BY name COLLATE NOCASE", (rarity,)
    ).fetchall()

def unit_name(uid):
    r = db.execute("SELECT name FROM units WHERE id=?", (int(uid),)).fetchone()
    return r["name"] if r else None

def contact(user_id: int) -> str:
    r = db.execute("SELECT username, full_name FROM users WHERE user_id=?", (user_id,)).fetchone()
    if r and r["username"]:
        return f"@{escape(r['username'])}"
    name = escape(r["full_name"]) if r and r["full_name"] else "Игрок"
    return f'<a href="tg://user?id={user_id}">{name}</a>'

def trade_items(r):
    """Возвращает (отдаёт: {юнит: шт}, хочет: {юнит: шт}) — поддерживает и старые трейды."""
    if r["items"]:
        d = json.loads(r["items"])
        return d["a"], d["b"]
    return {r["unit"]: r["qty"] or 1}, {r["want"]: r["want_qty"] or 1}

def fmt_items(items: dict) -> str:
    return " + ".join(f"<b>{q}× {escape(n)}</b>" for n, q in items.items())

def trade_line(r) -> str:
    a, b = trade_items(r)
    return f"{fmt_items(a)} ➜ {fmt_items(b)}"

def describe(r) -> str:
    if r["kind"] == "give":
        return f"🎁 Отдаю: <b>{escape(r['unit'])}</b>"
    if r["kind"] == "seek":
        return f"🔍 Ищу: <b>{escape(r['unit'])}</b>"
    return f"🔄 Трейд: {trade_line(r)}"

def create_single(user_id, kind, unit):
    dup = db.execute(
        "SELECT id FROM requests WHERE user_id=? AND kind=? AND unit=? COLLATE NOCASE AND status='active'",
        (user_id, kind, unit),
    ).fetchone()
    if dup:
        return None
    cur = db.execute("INSERT INTO requests(user_id, kind, unit) VALUES (?,?,?)", (user_id, kind, unit))
    db.commit()
    return db.execute("SELECT * FROM requests WHERE id=?", (cur.lastrowid,)).fetchone()

def create_trade(user_id, a: dict, b: dict):
    items = json.dumps({"a": a, "b": b}, ensure_ascii=False, sort_keys=True)
    dup = db.execute(
        "SELECT id FROM requests WHERE user_id=? AND kind='trade' AND items=? AND status='active'",
        (user_id, items),
    ).fetchone()
    if dup:
        return None
    cur = db.execute("INSERT INTO requests(user_id, kind, items) VALUES (?,?,?)", (user_id, "trade", items))
    db.commit()
    return db.execute("SELECT * FROM requests WHERE id=?", (cur.lastrowid,)).fetchone()

def _norm(d: dict) -> dict:
    return {k.lower(): v for k, v in d.items()}

def find_match(r):
    if r["kind"] in ("give", "seek"):
        other = "seek" if r["kind"] == "give" else "give"
        return db.execute(
            "SELECT * FROM requests WHERE status='active' AND kind=? AND unit=? COLLATE NOCASE "
            "AND user_id!=? ORDER BY id LIMIT 1", (other, r["unit"], r["user_id"]),
        ).fetchone()
    # трейд: чужой набор «отдаю» = мой набор «хочу» и наоборот (с количеством)
    my_a, my_b = map(_norm, trade_items(r))
    rows = db.execute(
        "SELECT * FROM requests WHERE status='active' AND kind='trade' AND user_id!=? ORDER BY id",
        (r["user_id"],),
    ).fetchall()
    for c in rows:
        ca, cb = map(_norm, trade_items(c))
        if ca == my_b and cb == my_a:
            return c
    return None

def close_requests(*ids):
    for i in ids:
        db.execute("UPDATE requests SET status='done' WHERE id=?", (i,))
    db.commit()

def parse_rarity(token: str):
    t = token.lower()
    if t.startswith(("миф", "м", "m")):
        return "m"
    if t.startswith(("год", "г", "g")):
        return "g"
    if t.startswith(("лим", "л", "l")):
        return "l"
    return None

# ---------------------- КОРЗИНА (состояние) ----------------------
class Qty(StatesGroup):
    wait = State()

def cur(d):
    return d[d["side"]]

def fmt_basket(basket: dict, with_qty: bool) -> str:
    parts = []
    for uid, q in basket.items():
        n = unit_name(uid)
        if n:
            parts.append(f"{escape(n)} ×{q}" if with_qty else escape(n))
    return ", ".join(parts) or "пусто"

def head(d) -> str:
    mode, side = d["mode"], d["side"]
    if mode == "give":
        t = "🎁 <b>Даю юнитов</b>\nНажимай на юнитов, которых готов отдать (можно несколько)."
    elif mode == "seek":
        t = "🔍 <b>Ищу юнитов</b>\nНажимай на юнитов, которых ищешь (можно несколько)."
    elif side == "a":
        t = "🔄 <b>Трейд · шаг 1/2</b>\nВыбери, что <b>ОТДАЁШЬ</b> (можно несколько)."
    else:
        t = (f"🔄 <b>Трейд · шаг 2/2</b>\nОтдаёшь: {fmt_basket(d['a'], True)}\n"
             "Выбери, что <b>ХОЧЕШЬ ПОЛУЧИТЬ</b> (можно несколько).")
    return f"{t}\n\n🧺 Выбрано: {fmt_basket(cur(d), mode == 'trade')}"

# ---------------------- КЛАВИАТУРЫ ----------------------
def main_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Начать поиск", callback_data="find")
    kb.button(text="🔄 Трейд", callback_data="go:trade")
    kb.button(text="🔎 Активный поиск", callback_data="asr")
    kb.button(text="📈 Активные трейды", callback_data="atr:0")
    kb.button(text="📋 Мои заявки", callback_data="my")
    kb.adjust(2, 2, 1)
    return kb.as_markup()

def find_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Даю юнитов", callback_data="go:give")
    kb.button(text="🔍 Ищу юнитов", callback_data="go:seek")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()

def bottom_rows(kb, d):
    n = len(cur(d))
    if n:
        kb.row(InlineKeyboardButton(text=f"🧺 Корзина ({n})", callback_data="cart"),
               InlineKeyboardButton(text="✅ Готово", callback_data="done"))

def picker_kb(d):
    kb = InlineKeyboardBuilder()
    for rk, label in RARITIES.items():
        kb.button(text=label, callback_data=f"rp:{rk}:0")
    kb.adjust(1)
    bottom_rows(kb, d)
    kb.row(InlineKeyboardButton(text="🏠 Меню", callback_data="menu"))
    return kb.as_markup()

def units_kb(d, rarity: str, page: int):
    units = get_units(rarity)
    pages = max(1, (len(units) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chosen = cur(d)
    kb = InlineKeyboardBuilder()
    for u in units[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        mark = "✅ " if str(u["id"]) in chosen else ""
        kb.button(text=f"{mark}{u['name']}", callback_data=f"tg:{rarity}:{page}:{u['id']}")
    kb.adjust(2)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"rp:{rarity}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"rp:{rarity}:{page + 1}"))
    kb.row(*nav)
    bottom_rows(kb, d)
    kb.row(InlineKeyboardButton(text="⬅️ Редкости", callback_data="rr"),
           InlineKeyboardButton(text="🏠 Меню", callback_data="menu"))
    return kb.as_markup()

def cart_text(d) -> str:
    mode, side = d["mode"], d["side"]
    label = {"give": "Отдаю", "seek": "Ищу"}.get(mode) or ("Отдаю" if side == "a" else "Хочу получить")
    b = cur(d)
    lines = []
    for uid, q in b.items():
        n = unit_name(uid)
        if n:
            lines.append(f"• {escape(n)}" + (f" ×{q}" if mode == "trade" else ""))
    body = "\n".join(lines) or "Пока пусто — добавь юнитов."
    extra = "\n\n➖/➕ меняют количество, нажми на название, чтобы ввести число." if mode == "trade" and b else ""
    return f"🧺 <b>Корзина — {label}</b>\n\n{body}{extra}"

def cart_kb(d):
    mode = d["mode"]
    kb = InlineKeyboardBuilder()
    for uid, q in cur(d).items():
        n = unit_name(uid)
        if not n:
            continue
        if mode == "trade":
            kb.row(
                InlineKeyboardButton(text="➖", callback_data=f"b-:{uid}"),
                InlineKeyboardButton(text=f"{n} ×{q}", callback_data=f"bs:{uid}"),
                InlineKeyboardButton(text="➕", callback_data=f"b+:{uid}"),
                InlineKeyboardButton(text="❌", callback_data=f"bx:{uid}"),
            )
        else:
            kb.row(InlineKeyboardButton(text=f"❌ {n}", callback_data=f"bx:{uid}"))
    kb.row(InlineKeyboardButton(text="➕ Добавить ещё", callback_data="rr"),
           InlineKeyboardButton(text="🗑 Очистить", callback_data="clr"))
    kb.row(InlineKeyboardButton(text="✅ Готово", callback_data="done"),
           InlineKeyboardButton(text="🏠 Меню", callback_data="menu"))
    return kb.as_markup()

def sub_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться", url=f"https://t.me/{CHANNEL.lstrip('@')}")],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
    ])

# ---------------------- ОП (подписка) ----------------------
async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL, user_id)
        return m.status in ("creator", "administrator", "member")
    except Exception as e:
        logging.warning("Не удалось проверить подписку (бот должен быть админом канала): %s", e)
        return True

SUB_TEXT = f"👋 Чтобы пользоваться ботом, подпишись на канал {CHANNEL} и нажми «Я подписался»."

class SubMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None or user.id == ADMIN_ID:
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "check_sub":
            return await handler(event, data)
        if await is_subscribed(data["bot"], user.id):
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer(SUB_TEXT, reply_markup=sub_kb())
        elif isinstance(event, CallbackQuery):
            await event.message.answer(SUB_TEXT, reply_markup=sub_kb())
            await event.answer()

# ---------------------- ВСПОМОГАТЕЛЬНОЕ ----------------------
async def show(event, text, kb=None):
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await event.message.answer(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)

MENU_TEXT = ("🎮 <b>Поиск юнитов</b>\n\n"
             "🔍 <b>Поиск</b> — отдай или найди сразу несколько юнитов.\n"
             "🔄 <b>Трейд</b> — обмен наборами: например, Спайдер + Кейк на Годли, или 10 на 2.\n"
             "🔎 <b>Активный поиск</b> / 📈 <b>Активные трейды</b> — что сейчас ищут другие.\n\n"
             f"Канал: {CHANNEL}")

async def settle(bot: Bot, r) -> str:
    """Ищет пару для заявки r. Возвращает строку для пользователя."""
    m = find_match(r)
    if not m:
        return f"⏳ {describe(r)} — ждём пару"
    close_requests(r["id"], m["id"])
    try:
        await bot.send_message(
            m["user_id"],
            f"🎉 <b>Нашлось!</b>\n{describe(m)}\n\nИгрок: {contact(r['user_id'])}",
        )
    except Exception as e:
        logging.warning("Не удалось уведомить %s: %s", m["user_id"], e)
    return f"🎉 <b>Нашлось!</b> {describe(r)}\nИгрок: {contact(m['user_id'])}"

async def load(c: CallbackQuery, state: FSMContext):
    """Достаёт корзину из состояния; если её нет (например, бот перезапускался) — возвращает в меню."""
    await state.set_state(None)
    d = await state.get_data()
    if not d.get("mode"):
        await show(c, "⌛ Сессия устарела, начни заново.\n\n" + MENU_TEXT, main_kb())
        return None
    return d

async def show_units(event, d, rk, page):
    await show(event, head(d) + f"\nРедкость: {RARITIES[rk]}", units_kb(d, rk, page))

async def show_cart(event, d):
    await show(event, cart_text(d), cart_kb(d))

# ---------------------- СПИСКИ АКТИВНОГО ----------------------
def trades_page(page: int, viewer_id: int):
    rows = db.execute(
        "SELECT * FROM requests WHERE kind='trade' AND status='active' ORDER BY id DESC"
    ).fetchall()
    total = len(rows)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    if not total:
        text = "📈 <b>Активные трейды</b>\n\nСейчас активных трейдов нет."
    else:
        lines = []
        for i, r in enumerate(chunk, start=page * PAGE_SIZE + 1):
            mine = " 👈 твой" if r["user_id"] == viewer_id else ""
            lines.append(f"{i}. {trade_line(r)}{mine}")
        text = f"📈 <b>Активные трейды</b>\nВсего сейчас: <b>{total}</b>\n\n" + "\n".join(lines)
    kb = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"atr:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"atr:{page + 1}"))
    kb.row(*nav)
    kb.row(InlineKeyboardButton(text="🔄 Обновить", callback_data=f"atr:{page}"),
           InlineKeyboardButton(text="🏠 Меню", callback_data="menu"))
    return text, kb.as_markup()

def search_text():
    def section(kind, header):
        rows = db.execute(
            "SELECT unit, COUNT(*) c FROM requests WHERE kind=? AND status='active' "
            "GROUP BY unit COLLATE NOCASE ORDER BY c DESC, unit COLLATE NOCASE", (kind,)
        ).fetchall()
        total = sum(r["c"] for r in rows)
        if not rows:
            return f"{header} (0): —"
        lines, size = [], 0
        for i, r in enumerate(rows):
            line = f"• {escape(r['unit'])} — {r['c']}"
            if size + len(line) > 1700:
                lines.append(f"…и ещё {len(rows) - i} юнит(ов)")
                break
            lines.append(line)
            size += len(line) + 1
        return f"{header} ({total}):\n" + "\n".join(lines)

    return ("🔎 <b>Активный поиск</b>\n\n"
            + section("give", "🎁 Отдают") + "\n\n" + section("seek", "🔍 Ищут"))

# ---------------------- ХЭНДЛЕРЫ ----------------------
dp = Dispatcher()
dp.message.outer_middleware(SubMiddleware())
dp.callback_query.outer_middleware(SubMiddleware())

@dp.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    save_user(m.from_user)
    await m.answer(MENU_TEXT, reply_markup=main_kb())

@dp.callback_query(F.data == "check_sub")
async def check_sub(c: CallbackQuery, bot: Bot):
    if await is_subscribed(bot, c.from_user.id):
        save_user(c.from_user)
        await show(c, MENU_TEXT, main_kb())
    else:
        await c.answer("Ты ещё не подписался 😉", show_alert=True)

@dp.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    save_user(c.from_user)
    await show(c, MENU_TEXT, main_kb())

@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()

@dp.callback_query(F.data == "find")
async def cb_find(c: CallbackQuery):
    save_user(c.from_user)
    await show(c, "🔍 <b>Поиск юнитов</b>\nЧто хочешь сделать?", find_kb())

@dp.callback_query(F.data == "asr")
async def cb_active_search(c: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data="asr")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(2)
    await show(c, search_text(), kb.as_markup())

@dp.callback_query(F.data.startswith("atr:"))
async def cb_active_trades(c: CallbackQuery):
    text, kb = trades_page(int(c.data.split(":")[1]), c.from_user.id)
    await show(c, text, kb)

# ----- сборка корзины -----
@dp.callback_query(F.data.startswith("go:"))
async def cb_go(c: CallbackQuery, state: FSMContext):
    save_user(c.from_user)
    mode = c.data.split(":")[1]
    await state.clear()
    await state.set_data({"mode": mode, "side": "a", "a": {}, "b": {}})
    d = await state.get_data()
    await show(c, head(d), picker_kb(d))

@dp.callback_query(F.data == "rr")
async def cb_rarities(c: CallbackQuery, state: FSMContext):
    d = await load(c, state)
    if d:
        await show(c, head(d), picker_kb(d))

@dp.callback_query(F.data.startswith("rp:"))
async def cb_rarity_page(c: CallbackQuery, state: FSMContext):
    d = await load(c, state)
    if not d:
        return
    _, rk, page = c.data.split(":")
    if not get_units(rk):
        return await c.answer("В этой редкости пока нет юнитов", show_alert=True)
    await show_units(c, d, rk, int(page))

@dp.callback_query(F.data.startswith("tg:"))
async def cb_toggle(c: CallbackQuery, state: FSMContext):
    d = await load(c, state)
    if not d:
        return
    _, rk, page, uid = c.data.split(":")
    if not unit_name(uid):
        return await c.answer("Этого юнита больше нет в списке", show_alert=True)
    b = cur(d)
    if uid in b:
        del b[uid]
    else:
        b[uid] = 1
    await state.update_data({d["side"]: b})
    await show_units(c, d, rk, int(page))

@dp.callback_query(F.data == "cart")
async def cb_cart(c: CallbackQuery, state: FSMContext):
    d = await load(c, state)
    if d:
        await show_cart(c, d)

@dp.callback_query(F.data.regexp(r"^b[+\-x]:"))
async def cb_basket_edit(c: CallbackQuery, state: FSMContext):
    d = await load(c, state)
    if not d:
        return
    op, uid = c.data.split(":")
    b = cur(d)
    if uid in b:
        if op == "b+":
            b[uid] = min(MAX_QTY, b[uid] + 1)
        elif op == "b-":
            b[uid] = max(1, b[uid] - 1)
        else:
            del b[uid]
    await state.update_data({d["side"]: b})
    await show_cart(c, d)

@dp.callback_query(F.data == "clr")
async def cb_clear(c: CallbackQuery, state: FSMContext):
    d = await load(c, state)
    if not d:
        return
    await state.update_data({d["side"]: {}})
    d = await state.get_data()
    await show_cart(c, d)

@dp.callback_query(F.data.startswith("bs:"))
async def cb_set_qty(c: CallbackQuery, state: FSMContext):
    d = await load(c, state)
    if not d:
        return
    uid = c.data.split(":")[1]
    name = unit_name(uid)
    if not name:
        return await c.answer("Юнит не найден", show_alert=True)
    await state.set_state(Qty.wait)
    await state.update_data(setuid=uid)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К корзине", callback_data="cart")
    await show(c, f"✏️ Напиши числом, сколько штук <b>{escape(name)}</b> (от 1 до {MAX_QTY}):", kb.as_markup())

@dp.callback_query(F.data == "done")
async def cb_done(c: CallbackQuery, state: FSMContext, bot: Bot):
    d = await load(c, state)
    if not d:
        return
    mode, side, basket = d["mode"], d["side"], cur(d)
    if not basket:
        return await c.answer("Сначала выбери хотя бы одного юнита", show_alert=True)
    uid_ = c.from_user.id

    # --- даю / ищу: по заявке на каждого юнита ---
    if mode in ("give", "seek"):
        lines = []
        for uid in basket:
            name = unit_name(uid)
            if not name:
                continue
            r = create_single(uid_, mode, name)
            lines.append(f"⚠️ {escape(name)} — такая заявка уже есть" if r is None else await settle(bot, r))
        await state.clear()
        await show(c, "✅ <b>Готово!</b>\n\n" + "\n\n".join(lines) +
                   "\n\nКак только найдётся пара — я напишу.", main_kb())
        return

    # --- трейд, шаг 1 -> шаг 2 ---
    if side == "a":
        await state.update_data(side="b")
        d = await state.get_data()
        return await show(c, head(d), picker_kb(d))

    # --- трейд, шаг 2: создаём заявку ---
    a = {unit_name(u): q for u, q in d["a"].items() if unit_name(u)}
    b = {unit_name(u): q for u, q in d["b"].items() if unit_name(u)}
    if {n.lower() for n in a} & {n.lower() for n in b}:
        return await c.answer("Один и тот же юнит нельзя и отдавать, и получать 🙂 Поправь корзину.",
                              show_alert=True)
    r = create_trade(uid_, a, b)
    await state.clear()
    text = "⚠️ Такой трейд у тебя уже есть." if r is None else "✅ <b>Трейд создан</b>\n\n" + await settle(bot, r)
    await show(c, text, main_kb())

@dp.callback_query(F.data == "my")
async def cb_my(c: CallbackQuery):
    rows = db.execute(
        "SELECT * FROM requests WHERE user_id=? AND status='active' ORDER BY id DESC", (c.from_user.id,)
    ).fetchall()
    kb = InlineKeyboardBuilder()
    if not rows:
        text = "📋 У тебя нет активных заявок."
    else:
        lines = []
        for r in rows:
            lines.append(f"#{r['id']} {describe(r)}")
            kb.button(text=f"❌ Отменить #{r['id']}", callback_data=f"cancel:{r['id']}")
        text = "📋 <b>Твои активные заявки:</b>\n\n" + "\n".join(lines)
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    await show(c, text, kb.as_markup())

@dp.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(c: CallbackQuery):
    rid = int(c.data.split(":")[1])
    db.execute(
        "UPDATE requests SET status='cancelled' WHERE id=? AND user_id=? AND status='active'",
        (rid, c.from_user.id),
    )
    db.commit()
    await c.answer("Заявка отменена")
    await cb_my(c)

# ---------------------- АДМИН ----------------------
admin = F.from_user.id == ADMIN_ID

@dp.message(Command("admin"), admin)
async def adm_help(m: Message):
    await m.answer(
        "🛠 <b>Админ-команды</b>\n\n"
        "/send Текст — рассылка всем пользователям бота\n"
        "(или ответь командой /send на любое сообщение — оно будет разослано, можно с фото)\n\n"
        "/addunit м Юнит1, Юнит2 — добавить мифических\n"
        "/addunit г Юнит1, Юнит2 — добавить годли\n"
        "/addunit л Юнит1, Юнит2 — добавить лимитных\n"
        "(если юнит уже есть — он перенесётся в указанную редкость)\n\n"
        "/delunit Юнит — удалить юнита\n"
        "/units — список юнитов по редкостям\n"
        "/stats — статистика"
    )

async def _deliver(bot: Bot, uid: int, src, body: str):
    if src:
        await bot.copy_message(uid, src.chat.id, src.message_id)
    else:
        await bot.send_message(uid, body)

@dp.message(Command("send"), admin)
async def adm_send(m: Message, bot: Bot):
    src = m.reply_to_message
    body = re.sub(r"^/send(@\w+)?\s*", "", m.html_text or "")
    if not src and not body.strip():
        return await m.answer("Использование:\n/send Текст рассылки\nили ответь командой /send на сообщение.")
    ids = [r["user_id"] for r in db.execute("SELECT user_id FROM users").fetchall()]
    status = await m.answer(f"📤 Рассылка запущена: {len(ids)} получателей...")
    ok = fail = 0
    for uid in ids:
        try:
            try:
                await _deliver(bot, uid, src, body)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                await _deliver(bot, uid, src, body)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await status.edit_text(f"✅ Рассылка завершена\nДоставлено: {ok}\nНе доставлено: {fail}")

@dp.message(Command("addunit"), admin)
async def adm_add(m: Message, command: CommandObject):
    parts = (command.args or "").split(None, 1)
    rarity = parse_rarity(parts[0]) if parts else None
    if len(parts) < 2 or not rarity:
        return await m.answer("Использование: /addunit м|г|л Юнит1, Юнит2\n"
                              "м — мифическая, г — годли, л — лимит")
    names = [n.strip() for n in parts[1].replace("\n", ",").split(",") if n.strip()]
    for n in names:
        db.execute(
            "INSERT INTO units(name, rarity) VALUES (?,?) "
            "ON CONFLICT(name) DO UPDATE SET rarity=excluded.rarity",
            (n, rarity),
        )
    db.commit()
    await m.answer(f"✅ {RARITIES[rarity]}: добавлено/обновлено {len(names)}")

@dp.message(Command("delunit"), admin)
async def adm_del(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("Использование: /delunit Юнит")
    name = command.args.strip()
    cur_ = db.execute("DELETE FROM units WHERE name=? COLLATE NOCASE", (name,))
    db.execute(
        "UPDATE requests SET status='cancelled' WHERE status='active' AND kind IN ('give','seek') "
        "AND unit=? COLLATE NOCASE", (name,),
    )
    for r in db.execute("SELECT * FROM requests WHERE status='active' AND kind='trade'").fetchall():
        a, b = trade_items(r)
        if name.lower() in {k.lower() for k in list(a) + list(b)}:
            db.execute("UPDATE requests SET status='cancelled' WHERE id=?", (r["id"],))
    db.commit()
    await m.answer("🗑 Удалено" if cur_.rowcount else "Такого юнита нет")

@dp.message(Command("units"), admin)
async def adm_units(m: Message):
    lines = []
    for rk, label in RARITIES.items():
        names = [escape(u["name"]) for u in get_units(rk)]
        lines.append(f"{label} ({len(names)}): {', '.join(names) or '—'}")
    await m.answer("📜 <b>Юниты</b>\n\n" + "\n\n".join(lines))

@dp.message(Command("stats"), admin)
async def adm_stats(m: Message):
    def q(sql):
        return db.execute(sql).fetchone()[0]

    users = q("SELECT COUNT(*) FROM users")
    units = q("SELECT COUNT(*) FROM units")
    active = q("SELECT COUNT(*) FROM requests WHERE status='active'")
    trades = q("SELECT COUNT(*) FROM requests WHERE status='active' AND kind='trade'")
    done = q("SELECT COUNT(*) FROM requests WHERE status='done'")
    await m.answer(
        "📊 <b>Статистика</b>\n"
        f"Пользователей: {users}\n"
        f"Юнитов: {units}\n"
        f"Активных заявок: {active} (из них трейдов: {trades})\n"
        f"Закрытых заявок (найдено пар): {done}"
    )

# Ввод числа (регистрируется последним, чтобы не перехватывать команды)
@dp.message(Qty.wait, F.text, ~F.text.startswith("/"))
async def qty_typed(m: Message, state: FSMContext):
    txt = m.text.strip()
    if not txt.isdigit() or not (1 <= int(txt) <= MAX_QTY):
        return await m.answer(f"Нужно число от 1 до {MAX_QTY}. Попробуй ещё раз:")
    d = await state.get_data()
    if not d.get("mode"):
        await state.clear()
        return await m.answer(MENU_TEXT, reply_markup=main_kb())
    b = cur(d)
    if d["setuid"] in b:
        b[d["setuid"]] = int(txt)
    await state.update_data({d["side"]: b})
    await state.set_state(None)
    await show_cart(m, await state.get_data())

# ---------------------- ЗАПУСК ----------------------
async def main():
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

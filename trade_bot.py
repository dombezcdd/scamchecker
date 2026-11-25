import asyncio
import logging
import os
import sqlite3
from html import escape

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ===================== НАСТРОЙКИ =====================
# Токен лучше брать из переменной окружения BOT_TOKEN, иначе впиши сюда
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_НОВЫЙ_ТОКЕН")
ADMIN_ID = 1749483152
CHANNEL = "@CTD_chanel"          # канал для обязательной подписки (ОП)
DB_PATH = "units.db"
PAGE_SIZE = 10                   # юнитов на страницу

# Редкости: ключ -> название
RARITIES = {
    "m": "🔮 Мифическая",
    "g": "👑 Годли",
    "l": "💎 Лимит",
}
# Стартовые юниты (остальное добавляй через /addunit)
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
            kind TEXT,              -- give / seek / trade
            unit TEXT,              -- give: отдаю, seek: ищу, trade: отдаю
            want TEXT,              -- только для trade: что хочу
            status TEXT DEFAULT 'active',   -- active / done / cancelled
            created TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    # миграция для старой базы без колонки rarity
    cols = [r["name"] for r in db.execute("PRAGMA table_info(units)").fetchall()]
    if "rarity" not in cols:
        db.execute("ALTER TABLE units ADD COLUMN rarity TEXT DEFAULT 'm'")
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


def unit_name(uid: int):
    r = db.execute("SELECT name FROM units WHERE id=?", (uid,)).fetchone()
    return r["name"] if r else None


def contact(user_id: int) -> str:
    r = db.execute("SELECT username, full_name FROM users WHERE user_id=?", (user_id,)).fetchone()
    if r and r["username"]:
        return f"@{escape(r['username'])}"
    name = escape(r["full_name"]) if r and r["full_name"] else "Игрок"
    return f'<a href="tg://user?id={user_id}">{name}</a>'


def describe(r) -> str:
    if r["kind"] == "give":
        return f"🎁 Отдаю: <b>{escape(r['unit'])}</b>"
    if r["kind"] == "seek":
        return f"🔍 Ищу: <b>{escape(r['unit'])}</b>"
    return f"🔄 Трейд: <b>{escape(r['unit'])}</b> ➜ <b>{escape(r['want'])}</b>"


def create_request(user_id, kind, unit, want=None):
    """Создаёт заявку. Возвращает (request_row, duplicate: bool)."""
    dup = db.execute(
        "SELECT id FROM requests WHERE user_id=? AND kind=? AND unit=? AND IFNULL(want,'')=? AND status='active'",
        (user_id, kind, unit, want or ""),
    ).fetchone()
    if dup:
        return None, True
    cur = db.execute(
        "INSERT INTO requests(user_id, kind, unit, want) VALUES (?,?,?,?)",
        (user_id, kind, unit, want),
    )
    db.commit()
    return db.execute("SELECT * FROM requests WHERE id=?", (cur.lastrowid,)).fetchone(), False


def find_match(r):
    if r["kind"] == "give":
        q = ("SELECT * FROM requests WHERE status='active' AND kind='seek' AND unit=? COLLATE NOCASE "
             "AND user_id!=? ORDER BY id LIMIT 1", (r["unit"], r["user_id"]))
    elif r["kind"] == "seek":
        q = ("SELECT * FROM requests WHERE status='active' AND kind='give' AND unit=? COLLATE NOCASE "
             "AND user_id!=? ORDER BY id LIMIT 1", (r["unit"], r["user_id"]))
    else:
        q = ("SELECT * FROM requests WHERE status='active' AND kind='trade' "
             "AND unit=? COLLATE NOCASE AND want=? COLLATE NOCASE AND user_id!=? ORDER BY id LIMIT 1",
             (r["want"], r["unit"], r["user_id"]))
    return db.execute(*q).fetchone()


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


# ---------------------- КЛАВИАТУРЫ ----------------------
def main_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Начать поиск", callback_data="find")
    kb.button(text="🔄 Трейд", callback_data="trade")
    kb.button(text="📋 Мои заявки", callback_data="my")
    kb.adjust(1)
    return kb.as_markup()


def find_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Даю юнита", callback_data="rar:give:0")
    kb.button(text="🔍 Искать юнита", callback_data="rar:seek:0")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def rarity_kb(action: str, extra: str):
    kb = InlineKeyboardBuilder()
    for rk, label in RARITIES.items():
        kb.button(text=label, callback_data=f"pg:{action}:{extra}:{rk}:0")
    kb.button(text="🏠 Меню", callback_data="menu")
    kb.adjust(1)
    return kb.as_markup()


def units_kb(action: str, extra: str, rarity: str, page: int):
    units = get_units(rarity)
    pages = max(1, (len(units) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = units[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    kb = InlineKeyboardBuilder()
    for u in chunk:
        kb.button(text=u["name"], callback_data=f"pk:{action}:{extra}:{u['id']}")
    kb.adjust(2)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"pg:{action}:{extra}:{rarity}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"pg:{action}:{extra}:{rarity}:{page + 1}"))
    kb.row(*nav)
    kb.row(
        InlineKeyboardButton(text="⬅️ Редкости", callback_data=f"rar:{action}:{extra}"),
        InlineKeyboardButton(text="🏠 Меню", callback_data="menu"),
    )
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
        # Если бот не админ в канале — не блокируем людей, только пишем в лог
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
        if isinstance(event, CallbackQuery):
            await event.answer("Сначала подпишись на канал", show_alert=True)
            if event.message:
                try:
                    await event.message.answer(SUB_TEXT, reply_markup=sub_kb())
                except Exception:
                    pass
        else:
            await event.answer(SUB_TEXT, reply_markup=sub_kb())


dp = Dispatcher()
dp.message.middleware(SubMiddleware())
dp.callback_query.middleware(SubMiddleware())


# ---------------------- ВСПОМОГАТЕЛЬНОЕ ----------------------
MENU_TEXT = (
    "👋 <b>Бот для обмена юнитами</b>\n\n"
    "🔍 Поиск — отдай юнита или найди нужного\n"
    "🔄 Трейд — обмен юнита на юнита\n"
    "Когда найдётся пара, я пришлю вам контакты друг друга."
)


async def show(c: CallbackQuery, text: str, kb=None):
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            logging.warning("edit_text: %s", e)
    try:
        await c.answer()
    except TelegramBadRequest:
        pass


def title(action: str, extra: str, rarity: str = None) -> str:
    suffix = ""
    if rarity:
        suffix = f"\nРедкость: {RARITIES.get(rarity, rarity)}\nВыбери юнита 👇"
    else:
        suffix = "\nВыбери редкость 👇"
    if action == "give":
        return "🎁 <b>Какого юнита отдаёшь?</b>" + suffix
    if action == "seek":
        return "🔍 <b>Какого юнита ищешь?</b>" + suffix
    if action == "tr1":
        return "🔄 <b>Трейд: что ты отдаёшь?</b>" + suffix
    if action == "tr2":
        give = unit_name(int(extra)) or "?"
        return f"🔄 Отдаёшь: <b>{escape(give)}</b>\n<b>Что хочешь получить?</b>" + suffix
    return "Выбери" + suffix


async def process_request(bot: Bot, user_id: int, kind: str, unit: str, want: str = None) -> str:
    req, dup = create_request(user_id, kind, unit, want)
    if dup:
        return "⚠️ Такая заявка у тебя уже есть. Смотри «📋 Мои заявки»."

    match = find_match(req)
    if not match:
        return (f"✅ Заявка создана:\n{describe(req)}\n\n"
                "Как только найдётся подходящий человек — я напишу тебе.")

    close_requests(req["id"], match["id"])
    other = match["user_id"]
    try:
        await bot.send_message(
            other,
            f"🎉 <b>Нашлась пара!</b>\n{describe(match)}\n\n"
            f"Пиши: {contact(user_id)}",
            reply_markup=main_kb(),
        )
    except Exception as e:
        logging.warning("Не удалось уведомить %s: %s", other, e)
    return (f"🎉 <b>Нашлась пара!</b>\n{describe(req)}\n\n"
            f"Пиши: {contact(other)}")


# ---------------------- ПОЛЬЗОВАТЕЛЬ ----------------------
@dp.message(CommandStart())
async def cmd_start(m: Message):
    save_user(m.from_user)
    await m.answer(MENU_TEXT, reply_markup=main_kb())


@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(c: CallbackQuery, bot: Bot):
    if await is_subscribed(bot, c.from_user.id):
        save_user(c.from_user)
        await show(c, MENU_TEXT, main_kb())
    else:
        await c.answer("Подписка не найдена 🙁", show_alert=True)


@dp.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    await show(c, MENU_TEXT, main_kb())


@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


@dp.callback_query(F.data == "find")
async def cb_find(c: CallbackQuery):
    await show(c, "🔍 <b>Что хочешь сделать?</b>", find_kb())


@dp.callback_query(F.data == "trade")
async def cb_trade(c: CallbackQuery):
    await show(c, title("tr1", "0"), rarity_kb("tr1", "0"))


@dp.callback_query(F.data.startswith("rar:"))
async def cb_rarity(c: CallbackQuery):
    _, action, extra = c.data.split(":")
    await show(c, title(action, extra), rarity_kb(action, extra))


@dp.callback_query(F.data.startswith("pg:"))
async def cb_page(c: CallbackQuery):
    _, action, extra, rarity, page = c.data.split(":")
    if not get_units(rarity):
        return await c.answer("В этой редкости пока нет юнитов", show_alert=True)
    await show(c, title(action, extra, rarity), units_kb(action, extra, rarity, int(page)))


@dp.callback_query(F.data.startswith("pk:"))
async def cb_pick(c: CallbackQuery, bot: Bot):
    _, action, extra, uid = c.data.split(":")
    save_user(c.from_user)
    name = unit_name(int(uid))
    if not name:
        return await c.answer("Этого юнита больше нет в списке", show_alert=True)

    if action in ("give", "seek"):
        text = await process_request(bot, c.from_user.id, action, name)
        await show(c, text, main_kb())

    elif action == "tr1":
        await show(c, title("tr2", uid), rarity_kb("tr2", uid))

    elif action == "tr2":
        give = unit_name(int(extra))
        if not give:
            return await c.answer("Юнит не найден", show_alert=True)
        if give.lower() == name.lower():
            return await c.answer("Нельзя менять юнита на такого же 🙂", show_alert=True)
        text = await process_request(bot, c.from_user.id, "trade", give, name)
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
        "/addunit м Юнит1, Юнит2 — добавить мифических\n"
        "/addunit г Юнит1, Юнит2 — добавить годли\n"
        "/addunit л Юнит1, Юнит2 — добавить лимитных\n"
        "(если юнит уже есть — он перенесётся в указанную редкость)\n\n"
        "/delunit Юнит — удалить юнита\n"
        "/units — список юнитов по редкостям\n"
        "/stats — статистика"
    )


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
    cur = db.execute("DELETE FROM units WHERE name=? COLLATE NOCASE", (name,))
    db.execute(
        "UPDATE requests SET status='cancelled' WHERE status='active' "
        "AND (unit=? COLLATE NOCASE OR want=? COLLATE NOCASE)",
        (name, name),
    )
    db.commit()
    await m.answer("🗑 Удалено" if cur.rowcount else "Такого юнита нет")


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
    done = q("SELECT COUNT(*) FROM requests WHERE status='done'")
    await m.answer(
        "📊 <b>Статистика</b>\n"
        f"Пользователей: {users}\n"
        f"Юнитов: {units}\n"
        f"Активных заявок: {active}\n"
        f"Закрытых заявок (найдено пар): {done}"
    )


# ---------------------- ЗАПУСК ----------------------
async def main():
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

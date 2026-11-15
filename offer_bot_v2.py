"""
Telegram-бот для офферов на NFT-подарки (стиль карточки как у MarketRedoBot).

Команда:
    /hello <ссылка на NFT> <сумма> <stars|ton> <язык>
Пример:
    /hello https://t.me/nft/ScaredCat-10668 500 ton ru

Важно:
    Синяя карточка с превью подарка ("Scared Cat #10668" + картинка +
    кнопка "ПОКАЗАТЬ ПОДАРОК") — это нативный виджет Telegram. Он появляется
    только если боту пересылают настоящий объект unique gift (forward
    реального подарка). Из простой ссылки такую карточку сгенерировать
    нельзя. Поэтому у бота два режима:

    1) /hello ... — присылает текстовый оффер с кнопками (без картинки).
    2) Если вы (или продавец) ПЕРЕСЫЛАЕТЕ боту сообщение с настоящим
       подарком, бот подхватывает его и пересылает дальше вместе с
       вашим оффером и кнопками — тогда появится и нативная карточка.

Установка:
    pip install aiogram==3.*
    export BOT_TOKEN="токен от @BotFather"
    python offer_bot_v2.py
"""
import asyncio
import os
import re
from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME_PLACEHOLDER = "@YourOfferBot"  # замените на имя вашего бота

NFT_LINK_RE = re.compile(r"^https://t\.me/nft/[A-Za-z0-9_\-]+$")

LANGS = ["ru", "en", "zh", "es", "tr", "ar"]
LANG_FLAGS = {"ru": "🇷🇺 RU", "en": "🇬🇧 EN", "zh": "🇨🇳 ZH",
              "es": "🇪🇸 ES", "tr": "🇹🇷 TR", "ar": "🇸🇦 AR"}

CURRENCY_NAMES = {
    "ru": {"stars": "⭐ звёзд", "ton": "TON"},
    "en": {"stars": "⭐ Stars", "ton": "TON"},
    "zh": {"stars": "⭐ 星星", "ton": "TON"},
    "es": {"stars": "⭐ Estrellas", "ton": "TON"},
    "tr": {"stars": "⭐ Yıldız", "ton": "TON"},
    "ar": {"stars": "⭐ نجوم", "ton": "TON"},
}

TITLE = {
    "ru": "🎁 Новый оффер",
    "en": "🎁 New offer",
    "zh": "🎁 新报价",
    "es": "🎁 Nueva oferta",
    "tr": "🎁 Yeni teklif",
    "ar": "🎁 عرض جديد",
}

BODY = {
    "ru": "Пользователь предлагает {amount} {cur} за подарок:\n{link}",
    "en": "User offers {amount} {cur} for the gift:\n{link}",
    "zh": "用户为该礼物出价 {amount} {cur}：\n{link}",
    "es": "El usuario ofrece {amount} {cur} por el regalo:\n{link}",
    "tr": "Kullanıcı bu hediye için {amount} {cur} teklif ediyor:\n{link}",
    "ar": "يعرض المستخدم {amount} {cur} مقابل الهدية:\n{link}",
}

BTN_ACCEPT = {"ru": "✅ Принять", "en": "✅ Accept", "zh": "✅ 接受",
              "es": "✅ Aceptar", "tr": "✅ Kabul et", "ar": "✅ قبول"}
BTN_DECLINE = {"ru": "❌ Отклонить", "en": "❌ Decline", "zh": "❌ 拒绝",
               "es": "❌ Rechazar", "tr": "❌ Reddet", "ar": "❌ رفض"}
BTN_OPEN = {"ru": "🔗 Открыть подарок", "en": "🔗 Open gift", "zh": "🔗 打开礼物",
            "es": "🔗 Abrir regalo", "tr": "🔗 Hediyeyi aç", "ar": "🔗 فتح الهدية"}

RESULT_ACCEPTED = {"ru": "✅ Оффер принят.", "en": "✅ Offer accepted.",
                    "zh": "✅ 报价已接受。", "es": "✅ Oferta aceptada.",
                    "tr": "✅ Teklif kabul edildi.", "ar": "✅ تم قبول العرض."}
RESULT_DECLINED = {"ru": "❌ Оффер отклонён.", "en": "❌ Offer declined.",
                    "zh": "❌ 报价已拒绝。", "es": "❌ Oferta rechazada.",
                    "tr": "❌ Teklif reddedildi.", "ar": "❌ تم رفض العرض."}

LANG_ALIASES = {
    "ru": "ru", "рус": "ru", "русский": "ru",
    "en": "en", "eng": "en", "английский": "en",
    "zh": "zh", "cn": "zh", "китайский": "zh",
    "es": "es", "испанский": "es",
    "tr": "tr", "турецкий": "tr",
    "ar": "ar", "арабский": "ar",
}

USAGE = (
    "Формат:\n"
    "/hello <ссылка на NFT> <сумма> <stars|ton> <язык>\n\n"
    "Пример:\n"
    "/hello https://t.me/nft/ScaredCat-10668 500 ton ru\n\n"
    "Языки: ru, en, zh, es, tr, ar\n\n"
    "Чтобы в оффере появилась настоящая карточка подарка — перешлите "
    "боту сообщение с реальным NFT-подарком."
)

dp = Dispatcher()

# простое хранилище данных офферов в памяти: offer_id -> dict
OFFERS: dict[str, dict] = {}
_counter = 0


def _new_offer_id() -> str:
    global _counter
    _counter += 1
    return str(_counter)


def build_text(offer: dict, lang: str) -> str:
    cur_label = CURRENCY_NAMES[lang][offer["currency"]]
    return (
        f"{TITLE[lang]}\n\n"
        + BODY[lang].format(amount=offer["amount"], cur=cur_label, link=offer["link"])
    )


def build_keyboard(offer_id: str, lang: str) -> InlineKeyboardMarkup:
    offer = OFFERS[offer_id]
    rows = [
        [
            InlineKeyboardButton(text=BTN_DECLINE[lang], callback_data=f"off:{offer_id}:decline"),
            InlineKeyboardButton(text=BTN_ACCEPT[lang], callback_data=f"off:{offer_id}:accept"),
        ],
        [
            InlineKeyboardButton(text=BTN_OPEN[lang], url=offer["link"]),
            InlineKeyboardButton(text=LANG_FLAGS[lang], callback_data=f"off:{offer_id}:lang"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("start", "help"))
async def start(message: Message):
    await message.answer("Бот для оформления офферов на NFT-подарки.\n\n" + USAGE)


@dp.message(Command("hello"))
async def hello(message: Message, command: CommandObject):
    args = (command.args or "").split()
    if len(args) != 4:
        await message.answer("❌ Нужно 4 параметра.\n\n" + USAGE)
        return

    link, amount_raw, cur_raw, lang_raw = args

    if not NFT_LINK_RE.match(link):
        await message.answer("❌ Ссылка должна выглядеть так: https://t.me/nft/Название-123")
        return

    try:
        amount = Decimal(amount_raw.replace(",", "."))
        if amount <= 0:
            raise InvalidOperation
    except InvalidOperation:
        await message.answer("❌ Сумма должна быть положительным числом.")
        return

    cur = cur_raw.lower()
    if cur in ("star", "stars", "звезды", "звёзды", "звезда"):
        cur = "stars"
    elif cur != "ton":
        await message.answer("❌ Валюта: stars или ton.")
        return
    if cur == "stars" and amount != amount.to_integral_value():
        await message.answer("❌ Звёзды — только целое число.")
        return

    lang = LANG_ALIASES.get(lang_raw.lower())
    if not lang:
        await message.answer("❌ Неизвестный язык. Доступны: ru, en, zh, es, tr, ar.")
        return

    offer_id = _new_offer_id()
    OFFERS[offer_id] = {
        "link": link,
        "amount": f"{amount.normalize():f}",
        "currency": cur,
        "lang": lang,
        "status": "pending",
        "from_user": message.from_user.id,
        "forwarded_gift_chat_id": None,
        "forwarded_gift_message_id": None,
    }

    # Если это ответ на пересланное сообщение с реальным подарком —
    # запоминаем его, чтобы потом переслать вместе с оффером.
    if message.reply_to_message and message.reply_to_message.forward_origin:
        OFFERS[offer_id]["forwarded_gift_chat_id"] = message.reply_to_message.chat.id
        OFFERS[offer_id]["forwarded_gift_message_id"] = message.reply_to_message.message_id

    await send_offer_card(message.bot, message.chat.id, offer_id)


async def send_offer_card(bot: Bot, chat_id: int, offer_id: str):
    offer = OFFERS[offer_id]
    lang = offer["lang"]
    text = build_text(offer, lang)
    kb = build_keyboard(offer_id, lang)

    # Если есть настоящий подарок для пересылки — сначала пересылаем его
    # (появится нативная карточка Telegram), затем шлём текст оффера с кнопками.
    if offer["forwarded_gift_chat_id"]:
        await bot.forward_message(
            chat_id=chat_id,
            from_chat_id=offer["forwarded_gift_chat_id"],
            message_id=offer["forwarded_gift_message_id"],
        )

    await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=False)


@dp.message(F.forward_origin)
async def catch_forwarded_gift(message: Message):
    # Подсказка, когда пользователь пересылает подарок без команды /hello
    await message.answer(
        "Это похоже на пересланный подарок. Теперь отправьте команду\n"
        "/hello <ссылка> <сумма> <stars|ton> <язык>\n"
        "ответом на это сообщение — и карточка подарка попадёт в оффер."
    )


@dp.callback_query(F.data.startswith("off:"))
async def on_callback(call: CallbackQuery):
    _, offer_id, action = call.data.split(":")
    offer = OFFERS.get(offer_id)
    if not offer:
        await call.answer("Оффер не найден.", show_alert=True)
        return

    lang = offer["lang"]

    if action == "lang":
        idx = LANGS.index(lang)
        new_lang = LANGS[(idx + 1) % len(LANGS)]
        offer["lang"] = new_lang
        await call.message.edit_text(
            build_text(offer, new_lang),
            reply_markup=build_keyboard(offer_id, new_lang),
        )
        await call.answer()
        return

    if offer["status"] != "pending":
        await call.answer("Оффер уже обработан.", show_alert=True)
        return

    if action == "accept":
        offer["status"] = "accepted"
        await call.message.edit_text(
            build_text(offer, lang) + "\n\n" + RESULT_ACCEPTED[lang],
        )
    elif action == "decline":
        offer["status"] = "declined"
        await call.message.edit_text(
            build_text(offer, lang) + "\n\n" + RESULT_DECLINED[lang],
        )
    await call.answer()


async def main():
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

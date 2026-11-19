import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from db import ASSETS, ensure_user, get_balances, exchange, get_history, init_db

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_FILE = STATIC_DIR / "index.html"

if not INDEX_FILE.is_file():
    raise RuntimeError("Не найден файл static/index.html")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="WALLET Mini App", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Фиксированные демонстрационные курсы. Это не рыночные котировки.
DEMO_RATES = {
    "TON": {"USDT": 5.0, "BTC": 0.00005, "ETH": 0.0015, "BNB": 0.007, "SOL": 0.04},
    "USDT": {"TON": 0.2, "BTC": 0.00001, "ETH": 0.0003, "BNB": 0.0014, "SOL": 0.008},
    "BTC": {"TON": 20000.0, "USDT": 100000.0, "ETH": 30.0, "BNB": 140.0, "SOL": 800.0},
    "ETH": {"TON": 666.6667, "USDT": 3333.3333, "BTC": 0.0333, "BNB": 4.6667, "SOL": 26.6667},
    "BNB": {"TON": 142.8571, "USDT": 714.2857, "BTC": 0.00714, "ETH": 0.2143, "SOL": 5.7143},
    "SOL": {"TON": 25.0, "USDT": 125.0, "BTC": 0.00125, "ETH": 0.0375, "BNB": 0.175},
}


def verify_init_data(init_data: str):
    if not BOT_TOKEN or not init_data:
        raise HTTPException(status_code=401, detail="Нет Telegram WebApp авторизации.")

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)

    if not received_hash:
        raise HTTPException(status_code=401, detail="Некорректные данные Telegram.")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(parsed.items())
    )

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256,
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Не прошла проверка подписи Telegram.")

    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError:
        raise HTTPException(status_code=401, detail="Некорректная дата авторизации.")

    if abs(time.time() - auth_date) > 86400:
        raise HTTPException(status_code=401, detail="Сессия устарела. Откройте Mini App заново.")

    try:
        return json.loads(parsed["user"])
    except (KeyError, json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=401, detail="Не удалось прочитать пользователя Telegram.")


def telegram_user(authorization: str | None):
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(status_code=401, detail="Откройте приложение через Telegram.")
    return verify_init_data(authorization[4:])


class ExchangeRequest(BaseModel):
    from_asset: str
    to_asset: str
    amount: float = Field(gt=0, le=1_000_000)


@app.get("/")
def index():
    return FileResponse(str(INDEX_FILE))


@app.get("/api/me")
def me(authorization: str | None = Header(default=None)):
    user = telegram_user(authorization)
    user_id = int(user["id"])
    ensure_user(user_id, user.get("username"))

    return {
        "user": {
            "id": user_id,
            "first_name": user.get("first_name", "Пользователь"),
        },
        "balances": get_balances(user_id),
        "assets": list(ASSETS),
        "notice": "ДЕМО-БАЛАНСЫ: это внутренние тестовые единицы, не реальные криптоактивы.",
    }


@app.get("/api/history")
def history(authorization: str | None = Header(default=None)):
    user = telegram_user(authorization)
    return {"items": get_history(int(user["id"]))}


@app.get("/api/rates")
def rates():
    return {"rates": DEMO_RATES, "mode": "DEMO_FIXED"}


@app.post("/api/exchange")
def do_exchange(
    body: ExchangeRequest,
    authorization: str | None = Header(default=None),
):
    user = telegram_user(authorization)
    from_asset = body.from_asset.upper()
    to_asset = body.to_asset.upper()

    if from_asset not in ASSETS or to_asset not in ASSETS or from_asset == to_asset:
        raise HTTPException(status_code=400, detail="Выберите разные поддерживаемые валюты.")

    rate = DEMO_RATES.get(from_asset, {}).get(to_asset)
    if rate is None:
        raise HTTPException(status_code=400, detail="Для этой пары нет демонстрационного курса.")

    try:
        received = exchange(int(user["id"]), from_asset, to_asset, body.amount, rate)
        return {"ok": True, "received": received, "rate": rate}
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

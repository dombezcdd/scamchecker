import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from db import ASSETS, ensure_user, get_balances, exchange, get_history, init_db

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
app = FastAPI(title="WALLET BOT Demo API")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Учебные фиксированные коэффициенты, НЕ рыночные котировки.
DEMO_RATES = {
    "TON": {"USDT": 5.0, "BTC": 0.00005, "ETH": 0.0015, "BNB": 0.007, "SOL": 0.04},
    "USDT": {"TON": 0.2, "BTC": 0.00001, "ETH": 0.0003, "BNB": 0.0014, "SOL": 0.008},
    "BTC": {"TON": 20000.0, "USDT": 100000.0, "ETH": 30.0, "BNB": 140.0, "SOL": 800.0},
    "ETH": {"TON": 666.6667, "USDT": 3333.3333, "BTC": 0.0333, "BNB": 4.6667, "SOL": 26.6667},
    "BNB": {"TON": 142.8571, "USDT": 714.2857, "BTC": 0.00714, "ETH": 0.2143, "SOL": 5.7143},
    "SOL": {"TON": 25.0, "USDT": 125.0, "BTC": 0.00125, "ETH": 0.0375, "BNB": 0.175},
}

def verify_init_data(init_data: str) -> dict:
    if not BOT_TOKEN or not init_data:
        raise HTTPException(401, "Нет Telegram WebApp авторизации.")
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Некорректные данные Telegram.")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, "Не прошла проверка подписи Telegram.")
    auth_date = int(parsed.get("auth_date", "0"))
    if abs(time.time() - auth_date) > 86400:
        raise HTTPException(401, "Сессия устарела. Откройте Mini App заново.")
    try:
        user = json.loads(parsed["user"])
        return user
    except Exception:
        raise HTTPException(401, "Не удалось прочитать пользователя Telegram.")

def telegram_user(authorization: str | None):
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Откройте приложение через Telegram.")
    return verify_init_data(authorization[4:])

class ExchangeRequest(BaseModel):
    from_asset: str
    to_asset: str
    amount: float = Field(gt=0, le=1_000_000)

@app.on_event("startup")
def startup():
    init_db()

@app.get("/")
def index():
    return FileResponse("static/index.html")

@app.get("/api/me")
def me(authorization: str | None = Header(default=None)):
    user = telegram_user(authorization)
    uid = int(user["id"])
    ensure_user(uid, user.get("username"))
    return {
        "user": {"id": uid, "first_name": user.get("first_name", "Пользователь")},
        "balances": get_balances(uid),
        "assets": list(ASSETS),
        "notice": "ДЕМО-БАЛАНСЫ: это внутренние тестовые единицы, не реальные криптоактивы."
    }

@app.get("/api/history")
def history(authorization: str | None = Header(default=None)):
    user = telegram_user(authorization)
    return {"items": get_history(int(user["id"]))}

@app.get("/api/rates")
def rates():
    return {"rates": DEMO_RATES, "mode": "DEMO_FIXED"}

@app.post("/api/exchange")
def do_exchange(body: ExchangeRequest, authorization: str | None = Header(default=None)):
    user = telegram_user(authorization)
    a, b = body.from_asset.upper(), body.to_asset.upper()
    if a not in ASSETS or b not in ASSETS or a == b:
        raise HTTPException(400, "Выберите разные поддерживаемые валюты.")
    rate = DEMO_RATES.get(a, {}).get(b)
    if not rate:
        raise HTTPException(400, "Для этой пары нет демонстрационного курса.")
    try:
        received = exchange(int(user["id"]), a, b, body.amount, rate)
        return {"ok": True, "received": received, "rate": rate}
    except ValueError as e:
        raise HTTPException(400, str(e))

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.getenv("DATABASE_PATH", "wallet.db")
ASSETS = ("TON", "USDT", "BTC", "ETH", "BNB", "SOL")

@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True) if os.path.dirname(DB_PATH) else None
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS balances (
            user_id INTEGER NOT NULL,
            asset TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, asset)
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            asset TEXT NOT NULL,
            amount REAL NOT NULL,
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS checks (
            id TEXT PRIMARY KEY,
            creator_id INTEGER NOT NULL,
            asset TEXT NOT NULL,
            amount REAL NOT NULL,
            claimed_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

def ensure_user(user_id: int, username: str | None = None):
    with connect() as c:
        c.execute(
            "INSERT INTO users(user_id, username) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (user_id, username),
        )
        for asset in ASSETS:
            c.execute(
                "INSERT OR IGNORE INTO balances(user_id, asset, amount) VALUES(?, ?, 0)",
                (user_id, asset),
            )

def get_balances(user_id: int):
    ensure_user(user_id)
    with connect() as c:
        rows = c.execute(
            "SELECT asset, amount FROM balances WHERE user_id=? ORDER BY asset",
            (user_id,),
        ).fetchall()
        return {r["asset"]: float(r["amount"]) for r in rows}

def credit(user_id: int, asset: str, amount: float, note: str):
    ensure_user(user_id)
    with connect() as c:
        c.execute(
            "UPDATE balances SET amount=amount+? WHERE user_id=? AND asset=?",
            (amount, user_id, asset),
        )
        c.execute(
            "INSERT INTO transactions(user_id, kind, asset, amount, note) VALUES(?,?,?,?,?)",
            (user_id, "CREDIT", asset, amount, note),
        )

def exchange(user_id: int, from_asset: str, to_asset: str, amount: float, rate: float):
    ensure_user(user_id)
    with connect() as c:
        row = c.execute(
            "SELECT amount FROM balances WHERE user_id=? AND asset=?",
            (user_id, from_asset),
        ).fetchone()
        if not row or float(row["amount"]) + 1e-12 < amount:
            raise ValueError("Недостаточно DEMO-кредитов.")
        received = amount * rate
        c.execute(
            "UPDATE balances SET amount=amount-? WHERE user_id=? AND asset=?",
            (amount, user_id, from_asset),
        )
        c.execute(
            "UPDATE balances SET amount=amount+? WHERE user_id=? AND asset=?",
            (received, user_id, to_asset),
        )
        c.execute(
            "INSERT INTO transactions(user_id, kind, asset, amount, note) VALUES(?,?,?,?,?)",
            (user_id, "EXCHANGE_OUT", from_asset, -amount, f"В {to_asset}; курс {rate}"),
        )
        c.execute(
            "INSERT INTO transactions(user_id, kind, asset, amount, note) VALUES(?,?,?,?,?)",
            (user_id, "EXCHANGE_IN", to_asset, received, f"Из {from_asset}; курс {rate}"),
        )
        return received

def create_check(check_id: str, creator_id: int, asset: str, amount: float):
    with connect() as c:
        c.execute(
            "INSERT INTO checks(id, creator_id, asset, amount) VALUES(?,?,?,?)",
            (check_id, creator_id, asset, amount),
        )

def claim_check(check_id: str, user_id: int):
    with connect() as c:
        row = c.execute("SELECT * FROM checks WHERE id=?", (check_id,)).fetchone()
        if not row:
            raise ValueError("Чек не найден.")
        if row["claimed_by"] is not None:
            raise ValueError("Этот чек уже активирован.")
        if int(row["creator_id"]) == user_id:
            raise ValueError("Нельзя активировать собственный чек.")
        c.execute("UPDATE checks SET claimed_by=? WHERE id=? AND claimed_by IS NULL", (user_id, check_id))
        if c.total_changes == 0:
            raise ValueError("Чек уже активирован.")
        c.execute(
            "INSERT INTO balances(user_id, asset, amount) VALUES(?,?,?) "
            "ON CONFLICT(user_id, asset) DO UPDATE SET amount=amount+excluded.amount",
            (user_id, row["asset"], row["amount"]),
        )
        c.execute(
            "INSERT INTO transactions(user_id, kind, asset, amount, note) VALUES(?,?,?,?,?)",
            (user_id, "CHECK_CLAIM", row["asset"], row["amount"], f"Чек {check_id}"),
        )
        return row["asset"], float(row["amount"])

def get_history(user_id: int, limit: int = 12):
    with connect() as c:
        rows = c.execute(
            "SELECT kind, asset, amount, note, created_at FROM transactions "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

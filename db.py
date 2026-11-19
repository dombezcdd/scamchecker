import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = os.getenv("DATABASE_PATH", "wallet.db")
ASSETS = ("TON", "USDT", "BTC", "ETH", "BNB", "SOL")


@contextmanager
def connect():
    db_file = Path(DB_PATH)
    if db_file.parent != Path("."):
        db_file.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_file), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with connect() as conn:
        conn.executescript("""
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
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET username = excluded.username
            """,
            (user_id, username),
        )

        for asset in ASSETS:
            conn.execute(
                """
                INSERT OR IGNORE INTO balances (user_id, asset, amount)
                VALUES (?, ?, 0)
                """,
                (user_id, asset),
            )


def get_balances(user_id: int):
    ensure_user(user_id)
    with connect() as conn:
        rows = conn.execute(
            "SELECT asset, amount FROM balances WHERE user_id = ? ORDER BY asset",
            (user_id,),
        ).fetchall()
        return {row["asset"]: float(row["amount"]) for row in rows}


def credit(user_id: int, asset: str, amount: float, note: str):
    asset = asset.upper()
    if asset not in ASSETS:
        raise ValueError("Неизвестная валюта.")
    if amount <= 0:
        raise ValueError("Сумма должна быть больше нуля.")

    ensure_user(user_id)
    with connect() as conn:
        conn.execute(
            "UPDATE balances SET amount = amount + ? WHERE user_id = ? AND asset = ?",
            (amount, user_id, asset),
        )
        conn.execute(
            """
            INSERT INTO transactions (user_id, kind, asset, amount, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, "CREDIT", asset, amount, note),
        )


def exchange(user_id: int, from_asset: str, to_asset: str, amount: float, rate: float):
    from_asset = from_asset.upper()
    to_asset = to_asset.upper()

    if from_asset not in ASSETS or to_asset not in ASSETS:
        raise ValueError("Неизвестная валюта.")
    if from_asset == to_asset:
        raise ValueError("Выбери разные валюты.")
    if amount <= 0 or rate <= 0:
        raise ValueError("Сумма и курс должны быть больше нуля.")

    ensure_user(user_id)
    received = amount * rate

    with connect() as conn:
        row = conn.execute(
            "SELECT amount FROM balances WHERE user_id = ? AND asset = ?",
            (user_id, from_asset),
        ).fetchone()

        if row is None or float(row["amount"]) + 1e-12 < amount:
            raise ValueError("Недостаточно DEMO-кредитов.")

        conn.execute(
            "UPDATE balances SET amount = amount - ? WHERE user_id = ? AND asset = ?",
            (amount, user_id, from_asset),
        )
        conn.execute(
            "UPDATE balances SET amount = amount + ? WHERE user_id = ? AND asset = ?",
            (received, user_id, to_asset),
        )
        conn.execute(
            """
            INSERT INTO transactions (user_id, kind, asset, amount, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, "EXCHANGE_OUT", from_asset, -amount, f"В {to_asset}; курс {rate}"),
        )
        conn.execute(
            """
            INSERT INTO transactions (user_id, kind, asset, amount, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, "EXCHANGE_IN", to_asset, received, f"Из {from_asset}; курс {rate}"),
        )

    return received


def create_check(check_id: str, creator_id: int, asset: str, amount: float):
    asset = asset.upper()
    if asset not in ASSETS:
        raise ValueError("Неизвестная валюта.")
    if amount <= 0:
        raise ValueError("Сумма должна быть больше нуля.")

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO checks (id, creator_id, asset, amount)
            VALUES (?, ?, ?, ?)
            """,
            (check_id, creator_id, asset, amount),
        )


def claim_check(check_id: str, user_id: int):
    with connect() as conn:
        # BEGIN IMMEDIATE prevents two users claiming the same check simultaneously.
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM checks WHERE id = ?",
            (check_id,),
        ).fetchone()

        if row is None:
            raise ValueError("Чек не найден.")
        if row["claimed_by"] is not None:
            raise ValueError("Этот чек уже активирован.")
        if int(row["creator_id"]) == user_id:
            raise ValueError("Нельзя активировать собственный чек.")

        updated = conn.execute(
            "UPDATE checks SET claimed_by = ? WHERE id = ? AND claimed_by IS NULL",
            (user_id, check_id),
        )
        if updated.rowcount != 1:
            raise ValueError("Чек уже активирован.")

        conn.execute(
            """
            INSERT INTO balances (user_id, asset, amount)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, asset)
            DO UPDATE SET amount = amount + excluded.amount
            """,
            (user_id, row["asset"], row["amount"]),
        )
        conn.execute(
            """
            INSERT INTO transactions (user_id, kind, asset, amount, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, "CHECK_CLAIM", row["asset"], row["amount"], f"Чек {check_id}"),
        )

        return row["asset"], float(row["amount"])


def get_history(user_id: int, limit: int = 12):
    limit = max(1, min(int(limit), 100))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT kind, asset, amount, note, created_at
            FROM transactions
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

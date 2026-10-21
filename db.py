"""SQLite storage with idempotent upserts."""
import json
import re
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "expenses.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    site TEXT NOT NULL,
    order_id TEXT NOT NULL,
    sub_platform TEXT,
    placed_at TEXT,
    total_amount REAL,
    currency TEXT,
    status TEXT,
    merchant TEXT,
    raw_json TEXT,
    PRIMARY KEY (site, order_id)
);
CREATE TABLE IF NOT EXISTS order_items (
    site TEXT NOT NULL,
    order_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    name TEXT NOT NULL,
    qty REAL,
    price REAL,
    PRIMARY KEY (site, order_id, idx),
    FOREIGN KEY (site, order_id) REFERENCES orders(site, order_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS item_categories (
    name_norm TEXT PRIMARY KEY,
    category TEXT NOT NULL
);
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)  # parallel sync workers write to the same file
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", name.lower())).strip()


def upsert_orders(con: sqlite3.Connection, site: str, orders: list[dict],
                  sub_platform: str | None = None) -> int:
    """Insert or update orders and replace their items. Safe to call repeatedly."""
    with con:
        for o in orders:
            con.execute(
                """INSERT INTO orders (site, order_id, sub_platform, placed_at, total_amount,
                                       currency, status, merchant, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(site, order_id) DO UPDATE SET
                     sub_platform=excluded.sub_platform, placed_at=excluded.placed_at,
                     total_amount=excluded.total_amount, currency=excluded.currency,
                     status=excluded.status, merchant=excluded.merchant, raw_json=excluded.raw_json""",
                (site, o["order_id"], o.get("sub_platform") or sub_platform, o["placed_at"],
                 o["total_amount"], o.get("currency") or "INR", o.get("status"),
                 o.get("merchant"), json.dumps(o.get("raw"), ensure_ascii=False)),
            )
            con.execute("DELETE FROM order_items WHERE site=? AND order_id=?", (site, o["order_id"]))
            con.executemany(
                "INSERT INTO order_items (site, order_id, idx, name, qty, price) VALUES (?,?,?,?,?,?)",
                [(site, o["order_id"], i, it["name"], it.get("qty"), it.get("price"))
                 for i, it in enumerate(o.get("items") or [])],
            )
    return len(orders)

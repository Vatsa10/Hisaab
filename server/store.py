"""Per-user Postgres storage (mirrors db.py, scoped by user_id)."""
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

CONNECTION_FIELDS = ("status", "last_synced_at", "orders_page_url", "proxy_session_id", "profile_blob_key")


def upsert_orders(conn, user_id: str, site: str, orders: list[dict],
                  sub_platform: str | None = None) -> int:
    """Insert or update a user's orders and replace their items, in one transaction."""
    with conn.transaction():
        for o in orders:
            conn.execute(
                """INSERT INTO orders (user_id, site, order_id, sub_platform, placed_at, total_amount,
                                       currency, status, merchant, raw_json)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (user_id, site, order_id) DO UPDATE SET
                     sub_platform=excluded.sub_platform, placed_at=excluded.placed_at,
                     total_amount=excluded.total_amount, currency=excluded.currency,
                     status=excluded.status, merchant=excluded.merchant, raw_json=excluded.raw_json""",
                (user_id, site, o["order_id"], o.get("sub_platform") or sub_platform, o["placed_at"],
                 o["total_amount"], o.get("currency") or "INR", o.get("status"),
                 o.get("merchant"), Jsonb(o.get("raw"))),
            )
            conn.execute("DELETE FROM order_items WHERE user_id=%s AND site=%s AND order_id=%s",
                         (user_id, site, o["order_id"]))
            items = o.get("items") or []
            if items:
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO order_items (user_id, site, order_id, idx, name, qty, price) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        [(user_id, site, o["order_id"], i, it["name"], it.get("qty"), it.get("price"))
                         for i, it in enumerate(items)],
                    )
    return len(orders)


def set_connection(conn, user_id: str, site: str, **fields) -> None:
    """Insert or update a user's connection row; only whitelisted columns are accepted."""
    bad = set(fields) - set(CONNECTION_FIELDS)
    if bad:
        raise ValueError(f"unknown connection field(s): {sorted(bad)}")
    cols = list(fields)
    insert_cols = ", ".join(["user_id", "site", *cols])
    placeholders = ", ".join(["%s"] * (2 + len(cols)))
    if cols:
        conflict = "DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in cols)
    else:
        conflict = "DO NOTHING"
    conn.execute(
        f"INSERT INTO connections ({insert_cols}) VALUES ({placeholders}) "
        f"ON CONFLICT (user_id, site) {conflict}",
        (user_id, site, *fields.values()),
    )


def get_connection(conn, user_id: str, site: str) -> dict | None:
    """A user's connection row for a site (status, orders_page_url, ...), or None."""
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute("SELECT * FROM connections WHERE user_id=%s AND site=%s", (user_id, site)).fetchone()
    if row is not None:
        row["user_id"] = str(row["user_id"])
    return row


def get_profile(conn, user_id: str) -> dict | None:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute("SELECT * FROM profiles WHERE user_id=%s", (user_id,)).fetchone()
    if row is not None:
        row["user_id"] = str(row["user_id"])
    return row


def get_recipe(conn, site: str, kind: str = "orders") -> dict | None:
    """The learned recipe for a site, or None. Recipes are shared by all users."""
    row = conn.execute("SELECT recipe FROM recipes WHERE site=%s AND kind=%s", (site, kind)).fetchone()
    return row[0] if row else None


def put_recipe(conn, site: str, recipe: dict, kind: str = "orders") -> int:
    """Insert or replace a site's recipe; the version increments on every put, and the site is healthy and
    unpaused again. Returns the new version."""
    return conn.execute(
        "INSERT INTO recipes (site, kind, recipe) VALUES (%s,%s,%s) "
        "ON CONFLICT (site, kind) DO UPDATE SET recipe=excluded.recipe, version=recipes.version+1, "
        "learned_at=now(), healthy=true, paused=false, health_note=null RETURNING version",
        (site, kind, Jsonb(recipe)),
    ).fetchone()[0]

"""Account deletion and app disconnect that leave nothing behind (Plan 7 R1, R2, R3).

delete_account removes, in one transaction, every row of the user in every `public` table with a `user_id` column
(discovered from the catalog, so tables added by later plans are covered), the rows keyed by their email address,
their name in `*_by` actor columns and the `auth.users` row, after folding their usage anonymously into
`usage_erased`. The vault blobs go after the commit, with retries, and a failure there is retryable: a second call
finds no rows and only deletes blobs. remaining() is the independent "nothing remains" scan.
"""
import logging
import time

from psycopg import sql

from server import sites, vault

log = logging.getLogger("server.erase")

BLOB_TRIES = 3
_sleep = time.sleep


class BlobDeleteFailed(Exception):
    """The database side is done, but the stored sessions could not all be removed yet. Safe to call again."""


def _blob_retry(fn) -> int:
    """Run a blob deletion up to BLOB_TRIES times. Only the exception's class is logged (it may name a key)."""
    for attempt in range(BLOB_TRIES):
        try:
            return fn()
        except Exception as e:
            log.warning("stored session deletion failed (try %d of %d): %s", attempt + 1, BLOB_TRIES,
                        type(e).__name__)
            if attempt + 1 < BLOB_TRIES:
                _sleep(0.2 * (attempt + 1))
    raise BlobDeleteFailed("stored sessions could not be removed")


def user_tables(conn) -> list[str]:
    """Base tables in `public` with a user_id column, children before parents (foreign keys among them)."""
    names = [r[0] for r in conn.execute(
        "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "join pg_attribute a on a.attrelid = c.oid and a.attname = 'user_id' and not a.attisdropped "
        "where n.nspname = 'public' and c.relkind in ('r', 'p') order by c.relname").fetchall()]
    members = set(names)
    children_of: dict[str, set[str]] = {n: set() for n in names}   # parent -> tables referencing it
    for child, parent in conn.execute(
            "select cl.relname, pl.relname from pg_constraint k "
            "join pg_class cl on cl.oid = k.conrelid join pg_class pl on pl.oid = k.confrelid "
            "join pg_namespace cn on cn.oid = cl.relnamespace join pg_namespace pn on pn.oid = pl.relnamespace "
            "where k.contype = 'f' and cn.nspname = 'public' and pn.nspname = 'public'").fetchall():
        if child in members and parent in members and child != parent:
            children_of[parent].add(child)
    # A table can be deleted from once every table that references it is gone.
    order: list[str] = []
    left = set(names)
    while left:
        ready = sorted(n for n in left if not (children_of[n] & left))
        if not ready:                      # a foreign-key cycle: fall back to name order for what is left
            ready = sorted(left)
        order.extend(ready)
        left -= set(ready)
    return order


def _actor_columns(conn) -> list[tuple[str, str]]:
    return conn.execute(
        "select table_name, column_name from information_schema.columns where table_schema = 'public' "
        "and data_type = 'uuid' and column_name like '%%\\_by' order by 1, 2").fetchall()


def delete_account(conn, store, user_id: str) -> dict:
    """Delete everything of this user. Idempotent. Returns {"rows": int, "blobs": int}."""
    user_id = str(user_id)
    rows = 0
    with conn.transaction():
        # First, the user row: jobs.enqueue takes it FOR KEY SHARE, so no job can be enqueued once this is held.
        row = conn.execute("select lower(email) from auth.users where id = %s::uuid for update",
                           (user_id,)).fetchone()
        email = row[0] if row and row[0] else None
        # Wait for any in-flight worker write (it holds its job row): what it wrote is then deleted below, and a
        # write that starts later finds no job row and is dropped.
        conn.execute("select id from jobs where user_id = %s::uuid for update", (user_id,)).fetchall()
        conn.execute(
            "insert into usage_erased (day, proxy_mb, ai_tokens, ai_input_tokens, ai_output_tokens, worker_seconds, syncs, cart_checks, cart_fills) "
            "select day, sum(proxy_mb), sum(ai_tokens), sum(ai_input_tokens), sum(ai_output_tokens), sum(worker_seconds), sum(syncs), sum(cart_checks), "
            "sum(cart_fills) "
            "from usage where user_id = %s::uuid group by day "
            "on conflict (day) do update set proxy_mb = usage_erased.proxy_mb + excluded.proxy_mb, "
            "ai_tokens = usage_erased.ai_tokens + excluded.ai_tokens, "
            "ai_input_tokens = usage_erased.ai_input_tokens + excluded.ai_input_tokens, "
            "ai_output_tokens = usage_erased.ai_output_tokens + excluded.ai_output_tokens, "
            "worker_seconds = usage_erased.worker_seconds + excluded.worker_seconds, "
            "syncs = usage_erased.syncs + excluded.syncs, "
            "cart_checks = usage_erased.cart_checks + excluded.cart_checks, "
            "cart_fills = usage_erased.cart_fills + excluded.cart_fills", (user_id,))
        for table in user_tables(conn):
            rows += conn.execute(sql.SQL("delete from public.{} where user_id = %s::uuid").format(
                sql.Identifier(table)), (user_id,)).rowcount
        if email:
            for table in ("admins", "invites", "waitlist", "notifications"):
                rows += conn.execute(sql.SQL("delete from public.{} where lower(email) = %s").format(
                    sql.Identifier(table)), (email,)).rowcount
        for table, column in _actor_columns(conn):
            conn.execute(sql.SQL("update public.{t} set {c} = null where {c} = %s::uuid").format(
                t=sql.Identifier(table), c=sql.Identifier(column)), (user_id,))
        if conn.execute("select to_regclass('auth.audit_log_entries')").fetchone()[0]:
            conn.execute("delete from auth.audit_log_entries where position(%s in payload::text) > 0 "
                         "or (%s <> '' and position(%s in lower(payload::text)) > 0)",
                         (user_id, email or "", email or ""))
        rows += conn.execute("delete from auth.users where id = %s::uuid", (user_id,)).rowcount
    blobs = _blob_retry(lambda: vault.delete_user_profiles(store, user_id))
    return {"rows": rows, "blobs": blobs}


def disconnect_site(conn, store, user_id: str, site: str) -> dict:
    """Remove one app's data for the user: the whole Chrome-profile group (swiggy covers instamart). Cart results,
    usage and everything else stay. Idempotent. Returns {"orders_deleted", "blobs"}."""
    user_id = str(user_id)
    group = list(sites.same_profile(site))
    stored = sorted({sites.db_site(s) for s in group})
    consent_sites = sorted(set(group) | set(stored))
    with conn.transaction():
        # Jobs first: an in-flight write holds its job row, so it finishes before we delete and later ones find none.
        conn.execute("select id from jobs where user_id = %s::uuid and site = any(%s) for update",
                     (user_id, group)).fetchall()
        conn.execute("delete from jobs where user_id = %s::uuid and site = any(%s)", (user_id, group))
        conn.execute("delete from order_items where user_id = %s::uuid and site = any(%s)", (user_id, stored))
        orders = conn.execute("delete from orders where user_id = %s::uuid and site = any(%s)",
                              (user_id, stored)).rowcount
        conn.execute("delete from connections where user_id = %s::uuid and site = any(%s)", (user_id, stored))
        conn.execute("delete from consents where user_id = %s::uuid and site = any(%s)", (user_id, consent_sites))
    blobs = _blob_retry(lambda: vault.delete_profile(store, user_id, sites.profile_for(site)))
    return {"orders_deleted": orders, "blobs": blobs}


def remaining(conn, store, user_id: str, email: str | None) -> list[str]:
    """Every `schema.table` in public and auth with a row whose text contains the id or the email, plus
    `blob:<key>` for each stored key under `<user_id>/`. Empty means nothing remains."""
    user_id = str(user_id).lower()
    needles = [user_id] + ([email.lower()] if email else [])
    found = []
    tables = conn.execute(
        "select table_schema, table_name from information_schema.tables "
        "where table_schema in ('public', 'auth') and table_type = 'BASE TABLE' order by 1, 2").fetchall()
    cond = " or ".join("position(%s in lower(x::text)) > 0" for _ in needles)
    for schema, table in tables:
        q = sql.SQL("select exists (select 1 from {}.{} x where " + cond + ")").format(
            sql.Identifier(schema), sql.Identifier(table))
        if conn.execute(q, needles).fetchone()[0]:
            found.append(f"{schema}.{table}")
    found += [f"blob:{k}" for k in store.list_prefix(f"{user_id}/")]
    return found

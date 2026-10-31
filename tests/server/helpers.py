"""Shared constants for server tests."""

A = "00000000-0000-0000-0000-00000000000a"
B = "00000000-0000-0000-0000-00000000000b"
# 32+ bytes: PyJWT warns (InsecureKeyLengthWarning) on shorter HS256 keys.
TEST_JWT_SECRET = "test-secret-of-at-least-32-bytes!!"


def order(oid, total=100.0, status="delivered", items=None):
    return {"order_id": oid, "placed_at": "2025-01-05T10:00:00", "total_amount": total, "currency": "INR",
            "status": status, "merchant": "Store", "items": items or [], "raw": {"id": oid}}


def add_user(conn, user_id, email):
    """Insert a row into the auth.users stand-in (Supabase's auth table) with this email."""
    conn.execute("insert into auth.users(id, email) values (%s, %s) "
                 "on conflict (id) do update set email = excluded.email", (str(user_id), email))


def settings_for_tests(**_):
    """Settings for worker tests; the autouse vault_env fixture points the vault at a fresh temp dir."""
    import os

    from server import vault
    from server.config import DEV_PROFILE_KEY, Settings
    return Settings(database_url=os.environ["TEST_DATABASE_URL"], jwt_secret=TEST_JWT_SECRET,
                    jwks_url="", otp_timeout_s=5,
                    profile_store_url=os.environ.get("PROFILE_STORE_URL", "file://vault_local"),
                    profile_master_key=vault.master_key_from_env(DEV_PROFILE_KEY))


def make_token(sub, secret=TEST_JWT_SECRET, aud="authenticated", exp_in=3600):
    import time

    import jwt
    return jwt.encode({"sub": sub, "aud": aud, "exp": int(time.time()) + exp_in}, secret, algorithm="HS256")


def auth_header(sub):
    return {"Authorization": f"Bearer {make_token(sub)}"}


def fake_login_env(monkeypatch, run, settings_location=None):
    """Patch only the browser edges of sync.login (open_site, navigation, snapshot) and LoginAgent.run,
    so the real sync.login runs. Returns a dict recording what the browser layer was asked to do."""
    import contextlib
    from types import SimpleNamespace

    import browser
    import sync

    seen = {"goto": [], "open_site": [], "agents": []}
    page = SimpleNamespace(url="https://www.zepto.com/", main_frame=None)
    capture = SimpleNamespace(items=[], clear=lambda: None)
    guard = SimpleNamespace(login_mode=True)

    @contextlib.contextmanager
    def fake_open(site, **k):
        seen["open_site"].append(k)
        yield None, page, guard, capture

    def fake_run(self):
        seen["agents"].append(self)
        return run(self)

    monkeypatch.setattr(sync, "open_site", fake_open)
    monkeypatch.setattr(sync.browser, "goto", lambda p, url, *a, **k: seen["goto"].append(url))
    monkeypatch.setattr(sync, "goto", lambda p, url: seen["goto"].append(url))
    monkeypatch.setattr(sync.recipes, "load", lambda site: {"orders_page_url": "https://www.zepto.com/orders"})
    monkeypatch.setattr(sync.agent, "snapshot", lambda p: {"elements": []})
    monkeypatch.setattr(sync.agent, "settle", lambda *a, **k: None)
    monkeypatch.setattr(sync.agent.LoginAgent, "run", fake_run)
    monkeypatch.setattr(browser, "load_settings",
                        lambda: {"location": settings_location} if settings_location else {})
    return seen


def destructive_db_allowed(url: str, env) -> bool:
    """The server tests drop and recreate the public and auth schemas. Only allow that on a local database
    (localhost / 127.0.0.1 / ::1 / a local socket) unless ALLOW_DESTRUCTIVE_TEST_DB=1 is set."""
    from server.local_db import is_local_db
    return env.get("ALLOW_DESTRUCTIVE_TEST_DB") == "1" or is_local_db(url, env)


def seed_everything(conn, user_id, email, store=None) -> None:
    """One row of this user in every user-keyed table (plus the email-keyed and *_by rows, an auth user and a vault
    blob), so a deletion test can prove nothing is left. Tables from branches not merged yet are seeded when they
    exist (app_addresses)."""
    import os
    import uuid

    from psycopg.types.json import Jsonb

    from server import vault
    u, em = str(user_id), email.lower()
    add_user(conn, u, em)
    conn.execute("insert into profiles(user_id, display_name, address) values (%s, 'Seed', 'Alkapuri, Vadodara')", (u,))
    for site in ("swiggy", "zomato"):
        conn.execute("insert into connections(user_id, site, status) values (%s, %s, 'connected')", (u, site))
        conn.execute("insert into orders(user_id, site, order_id, total_amount) values (%s, %s, 'o1', 10)", (u, site))
        conn.execute("insert into order_items(user_id, site, order_id, idx, name) values (%s, %s, 'o1', 0, 'x')",
                     (u, site))
        conn.execute("insert into consents(user_id, site, text_version) values (%s, %s, 'v1')", (u, site))
        conn.execute("insert into jobs(user_id, type, site, profile, status) values (%s, 'sync', %s, %s, 'done')",
                     (u, site, site))
    conn.execute("insert into usage(user_id, day, proxy_mb, ai_tokens, worker_seconds, syncs, cart_checks, cart_fills) "
                 "values (%s, current_date, 5, 100, 30, 1, 1, 1)", (u,))
    conn.execute("insert into push_subscriptions(endpoint, user_id, p256dh, auth) values (%s, %s, 'k', 'a')",
                 (f"https://push.example/{u}", u))
    conn.execute("insert into notifications(user_id, kind, title, body, dedupe_key) "
                 "values (%s, 'sync_done', 't', 'b', %s)", (u, f"seed:{u}"))
    conn.execute("insert into notifications(email, kind, title, body, dedupe_key) "
                 "values (%s, 'admin', 't', 'b', %s)", (em, f"seed-mail:{u}"))
    rid = conn.execute("insert into cart_requests(user_id, items, sites, area, lat, lon) "
                       "values (%s, %s, %s, 'Alkapuri', 22.3, 73.2) returning id",
                       (u, Jsonb([{"name": "milk"}]), ["zepto"])).fetchone()[0]
    conn.execute("insert into cart_results(request_id, user_id, site, status) values (%s, %s, 'zepto', 'ok')", (rid, u))
    conn.execute("insert into cart_fills(user_id, request_id, site, plan, priced_location) "
                 "values (%s, %s, 'zepto', '[]', '{}')", (u, rid))
    conn.execute("insert into restaurant_cache(user_id, site, geo_key, query_norm, results) "
                 "values (%s, 'swiggy', 'g', 'q', '[]')", (u,))
    conn.execute("insert into menu_cache(user_id, site, restaurant_id, menu) values (%s, 'swiggy', 'r', '{}')", (u,))
    if conn.execute("select to_regclass('public.app_addresses')").fetchone()[0]:
        conn.execute("insert into app_addresses(user_id, site, address_id) values (%s, 'swiggy', 'a1')", (u,))
    conn.execute("insert into admins(email) values (%s)", (em,))
    conn.execute("insert into invites(email, invited_by) values (%s, %s)", (em, u))
    conn.execute("insert into waitlist(email) values (%s)", (em,))
    conn.execute("insert into recipe_approvals(site, kind, reason, proposed, decided_by) "
                 "values ('zepto', 'orders', 'learned', '{}', %s)", (u,))
    conn.execute("insert into fee_rules(site, rules, updated_by) values ('zepto', '{}', %s) "
                 "on conflict (site) do update set updated_by = excluded.updated_by", (u,))
    store = store or vault.store_from_url(os.environ["PROFILE_STORE_URL"])
    store.put(vault.blob_key(u, "swiggy"), b"ciphertext-" + uuid.uuid4().bytes)

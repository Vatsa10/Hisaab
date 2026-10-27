"""Shared constants for server tests."""

A = "00000000-0000-0000-0000-00000000000a"
B = "00000000-0000-0000-0000-00000000000b"


def order(oid, total=100.0, status="delivered", items=None):
    return {"order_id": oid, "placed_at": "2025-01-05T10:00:00", "total_amount": total, "currency": "INR",
            "status": status, "merchant": "Store", "items": items or [], "raw": {"id": oid}}


def settings_for_tests(**_):
    """Settings for worker tests; the autouse vault_env fixture points the vault at a fresh temp dir."""
    import os

    from server import vault
    from server.config import DEV_PROFILE_KEY, Settings
    return Settings(database_url=os.environ["TEST_DATABASE_URL"], jwt_secret="test-secret",
                    jwks_url="", otp_timeout_s=5,
                    profile_store_url=os.environ.get("PROFILE_STORE_URL", "file://vault_local"),
                    profile_master_key=vault.master_key_from_env(DEV_PROFILE_KEY))


def make_token(sub, secret="test-secret", aud="authenticated", exp_in=3600):
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


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}


def destructive_db_allowed(url: str, env) -> bool:
    """The server tests drop and recreate the public and auth schemas. Only allow that on a local database
    (localhost / 127.0.0.1 / ::1 / a local socket) unless ALLOW_DESTRUCTIVE_TEST_DB=1 is set."""
    if env.get("ALLOW_DESTRUCTIVE_TEST_DB") == "1":
        return True
    import os

    from psycopg.conninfo import conninfo_to_dict
    try:
        params = conninfo_to_dict(url)
    except Exception:
        return False
    # libpq connects to hostaddr when it is given, whatever host says; with no host at all it uses PGHOST.
    target = params.get("hostaddr") or params.get("host") or env.get("PGHOST") or os.environ.get("PGHOST") or ""
    hosts = target.split(",")
    return all(h.strip().strip("[]") in LOCAL_HOSTS or h.startswith("/") for h in hosts)

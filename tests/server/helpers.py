"""Shared constants for server tests."""

A = "00000000-0000-0000-0000-00000000000a"
B = "00000000-0000-0000-0000-00000000000b"


def order(oid, total=100.0, status="delivered", items=None):
    return {"order_id": oid, "placed_at": "2025-01-05T10:00:00", "total_amount": total, "currency": "INR",
            "status": status, "merchant": "Store", "items": items or [], "raw": {"id": oid}}


def settings_for_tests():
    import os

    from server.config import Settings
    return Settings(database_url=os.environ["TEST_DATABASE_URL"], jwt_secret="test-secret",
                    jwks_url="", otp_timeout_s=5)


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

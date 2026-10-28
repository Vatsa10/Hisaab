import contextlib
from datetime import datetime, timezone

import pytest

import browser
import sync


class _Dead:
    def poll(self):
        return 1

    def wait(self, *a, **k):
        pass

    def kill(self):
        pass


def test_chrome_env_drops_secrets(monkeypatch):
    for k in ("DATABASE_URL", "OPENAI_API_KEY", "PROFILE_MASTER_KEY", "PROXY_URL_TEMPLATE", "SUPABASE_JWT_SECRET"):
        monkeypatch.setenv(k, "secret")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    env = browser.chrome_env()
    assert "secret" not in env.values() and env["DISPLAY"] == ":99" and env["TZ"] == "Asia/Kolkata"
    assert not {"DATABASE_URL", "PROXY_URL_TEMPLATE"} & set(env)


def test_chrome_env_is_case_insensitive_and_takes_base():
    env = browser.chrome_env({"Path": "/bin", "SystemRoot": "C:\\Windows", "SECRET": "x", "ProgramFiles(x86)": "p"})
    assert env == {"Path": "/bin", "SystemRoot": "C:\\Windows", "ProgramFiles(x86)": "p"}


def test_open_site_passes_scrubbed_env_and_webrtc_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "secret")
    seen = {}

    def fake_popen(args, **k):
        seen["args"], seen["env"] = args, k.get("env")
        return _Dead()

    monkeypatch.setattr(browser.subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError):
        with browser.open_site("zepto", profile_dir=tmp_path / "p", proxy_server="http://127.0.0.1:9"):
            pass
    assert seen["env"] is not None and "DATABASE_URL" not in seen["env"]
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in seen["args"]


def test_no_webrtc_flag_without_proxy(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(browser.subprocess, "Popen", lambda args, **k: seen.update(args=args) or _Dead())
    with pytest.raises(RuntimeError):
        with browser.open_site("zepto", profile_dir=tmp_path / "p"):
            pass
    assert not any("--force-webrtc" in a for a in seen["args"])


def test_classify_429_is_rate_limited():
    assert browser.classify_block(429, "") == "rate_limited"
    assert browser.classify_block(200, "ok") is None
    assert browser.classify_block(200, "<b>Attention Required! | Cloudflare</b>") == "challenge"
    assert browser.classify_block(500, "x px-captcha y") == "challenge"
    assert browser.classify_block(403, "plain forbidden") is None


class _Page:
    def __init__(self, r):
        self.r = r

    def evaluate(self, js, arg=None):
        return self.r


def _req():
    return {"url": "https://x/api", "method": "GET", "headers": {}}


def test_challenge_403_is_throttle_not_session_expired():
    page = _Page({"status": 403, "url": "https://x/api", "text": "<script src=/cdn-cgi/challenge-platform/cf-chl>",
                  "retryAfter": "120"})
    with pytest.raises(browser.SiteThrottled) as e:
        browser.fetch_in_page(page, _req())
    assert e.value.kind == "challenge" and e.value.retry_after_s == 120


def test_429_in_fetch_carries_retry_after():
    with pytest.raises(browser.SiteThrottled) as e:
        browser.fetch_in_page(_Page({"status": 429, "url": "u", "text": "", "retryAfter": "30"}), _req())
    assert e.value.kind == "rate_limited" and e.value.retry_after_s == 30


def test_plain_403_is_still_session_expired():
    with pytest.raises(browser.SessionExpired):
        browser.fetch_in_page(_Page({"status": 403, "url": "https://x/api", "text": "forbidden"}), _req())


def test_parse_retry_after_seconds_and_date():
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone.utc)
    assert browser.parse_retry_after("90") == 90
    assert browser.parse_retry_after("99999") == 3600
    assert browser.parse_retry_after("Wed, 28 Oct 2026 12:02:00 GMT", now) == 120
    assert browser.parse_retry_after("Wed, 28 Oct 2026 11:00:00 GMT", now) == 0
    assert browser.parse_retry_after(None) is None
    assert browser.parse_retry_after("soon") is None


def test_raise_if_blocked():
    with pytest.raises(browser.SiteThrottled) as e:
        browser.raise_if_blocked(_Page("Just a moment datadome captcha-delivery"))
    assert e.value.kind == "challenge"
    browser.raise_if_blocked(_Page("<html>fine orders</html>"))


def _fake_open():
    @contextlib.contextmanager
    def fake_open(*a, **k):
        yield None, object(), None, object()
    return fake_open


def test_fetch_orders_propagates_throttle_without_replay(monkeypatch):
    monkeypatch.setattr(sync, "open_site", _fake_open())
    monkeypatch.setattr(sync, "goto", lambda *a: None)

    def boom(page):
        raise browser.SiteThrottled("challenge")

    monkeypatch.setattr(sync, "raise_if_blocked", boom)
    monkeypatch.setattr(sync, "fast_path", lambda *a, **k: pytest.fail("fast_path"))
    monkeypatch.setattr(sync, "replay", lambda *a, **k: pytest.fail("replay"))
    monkeypatch.setattr(sync.agent, "discover", lambda *a, **k: pytest.fail("discover"))
    with pytest.raises(browser.SiteThrottled):
        sync.fetch_orders("zepto", None, recipe_loader=lambda s: {"orders_page_url": "https://x/orders"})


def test_fast_path_throttle_skips_replay(monkeypatch):
    monkeypatch.setattr(sync, "open_site", _fake_open())
    monkeypatch.setattr(sync, "goto", lambda *a: None)
    monkeypatch.setattr(sync, "raise_if_blocked", lambda p: None)

    def throttled(*a, **k):
        raise browser.SiteThrottled("rate_limited", 5)

    monkeypatch.setattr(sync, "fast_path", throttled)
    monkeypatch.setattr(sync, "replay", lambda *a, **k: pytest.fail("replay"))
    with pytest.raises(browser.SiteThrottled):
        sync.fetch_orders("zepto", None, recipe_loader=lambda s: {"orders_page_url": "u"})


def test_replay_429_is_rate_limited(monkeypatch):
    class Cap:
        items = []

        def clear(self):
            pass

    monkeypatch.setattr(sync.agent, "captured_for", lambda r, items: [{"status": 429, "json": {}}])
    with pytest.raises(browser.SiteThrottled) as e:
        sync.replay(object(), Cap(), {"nav_trace": [], "field_map": {}}, None)
    assert e.value.kind == "rate_limited"

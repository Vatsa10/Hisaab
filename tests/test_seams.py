import pytest
from playwright.sync_api import sync_playwright

import browser
from agent import LoginAgent


class _Dead:
    """Popen stand-in for a Chrome that exited at startup."""

    def poll(self):
        return 1

    def wait(self, *a, **k):
        pass

    def kill(self):
        pass


@pytest.fixture
def otp_page():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        page = b.new_page()
        page.set_content("<input autocomplete=one-time-code>")
        yield page
        b.close()


def test_otp_comes_from_prompt_not_input(monkeypatch, otp_page):
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("input() called"))
    la = LoginAgent("zepto", otp_page, None, None, "9000000000", prompt=lambda q: "4321")
    la.phone_typed = True
    la.on_observe()
    assert la.otp_entered and la.secrets["OTP"] == "4321"


def test_prompt_timeout_ends_run_cleanly(otp_page):
    def slow(q):
        raise TimeoutError

    la = LoginAgent("zepto", otp_page, None, None, "9000000000", prompt=slow)
    la.phone_typed = True
    la.on_observe()
    assert la.done == {"success": False, "note": "otp_timeout", "url": otp_page.url}


def test_open_site_uses_given_profile_dir(monkeypatch, tmp_path):
    seen = {}

    def fake_popen(args, **k):
        seen["args"] = args
        return _Dead()

    monkeypatch.setattr(browser.subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError):
        with browser.open_site("zepto", profile_dir=tmp_path / "u1-zepto"):
            pass
    assert f"--user-data-dir={(tmp_path / 'u1-zepto').resolve()}" in seen["args"]


def _timeout(q):
    raise TimeoutError


def test_ask_user_prompt_timeout_ends_run():
    from types import SimpleNamespace
    la = LoginAgent("zepto", SimpleNamespace(url="https://www.zepto.com/"), None, None, "9000000000", prompt=_timeout)
    assert la.ask_user("captcha?") == {"ok": True}
    assert la.done == {"success": False, "note": "ask_timeout", "url": "https://www.zepto.com/"}


def test_type_value_otp_prompt_timeout_ends_run():
    from types import SimpleNamespace
    la = LoginAgent("zepto", SimpleNamespace(url="https://www.zepto.com/"), None, None, "9000000000", prompt=_timeout)
    la.refs = {"e1": {"tag": "input"}}
    assert "error" in la.type_value("e1", "OTP")
    assert la.done == {"success": False, "note": "otp_timeout", "url": "https://www.zepto.com/"}


def _no_learning(monkeypatch):
    import contextlib
    import sync

    def boom(*a, **k):
        raise AssertionError("learning must not run")

    monkeypatch.setattr(sync, "learn", boom)
    monkeypatch.setattr(sync.agent, "discover", boom)
    monkeypatch.setattr(sync.recipes, "save", boom)
    monkeypatch.setattr(sync, "goto", lambda *a, **k: None)

    @contextlib.contextmanager
    def fake_open(*a, **k):
        yield None, object(), object(), type("Cap", (), {"clear": lambda self: None})()

    monkeypatch.setattr(sync, "open_site", fake_open)
    return sync


def test_fetch_orders_without_recipe_does_not_learn(monkeypatch):
    sync = _no_learning(monkeypatch)
    monkeypatch.setattr(sync.recipes, "load", lambda site: None)
    with pytest.raises(sync.RecipeError, match="no recipe for zepto"):
        sync.fetch_orders("zepto", None, allow_learn=False)


def test_fetch_orders_drift_does_not_rediscover(monkeypatch):
    sync = _no_learning(monkeypatch)
    monkeypatch.setattr(sync.recipes, "load", lambda site: {"orders_page_url": "https://x/", "fast_path_ok": True})

    def fail(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(sync, "fast_path", fail)
    monkeypatch.setattr(sync, "replay", fail)
    with pytest.raises(sync.RecipeError, match="drift"):
        sync.fetch_orders("zepto", None, allow_learn=False)

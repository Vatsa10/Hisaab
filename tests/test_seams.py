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
    kinds = []
    la = LoginAgent("zepto", otp_page, None, None, "9000000000", prompt=lambda q, kind: kinds.append(kind) or "4321")
    la.phone_typed = True
    la.on_observe()
    assert la.otp_entered and la.secrets["OTP"] == "4321" and kinds == ["otp"]


def test_prompt_timeout_ends_run_cleanly(otp_page):
    def slow(q, kind):
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


def _timeout(q, kind):
    _timeout.kinds.append(kind)
    raise TimeoutError


_timeout.kinds = []


def test_ask_user_prompt_timeout_ends_run():
    from types import SimpleNamespace
    la = LoginAgent("zepto", SimpleNamespace(url="https://www.zepto.com/"), None, None, "9000000000", prompt=_timeout)
    _timeout.kinds.clear()
    assert la.ask_user("captcha?") == {"ok": True}
    assert la.done == {"success": False, "note": "ask_timeout", "url": "https://www.zepto.com/"}
    assert _timeout.kinds == ["question"]


@pytest.mark.parametrize("injected", [True, False])
def test_ask_user_answer_is_not_put_in_model_history(monkeypatch, injected):
    from types import SimpleNamespace

    import agent
    monkeypatch.setattr(agent, "settle", lambda *a, **k: None)
    answer = "my flat is 4B, code 7781"
    if injected:
        la = LoginAgent("zepto", SimpleNamespace(url="https://www.zepto.com/"), None, None, "9000000000",
                        prompt=lambda q, kind: answer)
    else:
        monkeypatch.setattr("builtins.input", lambda *a: answer)
        la = LoginAgent("zepto", SimpleNamespace(url="https://www.zepto.com/"), None, None, "9000000000")
    assert la.ask_user("which flat?") == {"answer": answer}
    assert la.history == [f"user answered ({len(answer)} chars)"]
    assert answer not in " ".join(la.history)


def test_type_value_otp_prompt_timeout_ends_run():
    from types import SimpleNamespace
    la = LoginAgent("zepto", SimpleNamespace(url="https://www.zepto.com/"), None, None, "9000000000", prompt=_timeout)
    la.refs = {"e1": {"tag": "input"}}
    _timeout.kinds.clear()
    assert "error" in la.type_value("e1", "OTP")
    assert la.done == {"success": False, "note": "otp_timeout", "url": "https://www.zepto.com/"}
    assert _timeout.kinds == ["otp"]


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


SAVED = {"query": "Sunrise Apts, Akota, Vadodara, Gujarat", "lat": 22.3, "lon": 73.2}


def test_home_url_city_comes_from_given_location(monkeypatch):
    monkeypatch.setattr(browser, "load_settings", lambda: {"location": SAVED})
    assert browser.home_url("zomato") == "https://www.zomato.com/vadodara/restaurants"  # CLI: saved settings
    assert browser.home_url("zomato", {}) == "https://www.zomato.com/"  # given but empty: no city
    other = {"query": "Flat 2, Indiranagar, Bengaluru, Karnataka", "lat": 12.9, "lon": 77.6}
    assert browser.home_url("zomato", other) == "https://www.zomato.com/bangalore/restaurants"
    assert browser.home_url("zomato", {"query": None, "lat": 1.0, "lon": 2.0}) == "https://www.zomato.com/"


class _Recorder:
    """Accepts any method call and records it."""

    def __init__(self, calls, name):
        self._calls, self._name = calls, name

    def __getattr__(self, attr):
        def method(*a, **k):
            self._calls.append((f"{self._name}.{attr}", a))
            return {"targetInfos": []} if attr == "send" else None
        return method


def _fake_chrome(monkeypatch):
    import contextlib
    from types import SimpleNamespace
    calls = []
    ctx = _Recorder(calls, "context")
    ctx.pages = [SimpleNamespace(url="about:blank")]
    pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda url: SimpleNamespace(
        contexts=[ctx], new_browser_cdp_session=lambda: _Recorder(calls, "cdp"))))
    monkeypatch.setattr(browser, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(browser.subprocess, "Popen", lambda *a, **k: _Dead())
    monkeypatch.setattr(browser, "_wait_for_cdp", lambda *a, **k: None)
    monkeypatch.setattr(browser, "sync_playwright", lambda: contextlib.nullcontext(pw))
    monkeypatch.setattr(browser, "load_settings", lambda: {"location": SAVED})
    return calls


@pytest.mark.parametrize("location, granted", [(None, True), ({}, False),
                                               ({"query": "x", "lat": 1.0, "lon": 2.0}, True)])
def test_open_site_location_none_vs_empty(monkeypatch, tmp_path, location, granted):
    calls = _fake_chrome(monkeypatch)
    with browser.open_site("zepto", profile_dir=tmp_path / "p", location=location):
        pass
    geo = [a for name, a in calls if name == "context.set_geolocation"]
    assert bool(geo) == granted
    if location is None:
        assert geo == [({"latitude": 22.3, "longitude": 73.2},)]  # CLI default: the saved location
    elif granted:
        assert geo == [({"latitude": 1.0, "longitude": 2.0},)]


def test_recover_blocked_reloads_the_given_home(monkeypatch):
    from types import SimpleNamespace

    import agent
    went = []
    monkeypatch.setattr(agent, "goto", lambda page, url: went.append(url))
    monkeypatch.setattr(browser, "load_settings", lambda: {"location": SAVED})
    page = SimpleNamespace(url="https://www.zomato.com/", wait_for_timeout=lambda ms: None)
    la = LoginAgent("zomato", page, None, None, "9000000000", home="https://www.zomato.com/")
    la.blocked_page = lambda: False
    la.recover_blocked()
    assert went == ["https://www.zomato.com/"]

"""Plan 6 final-review fixes (I1-I5, M1, M2, M6): the cart Chrome is never warm, an idle read Chrome is parked
behind a block-all route, failures after an add are interrupted, engine writes never follow redirects, a store
switch that drops lines stops before any add, and service workers are unregistered before the first add."""
import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from test_cart_fill import ADDRESS_SELECT, add, recipe, run_fill, shop  # noqa: F401  (shop is a fixture)
from test_cart_guard import ADD, _fake_chrome

import browser
import cart_fill


# --- I3: engine writes never follow a redirect ------------------------------------------------------------

def test_fetch_js_uses_manual_redirect_for_writes():
    for js in (browser._FETCH_JS, browser._FETCH_MANY_JS):
        assert "credentials: 'include', redirect," in js and "'manual'" in js and "opaqueredirect" in js


def test_fetch_in_page_raises_redirected_write_on_a_redirect():
    page = SimpleNamespace(evaluate=lambda js, arg: {"status": 0, "url": "https://x/", "text": "", "redirected": True,
                                                     "retryAfter": None})
    with pytest.raises(browser.RedirectedWrite):
        browser.fetch_in_page(page, {"url": "https://x/a", "method": "POST", "headers": {}, "body": "{}"})
    with pytest.raises(browser.RedirectedWrite):
        browser._parse_fetch({"status": 302, "url": "https://x/", "text": "", "redirected": True})


def test_redirected_add_stops_the_fill(shop):
    real = shop.handle

    def handle(page, req):
        if req["url"].endswith("/api/cart/add"):
            shop.requests_sent.append({"product_id": "?", "qty": 0, "store_id": None})
            raise browser.RedirectedWrite("redirected_write")
        return real(page, req)
    cart_fill.browser.fetch_in_page = handle
    out = run_fill(shop, plan=[add("p1", 1), add("p2", 1)])
    assert shop.add_calls == 1
    assert out["status"] == "failed" and out["error"] == "redirected_write"


def test_redirected_location_select_stops_before_any_add(shop):
    shop.location = {"lat": 22.31, "lon": 73.17, "store_id": "S2"}
    real = shop.handle

    def handle(page, req):
        if req["url"].endswith("/api/location/select"):
            raise browser.RedirectedWrite("redirected_write")
        return real(page, req)
    cart_fill.browser.fetch_in_page = handle
    out = run_fill(shop, plan=[add("p1", 1)])
    assert shop.add_calls == 0 and out["status"] == "failed" and out["error"] == "redirected_write"


# --- I2: once an add went out, failures are interrupted -----------------------------------------------------

def test_first_add_5xx_is_interrupted_not_drift(shop):
    shop.fail_add = "500"
    with pytest.raises(cart_fill.CartInterrupted) as e:
        run_fill(shop, plan=[add("p1", 1), add("p2", 1)])
    assert not isinstance(e.value, cart_fill.CartDrift)
    assert e.value.cart_adds_sent == 1 and shop.add_calls == 1


def test_first_add_4xx_is_drift_with_no_add_landed(shop):
    def rejected(body):
        shop.requests_sent.append({"product_id": "p1", "qty": 1, "store_id": "S1"})
        raise RuntimeError('HTTP 400: {"message": "bad request"}')
    shop._add = rejected
    with pytest.raises(cart_fill.CartDrift) as e:
        run_fill(shop, plan=[add("p1", 1)])
    assert not getattr(e.value, "cart_adds_sent", 0)


def test_503_unavailable_is_not_taken_as_out_of_stock(shop):
    def down(body):
        shop.requests_sent.append({"product_id": "p1", "qty": 1, "store_id": "S1"})
        raise RuntimeError("HTTP 503: Service Unavailable")
    shop._add = down
    with pytest.raises(cart_fill.CartInterrupted):
        run_fill(shop, plan=[add("p1", 1)])


def test_session_expired_after_an_add_carries_adds_sent(shop):
    shop.fail_add = "expired"
    with pytest.raises(browser.SessionExpired) as e:
        run_fill(shop, plan=[add("p1", 1)])
    assert e.value.cart_adds_sent == 1


def test_throttle_after_an_add_propagates_with_adds_sent(shop):
    real = shop._add

    def throttled(body):
        if body["item"]["product_id"] == "p2":
            shop.requests_sent.append({"product_id": "p2", "qty": 1, "store_id": "S1"})
            raise browser.SiteThrottled("rate_limited")
        return real(body)
    shop._add = throttled
    with pytest.raises(browser.SiteThrottled) as e:
        run_fill(shop, plan=[add("p1", 1), add("p2", 1)])
    assert e.value.cart_adds_sent == 2


def test_session_expired_before_any_add_has_no_adds_sent(shop):
    shop.read_expired = True
    with pytest.raises(browser.SessionExpired) as e:
        run_fill(shop, plan=[add("p1", 1)])
    assert not getattr(e.value, "cart_adds_sent", 0)


# --- I5: a store switch that drops lines stops before any add ----------------------------------------------

def test_location_select_that_drops_existing_lines_stops_before_adding(shop):
    shop.cart = {"butter": 2}
    shop.location = {"lat": 22.31, "lon": 73.17, "store_id": "S2"}
    real = shop.handle

    def handle(page, req):
        out = real(page, req)
        if req["url"].endswith("/api/location/select"):
            shop.cart["butter"] = 0       # not sold at the new store
        return out
    cart_fill.browser.fetch_in_page = handle
    out = run_fill(shop, plan=[add("p1", 1)])
    assert shop.add_calls == 0
    assert out["status"] == "failed" and out["error"] == "existing_changed" and out["existing_changed"] is True
    assert out["location_selected"] is True


# --- M2: service workers are unregistered before the first add ----------------------------------------------

def test_service_workers_unregistered_before_first_add(shop, monkeypatch):
    events = []
    monkeypatch.setattr(browser, "unregister_service_workers", lambda page: events.append("sw") or 1)
    real = shop._add
    shop._add = lambda body: events.append("add") or real(body)
    run_fill(shop, plan=[add("p1", 1)])
    assert events == ["sw", "add"]


def test_service_worker_unregister_failure_stops_before_any_add(shop, monkeypatch):
    def boom(page):
        raise RuntimeError("no")
    monkeypatch.setattr(browser, "unregister_service_workers", boom)
    with pytest.raises(cart_fill.CartError):
        run_fill(shop, plan=[add("p1", 1)])
    assert shop.add_calls == 0


def test_unregister_service_workers_evaluates_in_page():
    seen = []
    page = SimpleNamespace(evaluate=lambda js: seen.append(js) or 2)
    assert browser.unregister_service_workers(page) == 2
    assert "getRegistrations" in seen[0] and "unregister" in seen[0]


# --- M1: the bypass record keeps the page objects, not their ids --------------------------------------------

def test_bypass_record_holds_page_objects(monkeypatch):
    sent = []
    ctx = SimpleNamespace(new_cdp_session=lambda p: SimpleNamespace(send=lambda m, a=None: sent.append((p, m))))
    done = browser._BypassRecord()
    a, b = object(), object()
    browser._bypass_service_workers(ctx, a, done)
    browser._bypass_service_workers(ctx, a, done)
    browser._bypass_service_workers(ctx, b, done)
    assert [p for p, m in sent if m == "Network.setBypassServiceWorker"] == [a, b]
    assert a in done and b in done and object() not in done


# --- I1: cart mode is never warm ------------------------------------------------------------------------------

def test_cart_mode_inside_a_warm_scope_opens_cold(monkeypatch, tmp_path):
    browser.close_warm()
    ctx, _ = _fake_chrome(monkeypatch, pages=["old"])
    with browser.warm_scope(("u", "zepto", True)):
        with browser.open_site("zepto", profile_dir=tmp_path / "p", location={}, cart_policy=ADD) as (_, p, g, c):
            assert browser.POOL.handle is None
    assert browser.POOL.handle is None


ROUTES = []


@pytest.fixture
def warm_chrome(monkeypatch):
    browser.close_warm()
    procs = []

    class Proc:
        def __init__(self, *a, **k):
            self.dead = False
            procs.append(self)

        def poll(self):
            return 1 if self.dead else None

        def wait(self, timeout=None):
            self.dead = True

        def kill(self):
            self.dead = True

    monkeypatch.setattr(browser.subprocess, "Popen", Proc)
    monkeypatch.setattr(browser, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(browser, "_free_port", lambda: 9)
    monkeypatch.setattr(browser, "_wait_for_cdp", lambda *a, **k: None)
    pw = MagicMock()
    ctx = MagicMock()
    main, extra = MagicMock(name="main"), MagicMock(name="extra")
    ctx.pages = [main, extra]
    ROUTES.clear()
    ctx.route.side_effect = lambda pattern, handler: ROUTES.append(handler)
    ctx.unroute.side_effect = lambda pattern, handler=None: ROUTES.remove(handler)
    ctx.unroute_all.side_effect = lambda **k: ROUTES.clear()
    br = pw.chromium.connect_over_cdp.return_value
    br.contexts = [ctx]
    br.new_browser_cdp_session.return_value.send.return_value = {"targetInfos": []}
    monkeypatch.setattr(browser, "sync_playwright", lambda: MagicMock(start=lambda: pw))
    yield SimpleNamespace(ctx=ctx, main=main, extra=extra, procs=procs)
    browser.close_warm()


def test_idle_read_chrome_is_parked_behind_a_block_all_route(warm_chrome, tmp_path):
    w = warm_chrome
    with browser.warm_scope(("u", "zepto", False)):
        with browser.open_site("zepto", allowlist=[], profile_dir=tmp_path / "u",
                               location={"lat": 1.0, "lon": 2.0}) as (_, page, guard, _c):
            assert ROUTES == [guard._handle]
    assert browser.POOL.handle is not None and not w.procs[0].dead       # still warm
    assert len(ROUTES) == 1 and ROUTES[0] is browser.POOL.handle.idle_route   # only the block-all route
    route = MagicMock()
    ROUTES[0](route, None)
    route.abort.assert_called_once()
    w.main.goto.assert_called_with("about:blank")
    w.extra.close.assert_called_once()
    w.ctx.clear_permissions.assert_called()

    # The next job swaps the block-all route for its own guard only once that guard is in place.
    with browser.warm_scope(("u", "zepto", False)):
        with browser.open_site("zepto", allowlist=[], profile_dir=tmp_path / "u", location={}) as (_, p, g2, _c):
            assert ROUTES == [g2._handle]
    assert len(w.procs) == 1


def test_park_failure_closes_the_warm_chrome(warm_chrome, tmp_path):
    warm_chrome.main.goto.side_effect = RuntimeError("gone")
    with browser.warm_scope(("u", "zepto", False)):
        with browser.open_site("zepto", allowlist=[], profile_dir=tmp_path / "u", location={}):
            pass
    assert browser.POOL.handle is None and warm_chrome.procs[0].dead

"""Discovery pages through an order list: it clicks the next page number even when the snapshot misses it, and it
only accepts "done" once a DIFFERENT page of orders has arrived (not the same first page requested again)."""
from types import SimpleNamespace

import pytest

import agent

playwright = pytest.importorskip("playwright.sync_api")

PAGER = """<div id="list">Showing 1-10 of 186 orders</div>
<div id="pager" style="position:fixed;bottom:0"><div>1</div><div>2</div><div>3</div><div>4</div><div>5</div></div>
<div style="position:fixed;bottom:0;left:0;right:0;height:80px;z-index:9999;background:#eee">Get the app</div>
<script>window.clicked = []; for (const d of document.querySelectorAll('#pager div')) d.onclick = () => window.clicked.push(d.textContent);</script>"""


def _page(html):
    pw = playwright.sync_playwright().start()
    try:
        try:
            b = pw.chromium.launch()
        except Exception:
            pytest.skip("no browser installed")
        p = b.new_page(viewport={"width": 390, "height": 500})
        p.set_content(html)
        return pw, b, p
    except BaseException:
        pw.stop()
        raise


def _disco(page):
    d = agent.Discovery.__new__(agent.Discovery)
    d.page, d.page_no, d.history, d.loads, d.site = page, 1, [], 0, "zomato"
    return d


def test_the_next_page_number_is_clicked_even_under_a_sticky_bar():
    pw, b, p = _page(PAGER)
    try:
        d = _disco(p)
        assert d._click_next_page_number() is True and d.page_no == 2
        assert p.evaluate("window.clicked") == ["2"]
        assert d._click_next_page_number() is True and d.page_no == 3  # then 3, in sequence
        assert p.evaluate("window.clicked") == ["2", "3"]
    finally:
        b.close()
        pw.stop()


def test_no_pager_means_no_click():
    pw, b, p = _page("<div>Showing 1-3 of 3 orders</div><div>1</div>")
    try:
        assert _disco(p)._click_next_page_number() is False
    finally:
        b.close()
        pw.stop()


def _items(*bodies):
    # a page of orders: a list of records with an id, a date and an amount, so it scores as an order collection
    return [{"url": "https://x.test/webroutes/user/orders", "method": "GET",
             "json": {"orders": [{"order_id": i, "placed_at": "2026-10-01", "total": 100 + i} for i in ids]}}
            for ids in bodies]


def test_the_same_first_page_requested_three_times_is_one_page():
    d = _disco(None)
    d.capture = SimpleNamespace(items=_items([1, 2, 3], [1, 2, 3], [1, 2, 3]))
    assert d.order_pages() == 1


def test_a_different_page_of_orders_counts_as_a_second_page():
    d = _disco(None)
    d.capture = SimpleNamespace(items=_items([1, 2, 3], [4, 5, 6]))
    assert d.order_pages() == 2


# ---- code shortcuts in order discovery -------------------------------------------------------------------

def _loaded(d, ids_per_response):
    d.capture = SimpleNamespace(items=_items(*ids_per_response))
    d.login, d.pager_clicks = False, 0
    return d


def test_the_login_agent_never_gets_the_pager_shortcut():
    d = _loaded(_disco(None), [[1, 2, 3]])
    d.login = True
    assert d.fast_decision() is None


def test_nothing_is_decided_before_any_orders_have_loaded():
    d = _loaded(_disco(None), [])
    assert d.fast_decision() is None


def test_once_a_different_page_has_arrived_discovery_finishes_without_a_model():
    d = _loaded(_disco(None), [[1, 2, 3], [4, 5, 6]])
    assert d.fast_decision() == ("finish", {"success": True, "note": "done"}, "code")


def test_code_clicks_the_pager_while_only_the_first_page_is_loaded_and_stops_at_the_cap():
    d = _loaded(_disco(None), [[1, 2, 3], [1, 2, 3]])

    def click():
        d.pager_clicks += 1
        return False
    d.auto_load_more = click
    for _ in range(agent.Discovery.PAGER_CLICK_CAP):
        assert d.fast_decision() == ("wait", {}, "code")
    assert d.fast_decision() is None  # a dead pager hands the page back to the model


def test_a_pager_that_cannot_be_clicked_defers_to_the_model():
    d = _loaded(_disco(None), [[1, 2, 3]])
    d.auto_load_more = lambda: False  # nothing clicked, nothing counted
    assert d.fast_decision() is None


def test_one_captured_different_page_is_enough_proof_to_finish():
    d = _loaded(_disco(None), [[1, 2, 3], [4, 5, 6]])
    d.refusals, d.loads = 0, 1
    assert d.premature() is None


def test_discovery_starts_on_the_admins_own_orders_page_when_given(monkeypatch):
    seen = []
    monkeypatch.setattr(agent, "goto", lambda page, url: seen.append(url))
    monkeypatch.setattr(agent.recipes, "load_seed", lambda site: None)
    page = SimpleNamespace(url="https://www.zomato.com/users/x/ordering")

    class Stub:
        trace = []

        def __init__(self, *a, **k):
            pass

        def run(self):
            return {"success": False, "note": "LOGGED_OUT", "url": ""}
    monkeypatch.setattr(agent, "Discovery", Stub)
    with pytest.raises(agent.SessionExpired):
        agent.discover("zomato", page, None, None, start_url="https://www.zomato.com/users/me/ordering")
    assert seen == ["https://www.zomato.com/users/me/ordering"]
    seen.clear()
    with pytest.raises(agent.SessionExpired):
        agent.discover("zomato", page, None, None)  # no start_url: the usual start page
    assert seen and seen != ["https://www.zomato.com/users/me/ordering"]

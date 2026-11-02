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

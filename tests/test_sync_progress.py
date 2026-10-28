"""on_progress(count, amount): the fetch reports orders found so far after each page (fast path, replay, and
through fetch_orders), so the app can show a live count during the first sync."""
import contextlib

import pytest

import sync


def _order(oid, total=100.0, status="delivered"):
    return {"order_id": oid, "placed_at": "2026-10-01T10:00:00", "total_amount": total, "status": status}


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)


def test_fast_path_reports_after_each_page(monkeypatch, no_wait):
    pages = [[_order("1", 100), _order("2", 50, "cancelled")], [_order("3", 25.5)], []]
    calls = iter(pages)
    monkeypatch.setattr(sync, "fetch_in_page", lambda page, req: next(calls))
    monkeypatch.setattr(sync.recipes, "first_request", lambda recipe: {"n": 0})
    monkeypatch.setattr(sync.recipes, "extract_orders", lambda data, fm: data)
    monkeypatch.setattr(sync.recipes, "next_request", lambda req, pag, data, n: {"n": req["n"] + 1})
    seen = []
    out = sync.fast_path(None, {"field_map": {}, "pagination": {}}, None, lambda c, a: seen.append((c, a)))
    assert [o["order_id"] for o in out] == ["1", "2", "3"]
    # cumulative count; the cancelled order counts as found but not as spend
    assert seen == [(2, 100.0), (3, 125.5)]


def test_replay_reports_when_new_orders_arrive(monkeypatch, no_wait):
    h1 = {"status": 200, "json": [_order("1", 10)]}
    h2 = {"status": 200, "json": [_order("1", 10), _order("2", 20, "refunded"), _order("3", 30)]}
    hits = iter([[h1], [h1, h2], [h1, h2], [h1, h2]])
    monkeypatch.setattr(sync.agent, "captured_for", lambda recipe, items: next(hits))
    monkeypatch.setattr(sync.agent, "wheel", lambda page: None)
    monkeypatch.setattr(sync.agent, "snapshot", lambda page: {"elements": []})
    monkeypatch.setattr(sync.recipes, "extract_orders", lambda data, fm: data)
    capture = type("Cap", (), {"clear": lambda self: None, "items": []})()
    seen = []
    out = sync.replay(object(), capture, {"field_map": {}, "nav_trace": []}, None, lambda c, a: seen.append((c, a)))
    assert len(out) == 3
    assert seen == [(1, 10.0), (3, 40.0)]  # once per batch of new orders, not on idle scrolls


def test_fetch_orders_passes_on_progress_to_both_paths(monkeypatch):
    @contextlib.contextmanager
    def fake_open(*a, **k):
        yield None, object(), object(), type("Cap", (), {"clear": lambda self: None})()

    monkeypatch.setattr(sync, "open_site", fake_open)
    monkeypatch.setattr(sync, "goto", lambda *a, **k: None)
    monkeypatch.setattr(sync, "raise_if_blocked", lambda page: None)
    got = {}

    def fast(page, recipe, since, on_progress=None):
        got["fast"] = on_progress
        return []

    def replay(page, capture, recipe, since, on_progress=None):
        got["replay"] = on_progress
        on_progress(1, 5.0)
        return [_order("1", 5)]

    monkeypatch.setattr(sync, "fast_path", fast)
    monkeypatch.setattr(sync, "replay", replay)
    seen = []
    cb = lambda c, a: seen.append((c, a))  # noqa: E731
    out = sync.fetch_orders("zepto", None, allow_learn=False, recipe_loader=lambda s: {"orders_page_url": "https://x/"},
                            on_progress=cb)
    assert out and got == {"fast": cb, "replay": cb} and seen == [(1, 5.0)]


def test_a_failing_callback_never_stops_the_fetch(caplog):
    def boom(c, a):
        raise RuntimeError("secret-ish text")

    sync.report_progress(boom, [_order("1")])  # no exception
    assert "RuntimeError" in caplog.text and "secret-ish" not in caplog.text
    sync.report_progress(None, [_order("1")])  # no callback: nothing to do

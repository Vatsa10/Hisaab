"""A full first import of a numbered-page feed fetches PARALLEL_PAGES pages at a time; everything else (an
incremental sync, a cursor feed) stays one page at a time."""
import pytest

import browser
import sync

RECIPE = {
    "endpoint": "https://x.test/orders", "method": "GET", "query": {"page": 1}, "headers": {}, "body": None,
    "field_map": {}, "pagination": {"type": "page", "param": "page", "start": 1},
}


def _order(oid):
    return {"order_id": oid, "placed_at": "2026-10-01T10:00:00", "total_amount": 10.0, "status": "delivered"}


@pytest.fixture
def feed(monkeypatch):
    """pages: {1: [ids], 2: [ids], ...}; a page past the end is empty. Records each parallel batch."""
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    monkeypatch.setattr(sync.recipes, "extract_orders", lambda data, fm: data)
    state = {"batches": [], "singles": [], "pages": {}}

    def page_of(req):
        return int(req["url"].split("page=")[1].split("&")[0])

    def many(page, reqs):
        state["batches"].append([page_of(r) for r in reqs])
        return [[_order(i) for i in state["pages"].get(page_of(r), [])] for r in reqs]

    def single(page, req):
        state["singles"].append(page_of(req))
        return [_order(i) for i in state["pages"].get(page_of(req), [])]

    monkeypatch.setattr(sync, "fetch_many_in_page", many)
    monkeypatch.setattr(sync, "fetch_in_page", single)
    return state


def test_a_full_import_fetches_four_pages_at_a_time_in_page_order(feed):
    feed["pages"] = {n: [f"{n}-{k}" for k in range(3)] for n in range(1, 10)}  # 9 pages of 3, then empty
    seen = []
    out = sync.fast_path(None, RECIPE, None, lambda c, a: seen.append(c))
    assert feed["batches"] == [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12]]
    assert feed["singles"] == []
    assert [o["order_id"] for o in out][:4] == ["1-0", "1-1", "1-2", "2-0"]  # page order is kept
    assert len(out) == 27 and seen[-1] == 27
    assert max(feed["batches"][-1]) == 12  # it stopped at the batch that held the empty page


def test_repeated_orders_past_the_end_end_the_import_without_duplicates(feed):
    feed["pages"] = {1: ["a", "b"], 2: ["c"], 3: ["a", "b"], 4: ["a", "b"]}  # past the end it repeats page 1
    out = sync.fast_path(None, RECIPE, None)
    assert [o["order_id"] for o in out] == ["a", "b", "c"]


def test_an_empty_first_page_is_a_recipe_error(feed):
    feed["pages"] = {}
    with pytest.raises(sync.RecipeError):
        sync.fast_path(None, RECIPE, None)


def test_an_incremental_sync_stays_one_page_at_a_time(feed):
    feed["pages"] = {1: ["a", "b"], 2: ["c"]}
    sync.fast_path(None, RECIPE, "2026-01-01T00:00:00")
    assert feed["batches"] == [] and feed["singles"][:2] == [1, 2]


def test_a_cursor_feed_stays_sequential(feed, monkeypatch):
    recipe = dict(RECIPE, pagination={"type": "cursor", "param": "cursor", "next_path": "next"})
    monkeypatch.setattr(sync.recipes, "next_request", lambda req, pag, data, n: None)
    feed["pages"] = {1: ["a"]}
    sync.fast_path(None, recipe, None)
    assert feed["batches"] == [] and feed["singles"] == [1]


def test_a_failed_request_in_a_batch_raises(monkeypatch):
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    monkeypatch.setattr(sync.recipes, "extract_orders", lambda data, fm: data)
    monkeypatch.setattr(sync, "fetch_many_in_page",
                        lambda page, reqs: [[_order("a")], browser.SiteThrottled("rate_limited"), []])
    with pytest.raises(browser.SiteThrottled):
        sync.fast_path(None, RECIPE, None)


def test_the_parallel_width_never_exceeds_the_browser_cap():
    assert 1 <= sync.PARALLEL_PAGES <= browser.MAX_PARALLEL_FETCH

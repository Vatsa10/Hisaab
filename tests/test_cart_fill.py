"""Add-only cart fill (Plan 6 T3). A test-local FakeShop answers fetch_in_page from an in-memory cart, location
and store; the browser edges (open_site, goto, fetch_in_page) and Plan 5's `search` module are faked."""
import contextlib
import copy
import json
import sys
import types
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

import browser
import cart_fill
import cart_recipe
from cart_guard import CartPolicy

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "cart_recipe.json").read_text(encoding="utf-8"))
PRICED = {"lat": 12.9716, "lon": 77.5946, "area": "Indiranagar"}
FAR = {"lat": 22.31, "lon": 73.17}
NAMES = {"p1": "Milk", "p2": "Bread", "p3": "Eggs", "p9": "Oat milk", "butter": "Butter", "jam": "Jam"}
SEARCH_RECIPE = {"kind": "search", "site": "shop", "field_map": {"products_path": "products"},
                 "store": {"lookup": {"method": "GET", "url": "https://shop.test/api/store"}, "store_id_path": "id"},
                 "read_post_allowlist": [{"url_regex": r"^https://shop\.test/api/search$", "why": "search"}]}


def recipe(**changes) -> dict:
    r = copy.deepcopy(FIXTURE)
    for path, value in changes.items():
        obj, *keys, last = [r, *path.split("__")]
        for k in keys:
            obj = obj[k]
        obj[last] = value
    return r


ADDRESS_SELECT = {"mode": "address_id", "method": "POST", "url": "https://shop.test/api/location/select",
                  "headers": {}, "body": {"address_id": "x"}, "address_id_path": "address_id",
                  "saved": {"method": "GET", "url": "https://shop.test/api/addresses", "headers": {},
                            "list_path": "addresses", "id": "id", "lat": "lat", "lon": "lng", "label": "label"}}


class FakeGuard:
    def __init__(self):
        self.redirected_writes: list[dict] = []
        self.blocked: list[dict] = []


class FakePage:
    def click(self, *a, **k):
        raise AssertionError("a fill must never click")


class FakeShop:
    """An in-memory delivery app behind fetch_in_page."""

    def __init__(self):
        self.cart: dict[str, int] = {}
        self.location = {"lat": PRICED["lat"], "lon": PRICED["lon"], "store_id": "S1", "area": "Indiranagar"}
        self.stores = {"S1": (PRICED["lat"], PRICED["lon"]), "S2": (FAR["lat"], FAR["lon"])}
        self.prices = {"S1": {"p1": 29.0, "p2": 50.0, "p3": 80.0, "p9": 70.0},
                       "S2": {"p1": 31.0, "p2": 55.0}}
        self.select_works = True
        self.saved = []
        self.oos: set[str] = set()
        self.cap: dict[str, int] = {}
        self.fail_add: str | None = None           # "500" | "expired"
        self.read_fails = False
        self.fail_reads: set[int] = set()          # 1-based cart-read numbers that fail
        self.read_expired = False
        self.drop_on_add: dict[str, tuple[str, int]] = {}
        self.redirect_on_add: set[str] = set()
        self.guard = FakeGuard()
        self.requests_sent: list[dict] = []        # add requests only: {product_id, qty, store_id}
        self.select_calls: list[dict] = []
        self.searches: list[str] = []
        self.reads = 0
        self.opened: dict = {}
        self.gotos: list[str] = []
        self.sleeps: list[float] = []

    @property
    def add_calls(self) -> int:
        return len(self.requests_sent)

    def store_at(self, loc) -> str | None:
        for sid, (lat, lon) in self.stores.items():
            if cart_fill.haversine_m(loc["lat"], loc["lon"], lat, lon) <= 500:
                return sid
        return None

    # --- the app's endpoints ---

    def handle(self, page, req: dict):
        url = urlsplit(req["url"])
        body = req.get("body")
        if url.path == "/api/cart/view":
            return self._view()
        if url.path == "/api/cart/add":
            return self._add(body)
        if url.path == "/api/location/current":
            loc = self.location
            return {"lat": loc["lat"], "lon": loc["lon"], "area": loc.get("area"),
                    "store": {"id": loc.get("store_id")}}
        if url.path == "/api/location/select":
            self.select_calls.append(copy.deepcopy(body))
            if self.select_works:
                if "address_id" in body:
                    a = next(a for a in self.saved if a["id"] == body["address_id"])
                    lat, lon = a["lat"], a["lng"]
                else:
                    lat, lon = body["latitude"], body["longitude"]
                self.location = {"lat": lat, "lon": lon, "store_id": self.store_at({"lat": lat, "lon": lon}),
                                 "area": "Selected"}
            return {"ok": True}
        if url.path == "/api/addresses":
            return {"addresses": self.saved}
        if url.path == "/api/search":
            q = dict(parse_qsl(url.query))
            self.searches.append(q["q"])
            prices = self.prices.get(q.get("store"), {})
            return {"products": [{"product_id": pid, "name": NAMES[pid], "price": price, "in_stock": True}
                                 for pid, price in prices.items() if NAMES[pid].lower() == q["q"].lower()]}
        raise AssertionError(f"unexpected request {req['method']} {req['url']}")

    def _view(self):
        self.reads += 1
        if self.read_expired:
            raise browser.SessionExpired("HTTP 401")
        if self.read_fails or self.reads in self.fail_reads:
            raise RuntimeError("HTTP 500: down")
        items = [{"pid": pid, "name": NAMES.get(pid, pid), "count": qty, "price": 10.0, "oos": False}
                 for pid, qty in self.cart.items() if qty > 0]
        sub = sum(10.0 * i["count"] for i in items)
        return {"cart": {"items": items, "bill": {"item_total": sub, "to_pay": sub + 15.0, "handling": 5.0,
                                                  "delivery": 10.0}}}

    def _add(self, body):
        pid, qty, store = body["item"]["product_id"], body["item"]["quantity"], body["store"]["id"]
        self.requests_sent.append({"product_id": pid, "qty": qty, "store_id": store})
        if self.fail_add == "expired":
            raise browser.SessionExpired("HTTP 401")
        if self.fail_add == "500":
            raise RuntimeError("HTTP 500: internal error")
        if pid in self.redirect_on_add:
            self.guard.redirected_writes.append({"method": "POST", "url": "https://shop.test/api/cart/add",
                                                 "status": 302})
        if pid in self.oos:
            raise RuntimeError('HTTP 400: {"message": "Item is out of stock"}')
        if pid in self.drop_on_add:
            other, q = self.drop_on_add[pid]
            self.cart[other] = q
        target = qty if FIXTURE_MODE[0] == "set" else self.cart.get(pid, 0) + qty
        self.cart[pid] = min(target, self.cap.get(pid, 10 ** 6))
        return {"ok": True}


FIXTURE_MODE = ["set"]


@pytest.fixture
def shop(monkeypatch):
    s = FakeShop()
    FIXTURE_MODE[0] = "set"

    @contextlib.contextmanager
    def fake_open_site(site, *args, **kwargs):
        s.opened = {"site": site, "args": args, **kwargs}
        yield None, FakePage(), s.guard, None

    monkeypatch.setattr(browser, "open_site", fake_open_site)
    monkeypatch.setattr(browser, "goto", lambda page, url, *a, **k: s.gotos.append(url))
    monkeypatch.setattr(browser, "fetch_in_page", s.handle)
    monkeypatch.setattr(cart_fill.time, "sleep", s.sleeps.append)

    def build_request(rec, text, location, store_id=None):
        return {"method": "GET", "url": f"https://shop.test/api/search?q={text}&store={store_id}",
                "headers": {}, "body": None}

    fake_search = types.SimpleNamespace(
        build_request=build_request,
        extract_products=lambda data, field_map: data["products"],
        resolve_store=lambda page, rec, location: s.store_at(location))
    monkeypatch.setitem(sys.modules, "search", fake_search)
    return s


def add(pid, packs=1, price=None, idx=None):
    return {"idx": idx, "label": NAMES[pid].lower(), "action": "add", "product_id": pid, "name": NAMES[pid],
            "packs": packs, "price": price if price is not None else 29.0, "question": None, "choices": [],
            "reason": None}


def ask_item(idx=None):
    return {"idx": idx, "label": "milk", "action": "ask", "product_id": None, "name": None, "packs": 1,
            "price": None, "question": "Which milk?",
            "choices": [{"id": "c1", "label": "Milk 500 ml", "product_id": "p1", "name": "Milk", "packs": 2,
                         "price": 29.0},
                        {"id": "c2", "label": "Oat milk 1 l", "product_id": "p9", "name": "Oat milk", "packs": 1,
                         "price": 70.0}],
            "reason": "swap"}


def skip_item(reason, idx=None):
    return {"idx": idx, "label": "eggs", "action": "skip", "product_id": "p3", "name": "Eggs", "packs": 1,
            "price": 80.0, "question": None, "choices": [], "reason": reason}


def run_fill(shop, plan, ask=lambda q, c: None, rec=None, search_recipe=SEARCH_RECIPE, priced=PRICED,
             confirm_location=None, on_item=None, stages=None):
    for i, item in enumerate(plan):
        if item["idx"] is None:
            item["idx"] = i
    seen = on_item if on_item is not None else []
    return cart_fill.fill_cart("shop", plan, priced, rec or recipe(), search_recipe, Path("profiles/u1/shop"),
                               ask=ask, on_item=seen.append,
                               on_stage=(stages.append if stages is not None else (lambda s: None)),
                               confirm_location=confirm_location)


def sent(pid, packs=1, status="added"):
    return {"idx": 0, "label": pid, "name": pid, "product_id": pid, "packs": packs, "status": status,
            "added_qty": 0, "skip_reason": None}


# --- the fill --------------------------------------------------------------------------------

def test_adds_planned_items_and_reports_cart(monkeypatch, shop):
    seen, stages = [], []
    out = run_fill(shop, plan=[add("p1", 2), add("p2", 1)], on_item=seen, stages=stages)
    assert out["status"] == "done" and out["error"] is None and out["existing_changed"] is False
    assert [(i["product_id"], i["status"], i["added_qty"]) for i in out["items"]] == [("p1", "added", 2),
                                                                                       ("p2", "added", 1)]
    assert out["cart"] == {"subtotal": 30.0, "total": 45.0, "fees": {"handling": 5.0, "delivery": 10.0},
                           "line_count": 2, "existing_count": 0}
    assert len(seen) == 2 and [s["product_id"] for s in seen] == ["p1", "p2"]
    assert stages == ["checking_location", "adding"]
    assert [r["store_id"] for r in shop.requests_sent] == ["S1", "S1"]
    assert len(shop.sleeps) == 1 and 0.8 <= shop.sleeps[0] <= 1.6
    assert out["location_selected"] is False and out["repriced"] is None
    assert out["app_location"]["store_id"] == "S1"


def test_opens_site_in_cart_mode_with_media_blocked(monkeypatch, shop):
    run_fill(shop, plan=[add("p1", 1)])
    o = shop.opened
    assert o["site"] == "shop" and isinstance(o["cart_policy"], CartPolicy)
    assert o["cart_policy"] == cart_recipe.policy_for(recipe(), SEARCH_RECIPE)
    assert o["block_media"] is True and not o.get("login_mode")
    assert o["profile_dir"] == Path("profiles/u1/shop")
    assert o["location"] == {"query": "Indiranagar", "lat": PRICED["lat"], "lon": PRICED["lon"]}
    assert shop.gotos == ["https://shop.test/"]


def test_set_mode_adds_on_top_of_existing_quantity(monkeypatch, shop):
    shop.cart = {"p1": 2}
    out = run_fill(shop, plan=[add("p1", packs=1)])
    assert shop.requests_sent[-1]["qty"] == 3 and shop.cart["p1"] == 3 and out["items"][0]["status"] == "added"


def test_set_mode_same_product_twice_counts_what_was_sent(monkeypatch, shop):
    shop.cart = {"p1": 1}
    out = run_fill(shop, plan=[add("p1", 1), add("p1", 2)])
    assert [r["qty"] for r in shop.requests_sent] == [2, 4] and shop.cart["p1"] == 4
    assert [i["status"] for i in out["items"]] == ["added", "added"]


def test_increment_mode_sends_packs(monkeypatch, shop):
    FIXTURE_MODE[0] = "increment"
    shop.cart = {"p1": 2}
    out = run_fill(shop, plan=[add("p1", packs=2)], rec=recipe(add__qty_mode="increment"))
    assert shop.requests_sent[-1]["qty"] == 2 and shop.cart["p1"] == 4
    assert out["items"][0]["status"] == "added" and out["items"][0]["added_qty"] == 2


def test_existing_lines_untouched(monkeypatch, shop):
    shop.cart = {"butter": 1}
    out = run_fill(shop, plan=[add("p1", 2)])
    assert shop.cart["butter"] == 1 and out["cart"]["existing_count"] == 1 and out["existing_changed"] is False
    assert out["cart"]["line_count"] == 2 and out["status"] == "done"


def test_existing_line_dropping_is_reported(monkeypatch, shop):
    shop.cart = {"butter": 2}
    shop.drop_on_add = {"p1": ("butter", 1)}
    out = run_fill(shop, plan=[add("p1", 1)])
    assert out["existing_changed"] is True
    assert out["status"] == "failed" and out["error"] == "existing_changed"


def test_existing_line_dropping_aborts_the_rest_of_the_fill(monkeypatch, shop):
    shop.cart = {"butter": 2, "jam": 1}
    shop.drop_on_add = {"p1": ("jam", 0)}
    out = run_fill(shop, plan=[add("p1", 1), add("p2", 1), add("p3", 1)])
    assert [r["product_id"] for r in shop.requests_sent] == ["p1"]          # nothing more was sent
    assert out["existing_changed"] is True and out["status"] == "failed" and out["error"] == "existing_changed"
    assert [i["status"] for i in out["items"]] == ["added", "not_added", "not_added"]


def test_redirected_write_aborts_the_fill(monkeypatch, shop):
    shop.redirect_on_add = {"p1"}
    seen = []
    out = run_fill(shop, plan=[add("p1", 1), add("p2", 1)], on_item=seen)
    assert [r["product_id"] for r in shop.requests_sent] == ["p1"]
    assert out["status"] == "failed" and out["error"] == "redirected_write"
    assert out["items"][1]["status"] == "not_added" and len(seen) == 2


def test_redirected_location_select_adds_nothing(monkeypatch, shop):
    shop.location = {"lat": PRICED["lat"] + 0.027, "lon": PRICED["lon"], "store_id": "S3", "area": "Elsewhere"}
    real = shop.handle

    def handle(page, req):
        if urlsplit(req["url"]).path == "/api/location/select":
            shop.guard.redirected_writes.append({"method": "POST", "url": req["url"], "status": 307})
        return real(page, req)

    monkeypatch.setattr(browser, "fetch_in_page", handle)
    out = run_fill(shop, plan=[add("p1", 1)])
    assert shop.add_calls == 0 and out["status"] == "failed" and out["error"] == "redirected_write"


def test_guard_without_redirected_writes_fails_closed(monkeypatch, shop):
    del shop.guard.redirected_writes
    with pytest.raises(cart_fill.CartError, match="redirected writes"):
        run_fill(shop, plan=[add("p1", 1)])
    assert shop.add_calls == 0 and shop.reads == 0


def test_real_cart_guard_has_redirected_writes():
    from cart_guard import CartGuard
    assert CartGuard(CartPolicy()).redirected_writes == []


def test_out_of_stock_at_fill_time(monkeypatch, shop):
    shop.oos = {"p2"}
    out = run_fill(shop, plan=[add("p1", 1), add("p2", 1)])
    assert [i["status"] for i in out["items"]] == ["added", "out_of_stock"]
    assert out["status"] == "partial"


def test_partial_quantity_cap(monkeypatch, shop):
    shop.cap = {"p1": 2}
    out = run_fill(shop, plan=[add("p1", 3)])
    assert out["items"][0]["status"] == "partial" and out["items"][0]["added_qty"] == 2
    assert out["status"] == "partial"


def test_ask_choice_adds_chosen_product(monkeypatch, shop):
    asked = []
    out = run_fill(shop, plan=[ask_item()], ask=lambda q, c: asked.append((q, c)) or "c2")
    assert asked[0][0] == "Which milk?" and [c["id"] for c in asked[0][1]] == ["c1", "c2"]
    assert shop.requests_sent == [{"product_id": "p9", "qty": 1, "store_id": "S1"}]
    item = out["items"][0]
    assert (item["product_id"], item["name"], item["status"], item["packs"]) == ("p9", "Oat milk", "added", 1)
    assert out["status"] == "done"


def test_choice_ids_compare_as_strings(monkeypatch, shop):
    item = ask_item()
    for n, c in enumerate(item["choices"], 1):
        c["id"] = n
    out = run_fill(shop, plan=[item], ask=lambda q, c: "2")
    assert shop.requests_sent == [{"product_id": "p9", "qty": 1, "store_id": "S1"}] and out["status"] == "done"


def test_user_skip_still_done(monkeypatch, shop):
    out = run_fill(shop, plan=[skip_item("you_skipped"), skip_item("no_answer"), add("p1", 1)])
    assert out["status"] == "done"


def test_unanswered_question_skips_item_and_continues(monkeypatch, shop):
    out = run_fill(shop, plan=[ask_item(), add("p1", 1)], ask=lambda q, c: None)
    assert [i["status"] for i in out["items"]] == ["skipped", "added"] and out["items"][0]["skip_reason"] == "no_answer"
    assert out["status"] == "done"


def test_skip_answer_and_unknown_choice_are_you_skipped(monkeypatch, shop):
    for answer in ("skip", "c-made-up"):
        out = run_fill(shop, plan=[ask_item(), add("p1", 1)], ask=lambda q, c: answer)
        assert out["items"][0]["status"] == "skipped" and out["items"][0]["skip_reason"] == "you_skipped"
    assert all(r["product_id"] == "p1" for r in shop.requests_sent)


def test_skip_items_reported_with_reason(monkeypatch, shop):
    seen = []
    out = run_fill(shop, plan=[skip_item("out_of_stock"), skip_item("not_found"), add("p1", 1)], on_item=seen)
    assert [(i["status"], i["skip_reason"]) for i in out["items"]] == [("skipped", "out_of_stock"),
                                                                      ("skipped", "not_found"), ("added", None)]
    assert len(seen) == 3 and shop.add_calls == 1 and out["status"] == "partial"   # plan-time skips count


def test_everything_skipped_is_failed_nothing_added(monkeypatch, shop):
    out = run_fill(shop, plan=[skip_item("not_found")])
    assert out["status"] == "failed" and out["error"] == "nothing_added" and shop.add_calls == 0


def test_bad_plan_item_is_never_sent(monkeypatch, shop):
    out = run_fill(shop, plan=[add("p1", 0), add("p2", 1)])
    assert [r["product_id"] for r in shop.requests_sent] == ["p2"]
    assert out["items"][0]["status"] == "not_added" and out["status"] == "partial"


# --- location ----------------------------------------------------------------------------------

def test_location_selected_when_app_elsewhere(monkeypatch, shop):
    shop.location = {"lat": PRICED["lat"] + 0.027, "lon": PRICED["lon"], "store_id": "S3", "area": "Elsewhere"}
    stages = []
    out = run_fill(shop, plan=[add("p1", 1)], stages=stages)
    assert shop.select_calls == [{"latitude": PRICED["lat"], "longitude": PRICED["lon"]}]
    assert out["location_selected"] is True and out["status"] == "done"
    assert stages == ["checking_location", "selecting_location", "adding"]
    assert shop.requests_sent[0]["store_id"] == "S1"


def test_location_mismatch_reprices_and_adds_nothing(monkeypatch, shop):
    shop.location = {"lat": 22.31, "lon": 73.17, "store_id": "S2"}; shop.select_works = False
    out = run_fill(shop, plan=[add("p1", 2, price=29.0)])
    assert out["status"] == "needs_confirmation" and shop.add_calls == 0
    assert out["repriced"] == [{"idx": 0, "name": "Milk", "product_id": "p1", "old_price": 58.0, "new_price": 62.0,
                                "in_stock": True}]
    assert out["items"] == [] and out["cart"] is None and out["app_location"]["store_id"] == "S2"


def test_reprice_missing_product_and_no_search_recipe(monkeypatch, shop):
    shop.location = {"lat": 22.31, "lon": 73.17, "store_id": "S2"}; shop.select_works = False
    out = run_fill(shop, plan=[add("p3", 1, price=80.0), ask_item()])
    assert out["repriced"] == [{"idx": 0, "name": "Eggs", "product_id": "p3", "old_price": 80.0, "new_price": None,
                                "in_stock": False}]
    out = run_fill(shop, plan=[add("p1", 1)], search_recipe=None)
    assert out["status"] == "needs_confirmation" and out["repriced"] == [] and shop.add_calls == 0


def test_confirm_location_moved_again_fails(monkeypatch, shop):
    shop.location = {"lat": 22.31, "lon": 73.17, "store_id": "S2"}
    confirmed = {"lat": PRICED["lat"], "lon": PRICED["lon"], "area": "Indiranagar"}
    out = run_fill(shop, plan=[add("p1", 1)], confirm_location=confirmed)
    assert out["status"] == "failed" and out["error"] == "location_changed"
    assert shop.add_calls == 0 and shop.select_calls == [] and out["app_location"]["store_id"] == "S2"


def test_confirm_location_matching_fills(monkeypatch, shop):
    shop.location = {"lat": 22.31, "lon": 73.17, "store_id": "S2"}
    out = run_fill(shop, plan=[add("p1", 1)], confirm_location={"lat": 22.31, "lon": 73.17, "area": "Vadodara"})
    assert out["status"] == "done" and shop.requests_sent[0]["store_id"] == "S2"


def test_store_id_match_wins_over_distance():
    target = {"lat": 12.9716, "lon": 77.5946}
    far_same_store = {"lat": 13.0, "lon": 77.5946, "store_id": "S1"}
    near_other_store = {"lat": 12.9716, "lon": 77.5946, "store_id": "S2"}
    assert cart_fill.location_matches(far_same_store, target, "S1", 500) is True
    assert cart_fill.location_matches(near_other_store, target, "S1", 500) is False
    assert cart_fill.location_matches(near_other_store, target, None, 500) is True
    assert cart_fill.location_matches({"lat": 13.0, "lon": 77.5946, "store_id": None}, target, "S1", 500) is False
    assert cart_fill.location_matches({"store_id": "S1"}, {"lat": None, "lon": None}, None, 500) is False
    assert cart_fill.location_matches(None, target, "S1", 500) is False


def test_haversine():
    assert cart_fill.haversine_m(12.9716, 77.5946, 12.9716, 77.5946) == 0
    assert 2950 < cart_fill.haversine_m(12.9716, 77.5946, 12.9986, 77.5946) < 3050


def test_saved_address_selected_only_within_500m(monkeypatch, shop):
    rec = recipe(location__select=ADDRESS_SELECT)
    shop.location = {"lat": 22.31, "lon": 73.17, "store_id": "S2"}
    shop.saved = [{"id": "far", "lat": PRICED["lat"] + 0.0072, "lng": PRICED["lon"], "label": "Gym"},     # ~800 m
                  {"id": "near", "lat": PRICED["lat"] + 0.0027, "lng": PRICED["lon"], "label": "Home"},   # ~300 m
                  {"id": "nearer", "lat": PRICED["lat"] + 0.0018, "lng": PRICED["lon"], "label": "Work"}]  # ~200 m
    out = run_fill(shop, plan=[add("p1", 1)], rec=rec)
    assert shop.select_calls == [{"address_id": "nearer"}]
    assert out["location_selected"] is True and out["status"] == "done"

    shop.location = {"lat": 22.31, "lon": 73.17, "store_id": "S2"}
    shop.select_calls.clear()
    shop.saved = [{"id": "far", "lat": PRICED["lat"] + 0.0072, "lng": PRICED["lon"], "label": "Gym"}]
    shop.requests_sent.clear()
    out = run_fill(shop, plan=[add("p1", 1)], rec=rec)
    assert shop.select_calls == [] and out["status"] == "needs_confirmation" and shop.add_calls == 0


# --- failures --------------------------------------------------------------------------------

def test_first_add_failure_is_drift(monkeypatch, shop):
    shop.fail_add = "500"
    with pytest.raises(cart_fill.CartDrift, match="add failed"):
        run_fill(shop, plan=[add("p1", 1), add("p2", 1)])
    assert shop.add_calls == 1


def test_later_add_failure_is_not_added(monkeypatch, shop):
    real = shop._add

    def flaky(body):
        if body["item"]["product_id"] == "p2":
            shop.requests_sent.append({"product_id": "p2", "qty": 1, "store_id": "S1"})
            raise RuntimeError("HTTP 500: internal error")
        return real(body)

    shop._add = flaky
    out = run_fill(shop, plan=[add("p1", 1), add("p2", 1)])
    assert [i["status"] for i in out["items"]] == ["added", "not_added"] and out["status"] == "partial"


def test_cart_read_failure_is_drift(monkeypatch, shop):
    shop.read_fails = True
    with pytest.raises(cart_fill.CartDrift, match="cart read failed"):
        run_fill(shop, plan=[add("p1", 1)])
    assert shop.add_calls == 0


def test_mid_fill_read_failure_stops_adding_and_reports(monkeypatch, shop):
    shop.fail_reads = {2}                              # 1 before, 2 the check after p1
    out = run_fill(shop, plan=[add("p1", 1), add("p2", 1)])
    assert [r["product_id"] for r in shop.requests_sent] == ["p1"]
    assert out["status"] == "failed" and out["error"] == "cart_read_failed"
    assert [i["status"] for i in out["items"]] == ["added", "not_added"] and out["cart"]["line_count"] == 1


def test_final_read_failure_after_adds_returns_results(monkeypatch, shop):
    shop.fail_reads = {4}                              # before, two checks, then the final read
    seen = []
    out = run_fill(shop, plan=[add("p1", 1), add("p2", 1)], on_item=seen)
    assert shop.add_calls == 2 and len(seen) == 2
    assert out["status"] == "failed" and out["error"] == "cart_read_failed" and out["cart"] is None
    assert [(i["product_id"], i["status"]) for i in out["items"]] == [("p1", "added"), ("p2", "added")]
    assert out["app_location"]["store_id"] == "S1"


def test_session_expired_propagates(monkeypatch, shop):
    shop.read_expired = True
    with pytest.raises(browser.SessionExpired):
        run_fill(shop, plan=[add("p1", 1)])
    shop.read_expired = False
    shop.fail_add = "expired"
    with pytest.raises(browser.SessionExpired):
        run_fill(shop, plan=[add("p1", 1)])


def test_fill_never_clicks(monkeypatch, shop):
    import agent

    def boom(*a, **k):
        raise AssertionError("a fill must never click")

    monkeypatch.setattr(agent, "click_ref", boom)
    out = run_fill(shop, plan=[add("p1", 1), ask_item(), skip_item("not_found")], ask=lambda q, c: "c1")
    assert out["status"] == "partial" and shop.add_calls == 2      # the plan-time not_found skip makes it partial
    src = Path(cart_fill.__file__).read_text(encoding="utf-8")
    assert "click" not in src.replace("never clicks", "").replace("never calls agent.click_ref", "")


def test_logs_only_site_and_counts(monkeypatch, shop, caplog):
    caplog.set_level("DEBUG", logger="cart_fill")
    shop.cart = {"butter": 1}
    run_fill(shop, plan=[add("p1", 2), ask_item()], ask=lambda q, c: "c2")
    text = caplog.text
    for secret in ("Milk", "Oat", "p1", "p9", "Indiranagar", "12.97", "77.59", "S1", "Which milk"):
        assert secret not in text
    assert "shop" in text


# --- pure helpers ------------------------------------------------------------------------------

def test_verify_rules():
    items, changed = cart_fill.verify({"a": 1}, {"a": 1, "p": 1}, [sent("p", packs=2)])
    assert (items[0]["status"], items[0]["added_qty"], changed) == ("partial", 1, False)
    items, changed = cart_fill.verify({"a": 2}, {"a": 1, "p": 2}, [sent("p", packs=2)])
    assert (items[0]["status"], items[0]["added_qty"], changed) == ("added", 2, True)
    items, _ = cart_fill.verify({}, {}, [sent("p", packs=1), sent("q", packs=1, status="out_of_stock")])
    assert [(i["status"], i["added_qty"]) for i in items] == [("not_added", 0), ("out_of_stock", 0)]
    items, _ = cart_fill.verify({"p": 1}, {"p": 4}, [sent("p", packs=1), sent("p", packs=3)])
    assert [(i["status"], i["added_qty"]) for i in items] == [("added", 1), ("partial", 2)]
    items, _ = cart_fill.verify({}, {"p": 1}, [sent("p", packs=1, status="out_of_stock"), sent("p", packs=1)])
    assert [(i["status"], i["added_qty"]) for i in items] == [("out_of_stock", 0), ("added", 1)]


def test_classify_add():
    assert cart_fill.classify_add('HTTP 400: {"message": "Item is out of stock"}') == "out_of_stock"
    assert cart_fill.classify_add("Sold Out") == "out_of_stock"
    assert cart_fill.classify_add("HTTP 500: internal error") == "ok"
    assert cart_fill.classify_add(None) == "ok"


def test_outcome_shape():
    out = cart_fill.outcome("failed", [], None, error="nothing_added")
    assert out == {"status": "failed", "error": "nothing_added", "existing_changed": False, "items": [],
                   "cart": None, "app_location": None, "location_selected": False, "repriced": None}


def test_fixture_is_a_valid_cart_recipe(monkeypatch):
    import cart_guard
    monkeypatch.setattr(cart_guard, "host_ok", lambda site, host: host == "shop.test")
    assert cart_recipe.check_cart_recipe(recipe(), "shop") is None
    assert cart_recipe.check_cart_recipe(recipe(location__select=ADDRESS_SELECT), "shop") is None


def test_every_fill_request_passes_the_final_cart_guard(monkeypatch, shop):
    from cart_guard import decide_cart
    policy = cart_recipe.policy_for(recipe(), SEARCH_RECIPE)
    seen = []
    real = shop.handle

    def guarded(page, req):
        body = req.get("body")
        text = None if body is None else body if isinstance(body, str) else json.dumps(body)
        ok, why = decide_cart(req["method"], req["url"], False, text, policy, headers=req.get("headers"))
        seen.append((req["method"], urlsplit(req["url"]).path, ok, why))
        assert ok, (req["url"], why)
        return real(page, req)

    monkeypatch.setattr(browser, "fetch_in_page", guarded)
    shop.cart = {"butter": 2}
    out = run_fill(shop, plan=[add("p1", 2), ask_item()], ask=lambda q, c: "c2")
    assert out["status"] == "done"
    assert ("POST", "/api/cart/add", True, "cart add") in seen

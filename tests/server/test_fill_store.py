"""server.fill: the fill plan, fill options, starting / confirming fills and the fill row's life cycle."""
from datetime import date, datetime, timedelta, timezone

import pytest
from helpers import A, B
from psycopg.types.json import Jsonb

from server import fill, limits, usage
from server.config import OPEN_APP_URLS, Settings

NOW = datetime(2026, 10, 28, 6, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 28)


@pytest.fixture
def s():
    return Settings()


# --- builders ----------------------------------------------------------------------------------------------------

def product(pid, name, price, size_label="1 L"):
    return {"product_id": pid, "name": name, "brand": "Amul", "size_label": size_label, "price": price, "mrp": None,
            "in_stock": True}


def matched(idx, pid="p1", name="Amul Taaza Toned Milk 500 ml", price=29.0, packs=2):
    return {"idx": idx, "status": "matched", "confidence": 0.9, "product": product(pid, name, price, "500 ml"),
            "packs": packs, "line_total": packs * price, "swap_from": None, "candidates": []}


def swapped(idx):
    return {"idx": idx, "status": "swapped", "confidence": 0.8,
            "product": product("p9", "Lay's Magic Masala 52 g", 20.0, "52 g"), "packs": 1, "line_total": 20.0,
            "swap_from": {"name": "Lay's Classic Salted 52 g"}, "candidates": []}


def pick(idx):
    cands = [dict(product("c1", "Amul Butter 100 g", 56.0, "100 g"), packs=1, line_total=56.0, score=0.7),
             dict(product("c2", "Amul Butter 500 g", 270.0, "500 g"), packs=2, line_total=540.0, score=0.6)]
    return {"idx": idx, "status": "pick", "confidence": 0.6, "product": None, "packs": None, "line_total": None,
            "swap_from": None, "candidates": cands}


def missing(idx, status):
    return {"idx": idx, "status": status, "confidence": 0, "product": None, "packs": None, "line_total": None,
            "swap_from": None, "candidates": []}


REQ_ITEMS = [{"text": "milk 1L", "qty": 1}, {"text": "lays classic", "qty": 1}, {"text": "butter", "qty": 1},
             {"text": "eggs", "qty": 1}, {"text": "saffron", "qty": 1}]
RESULT_ITEMS = [matched(0), swapped(1), pick(2), missing(3, "out_of_stock"), missing(4, "not_found")]


def make_request(pg, user=A, mode="grocery", status="done", priced_at=NOW - timedelta(minutes=5), items=None,
                 choices=None, sites=("zepto", "blinkit", "instamart")):
    return str(pg.execute(
        "insert into cart_requests (user_id, mode, items, sites, status, area, lat, lon, choices, priced_at) "
        "values (%s,%s,%s,%s,%s,'Gorwa, Vadodara',22.33,73.16,%s,%s) returning id",
        (user, mode, Jsonb(items or REQ_ITEMS), list(sites), status, Jsonb(choices or {}), priced_at)).fetchone()[0])


def make_result(pg, rid, site="zepto", user=A, status="ok", items=None, priced_at=NOW - timedelta(minutes=5)):
    pg.execute("insert into cart_results (request_id, user_id, site, status, items, subtotal, total, priced_at) "
               "values (%s,%s,%s,%s,%s,100,120,%s)",
               (rid, user, site, status, Jsonb(RESULT_ITEMS if items is None else items), priced_at))


def cart_recipe(pg, site="zepto", healthy=True, paused=False):
    pg.execute("insert into recipes (site, kind, recipe, healthy, paused) values (%s,'cart','{}',%s,%s) "
               "on conflict (site, kind) do update set healthy = excluded.healthy, paused = excluded.paused",
               (site, healthy, paused))


def connect(pg, site="zepto", user=A, status="connected"):
    pg.execute("insert into connections (user_id, site, status) values (%s,%s,%s) "
               "on conflict (user_id, site) do update set status = excluded.status", (user, site, status))


def ready(pg, site="zepto", **k):
    rid = make_request(pg, **k)
    make_result(pg, rid, site)
    cart_recipe(pg, site)
    connect(pg, "swiggy" if site == "instamart" else site)
    return rid


def usage_row(pg, user):
    row = pg.execute("select cart_fills from usage where user_id = %s and day = %s", (user, TODAY)).fetchone()
    return {"cart_fills": row[0] if row else 0}


def fill_row(pg, fid):
    return fill.get_fill(pg, fid)


def pause_budget(pg):
    pg.execute("insert into budget_state (month, level, spent_inr, budget_inr) values ('2026-10','paused',5000,5000)")


# --- the plan ----------------------------------------------------------------------------------------------------

def test_plan_items_from_result():
    req = {"items": REQ_ITEMS, "choices": {}}
    plan = fill.plan_items(req, {"items": RESULT_ITEMS}, "zepto")
    assert [p["action"] for p in plan] == ["add", "ask", "ask", "skip", "skip"]
    a = plan[0]
    assert (a["idx"], a["label"], a["product_id"], a["name"], a["packs"], a["price"]) == \
        (0, "milk 1L", "p1", "Amul Taaza Toned Milk 500 ml", 2, 29.0)
    sw = plan[1]
    assert sw["question"] == "Lay's Classic Salted 52 g is out of stock on Zepto. Add Lay's Magic Masala 52 g (₹20) instead?"
    assert sw["choices"] == [
        {"id": "p9", "label": "Add Lay's Magic Masala 52 g · ₹20", "product_id": "p9", "name": "Lay's Magic Masala 52 g",
         "packs": 1, "price": 20.0},
        {"id": "skip", "label": "Skip this item"}]
    pk = plan[2]
    assert pk["question"] == "Which butter should we add?"
    assert [c["label"] for c in pk["choices"]] == ["Amul Butter 100 g · 100 g · ₹56",
                                                   "Amul Butter 500 g · 500 g · ₹540 ×2", "Skip this item"]
    assert pk["choices"][1] == {"id": "c2", "label": "Amul Butter 500 g · 500 g · ₹540 ×2", "product_id": "c2",
                                "name": "Amul Butter 500 g", "packs": 2, "price": 270.0}
    assert (plan[3]["reason"], plan[4]["reason"]) == ("out_of_stock", "not_found")
    assert plan[3]["product_id"] is None and plan[3]["question"] is None


def test_plan_pick_keeps_at_most_five_candidates():
    cands = [dict(product(f"c{i}", f"Butter {i}", 10.0 + i, "100 g"), packs=1, line_total=10.0 + i) for i in range(7)]
    res = {"items": [{"idx": 0, "status": "pick", "candidates": cands}]}
    plan = fill.plan_items({"items": [{"text": "butter"}], "choices": {}}, res, "blinkit")
    assert len(plan[0]["choices"]) == 6 and plan[0]["choices"][-1]["id"] == "skip"


def test_plan_uses_choice():
    req = {"items": REQ_ITEMS, "choices": {"2": {"zepto": "c2", "blinkit": "zz"}}}
    plan = fill.plan_items(req, {"items": RESULT_ITEMS}, "zepto")
    assert plan[2]["action"] == "add"
    assert (plan[2]["product_id"], plan[2]["name"], plan[2]["packs"], plan[2]["price"]) == \
        ("c2", "Amul Butter 500 g", 2, 270.0)
    # A choice made for another app does not apply here.
    assert fill.plan_items(req, {"items": RESULT_ITEMS}, "blinkit")[2]["action"] == "ask"


def test_db_site():
    assert (fill.db_site("instamart"), fill.db_site("zepto")) == ("swiggy", "zepto")


# --- start_fill --------------------------------------------------------------------------------------------------

def test_start_fill_consumes_one_and_enqueues(pg, s):
    rid = ready(pg)
    out = fill.start_fill(pg, A, rid, "zepto", NOW, s)
    assert out["fills_left"] == 4
    job = pg.execute("select type, site, profile, payload, status from jobs where id = %s", (out["job_id"],)).fetchone()
    assert job == ("fill_cart", "zepto", "zepto", {"fill_id": out["fill_id"]}, "queued")
    f = fill_row(pg, out["fill_id"])
    assert (f["status"], f["job_id"], f["user_id"], f["request_id"], f["counted"]) == \
        ("queued", out["job_id"], A, rid, True)
    assert f["priced_location"] == {"lat": 22.33, "lon": 73.16, "area": "Gorwa, Vadodara"}
    assert [p["action"] for p in f["plan"]] == ["add", "ask", "ask", "skip", "skip"]
    assert usage_row(pg, A)["cart_fills"] == 1


def test_instamart_fill_locks_swiggy_profile_and_needs_swiggy(pg, s):
    rid = make_request(pg)
    make_result(pg, rid, "instamart")
    cart_recipe(pg, "instamart")
    connect(pg, "instamart")                    # an "instamart" connection row is not the Swiggy login
    with pytest.raises(fill.NeedsConnection) as e:
        fill.start_fill(pg, A, rid, "instamart", NOW, s)
    assert e.value.message == "Connect Swiggy first. Instamart uses your Swiggy login."
    connect(pg, "swiggy", status="needs_login")
    with pytest.raises(fill.NeedsConnection) as e:
        fill.start_fill(pg, A, rid, "instamart", NOW, s)
    assert e.value.message == "Swiggy logged you out. Reconnect it first."
    connect(pg, "swiggy")
    out = fill.start_fill(pg, A, rid, "instamart", NOW, s)
    assert pg.execute("select site, profile from jobs where id = %s", (out["job_id"],)).fetchone() == \
        ("instamart", "swiggy")
    assert usage_row(pg, A)["cart_fills"] == 1


def test_needs_connection_copy_for_other_apps(pg, s):
    rid = make_request(pg)
    make_result(pg, rid, "blinkit")
    cart_recipe(pg, "blinkit")
    with pytest.raises(fill.NeedsConnection) as e:
        fill.start_fill(pg, A, rid, "blinkit", NOW, s)
    assert str(e.value) == e.value.message == "Connect Blinkit first."
    connect(pg, "blinkit", status="needs_login")
    with pytest.raises(fill.NeedsConnection) as e:
        fill.start_fill(pg, A, rid, "blinkit", NOW, s)
    assert e.value.message == "Blinkit logged you out. Reconnect it first."
    assert usage_row(pg, A)["cart_fills"] == 0


def test_second_fill_same_check_is_already_filled_and_not_counted(pg, s):
    rid = ready(pg)
    fill.start_fill(pg, A, rid, "zepto", NOW, s)
    with pytest.raises(fill.AlreadyFilled):
        fill.start_fill(pg, A, rid, "zepto", NOW, s)
    assert usage_row(pg, A)["cart_fills"] == 1


def test_unique_index_race_is_already_filled_and_rolls_back(pg, s, monkeypatch):
    """Two taps that both pass the pre-check: the index refuses the second, and its consume is rolled back."""
    rid = ready(pg)
    fill.start_fill(pg, A, rid, "zepto", NOW, s)
    pg.execute("update jobs set status = 'done'")          # free the job slot so only the fill index can refuse
    monkeypatch.setattr(fill, "_slot_taken", lambda *a, **k: False)
    with pytest.raises(fill.AlreadyFilled):
        fill.start_fill(pg, A, rid, "zepto", NOW, s)
    assert usage_row(pg, A)["cart_fills"] == 1
    assert pg.execute("select count(*) from cart_fills").fetchone()[0] == 1
    assert pg.execute("select count(*) from jobs").fetchone()[0] == 1


def test_running_fill_job_is_fill_running_and_rolls_back(pg, s):
    rid = ready(pg)
    rid2 = make_request(pg)
    make_result(pg, rid2, "zepto")
    fill.start_fill(pg, A, rid, "zepto", NOW, s)
    with pytest.raises(fill.FillRunning):
        fill.start_fill(pg, A, rid2, "zepto", NOW, s)
    assert usage_row(pg, A)["cart_fills"] == 1
    assert pg.execute("select count(*) from cart_fills").fetchone()[0] == 1


def test_stale_prices_refused(pg, s):
    rid = ready(pg, priced_at=NOW - timedelta(minutes=31))
    pg.execute("update cart_results set priced_at = %s", (NOW - timedelta(minutes=31),))
    with pytest.raises(fill.Stale):
        fill.start_fill(pg, A, rid, "zepto", NOW, s)
    assert usage_row(pg, A)["cart_fills"] == 0


def test_food_request_not_fillable(pg, s):
    rid = make_request(pg, mode="food", sites=("swiggy", "zomato"))
    make_result(pg, rid, "swiggy")
    make_result(pg, rid, "zomato")
    with pytest.raises(fill.FoodNotFillable):
        fill.start_fill(pg, A, rid, "swiggy", NOW, s)
    opts = fill.fill_options(pg, A, rid, NOW, s)
    assert set(opts["sites"]) == {"swiggy", "zomato"}
    assert opts["sites"]["swiggy"] == {"state": "unavailable", "label": "Fill cart on Swiggy",
                                       "sublabel": "Filling isn't available for food orders.", "href": None,
                                       "fill_id": None, "preview": None}
    assert opts["sites"]["zomato"]["label"] == "Fill cart on Zomato"


def test_start_fill_order_of_refusals(pg, s):
    with pytest.raises(fill.NotFound):
        fill.start_fill(pg, A, "00000000-0000-0000-0000-000000000123", "zepto", NOW, s)
    rid = ready(pg)
    with pytest.raises(fill.NotFound):
        fill.start_fill(pg, B, rid, "zepto", NOW, s)
    pg.execute("update cart_requests set status = 'searching'")
    with pytest.raises(fill.NotReady):
        fill.start_fill(pg, A, rid, "zepto", NOW, s)
    pg.execute("update cart_requests set status = 'done'")
    with pytest.raises(fill.NoPrices):
        fill.start_fill(pg, A, rid, "blinkit", NOW, s)          # no card
    make_result(pg, rid, "blinkit", status="unserviceable")
    with pytest.raises(fill.NoPrices):
        fill.start_fill(pg, A, rid, "blinkit", NOW, s)
    assert usage_row(pg, A)["cart_fills"] == 0


def test_nothing_to_add(pg, s):
    rid = make_request(pg, items=[{"text": "eggs"}])
    make_result(pg, rid, items=[missing(0, "out_of_stock")])
    cart_recipe(pg)
    connect(pg)
    with pytest.raises(fill.NothingToAdd):
        fill.start_fill(pg, A, rid, "zepto", NOW, s)
    assert usage_row(pg, A)["cart_fills"] == 0


def test_unavailable_without_approved_cart_recipe(pg, s):
    rid = make_request(pg)
    make_result(pg, rid)
    connect(pg)
    assert fill.cart_recipe_ready(pg, "zepto") is False
    with pytest.raises(fill.Unavailable):
        fill.start_fill(pg, A, rid, "zepto", NOW, s)
    cart_recipe(pg, healthy=False)
    assert fill.cart_recipe_ready(pg, "zepto") is False
    with pytest.raises(fill.Unavailable):
        fill.start_fill(pg, A, rid, "zepto", NOW, s)
    cart_recipe(pg, paused=True)
    assert fill.cart_recipe_ready(pg, "zepto") is False
    with pytest.raises(fill.Unavailable):
        fill.start_fill(pg, A, rid, "zepto", NOW, s)
    cart_recipe(pg)
    assert fill.cart_recipe_ready(pg, "zepto") is True
    assert usage_row(pg, A)["cart_fills"] == 0


def test_sixth_fill_is_limit(pg, s):
    rid = ready(pg)
    usage.record(pg, A, TODAY, cart_fills=5)
    with pytest.raises(limits.LimitExceeded) as e:
        fill.start_fill(pg, A, rid, "zepto", NOW, s)
    assert (e.value.kind, e.value.limit, e.value.message) == \
        ("fill", 5, "You can fill 5 carts a day. Try again tomorrow.")
    assert usage_row(pg, A)["cart_fills"] == 5
    assert pg.execute("select count(*) from cart_fills").fetchone()[0] == 0


def test_fill_paused_budget(pg, s):
    rid = ready(pg)
    pause_budget(pg)
    with pytest.raises(limits.BudgetPaused) as e:
        fill.start_fill(pg, A, rid, "zepto", NOW, s)
    assert (e.value.kind, e.value.resumes_on, e.value.message) == \
        ("fill", date(2026, 11, 1), "Filling carts is paused until 1 November.")
    assert pg.execute("select count(*) from cart_fills").fetchone()[0] == 0


# --- life cycle --------------------------------------------------------------------------------------------------

def outcome(status, items, error=None, cart=None, **k):
    out = {"status": status, "error": error, "existing_changed": False, "items": items, "cart": cart,
           "app_location": None, "location_selected": False, "repriced": None}
    out.update(k)
    return out


def res_item(idx, status, name="Amul Taaza Toned Milk 500 ml", packs=2, added_qty=0, skip_reason=None):
    return {"idx": idx, "label": REQ_ITEMS[idx]["text"], "name": name, "product_id": "p1", "packs": packs,
            "status": status, "added_qty": added_qty, "skip_reason": skip_reason}


def test_mark_started_and_record_item_replaces_by_idx(pg, s):
    rid = ready(pg)
    fid = fill.start_fill(pg, A, rid, "zepto", NOW, s)["fill_id"]
    fill.mark_started(pg, fid)
    f = fill_row(pg, fid)
    assert f["status"] == "filling" and f["started_at"] is not None
    assert [(i["idx"], i["status"]) for i in f["items"]] == [(i, "pending") for i in range(5)]
    assert f["items"][0]["label"] == "milk 1L"
    fill.record_item(pg, fid, res_item(0, "added", added_qty=2))
    fill.record_item(pg, fid, res_item(3, "skipped", skip_reason="out_of_stock"))
    fill.record_item(pg, fid, res_item(0, "partial", added_qty=1))
    items = fill_row(pg, fid)["items"]
    assert len(items) == 5
    assert [(i["idx"], i["status"]) for i in items] == \
        [(0, "partial"), (1, "pending"), (2, "pending"), (3, "skipped"), (4, "pending")]


def test_record_item_replaces_by_idx(pg, s):
    rid = ready(pg)
    fid = fill.start_fill(pg, A, rid, "zepto", NOW, s)["fill_id"]
    fill.record_item(pg, fid, res_item(1, "added", added_qty=1))     # no pending rows yet: appended
    fill.record_item(pg, fid, res_item(1, "not_added"))
    assert [(i["idx"], i["status"]) for i in fill_row(pg, fid)["items"]] == [(1, "not_added")]


def test_finish_writes_outcome_and_summary(pg, s):
    rid = ready(pg)
    fid = fill.start_fill(pg, A, rid, "zepto", NOW, s)["fill_id"]
    fill.mark_started(pg, fid)
    cart = {"subtotal": 58, "total": 73, "fees": {"handling": 15}, "line_count": 1, "existing_count": 0}
    summary = fill.finish_fill(pg, fid, outcome("done", [res_item(0, "added", added_qty=2)], cart=cart,
                                                location_selected=True,
                                                app_location={"lat": 22.33, "lon": 73.16, "store_id": "S1"}),
                               OPEN_APP_URLS["zepto"], s)
    assert summary["title"] == "Added 1 item to your Zepto cart"
    assert summary["location_line"] == "We set Zepto's delivery location to Gorwa, Vadodara."
    f = fill_row(pg, fid)
    assert (f["status"], f["open_url"], f["cart"], f["location_selected"], f["counted"]) == \
        ("done", "https://www.zepto.com/", cart, True, True)
    assert f["finished_at"] is not None and f["summary"] == summary
    assert f["app_location"]["store_id"] == "S1"
    assert usage_row(pg, A)["cart_fills"] == 1


def test_finish_refunds_when_nothing_added(pg, s):
    rid = ready(pg)
    fid = fill.start_fill(pg, A, rid, "zepto", NOW, s)["fill_id"]
    assert usage_row(pg, A)["cart_fills"] == 1
    summary = fill.finish_fill(pg, fid, outcome("failed", [res_item(0, "not_added")], error="nothing_added"),
                               OPEN_APP_URLS["zepto"], s)
    assert summary["counted"] is False
    f = fill_row(pg, fid)
    assert (f["status"], f["error"], f["counted"]) == ("failed", "nothing_added", False)
    assert usage_row(pg, A)["cart_fills"] == 0
    # Finishing again never refunds twice.
    fill.finish_fill(pg, fid, outcome("failed", [], error="nothing_added"), OPEN_APP_URLS["zepto"], s)
    assert usage_row(pg, A)["cart_fills"] == 0


def test_needs_confirmation_is_refunded(pg, s):
    rid = ready(pg)
    fid = fill.start_fill(pg, A, rid, "zepto", NOW, s)["fill_id"]
    fill.finish_fill(pg, fid, outcome("needs_confirmation", [], repriced=[]), OPEN_APP_URLS["zepto"], s)
    assert fill_row(pg, fid)["counted"] is False and usage_row(pg, A)["cart_fills"] == 0


def test_partial_is_counted(pg, s):
    rid = ready(pg)
    fid = fill.start_fill(pg, A, rid, "zepto", NOW, s)["fill_id"]
    fill.finish_fill(pg, fid, outcome("partial", [res_item(0, "partial", added_qty=1), res_item(1, "not_added")]),
                     OPEN_APP_URLS["zepto"], s)
    assert fill_row(pg, fid)["counted"] is True and usage_row(pg, A)["cart_fills"] == 1


def test_engine_stop_with_unknown_cart_is_not_refunded(pg, s):
    """redirected_write / cart_read_failed / existing_changed / cart_changed: adds may be in the app's cart."""
    rid = ready(pg)
    fid = fill.start_fill(pg, A, rid, "zepto", NOW, s)["fill_id"]
    summary = fill.finish_fill(pg, fid, outcome("failed", [res_item(0, "not_added")], error="redirected_write"),
                               OPEN_APP_URLS["zepto"], s)
    assert summary["open"] == {"label": "Open Zepto to pay", "url": "https://www.zepto.com/"}
    assert fill_row(pg, fid)["counted"] is True and usage_row(pg, A)["cart_fills"] == 1


def test_refund_uses_ist_day_of_created_at(pg, s):
    late = datetime(2026, 10, 27, 18, 0, tzinfo=timezone.utc)          # 23:30 IST on the 27th
    rid = ready(pg, priced_at=late - timedelta(minutes=5))
    pg.execute("update cart_results set priced_at = %s", (late - timedelta(minutes=5),))
    fid = fill.start_fill(pg, A, rid, "zepto", late, s)["fill_id"]
    usage.record(pg, A, TODAY, cart_fills=2)
    fill.finish_fill(pg, fid, outcome("failed", [], error="nothing_added"), OPEN_APP_URLS["zepto"], s)
    rows = dict(pg.execute("select day, cart_fills from usage where user_id = %s", (A,)).fetchall())
    assert rows == {date(2026, 10, 27): 0, TODAY: 2}


def test_fail_fill_uses_current_items(pg, s):
    rid = ready(pg)
    fid = fill.start_fill(pg, A, rid, "zepto", NOW, s)["fill_id"]
    fill.mark_started(pg, fid)
    fill.record_item(pg, fid, res_item(0, "added", added_qty=2))
    summary = fill.fail_fill(pg, fid, "needs_login", s)
    f = fill_row(pg, fid)
    assert (f["status"], f["error"], f["counted"]) == ("failed", "needs_login", True)
    assert [i["status"] for i in f["items"]] == ["added", "not_added", "not_added", "not_added", "not_added"]
    assert summary["error_line"] == ("Zepto logged you out. Reconnect it, then try again. "
                                     "Some items were added before it stopped.")
    assert summary["open"]["url"] == "https://www.zepto.com/"


def test_fail_fill_before_start_refunds(pg, s):
    rid = ready(pg)
    fid = fill.start_fill(pg, A, rid, "zepto", NOW, s)["fill_id"]
    summary = fill.fail_fill(pg, fid, "cart_unavailable", s)
    assert summary["error_line"] == "Something went wrong. Nothing was added."
    assert fill_row(pg, fid)["counted"] is False and usage_row(pg, A)["cart_fills"] == 0


# --- confirm -----------------------------------------------------------------------------------------------------

def _needs_confirmation(pg, s, finished=NOW - timedelta(minutes=2)):
    rid = ready(pg)
    fid = fill.start_fill(pg, A, rid, "zepto", NOW - timedelta(minutes=3), s)["fill_id"]
    app = {"lat": 22.31, "lon": 73.17, "store_id": "S2", "area": "Alkapuri"}
    fill.finish_fill(pg, fid, outcome("needs_confirmation", [], app_location=app,
                                      repriced=[{"idx": 0, "name": "Milk", "product_id": "p1", "old_price": 58.0,
                                                 "new_price": 62.0, "in_stock": True}]),
                     OPEN_APP_URLS["zepto"], s)
    pg.execute("update cart_fills set finished_at = %s where id = %s", (finished, fid))
    pg.execute("update jobs set status = 'done'")
    return rid, fid, app


def test_confirm_creates_pinned_fill(pg, s):
    rid, fid, app = _needs_confirmation(pg, s)
    assert usage_row(pg, A)["cart_fills"] == 0          # the re-priced fill was refunded
    out = fill.confirm_fill(pg, A, fid, NOW, s)
    assert out["fills_left"] == 4 and usage_row(pg, A)["cart_fills"] == 1
    f = fill_row(pg, out["fill_id"])
    old = fill_row(pg, fid)
    assert (f["status"], f["confirm_location"], f["confirmed_from"], f["request_id"], f["site"]) == \
        ("queued", app, fid, rid, "zepto")
    assert f["plan"] == old["plan"] and f["priced_location"] == old["priced_location"]
    assert pg.execute("select payload from jobs where id = %s", (out["job_id"],)).fetchone()[0] == \
        {"fill_id": out["fill_id"]}
    # Confirming the same one again: it is no longer the newest for its check and app.
    with pytest.raises(fill.NotConfirmable):
        fill.confirm_fill(pg, A, fid, NOW, s)


def test_confirm_skips_the_staleness_check(pg, s):
    rid, fid, _ = _needs_confirmation(pg, s)
    pg.execute("update cart_results set priced_at = %s", (NOW - timedelta(hours=2),))
    assert fill.confirm_fill(pg, A, fid, NOW, s)["fill_id"]


def test_confirm_after_window_refused(pg, s):
    _, fid, _ = _needs_confirmation(pg, s, finished=NOW - timedelta(seconds=601))
    with pytest.raises(fill.NotConfirmable):
        fill.confirm_fill(pg, A, fid, NOW, s)
    assert usage_row(pg, A)["cart_fills"] == 0


def test_confirm_refusals(pg, s):
    _, fid, _ = _needs_confirmation(pg, s)
    with pytest.raises(fill.NotConfirmable):
        fill.confirm_fill(pg, B, fid, NOW, s)                    # not the owner
    with pytest.raises(fill.NotConfirmable):
        fill.confirm_fill(pg, A, "00000000-0000-0000-0000-000000000123", NOW, s)
    cart_recipe(pg, paused=True)
    with pytest.raises(fill.Unavailable):
        fill.confirm_fill(pg, A, fid, NOW, s)
    cart_recipe(pg)
    connect(pg, status="needs_login")
    with pytest.raises(fill.NeedsConnection):
        fill.confirm_fill(pg, A, fid, NOW, s)
    connect(pg)
    usage.record(pg, A, TODAY, cart_fills=5)
    with pytest.raises(limits.LimitExceeded):
        fill.confirm_fill(pg, A, fid, NOW, s)
    pg.execute("update cart_fills set status = 'failed' where id = %s", (fid,))
    pg.execute("update usage set cart_fills = 0")
    with pytest.raises(fill.NotConfirmable):
        fill.confirm_fill(pg, A, fid, NOW, s)


# --- fill_options ------------------------------------------------------------------------------------------------

def card(opts, site="zepto"):
    return opts["sites"][site]


def test_fill_options_states_and_copy(pg, s):
    rid = make_request(pg)
    make_result(pg, rid, "zepto")
    make_result(pg, rid, "blinkit", status="unserviceable")      # only ok cards get a button
    make_result(pg, rid, "instamart")

    def opts():
        return fill.fill_options(pg, A, rid, NOW, s)

    o = opts()
    assert set(o["sites"]) == {"zepto", "instamart"}
    assert (o["fills_left"], o["fills_limit"]) == (5, 5)
    # 4. unavailable: no approved cart recipe
    assert card(o) == {"state": "unavailable", "label": "Fill cart on Zepto", "sublabel": "Not available for Zepto yet",
                       "href": None, "fill_id": None, "preview": None}
    cart_recipe(pg, "zepto")
    cart_recipe(pg, "instamart")
    # 6. connect
    assert {k: card(opts())[k] for k in ("state", "label", "sublabel", "href")} == \
        {"state": "connect", "label": "Connect Zepto to fill", "sublabel": None, "href": "/accounts"}
    assert card(opts(), "instamart")["label"] == "Connect Swiggy to fill"
    # 7. needs_login
    connect(pg, "zepto", status="needs_login")
    connect(pg, "swiggy", status="needs_login")
    assert {k: card(opts())[k] for k in ("state", "label", "href")} == \
        {"state": "needs_login", "label": "Reconnect Zepto to fill", "href": "/accounts"}
    assert card(opts(), "instamart")["label"] == "Reconnect Swiggy to fill"
    connect(pg, "zepto")
    connect(pg, "swiggy")
    # 10. ready
    c = card(opts())
    assert (c["state"], c["label"], c["sublabel"], c["href"], c["fill_id"]) == \
        ("ready", "Fill cart on Zepto", "5 fills left today", None, None)
    assert c["preview"] == {"add": ["milk 1L"], "ask": ["lays classic", "butter"], "skip": ["eggs", "saffron"]}
    usage.record(pg, A, TODAY, cart_fills=4)
    assert card(opts())["sublabel"] == "1 fill left today"
    # 9. limit
    usage.record(pg, A, TODAY, cart_fills=1)
    o = opts()
    assert o["fills_left"] == 0
    assert {k: card(o)[k] for k in ("state", "label", "sublabel", "preview")} == \
        {"state": "limit", "label": "Fill cart on Zepto", "sublabel": "No fills left today", "preview": None}
    # 8. stale beats limit
    pg.execute("update cart_results set priced_at = %s where site = 'zepto'", (NOW - timedelta(minutes=31),))
    assert {k: card(opts())[k] for k in ("state", "label", "sublabel")} == \
        {"state": "stale", "label": "Fill cart on Zepto",
         "sublabel": "Prices are over 30 minutes old. Check again first."}
    # 7 beats 8
    connect(pg, "zepto", status="needs_login")
    assert card(opts())["state"] == "needs_login"
    # 5. paused beats connect / needs_login
    pause_budget(pg)
    assert {k: card(opts())[k] for k in ("state", "label", "sublabel", "href")} == \
        {"state": "paused", "label": "Fill cart on Zepto", "sublabel": "Filling carts is paused until 1 November.",
         "href": None}
    # 4. unavailable beats paused
    cart_recipe(pg, "zepto", paused=True)
    assert card(opts())["state"] == "unavailable"


def test_fill_options_fill_states(pg, s):
    rid = ready(pg)
    fid = fill.start_fill(pg, A, rid, "zepto", NOW, s)["fill_id"]
    o = fill.fill_options(pg, A, rid, NOW, s)
    assert o["fills_left"] == 4
    # 1. filling (queued counts as filling)
    assert card(o) == {"state": "filling", "label": "Filling your cart…", "sublabel": None, "href": None,
                       "fill_id": fid, "preview": None}
    fill.mark_started(pg, fid)
    assert card(fill.fill_options(pg, A, rid, NOW, s))["state"] == "filling"
    # 3. filled
    fill.finish_fill(pg, fid, outcome("partial", [res_item(0, "added", added_qty=2)]), OPEN_APP_URLS["zepto"], s)
    c = card(fill.fill_options(pg, A, rid, NOW, s))
    assert (c["state"], c["label"], c["fill_id"]) == ("filled", "See what was added", fid)
    # filled beats unavailable / paused
    cart_recipe(pg, paused=True)
    pause_budget(pg)
    assert card(fill.fill_options(pg, A, rid, NOW, s))["state"] == "filled"


def test_fill_options_review_state(pg, s):
    rid, fid, _ = _needs_confirmation(pg, s)
    c = card(fill.fill_options(pg, A, rid, NOW, s))
    assert (c["state"], c["label"], c["sublabel"], c["fill_id"]) == ("review", "See new prices", None, fid)
    # Outside the window it is a normal card again.
    pg.execute("update cart_fills set finished_at = %s", (NOW - timedelta(seconds=601),))
    assert card(fill.fill_options(pg, A, rid, NOW, s))["state"] == "ready"


def test_fill_options_failed_fill_can_fill_again(pg, s):
    rid = ready(pg)
    fid = fill.start_fill(pg, A, rid, "zepto", NOW, s)["fill_id"]
    fill.fail_fill(pg, fid, "cart_unavailable", s)
    assert card(fill.fill_options(pg, A, rid, NOW, s))["state"] == "ready"


def test_fill_options_owner_only(pg, s):
    rid = ready(pg)
    with pytest.raises(fill.NotFound):
        fill.fill_options(pg, B, rid, NOW, s)


# --- lost fills --------------------------------------------------------------------------------------------------

def test_finalize_lost_started_fill_is_interrupted(pg, s):
    rid = ready(pg)
    out = fill.start_fill(pg, A, rid, "zepto", NOW, s)
    fill.mark_started(pg, out["fill_id"])
    fill.record_item(pg, out["fill_id"], res_item(0, "added", added_qty=2))
    pg.execute("update jobs set status = 'failed', error = 'worker_lost' where id = %s", (out["job_id"],))
    assert fill.finalize_lost(pg, s, NOW) == 1
    f = fill_row(pg, out["fill_id"])
    assert (f["status"], f["error"], f["counted"]) == ("interrupted", "worker_lost", True)
    assert f["finished_at"] is not None
    assert f["summary"]["title"] == "Your Zepto cart may be half filled"
    assert f["summary"]["error_line"] == "We lost track of this fill. Check your Zepto cart before filling again."
    assert usage_row(pg, A)["cart_fills"] == 1
    # It still holds the slot: the same check can't be filled again.
    with pytest.raises(fill.AlreadyFilled):
        fill.start_fill(pg, A, rid, "zepto", NOW, s)
    assert fill.finalize_lost(pg, s, NOW) == 0


def test_finalize_lost_queued_fill_is_failed_and_refunded(pg, s):
    rid = ready(pg)
    out = fill.start_fill(pg, A, rid, "zepto", NOW, s)
    pg.execute("delete from jobs where id = %s", (out["job_id"],))      # the job is missing
    assert fill.finalize_lost(pg, s, NOW) == 1
    f = fill_row(pg, out["fill_id"])
    assert (f["status"], f["error"], f["counted"]) == ("failed", "worker_lost", False)
    assert usage_row(pg, A)["cart_fills"] == 0


def test_finalize_lost_leaves_live_fills(pg, s):
    rid = ready(pg)
    out = fill.start_fill(pg, A, rid, "zepto", NOW, s)
    assert fill.finalize_lost(pg, s, NOW) == 0
    pg.execute("update jobs set status = 'running' where id = %s", (out["job_id"],))
    fill.mark_started(pg, out["fill_id"])
    assert fill.finalize_lost(pg, s, NOW) == 0
    assert fill_row(pg, out["fill_id"])["status"] == "filling"


def test_get_fill_owner_filter(pg, s):
    rid = ready(pg)
    fid = fill.start_fill(pg, A, rid, "zepto", NOW, s)["fill_id"]
    assert fill.get_fill(pg, fid, A)["id"] == fid
    assert fill.get_fill(pg, fid, B) is None
    assert fill.get_fill(pg, "not-a-uuid") is None

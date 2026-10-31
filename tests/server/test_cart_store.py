"""Cart store (Plan 5 T6): connected-app access, item lists, the usual items, checks, finalising and choices."""
import dataclasses
import threading
from datetime import datetime, timedelta, timezone

import pytest
from helpers import A, B

from server import cart, jobs
from server.config import Settings

NOW = datetime(2026, 10, 28, 6, 0, tzinfo=timezone.utc)   # 11:30 IST
DAY = NOW.date()
HOME = (22.3310, 73.1510)
ADDRESS = "12 Main Road, Gorwa, Vadodara, Gujarat 390016"


@pytest.fixture
def s():
    return Settings()


def connect(pg, user, site, status="connected", synced=None):
    pg.execute("insert into connections (user_id, site, status, last_synced_at) values (%s,%s,%s,%s)",
               (user, site, status, synced))


def recipe(pg, site, kind="search", healthy=True, paused=False):
    pg.execute("insert into recipes (site, kind, recipe, healthy, paused) values (%s,%s,'{}',%s,%s)",
               (site, kind, healthy, paused))


def profile(pg, user, lat=HOME[0], lon=HOME[1], address=ADDRESS):
    pg.execute("insert into profiles (user_id, address, lat, lon) values (%s,%s,%s,%s)", (user, address, lat, lon))


def saved(pg, user, site, lat=HOME[0], lon=HOME[1], address_id=None):
    pg.execute("insert into app_addresses (user_id, site, address_id, label, line, lat, lon, is_default) "
               "values (%s,%s,%s,'Home','12 Main Road',%s,%s,true)",
               (user, site, address_id or f"{site}-home", lat, lon))


def grocery_user(pg, user=A, sites=("zepto", "swiggy")):
    """A user with a priced address and these accounts connected, every search recipe healthy."""
    profile(pg, user)
    for site in ("zepto", "blinkit", "instamart"):
        if not pg.execute("select 1 from recipes where site=%s and kind='search'", (site,)).fetchone():
            recipe(pg, site)
    for site in sites:
        connect(pg, user, site)
        saved(pg, user, site)


ITEMS = [{"text": "Amul Taza Milk 500ml", "qty": 2}, {"text": "Farm Eggs 6 pcs", "qty": 1}]


def matched(idx, name, line_total, packs=1):
    return {"idx": idx, "status": "matched", "confidence": 0.9, "packs": packs, "line_total": line_total,
            "product": {"product_id": f"p{idx}", "name": name, "price": line_total / packs}, "swap_from": None,
            "candidates": []}


def cart_checks(pg, user=A):
    row = pg.execute("select cart_checks from usage where user_id=%s", (user,)).fetchone()
    return row[0] if row else 0


# --- access -----------------------------------------------------------------------------------------------------

def test_site_access_statuses(pg):
    for site in ("zepto", "blinkit", "instamart"):
        recipe(pg, site)
    connect(pg, A, "zepto")
    connect(pg, A, "blinkit", "needs_login")
    connect(pg, A, "swiggy", "error", synced=NOW)
    kinds = {s: ("search",) for s in cart.SEARCH_SITES}
    assert cart.site_access(pg, A, cart.SEARCH_SITES, kinds) == {
        "zepto": "ok", "blinkit": "needs_login", "instamart": "ok"}

    pg.execute("update recipes set healthy=false where site='zepto'")
    pg.execute("update recipes set paused=true where site='instamart'")
    assert cart.site_access(pg, A, cart.SEARCH_SITES, kinds) == {
        "zepto": "unavailable", "blinkit": "needs_login", "instamart": "unavailable"}

    # error without a past sync, disconnected, no row: all not_connected; syncing counts as connected
    connect(pg, B, "zepto", "error")
    connect(pg, B, "blinkit", "disconnected")
    assert cart.site_access(pg, B, cart.SEARCH_SITES, kinds) == {
        "zepto": "not_connected", "blinkit": "not_connected", "instamart": "not_connected"}
    pg.execute("update connections set status='syncing' where user_id=%s and site='blinkit'", (B,))
    assert cart.site_access(pg, B, ("blinkit",), kinds) == {"blinkit": "ok"}


def test_site_access_needs_every_kind(pg):
    connect(pg, A, "swiggy")
    connect(pg, A, "zomato")
    recipe(pg, "swiggy", "restaurants")
    recipe(pg, "swiggy", "menu")
    recipe(pg, "zomato", "restaurants")   # no menu recipe yet
    kinds = {"swiggy": ("restaurants", "menu"), "zomato": ("restaurants", "menu")}
    assert cart.site_access(pg, A, ("swiggy", "zomato"), kinds) == {"swiggy": "ok", "zomato": "unavailable"}


# --- items ------------------------------------------------------------------------------------------------------

def test_normalize_merges_duplicate_lines():
    got = cart.normalize_items([{"text": " Amul  Milk ", "qty": 2}, {"text": "amul milk!", "qty": 19},
                                {"text": "Bread", "qty": 1}, {"text": "  ", "qty": 3}], 30)
    assert got == [{"text": "Amul Milk", "qty": 20}, {"text": "Bread", "qty": 1}]
    assert cart.normalize_items([{"text": "a", "qty": 1}, {"text": "A", "qty": 1}], 1) == [{"text": "a", "qty": 2}]
    with pytest.raises(ValueError, match=r"^Keep the list to 30 items or fewer\.$"):
        cart.normalize_items([{"text": f"item {i}", "qty": 1} for i in range(31)], 30)


def _order(pg, user, site, oid, days_ago, names, status="delivered", sub=None):
    placed = (NOW + timedelta(hours=5, minutes=30) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")
    pg.execute("insert into orders (user_id, site, order_id, sub_platform, placed_at, status) "
               "values (%s,%s,%s,%s,%s,%s)", (user, site, oid, sub, placed, status))
    for i, (name, qty) in enumerate(names):
        pg.execute("insert into order_items (user_id, site, order_id, idx, name, qty) values (%s,%s,%s,%s,%s,%s)",
                   (user, site, oid, i, name, qty))


def test_usual_items_three_times_in_90_days_grocery_only(pg):
    _order(pg, A, "zepto", "z1", 5, [("Amul Taza Milk 500ml", 2), ("Farm Eggs", 1)])
    _order(pg, A, "blinkit", "b1", 20, [("amul taza milk 500ml", 1), ("Farm Eggs", 1)])
    _order(pg, A, "zepto", "z2", 1, [("Amul Taza Milk 500ml", 2), ("Farm Eggs", 1)], status="cancelled")
    _order(pg, A, "swiggy", "i1", 40, [("Amul Taza Milk 500ml", 2), ("Bread", 1)], sub="instamart")
    _order(pg, A, "swiggy", "i2", 41, [("Bread", 1)], sub="instamart")
    _order(pg, A, "swiggy", "i3", 100, [("Bread", 1)], sub="instamart")              # older than 90 days
    for n in range(3):                                                                 # Swiggy food never counts
        _order(pg, A, "swiggy", f"f{n}", n + 1, [("Chicken Biryani", 1)])
        _order(pg, A, "zomato", f"m{n}", n + 1, [("Chicken Biryani", 1)])
    _order(pg, B, "zepto", "bz", 2, [("Farm Eggs", 1)])                               # another user's order
    _order(pg, B, "zepto", "bz2", 3, [("Farm Eggs", 1)])

    assert cart.usual_items(pg, A, NOW) == [{"text": "Amul Taza Milk 500ml", "qty": 2, "times": 3}]
    _order(pg, A, "blinkit", "b2", 3, [("Farm Eggs", 2)])
    got = cart.usual_items(pg, A, NOW)
    assert [(g["text"], g["times"]) for g in got] == [("Amul Taza Milk 500ml", 3), ("Farm Eggs", 3)]
    assert got[1]["qty"] == 2
    assert cart.usual_items(pg, A, NOW, limit=1) == got[:1]


# --- starting a check -------------------------------------------------------------------------------------------

def test_start_check_enqueues_only_connected_sites(pg, s):
    grocery_user(pg)
    out = cart.start_check(pg, A, ITEMS, NOW, s)
    assert out["sites"] == ["zepto", "blinkit", "instamart"] and out["cart_checks_left"] == 9
    assert sorted(out["enqueue"]) == ["instamart", "zepto"]
    rows = pg.execute("select site, profile, type, payload from jobs order by site").fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("instamart", "swiggy", "search"), ("zepto", "zepto", "search")]
    for site, _, _, payload in rows:
        assert payload["op"] == "products" and payload["request_id"] == out["request_id"]
        assert payload["address_id"] == f"{cart.db_site(site)}-home"
    assert pg.execute("select site, status from cart_results").fetchall() == [("blinkit", "not_connected")]
    req = pg.execute("select mode, status, area, lat, lon, items, sites from cart_requests").fetchone()
    assert req == ("grocery", "searching", "Gorwa, Vadodara", HOME[0], HOME[1], ITEMS,
                   ["zepto", "blinkit", "instamart"])
    assert cart_checks(pg) == 1


def test_no_connected_app_is_409_and_not_counted(pg, s):
    profile(pg, A)
    for site in cart.SEARCH_SITES:
        recipe(pg, site)
    connect(pg, A, "zomato")
    with pytest.raises(cart.NoConnectedApp) as e:
        cart.start_check(pg, A, ITEMS, NOW, s)
    assert e.value.mode == "grocery"
    # every connected app's recipe broken: unavailable, also not counted
    connect(pg, A, "zepto")
    pg.execute("update recipes set healthy=false where site='zepto'")
    with pytest.raises(cart.Unavailable):
        cart.start_check(pg, A, ITEMS, NOW, s)
    assert cart_checks(pg) == 0 and pg.execute("select count(*) from cart_requests").fetchone()[0] == 0


def test_check_needs_address(pg, s):
    grocery_user(pg)
    pg.execute("update profiles set lat=null, lon=null")
    with pytest.raises(cart.NoAddress):
        cart.start_check(pg, A, ITEMS, NOW, s)
    assert cart_checks(pg) == 0


def test_needs_login_site_gets_row_without_job(pg, s):
    grocery_user(pg, sites=("zepto",))
    connect(pg, A, "blinkit", "needs_login")
    out = cart.start_check(pg, A, ITEMS, NOW, s)
    assert out["enqueue"] == ["zepto"]
    assert pg.execute("select site from jobs").fetchall() == [("zepto",)]
    assert sorted(pg.execute("select site, status from cart_results").fetchall()) == [
        ("blinkit", "needs_login"), ("instamart", "not_connected")]


def test_each_app_is_priced_at_its_own_saved_address(pg, s):
    grocery_user(pg, sites=("zepto",))
    connect(pg, A, "swiggy")
    saved(pg, A, "swiggy", lat=HOME[0] + 0.05, lon=HOME[1])          # 5 km away: not this place
    out = cart.start_check(pg, A, ITEMS, NOW, s)
    assert out["enqueue"] == ["zepto"]
    assert pg.execute("select status, error from cart_results where site='instamart'").fetchone() == (
        "unserviceable", "no_saved_address")
    # no connected app has a saved address here: refused before a check is used
    pg.execute("delete from jobs")
    pg.execute("delete from app_addresses where site='zepto'")
    before = cart_checks(pg)
    with pytest.raises(cart.NoSavedAddress):
        cart.start_check(pg, A, ITEMS, NOW, s)
    assert cart_checks(pg) == before


def test_check_running_is_refused_and_conflict_restores_the_check(pg, s):
    grocery_user(pg)
    cart.start_check(pg, A, ITEMS, NOW, s)
    with pytest.raises(cart.CheckRunning):
        cart.start_check(pg, A, ITEMS, NOW, s)
    assert cart_checks(pg) == 1
    # a lookup job (no request_id) on one app: the enqueue conflicts, everything rolls back
    pg.execute("delete from jobs")
    jobs.enqueue(pg, A, "search", "zepto", {"op": "restaurants"})
    with pytest.raises(cart.CheckRunning):
        cart.start_check(pg, A, ITEMS, NOW, s)
    assert cart_checks(pg) == 1
    assert pg.execute("select count(*) from cart_requests").fetchone()[0] == 1


# --- finalising -------------------------------------------------------------------------------------------------

def _started(pg, s, sites=("zepto", "swiggy")):
    grocery_user(pg, sites=sites)
    return cart.start_check(pg, A, ITEMS, NOW, s)["request_id"]


def test_finalize_waits_for_every_site_then_builds_summary(pg, s):
    rid = _started(pg, s)
    cart.record_site_result(pg, rid, "zepto", "ok", [matched(0, "Amul Taza Milk 500ml", 54, 2),
                                                     matched(1, "Farm Eggs 6 pcs", 50)], meta={"fee_info": {}})
    assert cart.finalize_if_complete(pg, rid, s) is False
    assert pg.execute("select status from cart_requests").fetchone()[0] == "searching"
    cart.record_site_result(pg, rid, "instamart", "unserviceable")
    assert cart.finalize_if_complete(pg, rid, s) is True
    status, summary, priced = pg.execute("select status, summary, priced_at from cart_requests").fetchone()
    assert status == "done" and priced is not None
    assert summary["best_site"] == "zepto" and summary["request_id"] == rid
    assert [c["site"] for c in summary["cards"]] == ["zepto", "blinkit", "instamart"]
    sub, total, est, fees = pg.execute(
        "select subtotal, total, fees_estimated, fees from cart_results where site='zepto'").fetchone()
    assert float(sub) == 104 and float(total) == summary["cards"][0]["total"] and est is True
    assert fees["total"] == summary["cards"][0]["fees"]["total"]
    assert cart.finalize_if_complete(pg, rid, s) is False          # already done
    assert cart_checks(pg) == 1


def test_finalize_treats_failed_job_without_result_as_failed(pg, s):
    rid = _started(pg, s)
    cart.record_site_result(pg, rid, "zepto", "ok", [matched(0, "Milk", 54, 2), matched(1, "Eggs", 50)])
    assert cart.finalize_if_complete(pg, rid, s) is False
    pg.execute("update jobs set status='failed', error='boom' where site='instamart'")
    assert cart.finalize_if_complete(pg, rid, s) is True
    assert pg.execute("select status, error from cart_results where site='instamart'").fetchone() == (
        "failed", "worker_lost")
    assert pg.execute("select status from cart_requests").fetchone()[0] == "done"


def test_all_sites_failed_refunds_check(pg, s):
    rid = _started(pg, s)
    assert cart_checks(pg) == 1
    cart.record_site_result(pg, rid, "zepto", "failed", error="recipe_drift")
    cart.record_site_result(pg, rid, "instamart", "needs_login")
    assert cart.finalize_if_complete(pg, rid, s) is True
    status, summary = pg.execute("select status, summary from cart_requests").fetchone()
    assert status == "failed" and summary["status"] == "failed" and summary["best_site"] is None
    assert cart_checks(pg) == 0
    assert cart.finalize_if_complete(pg, rid, s) is False and cart_checks(pg) == 0


def test_finalize_is_idempotent_under_race(pg, pg2, s):
    rid = _started(pg, s)
    cart.record_site_result(pg, rid, "zepto", "failed")
    cart.record_site_result(pg, rid, "instamart", "failed")
    out, barrier = [], threading.Barrier(2)

    def run(conn):
        barrier.wait()
        out.append(cart.finalize_if_complete(conn, rid, s))

    threads = [threading.Thread(target=run, args=(c,)) for c in (pg, pg2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(out) == [False, True]
    assert cart_checks(pg) == 0                                # refunded exactly once


def test_finalize_stale_uses_1500s(pg, s):
    assert s.cart_stale_s == 1500
    rid = _started(pg, s)
    cart.record_site_result(pg, rid, "zepto", "ok", [matched(0, "Milk", 54, 2), matched(1, "Eggs", 50)])
    assert cart.finalize_stale(pg, s, NOW + timedelta(minutes=20)) == 0
    assert pg.execute("select status from cart_requests").fetchone()[0] == "searching"
    assert cart.finalize_stale(pg, s, NOW + timedelta(minutes=26)) == 1
    assert pg.execute("select status, error from cart_results where site='instamart'").fetchone() == (
        "failed", "timeout")
    assert pg.execute("select status from cart_requests").fetchone()[0] == "done"
    assert cart.finalize_stale(pg, s, NOW + timedelta(minutes=40)) == 0


def test_stale_with_nothing_priced_fails_and_refunds(pg, s):
    _started(pg, s)
    assert cart.finalize_stale(pg, s, NOW + timedelta(minutes=26)) == 1
    assert pg.execute("select status, error from cart_requests").fetchone() == ("failed", "timeout")
    assert cart_checks(pg) == 0


# --- choices ----------------------------------------------------------------------------------------------------

def _pick(idx):
    return {"idx": idx, "status": "pick", "confidence": 0.6, "product": None, "packs": None, "line_total": None,
            "swap_from": None, "candidates": [
                {"product_id": "z-milk", "name": "Amul Taaza 500 ml", "price": 27, "packs": 2, "line_total": 54},
                {"product_id": "z-bikis", "name": "Milk Bikis 200 g", "price": 30, "packs": 1, "line_total": 30,
                 "in_stock": False}]}


def test_set_choice_recomputes_without_consuming(pg, s):
    rid = _started(pg, s, sites=("zepto",))
    with pytest.raises(cart.NotReady):
        cart.set_choice(pg, A, rid, 0, "zepto", "z-milk", s)
    cart.record_site_result(pg, rid, "zepto", "ok", [_pick(0), matched(1, "Farm Eggs 6 pcs", 50)])
    assert cart.finalize_if_complete(pg, rid, s) is True
    before = pg.execute("select summary from cart_requests").fetchone()[0]
    assert before["cards"][0]["to_pick"] == 1 and len(before["picks"]) == 1

    summary = cart.set_choice(pg, A, rid, 0, "zepto", "z-milk", s)
    assert summary["cards"][0]["to_pick"] == 0 and summary["picks"] == []
    assert summary["table"]["rows"][0]["cells"]["zepto"]["status"] == "chosen"
    assert summary["cards"][0]["subtotal"] == 104
    stored, choices = pg.execute("select summary, choices from cart_requests").fetchone()
    assert stored == summary and choices == {"0": {"zepto": "z-milk"}}
    assert float(pg.execute("select subtotal from cart_results where site='zepto'").fetchone()[0]) == 104
    assert cart_checks(pg) == 1

    for idx, site, cid in ((0, "zepto", "z-bikis"), (0, "zepto", "nope"), (1, "zepto", "z-milk"),
                           (9, "zepto", "z-milk"), (0, "blinkit", "z-milk"), (0, "amazon", "z-milk")):
        with pytest.raises(cart.BadChoice):
            cart.set_choice(pg, A, rid, idx, site, cid, s)
    with pytest.raises(cart.NotFound):
        cart.set_choice(pg, B, rid, 0, "zepto", "z-milk", s)


# --- fee rules --------------------------------------------------------------------------------------------------

GROCERY = {"delivery": [{"min_subtotal": 0, "fee": 30}, {"min_subtotal": 199, "fee": 0}], "handling": 12}
FOOD = {"kind": "food", "delivery_by_km": [{"max_km": 3, "fee": 30}, {"max_km": None, "fee": 60}],
        "packaging_per_item": 10, "platform": 15, "gst": {"food_pct": 5, "fees_pct": 18}}


def test_put_fee_rules_validates_food_and_grocery(pg, fee_rules_snapshot):
    pg.execute("insert into auth.users (id, email) values (%s, 'admin@x.test') "
               "on conflict (id) do update set email = excluded.email", (A,))
    assert set(cart.fee_rules(pg)) == {"zepto", "blinkit", "instamart", "swiggy", "zomato"}
    assert cart.put_fee_rules(pg, "zepto", GROCERY, True, A) == 2
    assert cart.fee_rules(pg)["zepto"] == (GROCERY, True)
    row = pg.execute("select source, updated_by from fee_rules where site='zepto'").fetchone()
    assert row == ("admin", __import__("uuid").UUID(A))
    assert cart.put_fee_rules(pg, "swiggy", FOOD, False, A) == 2
    assert cart.put_fee_rules(pg, "swiggy", FOOD, True, A) == 3

    with pytest.raises(cart.InvalidRules) as e:
        cart.put_fee_rules(pg, "zepto", FOOD, True, A)
    assert e.value.errors == ["zepto needs grocery fee rules"]
    with pytest.raises(cart.InvalidRules) as e:
        cart.put_fee_rules(pg, "zomato", GROCERY, True, A)
    assert e.value.errors == ["zomato needs food fee rules"]
    with pytest.raises(cart.InvalidRules) as e:
        cart.put_fee_rules(pg, "blinkit", {"delivery": [], "handling": -1}, True, A)
    assert e.value.errors and all(isinstance(x, str) for x in e.value.errors)
    with pytest.raises(cart.InvalidRules):
        cart.put_fee_rules(pg, "amazon", GROCERY, True, A)
    assert pg.execute("select version from fee_rules where site='blinkit'").fetchone()[0] == 1


def test_settings_defaults_for_cart():
    s = Settings()
    assert (s.split_min_saving_inr, s.cart_max_items, s.food_max_items, s.cart_stale_s, s.match_judge,
            s.match_model, s.menu_cache_ttl_s, s.food_lookups_per_day) == (20, 30, 20, 1500, True, "", 1800, 30)
    assert dataclasses.is_dataclass(s)

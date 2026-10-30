"""Food store: usual restaurants and dishes, per-user caches, lookup cap, pairing and food checks (Plan 5 T12)."""
from datetime import datetime, timedelta, timezone

import pytest
from helpers import A, B
from psycopg.types.json import Jsonb

from server import budget, cart, food_cart, limits
from server.config import Settings

NOW = datetime.now(timezone.utc).replace(microsecond=0)   # real time: lookups are counted by the jobs' created_at
LAT, LON = 22.31, 73.18
S_URL = "https://www.swiggy.com/restaurants/behrouz-s1"
Z_URL = "https://www.zomato.com/vadodara/behrouz-z1"


@pytest.fixture
def s():
    return Settings()


def setup_user(pg, user=A, sites=("swiggy", "zomato"), status="connected"):
    pg.execute("insert into profiles (user_id, address, lat, lon) values (%s, 'Gorwa, Vadodara', %s, %s) "
               "on conflict do nothing", (user, LAT, LON))
    for site in sites:
        pg.execute("insert into connections (user_id, site, status, last_synced_at) values (%s, %s, %s, now())",
                   (user, site, status))
        # Each app prices at its own saved address (Plan 5 Task 14).
        pg.execute("insert into app_addresses (user_id, site, address_id, label, line, lat, lon, is_default) "
                   "values (%s, %s, %s, 'Home', 'Gorwa, Vadodara', %s, %s, true) on conflict do nothing",
                   (user, site, f"{site}-home", LAT, LON))
    for site in ("swiggy", "zomato"):
        for kind in ("restaurants", "menu"):
            pg.execute("insert into recipes (site, kind, recipe) values (%s, %s, '{}') on conflict do nothing",
                       (site, kind))


def listing(site, rid, name="Behrouz Biryani", url=None, lat=LAT, lon=LON, locality="Gorwa", distance_km=2.0):
    return {"restaurant_id": rid, "name": name, "locality": locality, "lat": lat, "lon": lon,
            "distance_km": distance_km, "is_open": True, "url": url}


def cache_restaurants(pg, user=A, ago=timedelta(minutes=5), query="behrouz", pair=True, **kw):
    gk = food_cart.geo_key(LAT, LON)
    food_cart.put_restaurants(pg, user, "swiggy", gk, query, [listing("swiggy", "s1", url=S_URL, **kw)],
                              now=NOW - ago)
    food_cart.put_restaurants(pg, user, "zomato", gk, query, [listing("zomato", "z1", url=Z_URL, **kw)],
                              now=NOW - ago)


def dishes(*rows):
    return [{"dish_id": f"d{i}", "name": n, "variant": None, "price": p, "veg": v, "in_stock": True,
             "packaging": 10} for i, (n, p, v) in enumerate(rows)]


def swiggy_menu(**kw):
    return {"is_open": True, "dishes": dishes(("Chicken Biryani", 200, False), ("Paneer Tikka", 150, True)), **kw}


def zomato_menu(**kw):
    return {"is_open": True, "dishes": dishes(("Chicken Biryani", 210, False), ("Paneer Tikka", 160, True)), **kw}


def cache_menus(pg, user=A, ago=timedelta(minutes=5), swiggy=None, zomato=None):
    food_cart.put_menu(pg, user, "swiggy", "s1", swiggy or swiggy_menu(), now=NOW - ago)
    food_cart.put_menu(pg, user, "zomato", "z1", zomato or zomato_menu(), now=NOW - ago)


ITEMS = [{"text": "Chicken Biryani", "qty": 2}, {"text": "Paneer Tikka", "qty": 2}]
BOTH = {"name": "Behrouz Biryani", "swiggy_id": "s1", "zomato_id": "z1", "confirmed": False, "declined": []}


def jobs_of(pg, user=A):
    return pg.execute("select site, payload from jobs where user_id = %s and type = 'search' order by site",
                      (user,)).fetchall()


def lookup_job(pg, user, site, op, request_id=None, status="done", created=NOW):
    payload = {"op": op, "query": "x"} | ({"request_id": request_id} if request_id else {})
    pg.execute("insert into jobs (user_id, type, site, profile, payload, status, created_at) "
               "values (%s, 'search', %s, %s, %s, %s, %s)", (user, site, site, Jsonb(payload), status, created))


def order(pg, oid, site, merchant, status="delivered", sub=None, placed="2026-10-20T20:00:00", items=(), user=A):
    pg.execute("insert into orders (user_id, site, order_id, sub_platform, placed_at, total_amount, status, merchant) "
               "values (%s,%s,%s,%s,%s,300,%s,%s)", (user, site, oid, sub, placed, status, merchant))
    for i, (name, qty) in enumerate(items):
        pg.execute("insert into order_items (user_id, site, order_id, idx, name, qty, price) values "
                   "(%s,%s,%s,%s,%s,%s,100)", (user, site, oid, i, name, qty))


def test_geo_key_two_decimals():
    assert food_cart.geo_key(22.3071, 73.1812) == "22.31,73.18"


def test_usual_restaurants_from_food_orders(pg):
    order(pg, "o1", "swiggy", "Behrouz Biryani", placed="2026-10-01T20:00:00")
    order(pg, "o2", "swiggy", "behrouz  biryani!", placed="2026-10-10T20:00:00")
    order(pg, "o3", "zomato", "Behrouz Biryani", placed="2026-10-20T20:00:00")
    order(pg, "o4", "swiggy", "Big Basket", sub="instamart")
    order(pg, "o5", "swiggy", "Pizza Hut", status="cancelled")
    order(pg, "o6", "zomato", "Chaat Corner", placed="2026-10-25T20:00:00")
    order(pg, "o7", "blinkit", "Blinkit Store")
    order(pg, "o8", "swiggy", "Behrouz Biryani", user=B)
    out = food_cart.usual_restaurants(pg, A)
    assert [(r["name"], r["sites"], r["orders"]) for r in out] == [
        ("Behrouz Biryani", ["swiggy", "zomato"], 3), ("Chaat Corner", ["zomato"], 1)]
    assert out[0]["last_order_at"] == "2026-10-20T20:00:00"
    assert len(food_cart.usual_restaurants(pg, A, limit=1)) == 1


def test_usual_dishes_at_restaurant(pg):
    order(pg, "o1", "swiggy", "Behrouz Biryani", items=[("Chicken Biryani", 2), ("Raita", 1)],
          placed="2026-10-01T20:00:00")
    order(pg, "o2", "zomato", "Behrouz Biryani", items=[("Chicken Biryani", 1)], placed="2026-10-12T20:00:00")
    order(pg, "o3", "swiggy", "Other Place", items=[("Pasta", 1)])
    order(pg, "o4", "swiggy", "Behrouz Biryani", status="cancelled", items=[("Kebab", 1)])
    out = food_cart.usual_dishes(pg, A, "behrouz biryani")
    assert [(d["text"], d["qty"], d["times"]) for d in out] == [("Chicken Biryani", 1, 2), ("Raita", 1, 1)]


def test_lookup_uses_fresh_cache_without_jobs(pg, s):
    setup_user(pg)
    cache_restaurants(pg, ago=timedelta(minutes=10))
    before = food_cart.lookups_left(pg, A, NOW, s)
    out = food_cart.lookup_restaurants(pg, A, "Behrouz", NOW, s)
    assert out["status"] == "ready"
    assert out["access"] == {"swiggy": "ok", "zomato": "ok"}
    assert [r["confidence"] for r in out["results"]] == ["sure"]
    assert jobs_of(pg) == []
    assert food_cart.lookups_left(pg, A, NOW, s) == before == 30


def test_lookup_expired_cache_enqueues_and_counts(pg, s):
    setup_user(pg)
    cache_restaurants(pg, ago=timedelta(minutes=31))
    out = food_cart.lookup_restaurants(pg, A, "Behrouz", NOW, s)
    assert out["status"] == "searching" and len(out["job_ids"]) == 2
    assert jobs_of(pg) == [("swiggy", {"op": "restaurants", "query": "Behrouz"}),
                           ("zomato", {"op": "restaurants", "query": "Behrouz"})]
    assert food_cart.lookups_left(pg, A, NOW, s) == 28


def test_polling_an_active_lookup_does_not_enqueue_or_count_again(pg, s):
    setup_user(pg)
    first = food_cart.lookup_restaurants(pg, A, "Behrouz", NOW, s)
    again = food_cart.lookup_restaurants(pg, A, "behrouz", NOW, s)
    assert again["status"] == "searching" and sorted(again["job_ids"]) == sorted(first["job_ids"])
    assert food_cart.lookups_left(pg, A, NOW, s) == 28


def test_cache_is_per_user(pg, s):
    setup_user(pg)
    setup_user(pg, B)
    cache_menus(pg)
    cache_restaurants(pg)
    assert food_cart.get_menu(pg, B, "swiggy", "s1", NOW, s) is None
    assert food_cart.get_menu(pg, A, "swiggy", "s1", NOW, s)["dishes"]
    assert food_cart.get_restaurants(pg, B, "swiggy", food_cart.geo_key(LAT, LON), "behrouz", NOW, s) is None
    out = food_cart.lookup_restaurants(pg, B, "behrouz", NOW, s)
    assert out["status"] == "searching"


def test_menu_cache_expires_after_ttl(pg, s):
    food_cart.put_menu(pg, A, "swiggy", "s1", swiggy_menu(), now=NOW - timedelta(minutes=29))
    food_cart.put_menu(pg, A, "zomato", "z1", zomato_menu(), now=NOW - timedelta(minutes=31))
    assert food_cart.get_menu(pg, A, "swiggy", "s1", NOW, s) is not None
    assert food_cart.get_menu(pg, A, "zomato", "z1", NOW, s) is None


def test_lookup_limit_and_budget_pause(pg, s):
    setup_user(pg)
    for _ in range(28):
        lookup_job(pg, A, "swiggy", "menu")
    lookup_job(pg, A, "swiggy", "menu", request_id="r1")          # attached to a check: not a lookup
    lookup_job(pg, A, "swiggy", "menu", created=NOW - timedelta(days=1))   # yesterday
    assert food_cart.lookups_left(pg, A, NOW, s) == 2
    lookup_job(pg, A, "zomato", "restaurants")
    with pytest.raises(limits.LimitExceeded) as e:                  # needs two lookups, one is left
        food_cart.lookup_restaurants(pg, A, "biryani", NOW, s)
    assert e.value.kind == "food_lookup" and e.value.limit == 30
    assert e.value.message == "You can search restaurants 30 times a day. Try again tomorrow."
    assert e.value.retry_after_s > 0
    # fresh cache stays free at the cap, and paused budget refuses only real lookups
    cache_restaurants(pg, query="biryani")
    assert food_cart.lookup_restaurants(pg, A, "biryani", NOW, s)["status"] == "ready"
    pg.execute("delete from restaurant_cache")
    pg.execute("insert into budget_state (month, level, spent_inr, budget_inr) values ('2026-10', 'paused', 1, 1)")
    assert budget.is_paused(pg, NOW, s.app_tz)
    with pytest.raises(limits.BudgetPaused):
        food_cart.lookup_restaurants(pg, A, "biryani", NOW, s)
    cache_restaurants(pg, query="biryani")
    assert food_cart.lookup_restaurants(pg, A, "biryani", NOW, s)["status"] == "ready"


def test_lookup_only_connected_food_apps(pg, s):
    setup_user(pg, sites=("swiggy",))
    pg.execute("insert into connections (user_id, site, status) values (%s, 'zomato', 'needs_login')", (A,))
    out = food_cart.lookup_restaurants(pg, A, "Behrouz", NOW, s)
    assert out["status"] == "searching" and len(out["job_ids"]) == 1
    assert out["access"] == {"swiggy": "ok", "zomato": "needs_login"}
    assert [site for site, _ in jobs_of(pg)] == ["swiggy"]
    assert food_cart.lookups_left(pg, A, NOW, s) == 29


def test_lookup_without_connected_food_app(pg, s):
    setup_user(pg, sites=())
    with pytest.raises(cart.NoConnectedApp):
        food_cart.lookup_restaurants(pg, A, "Behrouz", NOW, s)
    assert jobs_of(pg) == []


def test_lookup_menus_ready_with_primary_menu_and_usual_dishes(pg, s):
    setup_user(pg)
    cache_restaurants(pg)
    cache_menus(pg)
    order(pg, "o1", "swiggy", "Behrouz Biryani", items=[("Chicken Biryani", 2)])
    out = food_cart.lookup_menus(pg, A, {"swiggy": "s1", "zomato": "z1"}, NOW, s)
    assert out["status"] == "ready"
    assert out["restaurant"]["name"] == "Behrouz Biryani"
    assert [(d["name"], d["price_label"]) for d in out["menu"]] == [("Chicken Biryani", "₹200"),
                                                                    ("Paneer Tikka", "₹150")]
    assert [d["text"] for d in out["usual_dishes"]] == ["Chicken Biryani"]
    assert jobs_of(pg) == []


def test_menu_lookup_takes_the_url_from_the_users_cache_only(pg, s):
    setup_user(pg)
    cache_restaurants(pg)
    out = food_cart.lookup_menus(pg, A, {"swiggy": "s1"}, NOW, s)
    assert out["status"] == "searching"
    assert jobs_of(pg) == [("swiggy", {"op": "menu", "restaurant_id": "s1", "url": S_URL})]
    with pytest.raises(ValueError):                                 # an id the user never searched for
        food_cart.lookup_menus(pg, A, {"zomato": "unknown"}, NOW, s)
    cache_restaurants(pg, user=B)
    setup_user(pg, B)
    with pytest.raises(ValueError):                                 # B's cache does not unlock A's id
        food_cart.lookup_menus(pg, B, {"zomato": "z9"}, NOW, s)


def test_price_menu_closed_and_meta():
    status, matched, meta = food_cart.price_menu("zomato", {"is_open": False, "dishes": swiggy_menu()["dishes"]},
                                                 ITEMS)
    assert status == "closed" and matched == [] and meta["is_open"] is False
    status, matched, meta = food_cart.price_menu("swiggy", swiggy_menu(), ITEMS)
    assert status == "ok" and [m["status"] for m in matched] == ["matched", "matched"]
    assert meta["fee_info"]["packaging"] == 40 and meta["is_open"] is True


def test_price_menu_packaging_only_when_every_dish_has_it():
    menu = swiggy_menu()
    menu["dishes"][1]["packaging"] = None
    _, _, meta = food_cart.price_menu("swiggy", menu, ITEMS)
    assert "packaging" not in (meta["fee_info"] or {})


def test_food_check_inline_when_menus_fresh(pg, s, fee_rules_snapshot):
    setup_user(pg)
    cache_restaurants(pg)
    cache_menus(pg)
    out = food_cart.start_food_check(pg, A, BOTH, ITEMS, NOW, s)
    assert out["cart_checks_left"] == 9 and out["sites"] == ["swiggy", "zomato"]
    status, summary = pg.execute("select status, summary from cart_requests where id = %s",
                                 (out["request_id"],)).fetchone()
    assert status == "done"
    assert [c["total_label"] for c in summary["cards"] if c["status"] == "ok"] == ["₹836", "₹878"]
    assert summary["best_site"] == "swiggy" and summary["mode"] == "food"
    assert jobs_of(pg) == []


def test_food_check_enqueues_menu_jobs_when_stale(pg, s):
    setup_user(pg)
    cache_restaurants(pg)
    cache_menus(pg, ago=timedelta(minutes=31))
    out = food_cart.start_food_check(pg, A, BOTH, ITEMS, NOW, s)
    rid = out["request_id"]
    assert jobs_of(pg) == [("swiggy", {"op": "menu", "restaurant_id": "s1", "url": S_URL, "request_id": rid}),
                           ("zomato", {"op": "menu", "restaurant_id": "z1", "url": Z_URL, "request_id": rid})]
    assert pg.execute("select status from cart_requests where id = %s", (rid,)).fetchone() == ("searching",)
    assert pg.execute("select count(*) from cart_results where request_id = %s", (rid,)).fetchone() == (0,)
    listings = pg.execute("select restaurant from cart_requests where id = %s", (rid,)).fetchone()[0]["listings"]
    assert listings["swiggy"]["restaurant_id"] == "s1" and listings["swiggy"]["distance_km"] == 2.0
    assert "url" not in listings["swiggy"]
    assert food_cart.lookups_left(pg, A, NOW, s) == 30                # check jobs are not lookups


def test_food_check_counts_one_cart_check(pg, s, fee_rules_snapshot):
    setup_user(pg)
    cache_restaurants(pg)
    cache_menus(pg, ago=timedelta(minutes=31))
    food_cart.start_food_check(pg, A, BOTH, ITEMS, NOW, s)
    assert pg.execute("select cart_checks from usage where user_id = %s", (A,)).fetchone() == (1,)


def test_distance_falls_back_to_haversine(pg, s):
    setup_user(pg)
    gk = food_cart.geo_key(LAT, LON)
    for site, rid in (("swiggy", "s1"), ("zomato", "z1")):
        row = listing(site, rid, lat=LAT + 0.045, lon=LON, distance_km=None)    # about 5 km north
        food_cart.put_restaurants(pg, A, site, gk, "behrouz", [row], now=NOW - timedelta(minutes=5))
    out = food_cart.start_food_check(pg, A, BOTH | {"confirmed": True}, ITEMS, NOW, s)
    listings = pg.execute("select restaurant from cart_requests where id = %s",
                          (out["request_id"],)).fetchone()[0]["listings"]
    assert 4.5 < listings["swiggy"]["distance_km"] < 5.5


def test_unconfirmed_ask_pair_is_rejected(pg, s):
    setup_user(pg)
    cache_restaurants(pg, lat=None, lon=None, locality=None)          # same name, no place: only "ask"
    with pytest.raises(ValueError, match="Confirm it's the same restaurant first."):
        food_cart.start_food_check(pg, A, BOTH, ITEMS, NOW, s)
    assert pg.execute("select count(*) from cart_requests").fetchone() == (0,)
    assert pg.execute("select count(*) from usage").fetchone() == (0,)
    out = food_cart.start_food_check(pg, A, BOTH | {"confirmed": True}, ITEMS, NOW, s)
    assert out["request_id"]


def test_check_needs_a_listing_id_and_a_cached_listing(pg, s):
    setup_user(pg)
    cache_restaurants(pg)
    with pytest.raises(ValueError):
        food_cart.start_food_check(pg, A, {"name": "X", "confirmed": False, "declined": []}, ITEMS, NOW, s)
    with pytest.raises(ValueError):
        food_cart.start_food_check(pg, A, {"name": "X", "swiggy_id": "nope", "confirmed": False, "declined": []},
                                   ITEMS, NOW, s)


def test_check_without_connected_app_is_409_and_not_counted(pg, s):
    setup_user(pg, sites=())
    cache_restaurants(pg)
    with pytest.raises(cart.NoConnectedApp):
        food_cart.start_food_check(pg, A, BOTH, ITEMS, NOW, s)
    assert pg.execute("select count(*) from usage").fetchone() == (0,)


def test_declined_site_is_not_listed_with_note(pg, s, fee_rules_snapshot):
    setup_user(pg)
    cache_restaurants(pg)
    cache_menus(pg)
    one = {"name": "Behrouz Biryani", "swiggy_id": "s1", "confirmed": False, "declined": ["zomato"]}
    out = food_cart.start_food_check(pg, A, one, ITEMS, NOW, s)
    summary = pg.execute("select summary from cart_requests where id = %s", (out["request_id"],)).fetchone()[0]
    cards = {c["site"]: c for c in summary["cards"]}
    assert cards["zomato"]["status"] == "not_listed"
    assert cards["zomato"]["note"] == "Couldn't confirm this restaurant on Zomato."
    assert cards["swiggy"]["cheapest"] is True
    assert pg.execute("select status from cart_results where request_id = %s and site = 'zomato'",
                      (out["request_id"],)).fetchone() == ("not_listed",)


def test_closed_menu_priced_as_closed(pg, s, fee_rules_snapshot):
    setup_user(pg)
    cache_restaurants(pg)
    cache_menus(pg, zomato=zomato_menu(is_open=False))
    out = food_cart.start_food_check(pg, A, BOTH, ITEMS, NOW, s)
    summary = pg.execute("select summary from cart_requests where id = %s", (out["request_id"],)).fetchone()[0]
    cards = {c["site"]: c for c in summary["cards"]}
    assert cards["zomato"]["status"] == "closed" and cards["zomato"]["note"] == "Closed on Zomato right now."
    assert cards["zomato"]["total"] is None and cards["zomato"]["cheapest"] is False
    assert cards["swiggy"]["cheapest"] is True


def test_check_with_only_connected_site_priced(pg, s, fee_rules_snapshot):
    setup_user(pg, sites=("swiggy",))
    cache_restaurants(pg)
    cache_menus(pg)
    out = food_cart.start_food_check(pg, A, BOTH, ITEMS, NOW, s)
    assert out["sites"] == ["swiggy", "zomato"]
    rows = dict(pg.execute("select site, status from cart_results where request_id = %s",
                           (out["request_id"],)).fetchall())
    assert rows == {"swiggy": "ok", "zomato": "not_connected"}


def test_too_many_dishes_rejected(pg, s):
    setup_user(pg)
    cache_restaurants(pg)
    many = [{"text": f"Dish {i}", "qty": 1} for i in range(21)]
    with pytest.raises(ValueError):
        food_cart.start_food_check(pg, A, BOTH | {"confirmed": True}, many, NOW, s)
    with pytest.raises(ValueError):
        food_cart.start_food_check(pg, A, BOTH | {"confirmed": True}, [], NOW, s)


def test_purge_cache_older_than_a_day(pg):
    gk = food_cart.geo_key(LAT, LON)
    old, fresh = NOW - timedelta(hours=25), NOW - timedelta(hours=2)
    food_cart.put_restaurants(pg, A, "swiggy", gk, "old", [listing("swiggy", "s1")], now=old)
    food_cart.put_restaurants(pg, A, "swiggy", gk, "fresh", [listing("swiggy", "s1")], now=fresh)
    food_cart.put_menu(pg, A, "swiggy", "s1", swiggy_menu(), now=old)
    food_cart.put_menu(pg, A, "zomato", "z1", zomato_menu(), now=fresh)
    assert food_cart.purge_cache(pg, now=NOW) == 2
    assert pg.execute("select count(*) from restaurant_cache").fetchone() == (1,)
    assert pg.execute("select count(*) from menu_cache").fetchone() == (1,)

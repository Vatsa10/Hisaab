"""One-tap setup: the journey steps per app, and the run that takes the next step by itself, pauses for a human
and stops after two failures. Everything runs on fake data."""
from helpers import B, add_user

from server import journey, store


def _admin(pg, site="swiggy", connected=True):
    add_user(pg, B, "admin@example.com")
    pg.execute("insert into admins(email) values ('admin@example.com')")
    pg.execute("insert into profiles (user_id, address, lat, lon) values (%s,%s,%s,%s)", (B, "Flat 7", 10.1, 20.2))
    if connected:
        store.set_connection(pg, B, site, status="connected", orders_page_url=f"https://www.{site}.com/my-account")


def _recipe(pg, site, kind):
    pg.execute("insert into recipes (site, kind, recipe, version, healthy) values (%s,%s,%s,1,true) "
               "on conflict (site, kind) do nothing", (site, kind, '{"kind": "%s"}' % kind))


def _orders(pg, site, n=3):
    for i in range(n):
        pg.execute("insert into orders (user_id, site, order_id, total_amount) values (%s,%s,%s,10)",
                   (B, site, f"o{i}"))


def _end_jobs(pg, status="done", error=None):
    pg.execute("update jobs set status=%s, error=%s, finished_at=now()", (status, error))


def _steps(pg, site="swiggy"):
    return {s["id"]: s["status"] for s in journey.site_journey(pg, site, B)["steps"]}


def _kinds(pg):
    return [(r[0], r[1]) for r in pg.execute(
        "select type, coalesce(payload->>'kind','') from jobs order by created_at")]


def test_a_new_app_starts_at_connect_and_blocks_the_rest(pg):
    _admin(pg, connected=False)
    j = journey.site_journey(pg, "swiggy", B)
    assert j["next"]["id"] == "connect" and j["next"]["op"] == "connect"
    assert _steps(pg) == {"connect": "todo", "orders": "blocked", "imported": "blocked", "addresses": "blocked",
                          "food": "blocked"}


def test_a_connected_app_with_no_recipe_says_learn_orders(pg):
    _admin(pg)
    j = journey.site_journey(pg, "swiggy", B)
    assert j["next"]["id"] == "orders" and j["next"]["op"] == "learn" and j["done"] == 1


def test_each_food_step_follows_the_one_before(pg):
    _admin(pg)
    _recipe(pg, "swiggy", "orders")
    nxt = journey.site_journey(pg, "swiggy", B)["next"]
    assert nxt["id"] == "imported" and nxt["op"] == "sync"
    _orders(pg, "swiggy")
    assert journey.site_journey(pg, "swiggy", B)["next"]["id"] == "addresses"
    _recipe(pg, "swiggy", "addresses")
    assert journey.site_journey(pg, "swiggy", B)["next"]["id"] == "food"
    _recipe(pg, "swiggy", "restaurants")
    assert journey.site_journey(pg, "swiggy", B)["next"]["id"] == "food"  # menu is still missing
    _recipe(pg, "swiggy", "menu")
    j = journey.site_journey(pg, "swiggy", B)
    assert j["next"] is None and j["done"] == j["total"]


def test_a_grocery_app_has_search_then_cart(pg):
    _admin(pg, "zepto")
    for k in ("orders", "addresses"):
        _recipe(pg, "zepto", k)
    _orders(pg, "zepto")
    assert list(_steps(pg, "zepto")) == ["connect", "orders", "imported", "addresses", "search", "cart"]
    assert _steps(pg, "zepto")["search"] == "todo" and _steps(pg, "zepto")["cart"] == "blocked"


def test_food_waits_for_a_delivery_place(pg):
    _admin(pg)
    pg.execute("update profiles set lat=null, lon=null")
    for k in ("orders", "addresses"):
        _recipe(pg, "swiggy", k)
    _orders(pg, "swiggy")
    food = next(s for s in journey.site_journey(pg, "swiggy", B)["steps"] if s["id"] == "food")
    assert food["status"] == "blocked" and "delivery address" in food["detail"]
    pg.execute("insert into app_addresses (user_id, site, address_id, line, area, city, lat, lon, is_default) "
               "values (%s,'swiggy','a1','3 Society','Area','Town',22.3,73.1,true)", (B,))
    assert _steps(pg)["food"] == "todo"  # a saved address from the app is enough
    assert journey.ensure_location(pg, B) is True
    assert pg.execute("select address, lat from profiles where user_id=%s", (B,)).fetchone() == (
        "3 Society, Area, Town", 22.3)


def test_a_place_already_set_is_never_replaced(pg):
    _admin(pg)
    pg.execute("insert into app_addresses (user_id, site, address_id, line, lat, lon) "
               "values (%s,'swiggy','a1','Elsewhere',1,2)", (B,))
    journey.ensure_location(pg, B)
    assert pg.execute("select address from profiles where user_id=%s", (B,)).fetchone()[0] == "Flat 7"


def test_pressing_set_up_queues_the_first_step_once(pg):
    _admin(pg)
    j = journey.start_run(pg, "swiggy", B)
    assert j["run"]["status"] == "running" and _kinds(pg) == [("learn", "orders")]
    journey.advance(pg, "swiggy")
    journey.advance(pg, "swiggy")
    assert _kinds(pg) == [("learn", "orders")]  # a running step is waited for, never queued twice


def test_the_run_walks_every_step_and_finishes(pg):
    _admin(pg)
    journey.start_run(pg, "swiggy", B)
    _recipe(pg, "swiggy", "orders")
    _end_jobs(pg)
    assert journey.advance(pg, "swiggy") == "running" and _kinds(pg)[-1] == ("sync", "")
    _orders(pg, "swiggy")
    _end_jobs(pg)
    journey.advance(pg, "swiggy")
    assert _kinds(pg)[-1] == ("learn", "addresses")
    _recipe(pg, "swiggy", "addresses")
    _end_jobs(pg)
    journey.advance(pg, "swiggy")
    assert _kinds(pg)[-1] == ("learn", "food")
    _recipe(pg, "swiggy", "restaurants")
    _recipe(pg, "swiggy", "menu")
    _end_jobs(pg)
    assert journey.advance(pg, "swiggy") == "done"
    run = journey.site_journey(pg, "swiggy", B)["run"]
    assert run["status"] == "done" and "ready" in run["message"]
    assert journey.advance(pg, "swiggy") is None  # nothing running any more


def test_a_step_that_fails_twice_stops_the_run(pg):
    _admin(pg)
    journey.start_run(pg, "swiggy", B)
    _end_jobs(pg, "failed", "boom")
    assert journey.advance(pg, "swiggy") == "running"  # first failure: tried again
    assert len(_kinds(pg)) == 2
    _end_jobs(pg, "failed", "boom")
    assert journey.advance(pg, "swiggy") == "paused"
    run = journey.site_journey(pg, "swiggy", B)["run"]
    assert "failed twice" in run["message"] and "boom" in run["message"]
    assert len(_kinds(pg)) == 2


def test_a_held_recipe_pauses_the_run_for_the_admin(pg):
    _admin(pg)
    journey.start_run(pg, "swiggy", B)
    pg.execute("insert into recipe_approvals (site, kind, reason, proposed, status) "
               "values ('swiggy','orders','account_specific','{}','pending')")
    _end_jobs(pg)
    assert journey.advance(pg, "swiggy") == "paused"
    assert "OK" in journey.site_journey(pg, "swiggy", B)["run"]["message"]


def test_an_unconnected_app_pauses_with_what_to_do(pg):
    _admin(pg, connected=False)
    journey.start_run(pg, "swiggy", B)
    run = journey.site_journey(pg, "swiggy", B)["run"]
    assert run["status"] == "paused" and "OTP" in run["message"] and _kinds(pg) == []


def test_the_cart_learn_is_left_for_the_admin(pg):
    _admin(pg, "zepto")
    for k in ("orders", "addresses", "search"):
        _recipe(pg, "zepto", k)
    _orders(pg, "zepto")
    journey.start_run(pg, "zepto", B)
    run = journey.site_journey(pg, "zepto", B)["run"]
    assert run["status"] == "done" and "cart" in run["message"].lower() and _kinds(pg) == []


def test_stopping_a_run_ends_it(pg):
    _admin(pg)
    journey.start_run(pg, "swiggy", B)
    assert journey.stop_run(pg, "swiggy") is True
    assert journey.advance(pg, "swiggy") is None


def test_the_api_starts_lists_and_stops(client, pg, token_b):
    _admin(pg)
    h = token_b
    r = client.post("/admin/setup/swiggy", headers=h)
    assert r.status_code == 200 and r.json()["run"]["status"] == "running"
    assert len(client.get("/admin/journey", headers=h).json()) == 4
    assert client.delete("/admin/setup/swiggy", headers=h).status_code == 204
    assert client.post("/admin/setup/nowhere", headers=h).status_code == 400


def test_a_non_admin_cannot_start_a_setup(client, pg, token_a):
    add_user(pg, "00000000-0000-0000-0000-00000000000a", "a@example.com")
    assert client.post("/admin/setup/swiggy", headers=token_a).status_code in (401, 403)


def test_the_run_waits_for_the_first_address_read_before_food(pg):
    from server import jobs
    _admin(pg)
    pg.execute("update profiles set lat=null, lon=null")
    for k in ("orders", "addresses"):
        _recipe(pg, "swiggy", k)
    _orders(pg, "swiggy")
    jobs.enqueue(pg, B, "search", "swiggy", {"op": "addresses"})  # queued by publishing the addresses recipe
    assert _steps(pg)["addresses"] == "running"
    journey.start_run(pg, "swiggy", B)
    assert journey.site_journey(pg, "swiggy", B)["run"]["status"] == "running"  # not paused on "add an address"
    _end_jobs(pg)
    pg.execute("insert into app_addresses (user_id, site, address_id, line, lat, lon, is_default) "
               "values (%s,'swiggy','a1','3 Society',22.3,73.1,true)", (B,))
    journey.advance(pg, "swiggy")
    assert _kinds(pg)[-1] == ("learn", "food")  # the saved address filled the place, so food starts

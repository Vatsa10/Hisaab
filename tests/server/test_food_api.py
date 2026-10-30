"""Food routes: auth, validation, ready/searching responses and the error strings (Plan 5 T12)."""
from datetime import datetime, timedelta, timezone

import pytest
from helpers import A
from test_food_cart import BOTH, ITEMS, NOW, cache_menus, cache_restaurants, lookup_job, setup_user

from server import food_api


@pytest.fixture
def at_now(monkeypatch):
    """Run the routes at NOW so the cached rows written relative to it are fresh."""
    monkeypatch.setattr(food_api, "_now", lambda: NOW)


def body(items=None, **restaurant):
    return {"restaurant": {k: v for k, v in BOTH.items()} | restaurant, "items": ITEMS if items is None else items}


def test_food_routes_auth_and_validation(client, token_a, pg):
    for method, url in (("get", "/cart/food/usual"), ("get", "/cart/food/restaurants?q=biryani"),
                        ("post", "/cart/food/menus"), ("post", "/cart/food/checks")):
        assert getattr(client, method)(url).status_code == 401
    assert client.get("/cart/food/usual", headers=token_a).json() == {"restaurants": []}
    assert client.get("/cart/food/restaurants?q=b", headers=token_a).status_code == 422
    assert client.get("/cart/food/restaurants?q=" + "x" * 61, headers=token_a).status_code == 422
    twenty_one = [{"text": f"Dish {i}", "qty": 1} for i in range(21)]
    assert client.post("/cart/food/checks", json=body(twenty_one), headers=token_a).status_code == 422
    for bad in ([{"text": "", "qty": 1}], [{"text": "x", "qty": 21}], [{"text": "x", "qty": 0}],
                [{"text": "y" * 81, "qty": 1}], [{"text": "x", "qty": 1, "variant": "v" * 61}], []):
        assert client.post("/cart/food/checks", json=body(bad), headers=token_a).status_code == 422
    setup_user(pg, sites=())
    r = client.get("/cart/food/restaurants?q=biryani", headers=token_a)
    assert (r.status_code, r.json()["detail"]) == (409, "Connect Swiggy or Zomato to compare.")
    r = client.post("/cart/food/checks", json=body(), headers=token_a)
    assert (r.status_code, r.json()["detail"]) == (409, "Connect Swiggy or Zomato to compare.")


def test_menus_route_never_accepts_a_url(client, token_a, pg):
    setup_user(pg)
    r = client.post("/cart/food/menus", json={"name": "B", "swiggy_id": "s1", "url": "https://evil.test/"},
                    headers=token_a)
    assert r.status_code == 422
    r = client.post("/cart/food/menus", json={"name": "B"}, headers=token_a)
    assert r.status_code == 422


def test_restaurants_route_ready_then_searching(client, token_a, pg, at_now):
    setup_user(pg)
    cache_restaurants(pg, query="biryani")
    r = client.get("/cart/food/restaurants?q=Biryani", headers=token_a)
    assert r.status_code == 200 and r.json()["status"] == "ready" and r.json()["results"]
    r = client.get("/cart/food/restaurants?q=pizza", headers=token_a)
    assert r.status_code == 202 and r.json()["status"] == "searching" and len(r.json()["job_ids"]) == 2


def test_menus_route_ready_and_searching(client, token_a, pg, at_now):
    setup_user(pg)
    cache_restaurants(pg)
    r = client.post("/cart/food/menus", json={"name": "Behrouz", "swiggy_id": "s1", "zomato_id": "z1"},
                    headers=token_a)
    assert r.status_code == 202 and r.json()["status"] == "searching"
    pg.execute("delete from jobs")
    cache_menus(pg)
    r = client.post("/cart/food/menus", json={"name": "Behrouz", "swiggy_id": "s1", "zomato_id": "z1"},
                    headers=token_a)
    assert r.status_code == 200 and r.json()["status"] == "ready" and r.json()["menu"]


def test_lookup_limit_is_429_with_retry_after(client, token_a, pg, at_now):
    setup_user(pg)
    for _ in range(30):
        lookup_job(pg, A, "swiggy", "menu", created=NOW - timedelta(seconds=1))
    r = client.get("/cart/food/restaurants?q=pizza", headers=token_a)
    assert r.status_code == 429
    assert r.json()["detail"] == "You can search restaurants 30 times a day. Try again tomorrow."
    assert int(r.headers["Retry-After"]) > 0


def test_food_check_route_returns_202(client, token_a, pg, at_now):
    setup_user(pg)
    cache_restaurants(pg)
    cache_menus(pg, ago=timedelta(minutes=31))
    r = client.post("/cart/food/checks", json=body(), headers=token_a)
    assert r.status_code == 202
    out = r.json()
    assert set(out) == {"request_id", "sites", "cart_checks_left"}
    assert out["sites"] == ["swiggy", "zomato"] and out["cart_checks_left"] == 9


def test_food_check_unconfirmed_pair_is_422(client, token_a, pg, at_now):
    setup_user(pg)
    cache_restaurants(pg, lat=None, lon=None, locality=None)
    r = client.post("/cart/food/checks", json=body(), headers=token_a)
    assert (r.status_code, r.json()["detail"]) == (422, "Confirm it's the same restaurant first.")
    r = client.post("/cart/food/checks", json=body(confirmed=True), headers=token_a)
    assert r.status_code == 202


def test_food_check_error_strings(client, token_a, pg, at_now):
    setup_user(pg)
    pg.execute("update profiles set lat = null, lon = null where user_id = %s", (A,))
    r = client.post("/cart/food/checks", json=body(confirmed=True), headers=token_a)
    assert (r.status_code, r.json()["detail"]) == (409, "Add your delivery address first.")
    pg.execute("update profiles set lat = 22.31, lon = 73.18 where user_id = %s", (A,))
    cache_restaurants(pg)
    pg.execute("insert into budget_state (month, level, spent_inr, budget_inr) values ('2026-10','paused',1,1)")
    r = client.post("/cart/food/checks", json=body(), headers=token_a)
    assert r.status_code == 503 and "paused" in r.json()["detail"]

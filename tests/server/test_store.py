import pytest

from helpers import A, B, order
from server import store


def test_upsert_is_idempotent_per_user(pg):
    o = order("1", items=[{"name": "Milk", "qty": 1, "price": 30}])
    store.upsert_orders(pg, A, "zepto", [o])
    store.upsert_orders(pg, A, "zepto", [o])
    assert pg.execute("select count(*) from orders").fetchone() == (1,)
    assert pg.execute("select count(*) from order_items").fetchone() == (1,)


def test_same_order_id_for_two_users_is_two_rows(pg):
    store.upsert_orders(pg, A, "zepto", [order("1")])
    store.upsert_orders(pg, B, "zepto", [order("1")])
    assert pg.execute("select count(*) from orders").fetchone() == (2,)


def test_upsert_updates_status_replaces_items_and_stores_raw(pg):
    store.upsert_orders(pg, A, "zepto", [order("1", items=[{"name": "Milk", "qty": 1, "price": 30}])],
                        sub_platform="instamart")
    store.upsert_orders(pg, A, "zepto", [order("1", status="refunded", items=[{"name": "Curd", "qty": 2, "price": 50}])],
                        sub_platform="instamart")
    assert pg.execute("select status, sub_platform, raw_json from orders").fetchall() == [
        ("refunded", "instamart", {"id": "1"})]
    assert pg.execute("select name from order_items").fetchall() == [("Curd",)]


def test_upsert_is_atomic(pg):
    bad = {"order_id": "2"}  # missing required keys -> KeyError mid-batch
    with pytest.raises(KeyError):
        store.upsert_orders(pg, A, "zepto", [order("1"), bad])
    assert pg.execute("select count(*) from orders").fetchone() == (0,)


def test_set_connection_upserts(pg):
    store.set_connection(pg, A, "swiggy", status="syncing")
    store.set_connection(pg, A, "swiggy", status="connected", orders_page_url="https://www.swiggy.com/my-account/orders")
    assert pg.execute("select status, orders_page_url from connections").fetchall() == [
        ("connected", "https://www.swiggy.com/my-account/orders")]


def test_set_connection_rejects_unknown_column(pg):
    with pytest.raises(ValueError):
        store.set_connection(pg, A, "swiggy", **{"status": "connected", "user_id; drop table orders": "x"})


def test_get_profile(pg):
    assert store.get_profile(pg, A) is None
    pg.execute("insert into profiles (user_id, display_name, address) values (%s, 'Asha', 'Pune')", (A,))
    p = store.get_profile(pg, A)
    assert p["display_name"] == "Asha" and p["address"] == "Pune" and p["user_id"] == A


def test_recipe_put_get_and_version(pg):
    assert store.get_recipe(pg, "zepto") is None
    assert store.put_recipe(pg, "zepto", {"orders_page_url": "https://x/1", "n": 1}) == 1
    assert store.get_recipe(pg, "zepto") == {"orders_page_url": "https://x/1", "n": 1}
    assert store.put_recipe(pg, "zepto", {"orders_page_url": "https://x/2"}) == 2
    assert store.get_recipe(pg, "zepto") == {"orders_page_url": "https://x/2"}
    assert store.get_recipe(pg, "blinkit") is None
    assert store.get_recipe(pg, "zepto", kind="search") is None
    assert store.put_recipe(pg, "zepto", {"s": 1}, kind="search") == 1
    assert pg.execute("select healthy from recipes where site='zepto' and kind='orders'").fetchone() == (True,)

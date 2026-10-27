import db
from helpers import A, B, order
from server import store


def _cats(pg, rows):
    for name, cat in rows:
        pg.execute("insert into item_categories (name_norm, category) values (%s, %s)", (db.norm_name(name), cat))


def test_monthly_spend_excludes_cancelled_and_is_per_user(pg, as_user):
    store.upsert_orders(pg, A, "zepto", [order("1", 100), order("2", 50, status="cancelled"),
                                         order("2b", 50, status="refunded"), order("2c", 50, status="failed")])
    store.upsert_orders(pg, B, "zepto", [order("3", 999)])
    with as_user(pg, A):
        assert pg.execute("select month, site, orders, amount from monthly_spend").fetchall() == [
            ("2025-01", "zepto", 1, 100)]
    with as_user(pg, B):
        assert pg.execute("select amount from monthly_spend").fetchall() == [(999,)]


def test_category_spend_splits_unpriced_items_and_maps_food(pg, as_user):
    _cats(pg, [("Milk", "dairy"), ("Bread", "groceries")])
    store.upsert_orders(pg, A, "zepto", [order("1", 120, items=[
        {"name": "Milk", "qty": 2, "price": None}, {"name": "Bread", "qty": 1, "price": None}])])
    store.upsert_orders(pg, A, "zomato", [order("2", 300, items=[{"name": "Biryani", "qty": 1, "price": None}])])
    store.upsert_orders(pg, A, "swiggy", [order("3", 90, items=[{"name": "Mystery", "qty": 1, "price": 90}])],
                        sub_platform="instamart")
    store.upsert_orders(pg, A, "swiggy", [order("4", 70, items=[{"name": "Milk", "qty": 1, "price": 70}])])
    store.upsert_orders(pg, B, "zepto", [order("5", 500, items=[{"name": "Milk", "qty": 1, "price": None}])])
    with as_user(pg, A):
        rows = pg.execute("select month, category, amount from category_spend order by category").fetchall()
    # swiggy non-instamart (order 4) is food delivery too: 300 + 70
    assert rows == [("2025-01", "dairy", 80), ("2025-01", "food delivery", 370),
                    ("2025-01", "groceries", 40), ("2025-01", "uncategorized", 90)]


def test_merchant_spend_groups_by_merchant(pg, as_user):
    a = order("1", 100)
    b = order("2", 40)
    b["merchant"] = "Other"
    b["placed_at"] = "2025-02-01T10:00:00"
    c = order("3", 60)
    c["placed_at"] = "2025-01-20T10:00:00"
    store.upsert_orders(pg, A, "zepto", [a, b, c, order("4", 5, status="cancelled")])
    store.upsert_orders(pg, B, "zepto", [order("9", 777)])
    with as_user(pg, A):
        rows = pg.execute("select merchant, site, orders, amount, last_order_at from merchant_spend "
                          "order by merchant").fetchall()
    assert rows == [("Other", "zepto", 1, 40, "2025-02-01T10:00:00"),
                    ("Store", "zepto", 2, 160, "2025-01-20T10:00:00")]


def test_views_are_security_invoker_and_anon_has_no_access(pg):
    for v in ("monthly_spend", "category_spend", "merchant_spend"):
        opts = pg.execute("select reloptions from pg_class where relname=%s", (v,)).fetchone()[0]
        assert "security_invoker=true" in opts
        assert not pg.execute("select has_table_privilege('anon', %s, 'select')", (v,)).fetchone()[0]
        assert pg.execute("select has_table_privilege('authenticated', %s, 'select')", (v,)).fetchone()[0]


def test_norm_name_matches_python(pg):
    for s in ["Amul Taaza Milk 1L", "Lay's  Classic!!", "  Paneer-200g ", "Café_Latte  ₹50", ""]:
        assert pg.execute("select norm_name(%s)", (s,)).fetchone()[0] == db.norm_name(s)

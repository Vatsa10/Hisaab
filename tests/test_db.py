import db
import report


def order(oid, total=100.0, status="delivered", items=None):
    return {"order_id": oid, "placed_at": "2025-01-05T10:00:00", "total_amount": total, "currency": "INR",
            "status": status, "merchant": "Store", "items": items or [], "raw": {"id": oid}}


def counts(con):
    return (con.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM order_items").fetchone()[0])


def test_upsert_is_idempotent(tmp_path):
    con = db.connect(tmp_path / "t.db")
    orders = [order("1", items=[{"name": "Milk", "qty": 1, "price": 30}, {"name": "Bread", "qty": 1, "price": 40}]),
              order("2")]
    db.upsert_orders(con, "blinkit", orders)
    db.upsert_orders(con, "blinkit", orders)
    assert counts(con) == (2, 2)


def test_upsert_updates_status_and_replaces_items(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.upsert_orders(con, "zepto", [order("1", items=[{"name": "Milk", "qty": 1, "price": 30}])])
    db.upsert_orders(con, "zepto", [order("1", status="refunded", items=[{"name": "Curd", "qty": 2, "price": 50}])])
    row = con.execute("SELECT status FROM orders WHERE order_id='1'").fetchone()
    assert row["status"] == "refunded"
    assert [r["name"] for r in con.execute("SELECT name FROM order_items")] == ["Curd"]


def test_same_order_id_on_different_sites(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.upsert_orders(con, "zepto", [order("1")])
    db.upsert_orders(con, "blinkit", [order("1")])
    assert counts(con)[0] == 2


def test_instamart_tag_and_report_excludes_cancelled(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.upsert_orders(con, "swiggy", [order("1", 200)], sub_platform="instamart")
    db.upsert_orders(con, "swiggy", [order("2", 300), order("3", 999, status="cancelled")])
    data = report.build(con, "2025-01")
    assert dict((k, a) for k, _, a in data["platform"]) == {"swiggy/instamart": 200.0, "swiggy": 300.0}

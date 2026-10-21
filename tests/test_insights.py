import db
import insights
from test_db import order


def test_monthly_pivot_excludes_cancelled(tmp_path):
    con = db.connect(tmp_path / "t.db")
    db.upsert_orders(con, "zepto", [order("1", 100)])
    db.upsert_orders(con, "zomato", [order("2", 300), order("3", 999, status="cancelled")])
    header, rows = insights.monthly(con)
    assert header == ["month", "zepto", "zomato", "orders", "total"]
    assert rows == [("2025-01", 100.0, 300.0, 2, 400.0)]


def test_run_sql_is_read_only(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    db.upsert_orders(db.connect(path), "zepto", [order("1", 100)])
    monkeypatch.setattr(db, "DB_PATH", path)
    assert insights.run_sql("SELECT COUNT(*) FROM orders")["rows"] == [(1,)]
    assert "error" in insights.run_sql("DELETE FROM orders")
    assert "error" in insights.run_sql("SELECT 1; DELETE FROM orders")
    assert "error" in insights.run_sql("WITH x AS (SELECT 1) DELETE FROM orders")   # ro connection refuses
    assert insights.run_sql("SELECT COUNT(*) FROM orders")["rows"] == [(1,)]

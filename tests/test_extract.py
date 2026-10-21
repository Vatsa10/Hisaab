import json
from pathlib import Path

from recipes import extract_orders, get_path, normalize_ts, valid_orders

DATA = json.loads((Path(__file__).parent / "fixtures" / "orders_page1.json").read_text(encoding="utf-8"))
FIELD_MAP = {
    "orders_path": "data.orders",
    "order_id": "id", "placed_at": "created_at",
    "total_amount": "total", "amount_divisor": 100, "currency": "=INR",
    "status": "state", "store_or_restaurant": "store.name",
    "items_path": "line_items",
    "items": {"name": "title", "qty": "quantity", "price": "unit_price"}, "price_is_unit": True,
}


def test_extracts_orders_and_items():
    orders = extract_orders(DATA, FIELD_MAP)
    assert len(orders) == 3
    o = orders[0]
    assert o["order_id"] == "9001"
    assert o["total_amount"] == 549.0
    assert o["currency"] == "INR"
    assert o["status"] == "delivered"
    assert o["merchant"] == "Dark Store Koramangala"
    assert o["placed_at"].startswith("2025-01-01")
    assert o["items"] == [{"name": "Amul Taaza Milk 1L", "qty": 2.0, "price": 136.0},
                          {"name": "Lay's Classic Salted", "qty": 1.0, "price": 20.0}]


def test_parses_formatted_amount_and_cancelled_status():
    o = extract_orders(DATA, FIELD_MAP)[1]
    assert o["total_amount"] == 1230.0
    assert o["status"] == "cancelled"


def test_valid_orders_drops_rows_missing_required_fields():
    assert [o["order_id"] for o in valid_orders(extract_orders(DATA, FIELD_MAP))] == ["9001", "9000"]


def test_wrong_path_extracts_nothing():
    assert extract_orders(DATA, FIELD_MAP | {"orders_path": "data.nope"}) == []


def test_get_path_indexes_and_literals():
    assert get_path(DATA, "data.orders.0.store.name") == "Dark Store Koramangala"
    assert get_path(DATA, "data.orders.9.id") is None
    assert get_path(DATA, "=INR") == "INR"


def test_normalize_ts_formats():
    assert normalize_ts("2025-03-04 10:20:00") == "2025-03-04T10:20:00"
    assert normalize_ts("04 Mar 2025, 10:20 AM") == "2025-03-04T10:20:00"
    assert normalize_ts(1735727400) == normalize_ts(1735727400000)
    assert normalize_ts("not a date") is None


def test_prose_format_falls_back_to_auto_parse():
    assert normalize_ts("2026-07-22T11:45:18.661450Z", "ISO 8601") is not None


def test_yearless_dates_get_this_or_last_year():
    from datetime import datetime
    from recipes import _parse
    now = datetime(2026, 10, 21, 12, 0)
    f = "%d %b, %I:%M %p"
    assert _parse("18 Jul, 11:56 pm", f, now) == datetime(2026, 7, 18, 23, 56)
    assert _parse("05 Dec, 9:00 am", f, now) == datetime(2025, 12, 5, 9, 0)   # this year would be future
    assert _parse("29 Feb, 9:00 am", f, now) == datetime(2024, 2, 29, 9, 0)   # nearest past leap year
    assert _parse("01 Jan 2024", "%d %b %Y", now) == datetime(2024, 1, 1)
    assert normalize_ts("18 Jul, 11:56 pm", f)[:4] in ("2026", "2025")


def test_bracket_indexes_in_paths():
    assert get_path({"a": [{"id": 1}, {"id": 2}]}, "a[-1].id") == 2
    assert get_path({"a": [{"id": 1}]}, "a[0].id") == 1


def test_shape_hint_finds_nested_order_maps():
    from agent import shape_hint
    data = {"page_info": {"name": "user"}, "entities": {"ORDER": {
        "8585540883": {"orderId": 8585540883, "totalCost": "₹324.49", "orderDate": "September 09, 2026", "dishString": "x"},
        "8522471316": {"orderId": 8522471316, "totalCost": "₹410", "orderDate": "August 26, 2026", "dishString": "y"}}}}
    hint = shape_hint(data)
    assert "'entities.ORDER' (2 items)" in hint and "orderId" in hint
    orders = extract_orders(data, {"orders_path": "entities.ORDER", "order_id": "orderId",
                                   "placed_at": "orderDate", "total_amount": "totalCost"})
    assert [o["total_amount"] for o in valid_orders(orders)] == [324.49, 410.0]


def test_items_given_as_one_string():
    data = {"o": [{"id": 1, "t": "2026-01-01", "a": 324.49, "dishes": "2 x Veg Cheese Frankie, 1 x Veg Grilled Sandwich"}]}
    o = extract_orders(data, {"orders_path": "o", "order_id": "id", "placed_at": "t", "total_amount": "a",
                              "items_path": "dishes", "items": {"name": "title", "qty": "count"}})[0]
    assert o["items"] == [{"name": "Veg Cheese Frankie", "qty": 2.0, "price": None},
                          {"name": "Veg Grilled Sandwich", "qty": 1.0, "price": None}]

"""fill_summary.build: every user-facing line of a fill result (Plan 6 R10)."""
from server import fill_summary

ZEPTO = "https://www.zepto.com/"
AREA = "Gorwa, Vadodara"


def F(**k):
    base = {"site": "zepto", "status": "done", "items": [], "cart": None, "app_location": None,
            "location_selected": False, "repriced": None, "error": None, "existing_changed": False, "counted": True}
    base.update(k)
    return base


def item(name, status, packs=1, added_qty=0, skip_reason=None, label=None, idx=0):
    return {"idx": idx, "label": label or name, "name": name, "product_id": "p", "packs": packs, "status": status,
            "added_qty": added_qty, "skip_reason": skip_reason}


def added(name, qty):
    return item(name, "added", packs=qty, added_qty=qty)


def skipped(label, reason):
    return {"idx": 0, "label": label, "name": None, "product_id": None, "packs": None, "status": "skipped",
            "added_qty": 0, "skip_reason": reason}


def test_done_title_sections_and_cart_line():
    s = fill_summary.build(F(status="done", items=[added("Amul Taaza Toned Milk 1 L", 1), added("Amul Taza Milk 500ml", 2)],
                             cart={"subtotal": 465, "total": 480, "fees": {"handling": 15}, "line_count": 6,
                                   "existing_count": 1}),
                           AREA, ZEPTO)
    assert s["title"] == "Added 2 items to your Zepto cart"
    assert s["tone"] == "success" and s["status"] == "done"
    assert s["sections"][0] == {"key": "added", "heading": "Added",
                                "rows": ["Amul Taaza Toned Milk 1 L", "Amul Taza Milk 500ml ×2"]}
    assert len(s["sections"]) == 1
    assert s["cart_line"] == "Zepto cart total ₹480 · items ₹465 + fees ₹15"
    assert s["existing_line"] == "Includes 1 item that was already in your cart."
    assert s["open"] == {"label": "Open Zepto to pay", "url": "https://www.zepto.com/"}
    assert s["pay_note"] == "We never place the order or pay. Review your cart in Zepto and pay there."
    assert s["warning_line"] is None and s["location_line"] is None and s["reprice"] is None
    assert s["error_line"] is None and s["confirm_label"] is None and s["counted"] is True


def test_one_item_title_and_existing_plural():
    s = fill_summary.build(F(items=[added("Bread", 1)], cart={"subtotal": 40, "total": 40, "fees": {},
                                                               "line_count": 4, "existing_count": 3}), AREA, ZEPTO)
    assert s["title"] == "Added 1 item to your Zepto cart"
    assert s["existing_line"] == "Includes 3 items that were already in your cart."
    s = fill_summary.build(F(items=[added("Bread", 1)], cart={"subtotal": 40, "total": 40, "fees": {},
                                                               "line_count": 1, "existing_count": 0}), AREA, ZEPTO)
    assert s["existing_line"] is None


def test_partial_counts_and_skip_reasons():
    items = [added("Milk", 1), added("Bread", 1), added("Butter", 1), added("Curd", 1),
             item("Paneer 200 g", "partial", packs=3, added_qty=2),
             item("Farm Eggs 6 pcs", "out_of_stock", packs=1),
             skipped("chips", "you_skipped"), skipped("biscuits", "no_answer"),
             skipped("jam", "out_of_stock"), skipped("ghee", "not_found")]
    s = fill_summary.build(F(status="partial", items=items), AREA, ZEPTO)
    # a = 5 (4 added + 1 partial); t = 10 items − 2 user skips = 8
    assert s["title"] == "Added 5 of 8 items to your Zepto cart"
    assert s["tone"] == "warning"
    by = {x["key"]: x for x in s["sections"]}
    assert [x["heading"] for x in s["sections"]] == ["Added", "Only some added", "Out of stock now", "Skipped"]
    assert by["partial"]["rows"] == ["Paneer 200 g: 2 of 3"]
    assert by["out_of_stock"]["rows"] == ["Farm Eggs 6 pcs"]
    assert by["skipped"]["rows"] == ["chips (you skipped it)", "biscuits (no answer)",
                                     "jam (out of stock when we checked)", "ghee (not found)"]
    assert s["open"]["label"] == "Open Zepto to pay"


def test_partial_six_items_with_out_of_stock():
    items = [added(f"Item {i}", 1) for i in range(5)] + [item("Farm Eggs 6 pcs", "out_of_stock")]
    s = fill_summary.build(F(status="partial", items=items), AREA, ZEPTO)
    assert s["title"] == "Added 5 of 6 items to your Zepto cart"
    assert {x["key"]: x["rows"] for x in s["sections"]}["out_of_stock"] == ["Farm Eggs 6 pcs"]


def test_not_added_section_and_failed_title():
    s = fill_summary.build(F(status="failed", error="nothing_added", items=[item("Milk", "not_added")]), AREA, ZEPTO)
    assert s["title"] == "Couldn't fill your Zepto cart" and s["tone"] == "error"
    assert s["sections"] == [{"key": "not_added", "heading": "Not added", "rows": ["Milk"]}]
    assert s["error_line"] == "Zepto didn't accept any of the items. Nothing was added."
    assert s["open"] is None


def test_location_selected_line():
    s = fill_summary.build(F(items=[added("Milk", 1)], location_selected=True), AREA, ZEPTO)
    assert s["location_line"] == "We set Zepto's delivery location to Gorwa, Vadodara."


def test_warning_line_when_existing_changed():
    s = fill_summary.build(F(status="partial", items=[added("Milk", 1), item("Bread", "not_added")],
                             existing_changed=True), AREA, ZEPTO)
    assert s["warning_line"] == ("Something already in your Zepto cart changed while we were adding. "
                                 "Please check your cart.")


def test_reprice_copy():
    rep = [{"idx": 0, "name": "Amul Taaza Toned Milk 500 ml", "product_id": "p1", "old_price": 58.0,
            "new_price": 62.0, "in_stock": True},
           {"idx": 1, "name": "Harvest Gold Bread", "product_id": "p2", "old_price": 227.0, "new_price": 231.0,
            "in_stock": True},
           {"idx": 2, "name": "Farm Eggs 6 pcs", "product_id": "p3", "old_price": 60.0, "new_price": None,
            "in_stock": False}]
    s = fill_summary.build(F(status="needs_confirmation", repriced=rep,
                             app_location={"lat": 22.3, "lon": 73.1, "store_id": "S2", "area": "Alkapuri"}),
                           AREA, ZEPTO)
    assert s["title"] == "Zepto is set to a different location" and s["tone"] == "warning"
    assert s["reprice"]["rows"] == [{"label": "Amul Taaza Toned Milk 500 ml", "old": "₹58", "new": "₹62"},
                                    {"label": "Harvest Gold Bread", "old": "₹227", "new": "₹231"},
                                    {"label": "Farm Eggs 6 pcs", "old": "₹60", "new": "Not available"}]
    assert s["reprice"]["total_line"] == "Items there: ₹293 instead of ₹285"
    assert s["confirm_label"] == "Fill at these prices"
    assert s["location_line"] == "Zepto delivers to Alkapuri right now, not Gorwa, Vadodara."
    assert s["open"] is None and s["error_line"] is None and s["sections"] == []


def test_reprice_without_prices():
    s = fill_summary.build(F(status="needs_confirmation", repriced=[], app_location={"lat": 1, "lon": 2}), AREA, ZEPTO)
    assert s["reprice"] == {"rows": [], "total_line": "We couldn't get Zepto's prices there."}
    assert s["confirm_label"] is None
    assert s["location_line"] == "Zepto delivers to another area right now, not Gorwa, Vadodara."


def test_error_lines():
    none_added = [item("Milk", "not_added")]
    some_added = [added("Milk", 1), item("Bread", "not_added")]

    def line(error, items, status="failed"):
        return fill_summary.build(F(status=status, error=error, items=items), AREA, ZEPTO)["error_line"]

    assert line("needs_login", none_added) == "Zepto logged you out. Reconnect it, then try again. Nothing was added."
    assert line("needs_login", some_added) == ("Zepto logged you out. Reconnect it, then try again. "
                                               "Some items were added before it stopped.")
    assert line("recipe_drift", none_added) == ("Filling Zepto carts stopped working. We've told the team. "
                                                "Nothing was added.")
    assert line("recipe_drift", some_added) == ("Filling Zepto carts stopped working. We've told the team. "
                                                "Some items were added before it stopped.")
    assert line("location_changed", []) == "Zepto's location changed again. Nothing was added."
    assert line("nothing_added", none_added) == "Zepto didn't accept any of the items. Nothing was added."
    assert line("worker_lost", none_added) == ("We lost track of this fill. Check your Zepto cart before "
                                               "filling again.")
    assert line(None, some_added, status="interrupted") == ("We lost track of this fill. Check your Zepto cart "
                                                            "before filling again.")
    assert line("cart_unavailable", none_added) == "Something went wrong. Nothing was added."
    assert line("boom", some_added) == "Something went wrong. Some items were added before it stopped."
    assert line(None, some_added, status="done") is None


def test_interrupted_title_and_open():
    s = fill_summary.build(F(status="interrupted", error="worker_lost", items=[]), AREA, ZEPTO)
    assert s["title"] == "Your Zepto cart may be half filled" and s["tone"] == "warning"
    assert s["open"] == {"label": "Open Zepto to pay", "url": ZEPTO}


def test_engine_stop_codes_tell_the_user_to_check_the_app():
    """The engine stops on these with adds possibly already in the cart: say so, and always offer the app."""
    expected = {
        "redirected_write": ("Zepto answered one of our adds in a way we didn't expect, so we stopped. "
                             "Some items may already be in your cart. Check your Zepto cart before filling again."),
        "existing_changed": ("We stopped adding. Some items may already be in your cart. "
                             "Check your Zepto cart before filling again."),
        "cart_changed": ("Your Zepto cart changed while we were adding, so we stopped. "
                         "Some items may already be in your cart. Check your Zepto cart before filling again."),
        "cart_read_failed": ("We couldn't read your Zepto cart after adding, so we stopped. "
                             "Some items may already be in your cart. Check your Zepto cart before filling again."),
    }
    for code, text in expected.items():
        # The store ends these fills as interrupted (the slot stays taken); the engine reports them as failed.
        for status, items in [(st, it) for st in ("interrupted", "failed")
                              for it in ([item("Milk", "not_added")], [added("Milk", 1)])]:
            s = fill_summary.build(F(status=status, error=code, items=items,
                                     existing_changed=code == "existing_changed"), AREA, ZEPTO)
            assert s["error_line"] == text, (code, status)
            assert s["title"] == "Your Zepto cart may be half filled", code
            assert s["tone"] == "warning", code
            assert s["open"] == {"label": "Open Zepto to pay", "url": ZEPTO}, code


def test_instamart_needs_login_names_swiggy():
    s = fill_summary.build(F(site="instamart", status="failed", error="needs_login", items=[]), AREA,
                           "https://www.swiggy.com/instamart")
    assert s["error_line"] == "Swiggy logged you out. Reconnect it, then try again. Nothing was added."
    assert s["title"] == "Couldn't fill your Instamart cart"
    assert s["pay_note"] == "We never place the order or pay. Review your cart in Instamart and pay there."


def test_counted_passes_through():
    assert fill_summary.build(F(status="failed", error="nothing_added", counted=False), AREA, ZEPTO)["counted"] is False

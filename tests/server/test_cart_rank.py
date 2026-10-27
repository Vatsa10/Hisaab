"""Ranked cart summaries: money labels, area labels, cards, item table, picks and split (pure)."""
import json
from datetime import datetime, timezone

from server import cart_rank
from test_fees import SEEDS

AT = "2026-10-27T10:00:00+05:30"
FREE = ({"delivery": [{"min_subtotal": 0, "fee": 0}], "handling": 0, "surge": {"possible": False}}, True)


def it(idx, status, price=None, name=None, packs=1, cands=None, swap_from=None, pid=None):
    """A match_item / match_dish-shaped cell. `price` is the line total."""
    priced = price is not None
    product = {"product_id": pid or f"p{idx}-{price}", "name": name or f"item {idx}", "brand": None,
               "size_label": None, "price": round(price / packs, 2), "mrp": None, "in_stock": True} if priced else None
    return {"idx": idx, "status": status, "confidence": "sure" if status == "matched" else None, "product": product,
            "packs": packs if priced else None, "line_total": float(price) if priced else None,
            "swap_from": swap_from, "candidates": cands or []}


def priced(*prices):
    return [it(i, "matched", p) if p is not None else it(i, "not_found") for i, p in enumerate(prices)]


def res(site, status="ok", items=(), fee_info=None, priced_at=AT, **meta):
    m = dict(meta)
    if fee_info is not None:
        m["fee_info"] = fee_info
    return {"site": site, "status": status, "items": list(items), "meta": m, "priced_at": priced_at, "error": None}


def greq(n=3, choices=None, texts=None):
    texts = texts or [("Amul Taaza 1L", 2), ("Bread 400g", 1), ("Lay's Classic", 1), ("Eggs 6", 1)]
    items = [{"text": t, "qty": q} for t, q in texts[:n]]
    return {"id": "req-1", "mode": "grocery", "items": items, "area": "Gorwa, Vadodara", "restaurant": None,
            "choices": choices or {}}


SEED_RULES = {site: (SEEDS[site], False) for site in SEEDS}
NO_MEMBER = {"member": False}


def build(request, results, rules=None, split_min=20):
    return cart_rank.build_summary(request, results, SEED_RULES if rules is None else rules, split_min)


def card(summary, site):
    return next(c for c in summary["cards"] if c["site"] == site)


# --- labels ------------------------------------------------------------------------------------------------------

def test_inr_and_area_label():
    assert cart_rank.inr(15) == "₹15"
    assert cart_rank.inr(123456) == "₹1,23,456"
    assert cart_rank.inr(49.5) == "₹49.50"
    assert cart_rank.inr(0) == "₹0"
    assert cart_rank.inr(999) == "₹999"
    assert cart_rank.inr(1000) == "₹1,000"
    assert cart_rank.inr(1234567.5) == "₹12,34,567.50"
    assert cart_rank.inr(49.999) == "₹50"
    assert cart_rank.inr(412.0) == "₹412"
    assert cart_rank.inr(-37) == "-₹37"
    assert cart_rank.area_label("12 MG Road, Gorwa, Vadodara, Gujarat 390016, India") == "Gorwa, Vadodara"
    assert cart_rank.area_label("Gorwa, Vadodara, Gujarat, 390016") == "Gorwa, Vadodara"
    assert cart_rank.area_label("Vadodara, Gujarat 390016, India") == "Vadodara"
    assert cart_rank.area_label("Gorwa") == "Gorwa"
    assert cart_rank.area_label("22.3310,73.1510") == "your address"
    assert cart_rank.area_label(" 22.33 , 73.15 ") == "your address"
    assert cart_rank.area_label(None) == "your address"
    assert cart_rank.area_label("") == "your address"
    assert cart_rank.area_label("India") == "your address"
    assert cart_rank.area_label("390016, India") == "your address"


# --- ranking -----------------------------------------------------------------------------------------------------

def mockup_results():
    return [res("blinkit", items=priced(138, 50, 252), fee_info=NO_MEMBER),
            res("zepto", items=priced(136, 50, 211), fee_info=NO_MEMBER),
            res("instamart", items=priced(140, 45, 266), fee_info=NO_MEMBER)]


def test_ranks_cheapest_complete_first_with_deltas():
    s = build(greq(), mockup_results())
    assert [c["site"] for c in s["cards"]] == ["zepto", "blinkit", "instamart"]
    assert [c["rank"] for c in s["cards"]] == [1, 2, 3]
    assert [c["total_label"] for c in s["cards"]] == ["₹412", "₹449", "₹461"]
    assert [c["cheapest"] for c in s["cards"]] == [True, False, False]
    assert [c["delta_label"] for c in s["cards"]] == [None, "+₹37", "+₹49"]
    assert [c["name"] for c in s["cards"]] == ["Zepto", "Blinkit", "Instamart"]
    z = s["cards"][0]
    assert (z["status"], z["subtotal"], z["total"]) == ("ok", 397, 412)
    assert z["fees"] == {"delivery": 0, "handling": 15, "small_cart": 0, "surge": 0, "total": 15}
    assert z["fees_estimated"] is True                          # unverified seed, surge possible
    assert z["fee_line"] == "Items ₹397 + delivery ₹0 + handling ₹15"
    assert (z["note"], z["action"], z["swaps"], z["to_pick"]) == (None, None, [], 0)
    assert (s["status"], s["best_site"], s["mode"], s["request_id"]) == ("done", "zepto", "grocery", "req-1")
    assert (s["title"], s["count_label"], s["area"]) == ("Cheapest cart", "3 items", "Gorwa, Vadodara")
    assert s["price_note"] == "Your prices, with your memberships and your store's stock."
    assert s["offers_note"] is None and s["restaurant"] is None
    assert s["priced_at"] == AT


def test_same_total_delta_and_site_order_tiebreak():
    s = build(greq(2), [res("instamart", items=priced(100, 100), fee_info=NO_MEMBER),
                        res("zepto", items=priced(100, 100), fee_info=NO_MEMBER)], rules={"zepto": FREE, "instamart": FREE})
    assert [c["site"] for c in s["cards"]] == ["zepto", "instamart"]
    assert [c["delta_label"] for c in s["cards"]] == [None, "Same total"]


def test_incomplete_cart_never_outranks_complete():
    s = build(greq(), [res("zepto", items=priced(136, 50, 211), fee_info=NO_MEMBER),       # 412, complete
                       res("blinkit", items=priced(100, 50, None), fee_info=NO_MEMBER),    # 150 + fees, 1 missing
                       res("instamart", items=[it(0, "matched", 90), it(1, "out_of_stock"), it(2, "pick")],
                           fee_info=NO_MEMBER)])
    assert [c["site"] for c in s["cards"]] == ["zepto", "blinkit", "instamart"]
    assert [c["cheapest"] for c in s["cards"]] == [True, False, False]
    assert [c["delta_label"] for c in s["cards"]] == [None, None, None]   # unresolved counts differ
    assert s["best_site"] == "zepto"
    # deltas compare against the best card only, so carts with a different gap never get one
    s = build(greq(), [res("zepto", items=priced(136, 50, 211), fee_info=NO_MEMBER),
                       res("blinkit", items=priced(100, 50, None), fee_info=NO_MEMBER),
                       res("instamart", items=priced(None, 60, 100), fee_info=NO_MEMBER)])
    assert [c["site"] for c in s["cards"]] == ["zepto", "blinkit", "instamart"]   # 184 < 205 among one-gap carts
    assert [c["delta_label"] for c in s["cards"]] == [None, None, None]


def test_non_ok_cards_last_with_notes_and_actions():
    s = build(greq(), [res("instamart", "not_connected"), res("blinkit", "needs_login"),
                       res("zepto", items=priced(136, 50, 211), fee_info=NO_MEMBER)])
    assert [c["site"] for c in s["cards"]] == ["zepto", "blinkit", "instamart"]
    b, i = s["cards"][1], s["cards"][2]
    assert b["note"] == "Blinkit logged you out. Log in again to compare."
    assert b["action"] == {"label": "Log in again", "href": "/accounts"}
    assert i["note"] == "Connect Swiggy to compare Instamart."
    assert i["action"] == {"label": "Connect", "href": "/accounts"}
    for c in (b, i):
        assert (c["total"], c["total_label"], c["fees"], c["cheapest"], c["delta_label"]) == (None, "—", None, False, None)
        assert (c["fee_line"], c["stock_line"], c["member_note"], c["fee_hint"]) == (None, None, None, None)
    assert s["table"]["columns"] == ["zepto"]
    assert s["split"] is None                                   # fewer than 2 ok cards

    s = build(greq(), [res("zepto", "failed"), res("blinkit", "unavailable"), res("instamart", "unserviceable")])
    assert [c["site"] for c in s["cards"]] == ["instamart", "zepto", "blinkit"]
    assert [c["note"] for c in s["cards"]] == ["Instamart doesn't deliver to this address.",
                                               "Couldn't price Zepto right now.",
                                               "Blinkit prices are unavailable right now."]
    assert all(c["action"] is None for c in s["cards"])
    assert (s["status"], s["best_site"], s["split"]) == ("failed", None, None)
    assert s["table"] == {"columns": [], "rows": [
        {"idx": 0, "label": "Amul Taaza 1L ×2", "cells": {}}, {"idx": 1, "label": "Bread 400g", "cells": {}},
        {"idx": 2, "label": "Lay's Classic", "cells": {}}], "totals": {}}

    s = build(greq(), [res("zepto", "not_connected"), res("blinkit", "failed"),
                       res("instamart", "needs_login")])
    assert [c["site"] for c in s["cards"]] == ["instamart", "zepto", "blinkit"]
    assert s["cards"][1]["note"] == "Connect Zepto to compare."
    assert s["cards"][0]["note"] == "Instamart logged you out. Log in again to compare."


def test_nothing_priced_has_no_cheapest():
    s = build(greq(), [res("zepto", items=priced(None, None, None), fee_info=NO_MEMBER), res("blinkit", "failed")])
    z = s["cards"][0]
    assert (z["site"], z["rank"], z["status"], z["cheapest"]) == ("zepto", 1, "ok", False)
    assert (z["total"], z["total_label"], z["fees"], z["fee_line"]) == (None, "—", None, None)
    assert z["stock_line"] == "3 not found"
    assert (s["best_site"], s["status"]) == (None, "done")


def test_swap_note_member_note_and_stock_line():
    swapped = it(2, "swapped", 20, "Lay's Magic Masala Chips 52 g",
                 swap_from={"product_id": "x", "name": "Lay's Classic Salted Chips 52 g"})
    s = build(greq(), [
        res("zepto", items=priced(30, 20, 30), fee_info={"member": True}),                    # 80, Zepto Pass
        res("blinkit", items=priced(60, 50, 40), fee_info=NO_MEMBER),                         # 150, delivery 25
        res("instamart", items=[it(0, "matched", 60), it(1, "matched", 50), swapped], fee_info=NO_MEMBER),
    ])
    z, b, i = card(s, "zepto"), card(s, "blinkit"), card(s, "instamart")
    assert z["member_note"] == "Zepto Pass applied"
    assert z["fee_line"] == "Items ₹80 + delivery ₹0 + handling ₹15"
    assert z["stock_line"] == "all 3 in stock"
    assert b["member_note"] is None
    assert b["fee_hint"] == "Free delivery above ₹199"
    assert b["fee_line"] == "Items ₹150 + delivery ₹25 + handling ₹9"
    assert z["fee_hint"] is None                                # delivery already waived
    assert i["swaps"] == ["Lay's Classic Salted Chips 52 g swapped for Lay's Magic Masala Chips 52 g"]
    assert i["stock_line"] == "all 3 in stock"

    s = build(greq(4), [res("zepto", items=[it(0, "matched", 50), it(1, "out_of_stock"), it(2, "pick"),
                                            it(3, "matched", 30)], fee_info=NO_MEMBER),
                        res("blinkit", items=[it(0, "matched", 50), it(1, "not_found"), it(2, "out_of_stock"),
                                              it(3, "out_of_stock")], fee_info=NO_MEMBER)])
    z, b = card(s, "zepto"), card(s, "blinkit")
    assert z["stock_line"] == "1 out of stock · 1 to pick" and z["to_pick"] == 1
    assert z["fee_line"] == "Items ₹80 + delivery ₹30 + handling ₹15 + small-cart ₹25"
    assert b["stock_line"] == "2 out of stock · 1 not found" and b["to_pick"] == 0
    assert b["fee_line"] == "Items ₹50 + delivery ₹25 + handling ₹9 + small-cart ₹20"

    surge = ({"delivery": [{"min_subtotal": 0, "fee": 0}], "handling": 5, "surge": {"possible": True}}, True)
    s = build(greq(1), [res("zepto", items=priced(100), fee_info={"surge": 12})], rules={"zepto": surge})
    assert card(s, "zepto")["fee_line"] == "Items ₹100 + delivery ₹0 + handling ₹5 + surge ₹12"
    assert card(s, "zepto")["fees_estimated"] is False


def test_item_table_highlights_cheapest_per_row_and_ties():
    s = build(greq(3), [res("zepto", items=[it(0, "matched", 136, "Amul Taaza Toned Milk 1 L", packs=2),
                                            it(1, "matched", 50), it(2, "matched", 20)], fee_info=NO_MEMBER),
                        res("blinkit", items=priced(148, 50, 20), fee_info=NO_MEMBER),
                        res("instamart", items=[it(0, "matched", 140), it(1, "matched", 45), it(2, "out_of_stock")],
                            fee_info=NO_MEMBER)])
    t = s["table"]
    assert t["columns"] == ["zepto", "blinkit", "instamart"]
    assert [r["label"] for r in t["rows"]] == ["Amul Taaza 1L ×2", "Bread 400g", "Lay's Classic"]
    milk, bread, lays = t["rows"]
    assert milk["idx"] == 0
    assert milk["cells"]["zepto"] == {"status": "matched", "name": "Amul Taaza Toned Milk 1 L", "price_label": "₹136",
                                      "cheapest": True}
    assert [milk["cells"][x]["cheapest"] for x in t["columns"]] == [True, False, False]
    assert [bread["cells"][x]["cheapest"] for x in t["columns"]] == [False, False, True]
    assert [lays["cells"][x]["cheapest"] for x in t["columns"]] == [True, True, False]       # tie
    assert lays["cells"]["instamart"] == {"status": "out_of_stock", "name": None, "price_label": "Out of stock",
                                          "cheapest": False}
    assert t["totals"] == {x: card(s, x)["total_label"] for x in t["columns"]}

    s = build(greq(2), [res("zepto", items=[it(0, "not_found"), it(1, "pick")], fee_info=NO_MEMBER)])
    assert [s["table"]["rows"][i]["cells"]["zepto"]["price_label"] for i in (0, 1)] == ["n/a", "Pick"]


def cands():
    return [{"product_id": "c1", "name": "Lay's Classic 52 g", "brand": "Lay's", "size_label": "52 g", "price": 20.0,
             "mrp": 20.0, "in_stock": True, "packs": 1, "line_total": 20.0, "score": 0.6},
            {"product_id": "c2", "name": "Lay's Classic 90 g", "brand": "Lay's", "size_label": "90 g", "price": 30.0,
             "mrp": 30.0, "in_stock": True, "packs": 1, "line_total": 30.0, "score": 0.55},
            {"product_id": "c3", "name": "Lay's Classic 115 g", "brand": "Lay's", "size_label": "115 g", "price": 50.0,
             "mrp": 50.0, "in_stock": False, "packs": 1, "line_total": 50.0, "score": 0.5}]


def test_choice_replaces_pick_cell():
    results = [res("zepto", items=[it(0, "matched", 136), it(1, "matched", 50), it(2, "pick", cands=cands())],
                   fee_info=NO_MEMBER),
               res("blinkit", items=priced(138, 50, 25), fee_info=NO_MEMBER)]
    s = build(greq(), results)
    z = card(s, "zepto")
    assert (z["to_pick"], z["subtotal"], z["stock_line"]) == (1, 186, "1 to pick")
    assert s["picks"] == [{"idx": 2, "site": "zepto", "label": "Lay's Classic", "candidates": [
        {"product_id": "c1", "name": "Lay's Classic 52 g", "size_label": "52 g", "price_label": "₹20", "packs": 1},
        {"product_id": "c2", "name": "Lay's Classic 90 g", "size_label": "90 g", "price_label": "₹30", "packs": 1}]}]
    assert s["cards"][0]["site"] == "blinkit"                    # complete beats one-to-pick

    s = build(greq(choices={"2": {"zepto": "c1"}}), results)
    z = card(s, "zepto")
    assert (z["to_pick"], z["subtotal"], z["stock_line"]) == (0, 206, "all 3 in stock")
    cell = s["table"]["rows"][2]["cells"]["zepto"]
    assert cell == {"status": "chosen", "name": "Lay's Classic 52 g", "price_label": "₹20", "cheapest": True}
    assert s["picks"] == []
    assert [c["site"] for c in s["cards"]] == ["zepto", "blinkit"]   # 206 + 15 = 221 < 213 + 9 = 222
    s_bad = build(greq(choices={"2": {"zepto": "nope"}, "1": {"blinkit": "c1"}}), results)
    assert card(s_bad, "zepto")["to_pick"] == 1 and card(s_bad, "blinkit")["subtotal"] == 213


# --- split -------------------------------------------------------------------------------------------------------

def test_split_lines():
    free = {"zepto": FREE, "blinkit": FREE, "instamart": FREE}
    s = build(greq(2), [res("zepto", items=priced(100, 200)), res("blinkit", items=priced(150, 120))],
              rules={"zepto": ({"delivery": [{"min_subtotal": 0, "fee": 0}], "handling": 5}, True),
                     "blinkit": ({"delivery": [{"min_subtotal": 0, "fee": 0}], "handling": 5}, True)})
    assert s["best_site"] == "blinkit"
    assert s["split"] == {"worth_it": True, "sites": ["blinkit", "zepto"], "saving": 45.0,
                          "line": "Splitting between Blinkit and Zepto saves ₹45 even after the second order's fees."}

    h30 = ({"delivery": [{"min_subtotal": 0, "fee": 0}], "handling": 30}, True)
    s = build(greq(2), [res("zepto", items=priced(100, 190)), res("blinkit", items=priced(110, 180))],
              rules={"zepto": h30, "blinkit": h30})
    assert s["split"]["worth_it"] is False and s["split"]["saving"] == -20.0
    assert s["split"]["line"] == "Splitting across apps saves ₹10 on items but adds ₹30 in fees. Not worth it."

    s = build(greq(2), [res("zepto", items=priced(100, 200)), res("blinkit", items=priced(110, 195))], rules=free)
    assert s["split"] == {"worth_it": False, "sites": ["zepto", "blinkit"], "saving": 5.0,
                          "line": "Splitting across apps saves only ₹5. Not worth a second order."}

    s = build(greq(2), [res("zepto", items=priced(100, 100)), res("blinkit", items=priced(110, 100)),
                        res("instamart", items=priced(100, 120))], rules=free)
    assert s["split"] == {"worth_it": False, "sites": [], "saving": 0,
                          "line": "No item is cheaper on another app, so one order on Zepto is best."}

    s = build(greq(2), [res("zepto", items=priced(100, 200)), res("blinkit", items=priced(150, 120))], rules=free,
              split_min=100)
    assert s["split"]["worth_it"] is False
    assert s["split"]["line"] == "Splitting across apps saves only ₹50. Not worth a second order."


def test_split_pair_must_cover_best_items():
    free = {"zepto": FREE, "blinkit": FREE, "instamart": FREE}
    s = build(greq(3), [res("zepto", items=priced(100, 100, 100)), res("blinkit", items=priced(50, 50, None)),
                        res("instamart", items=priced(None, None, 60))], rules=free)
    assert [c["site"] for c in s["cards"]] == ["zepto", "blinkit", "instamart"]
    assert s["split"]["sites"] == ["blinkit", "instamart"] and s["split"]["saving"] == 140.0
    assert s["split"]["line"] == "Splitting between Blinkit and Instamart saves ₹140 even after the second order's fees."
    # without instamart, blinkit cannot cover the third item alone, so the pair must include zepto
    s = build(greq(3), [res("zepto", items=priced(100, 100, 100)), res("blinkit", items=priced(50, 50, None))],
              rules=free)
    assert s["split"]["sites"] == ["zepto", "blinkit"] and s["split"]["saving"] == 100.0


# --- food --------------------------------------------------------------------------------------------------------

def freq(declined=(), n=3):
    dishes = [("Chicken Biryani (Half)", 1), ("Paneer Biryani", 1), ("Chicken 65", 1)]
    return {"id": "req-f", "mode": "food", "items": [{"text": t, "qty": q, "variant": None, "veg": None} for t, q in dishes[:n]],
            "area": "Gorwa, Vadodara", "choices": {},
            "restaurant": {"name": "Meghana Foods", "locality": "Gorwa", "listings": {}, "confirmed": True,
                           "declined": list(declined)}}


def swiggy(**kw):
    return res("swiggy", items=priced(220, 260, 220), fee_info={"packaging": 40, "member": False}, distance_km=0.15,
               is_open=True, menu_fetched_at="2026-10-27T09:50:00+05:30", **kw)


def test_food_summary_cards():
    s = build(freq(), [res("zomato", items=priced(235, 250, 235), fee_info={"member": False}, distance_km=0.15,
                           is_open=True, menu_fetched_at="2026-10-27T09:55:00+05:30"), swiggy()])
    assert [c["site"] for c in s["cards"]] == ["swiggy", "zomato"]
    assert [c["total_label"] for c in s["cards"]] == ["₹836", "₹878"]
    assert [c["delta_label"] for c in s["cards"]] == [None, "+₹42"]
    sw, zo = s["cards"]
    assert sw["fee_line"] == "Items ₹700 + delivery ₹30 + packaging ₹40 + platform ₹20 + GST ₹46"
    assert zo["fee_line"] == "Items ₹720 + delivery ₹40 + packaging ₹60 + platform ₹10 + GST ₹48"
    assert sw["fees"] == {"delivery": 30, "packaging": 40, "platform": 20, "small_order": 0, "surge": 0, "gst": 46.0,
                          "total": 136.0}
    assert sw["stock_line"] == "all 3 available" and sw["fee_hint"] is None and sw["cheapest"] is True
    assert s["offers_note"] == "Offers in the app may lower this."
    assert (s["title"], s["count_label"], s["split"], s["mode"]) == ("Meghana Foods", "3 dishes", None, "food")
    assert s["restaurant"] == {"name": "Meghana Foods", "locality": "Gorwa"}
    assert s["priced_at"] == "2026-10-27T09:50:00+05:30"        # oldest menu used
    assert s["best_site"] == "swiggy"
    assert json.loads(json.dumps(s)) == s                        # stored as jsonb


def test_food_small_order_line_and_unavailable_dish():
    s = build(freq(n=2), [res("swiggy", items=[it(0, "matched", 120), it(1, "out_of_stock")],
                              fee_info={"packaging": 10, "member": False, "surge": 15}, distance_km=1.0,
                              menu_fetched_at=AT)])
    sw = s["cards"][0]
    assert sw["stock_line"] == "1 not available"
    assert s["table"]["rows"][1]["cells"]["swiggy"]["price_label"] == "Not available"
    assert sw["fee_line"] == ("Items ₹120 + delivery ₹30 + packaging ₹10 + platform ₹20 + GST ₹21.80"
                              " + small-order ₹20 + surge ₹15")
    assert s["count_label"] == "2 dishes"
    one = build(freq(n=1), [swiggy()])
    assert one["count_label"] == "1 dish"


def test_closed_restaurant_card_last_with_note():
    s = build(freq(), [res("zomato", "closed", meta_is_open=False), swiggy()])
    assert [c["site"] for c in s["cards"]] == ["swiggy", "zomato"]
    zo = s["cards"][1]
    assert zo["note"] == "Closed on Zomato right now."
    assert (zo["total"], zo["total_label"], zo["cheapest"], zo["action"]) == (None, "—", False, None)
    assert s["cards"][0]["cheapest"] is True and s["status"] == "done"


def test_declined_pair_note():
    s = build(freq(declined=["zomato"]), [res("zomato", "not_listed"), swiggy()])
    assert card(s, "zomato")["note"] == "Couldn't confirm this restaurant on Zomato."
    s = build(freq(), [res("zomato", "not_listed"), swiggy()])
    assert card(s, "zomato")["note"] == "Not on Zomato."
    s = build(freq(), [res("zomato", "not_listed"), res("swiggy", "closed")])
    assert [c["site"] for c in s["cards"]] == ["swiggy", "zomato"]     # closed before not_listed
    assert s["status"] == "failed"


def test_priced_at_accepts_datetimes_and_picks_oldest():
    early = datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)
    late = datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc)
    s = build(greq(1), [res("zepto", items=priced(100), priced_at=late), res("blinkit", items=priced(90), priced_at=early)],
              rules={"zepto": FREE, "blinkit": FREE})
    assert s["priced_at"] == early.isoformat()


def test_missing_fee_rules_are_estimated_not_fatal():
    s = build(greq(1), [res("zepto", items=priced(100))], rules={})
    z = s["cards"][0]
    assert (z["total_label"], z["fees_estimated"]) == ("₹100", True)

"""Fee rules: validation, grocery and food fee computation (pure)."""
import copy

from server import fees

RULES = {"delivery": [{"min_subtotal": 0, "fee": 30}, {"min_subtotal": 199, "fee": 0}], "handling": 15,
         "small_cart": {"below": 99, "fee": 25}, "surge": {"possible": False},
         "membership": {"name": "Zepto Pass", "waives": ["delivery", "small_cart"]}}

SW = {"kind": "food", "delivery_by_km": [{"max_km": 3, "fee": 30}, {"max_km": 7, "fee": 45}, {"max_km": None, "fee": 65}],
      "packaging_per_item": 10, "platform": 20, "small_order": {"below": 149, "fee": 20}, "surge": {"possible": False},
      "gst": {"food_pct": 5, "fees_pct": 18}}

SEEDS = {
    "zepto": {"delivery": [{"min_subtotal": 0, "fee": 30}, {"min_subtotal": 99, "fee": 0}], "handling": 15,
              "small_cart": {"below": 99, "fee": 25}, "membership": {"name": "Zepto Pass", "waives": ["delivery", "small_cart"]}},
    "blinkit": {"delivery": [{"min_subtotal": 0, "fee": 25}, {"min_subtotal": 199, "fee": 0}], "handling": 9,
                "small_cart": {"below": 99, "fee": 20}},
    "instamart": {"delivery": [{"min_subtotal": 0, "fee": 35}, {"min_subtotal": 199, "fee": 0}], "handling": 10,
                  "small_cart": {"below": 99, "fee": 15}, "membership": {"name": "Swiggy One", "waives": ["delivery"]}},
    "swiggy": {"kind": "food", "delivery_by_km": [{"max_km": 3, "fee": 30}, {"max_km": 7, "fee": 45}, {"max_km": None, "fee": 65}],
               "packaging_per_item": 10, "platform": 20, "small_order": {"below": 149, "fee": 20},
               "gst": {"food_pct": 5, "fees_pct": 18}, "membership": {"name": "Swiggy One", "waives": ["delivery"]}},
    "zomato": {"kind": "food", "delivery_by_km": [{"max_km": 3, "fee": 40}, {"max_km": 7, "fee": 55}, {"max_km": None, "fee": 75}],
               "packaging_per_item": 20, "platform": 10, "small_order": {"below": 149, "fee": 25},
               "gst": {"food_pct": 5, "fees_pct": 18}, "membership": {"name": "Zomato Gold", "waives": ["delivery"]}},
}
for _r in SEEDS.values():
    _r.update({"surge": {"possible": True}, "source_url": None, "note": "Seed values; verify against the app before launch."})


# --- compute (grocery) -------------------------------------------------------------------------------------------

def test_compute_tiers_and_small_cart():
    assert fees.compute(RULES, 397, True, {"member": False}) == {"delivery": 0, "handling": 15, "small_cart": 0, "surge": 0, "total": 15, "estimated": False, "member": None}
    assert fees.compute(RULES, 80, True, {"member": False})["total"] == 70
    assert fees.compute(RULES, 199, True)["delivery"] == 0 and fees.compute(RULES, 198.99, True)["delivery"] == 30
    assert fees.compute(RULES, 99, True, {"member": False})["small_cart"] == 0     # not below 99
    assert fees.compute(RULES, 98.5, True, {"member": False})["small_cart"] == 25


def test_membership_waives_only_when_member_observed():
    assert fees.compute(RULES, 80, True, {"member": True})["total"] == 15
    assert fees.compute(RULES, 80, True, {"member": True})["member"] == "Zepto Pass"
    assert fees.compute(RULES, 80, True)["estimated"] is True       # membership defined, unknown
    assert fees.compute(RULES, 80, True)["total"] == 70             # unknown member: no waiver
    assert fees.compute(RULES, 80, True, {"member": False})["member"] is None
    named = fees.compute(RULES, 80, True, {"member": "Zepto Pass Plus"})
    assert (named["member"], named["delivery"], named["small_cart"]) == ("Zepto Pass Plus", 0, 0)
    no_plan = {k: v for k, v in RULES.items() if k != "membership"}
    plain = fees.compute(no_plan, 80, True, {"member": True})
    assert (plain["member"], plain["total"]) == (None, 70)           # nothing to waive without a membership rule


def test_observed_fee_beats_rule():
    f = fees.compute(RULES, 80, True, {"member": False, "delivery": 12})
    assert (f["delivery"], f["small_cart"], f["total"]) == (12, 25, 52)
    f = fees.compute(RULES, 80, True, {"member": True, "delivery": 12, "handling": 0})
    assert (f["delivery"], f["handling"], f["small_cart"], f["total"]) == (12, 0, 0, 12)   # observed beats waiver
    assert fees.compute(RULES, 397, True, {"member": False, "surge": 10})["total"] == 25


def test_estimated_when_unverified_or_surge_possible():
    assert fees.compute(RULES, 397, False, {"member": False})["estimated"] is True
    surge = copy.deepcopy(RULES)
    surge["surge"] = {"possible": True, "fee": 20}
    assert fees.compute(surge, 397, True, {"member": False})["estimated"] is True
    observed = fees.compute(surge, 397, True, {"member": False, "surge": 0})
    assert observed["estimated"] is False and observed["surge"] == 0
    # every component observed: an unverified rule contributes nothing
    all_obs = {"member": False, "delivery": 0, "handling": 15, "small_cart": 0, "surge": 0}
    assert fees.compute(RULES, 397, False, all_obs)["estimated"] is False


def test_zero_subtotal_is_all_zeros():
    assert fees.compute(RULES, 0, False) == {"delivery": 0, "handling": 0, "small_cart": 0, "surge": 0, "total": 0,
                                             "estimated": False, "member": None}
    z = fees.compute_food(SW, 0, 0, None, False)
    assert z["total"] == 0 and z["gst"] == 0 and z["estimated"] is False and z["member"] is None


# --- compute_food ------------------------------------------------------------------------------------------------

def test_compute_food_exact():
    f = fees.compute_food(SW, 700, 3, 0.15, True, {"packaging": 40})
    assert (f["delivery"], f["packaging"], f["platform"], f["small_order"], f["gst"], f["total"]) == (30, 40, 20, 0, 46.0, 136.0)
    assert f["estimated"] is False and f["member"] is None
    assert fees.compute_food(SW, 700, 3, None, True, {"packaging": 40})["delivery"] == 65    # unknown distance → last tier
    assert fees.compute_food(SW, 700, 3, None, True, {"packaging": 40})["estimated"] is True
    assert fees.compute_food(SW, 700, 3, 0.15, True)["packaging"] == 30                      # rule fallback, estimated
    assert fees.compute_food(SW, 700, 3, 0.15, True)["estimated"] is True
    assert fees.compute_food(SW, 700, 3, 3.0, True, {"packaging": 40})["delivery"] == 30     # max_km is inclusive
    assert fees.compute_food(SW, 700, 3, 3.01, True, {"packaging": 40})["delivery"] == 45


def test_compute_food_small_order_member_and_observed():
    f = fees.compute_food(SW, 120, 1, 1.0, True, {"packaging": 10})
    # gst = 5% × 130 + 18% × (30 + 20 + 20) = 6.5 + 12.6
    assert (f["small_order"], f["gst"], f["total"]) == (20, 19.1, 99.1)
    gold = dict(SW, membership={"name": "Swiggy One", "waives": ["delivery", "packaging"]})
    m = fees.compute_food(gold, 700, 3, 0.15, True, {"member": True})
    assert (m["delivery"], m["packaging"], m["member"], m["estimated"]) == (0, 0, "Swiggy One", False)
    assert fees.compute_food(gold, 700, 3, 0.15, True, {"member": True, "delivery": 15})["delivery"] == 15
    assert fees.compute_food(gold, 700, 3, 0.15, True, {"packaging": 40})["estimated"] is True     # member unknown
    assert fees.compute_food(SW, 700, 3, 0.15, True, {"packaging": 40, "gst": 50})["gst"] == 50
    assert fees.compute_food(SW, 700, 3, 0.15, False, {"packaging": 40})["estimated"] is True


def test_compute_food_matches_seeds():
    sw = fees.compute_food(SEEDS["swiggy"], 700, 3, 0.15, False, {"packaging": 40, "member": False, "surge": 0,
                                                                   "delivery": 30, "platform": 20})
    assert sw["total"] == 136.0
    zo = fees.compute_food(SEEDS["zomato"], 720, 3, 0.4, False, {"member": False})
    assert (zo["delivery"], zo["packaging"], zo["platform"], zo["gst"], zo["total"]) == (40, 60, 10, 48.0, 158.0)


# --- validate ----------------------------------------------------------------------------------------------------

def test_validate_accepts_seeds_and_test_rules():
    for site, rules in SEEDS.items():
        assert fees.validate(rules) == [], site
    assert fees.validate(RULES) == []
    assert fees.validate(SW) == []
    assert fees.validate(dict(RULES, kind="grocery")) == []


def test_validate_rejects_bad_rules():
    bad_tier = copy.deepcopy(RULES)
    bad_tier["delivery"][0]["min_subtotal"] = 10
    assert fees.validate(bad_tier)
    bad_gst = copy.deepcopy(SW)
    bad_gst["gst"]["food_pct"] = 40
    assert fees.validate(bad_gst)
    assert fees.validate(dict(RULES, tip=5))
    assert fees.validate(dict(SW, tip=5))
    assert any("tip" in e for e in fees.validate(dict(RULES, tip=5)))


def test_validate_grocery_details():
    def errs(**over):
        r = copy.deepcopy(RULES)
        r.update(over)
        return fees.validate(r)
    assert errs(delivery=[])
    assert errs(delivery=[{"min_subtotal": 0, "fee": 30}, {"min_subtotal": 0, "fee": 0}])       # not strictly ascending
    assert errs(delivery=[{"min_subtotal": 0, "fee": 30}, {"min_subtotal": 199, "fee": 0, "x": 1}])
    assert errs(delivery=[{"min_subtotal": 0, "fee": 501}])
    assert errs(handling=-1) and errs(handling="15") and errs(handling=True)
    assert errs(small_cart={"below": 2001, "fee": 10}) and errs(small_cart={"below": 99}) and not errs(small_cart=None)
    assert errs(surge={"possible": "yes"}) and errs(surge={"possible": True, "fee": 600}) and not errs(surge={"possible": True, "fee": 20})
    assert errs(membership={"name": "x" * 41, "waives": ["delivery"]})
    assert errs(membership={"name": "Pass", "waives": ["gst"]})
    assert errs(membership={"name": "", "waives": []})
    assert not errs(membership=None)
    assert errs(source_url=5) and not errs(source_url="https://example.com/fees")
    assert errs(note=5)
    assert errs(kind="pizza")
    noh = copy.deepcopy(RULES)
    del noh["delivery"]
    assert fees.validate(noh)
    assert fees.validate("rules") and fees.validate(None)


def test_validate_food_details():
    def errs(**over):
        r = copy.deepcopy(SW)
        r.update(over)
        return fees.validate(r)
    assert errs(delivery_by_km=[{"max_km": 3, "fee": 30}])                                      # last must be null
    assert errs(delivery_by_km=[{"max_km": 7, "fee": 30}, {"max_km": 3, "fee": 30}, {"max_km": None, "fee": 65}])
    assert errs(delivery_by_km=[{"max_km": None, "fee": 30}, {"max_km": None, "fee": 65}])
    assert not errs(delivery_by_km=[{"max_km": None, "fee": 30}])
    assert errs(packaging_per_item=501) and errs(platform=-2)
    assert errs(gst={"food_pct": 5}) and errs(gst={"food_pct": 5, "fees_pct": 29}) and errs(gst={"food_pct": 5, "fees_pct": 18, "x": 1})
    assert errs(small_order={"below": 3000, "fee": 20})
    assert errs(membership={"name": "Swiggy One", "waives": ["handling"]})                      # grocery component
    assert not errs(membership={"name": "Swiggy One", "waives": ["delivery", "packaging", "platform"]})
    assert errs(handling=10)                                                                    # grocery key on food

"""Product and dish matching (Plan 5 R4, R14): pure, no database, the model judge is always a fake."""
import json
import pathlib
import re

import pytest

from server import match
from server.match import Size


def P(name, price=20.0, brand=None, size=None, in_stock=True, pid=None):
    p = match.from_raw({"product_id": pid or name, "name": name, "brand": brand, "pack_size": size,
                        "price": price, "mrp": None, "in_stock": in_stock})
    assert p is not None
    return p


def D(name, price, veg=None, in_stock=True, variant=None, packaging=None, did=None):
    d = match.dish_from_raw({"dish_id": did or name, "name": name, "variant": variant, "price": price,
                             "veg": veg, "in_stock": in_stock, "packaging": packaging})
    assert d is not None
    return d


def never_judge(query, names):
    raise AssertionError("the judge must not be called")


# --- sizes, tokens, queries -------------------------------------------------------------------

def test_parse_size_units_and_multipacks():
    assert match.parse_size("Amul Taaza Toned Milk 1 L") == Size(1000, "ml")
    assert match.parse_size("Coke 6 x 300 ml") == Size(1800, "ml")
    assert match.parse_size("Pack of 2 (500 g each)") == Size(1000, "g")
    assert match.parse_size("Coriander Bunch") is None


def test_parse_size_unit_spellings():
    assert match.parse_size("Aashirvaad Atta 5kg") == Size(5000, "g")
    assert match.parse_size("Sugar 1.5 kg") == Size(1500, "g")
    assert match.parse_size("Oil 1 ltr") == Size(1000, "ml")
    assert match.parse_size("Juice 2 Litres") == Size(2000, "ml")
    assert match.parse_size("Rice 500 gms") == Size(500, "g")
    assert match.parse_size("Dal 250 gm") == Size(250, "g")
    assert match.parse_size("Cheese 200 grams") == Size(200, "g")
    assert match.parse_size("Coke 300 ml x 6") == Size(1800, "ml")
    assert match.parse_size("Eggs 6 pcs") == Size(6, "pc")
    assert match.parse_size("Banana 4 pieces") == Size(4, "pc")
    assert match.parse_size("Lemon 3 nos") == Size(3, "pc")
    assert match.parse_size("Soap 4 units") == Size(4, "pc")
    assert match.parse_size("Maggi Noodles 12 pack") == Size(12, "pc")
    assert match.parse_size("Coca Cola 750 ml (Pack of 2)") == Size(1500, "ml")
    assert match.parse_size("") is None


def test_parse_size_dozen():
    assert match.parse_size("Eggs 1 dozen") == Size(12, "pc")
    assert match.parse_size("Bananas 2 Dozen") == Size(24, "pc")
    assert match.tokens("Eggs 1 dozen") == frozenset({"eggs"})


def test_name_size_times_pack_size_count():
    p = match.from_raw({"product_id": "a", "name": "Bread 400 g", "pack_size": "Pack of 2", "price": 90})
    assert p.size == Size(800, "g")
    p = match.from_raw({"product_id": "b", "name": "Bread 2 x 400 g", "pack_size": "Pack of 2", "price": 90})
    assert p.size == Size(800, "g")                                # the name already counts the packs
    p = match.from_raw({"product_id": "c", "name": "Eggs", "pack_size": "Pack of 6", "price": 50})
    assert p.size == Size(6, "pc")
    p = match.from_raw({"product_id": "d", "name": "Milk 1 L", "pack_size": "500 ml", "price": 29})
    assert p.size == Size(500, "ml")                               # a real pack size still wins


def test_tokens_keep_hindi_combining_marks():
    assert match.tokens("अमूल दूध 1 L") == frozenset({"अमूल", "दूध"})
    assert match.tokens("Toor Dal / अरहर दाल") == frozenset({"toor", "dal", "अरहर", "दाल"})
    m = match.match_item(match.parse_query(0, "दूध", 1), [P("अमूल दूध 500 ml", 29)])
    assert m["status"] == "matched"


def test_tokens_drop_sizes_stopwords_and_single_characters():
    assert match.tokens("Lay's Classic Salted Chips 52 g") == frozenset({"lay", "classic", "salted", "chips"})
    assert match.tokens("Pack of 2 Fresh Eggs 6 pcs each") == frozenset({"eggs"})
    assert match.tokens("Amul Taaza 1L") == frozenset({"amul", "taaza"})
    assert match.tokens("Atta 5kg x 2") == frozenset({"atta"})


def test_parse_query_reads_quantity_suffix():
    q = match.parse_query(0, "Amul Taaza 1L ×2", 1)
    assert (q.qty, q.size, q.tokens) == (2, Size(1000, "ml"), frozenset({"amul", "taaza"}))


def test_parse_query_suffix_forms():
    assert match.parse_query(1, "Bread x3", 2).qty == 6
    assert match.parse_query(2, "Bread x 3", 1).qty == 3
    assert match.parse_query(3, "Bread * 4", 1).qty == 4
    q = match.parse_query(4, "Coke 6 x 300 ml", 1)
    assert (q.qty, q.size, q.idx, q.text) == (1, Size(1800, "ml"), 4, "Coke 6 x 300 ml")


def test_from_raw_size_from_pack_size_then_name_and_rejects_bad_price():
    p = match.from_raw({"product_id": "a", "name": "Amul Taaza Milk", "brand": "Amul", "pack_size": "500 ml",
                        "price": 29, "mrp": 30, "in_stock": True})
    assert (p.size, p.size_label, p.price, p.mrp, p.in_stock) == (Size(500, "ml"), "500 ml", 29.0, 30.0, True)
    p = match.from_raw({"product_id": "b", "name": "Amul Taaza Milk 1 L", "price": "60"})
    assert (p.size, p.in_stock) == (Size(1000, "ml"), True)
    assert p.size_label == "1 L"
    assert match.from_raw({"product_id": "c", "name": "X", "price": 0}) is None
    assert match.from_raw({"product_id": "c", "name": "X", "price": None}) is None
    assert match.from_raw({"product_id": "c", "name": "X"}) is None
    assert match.from_raw({"product_id": "c", "name": "X", "price": "abc"}) is None


def test_score_prefers_full_query_coverage():
    q = match.parse_query(0, "Lay's Classic", 1)
    assert match.score(q, P("Lay's Classic Salted Chips 52 g")) >= match.SURE
    assert match.NEAR <= match.score(q, P("Lay's Magic Masala Chips 52 g")) < match.SURE


def test_score_size_component():
    q = match.parse_query(0, "Toned Milk 1L", 1)
    exact = match.score(q, P("Toned Milk 1 L"))
    half = match.score(q, P("Toned Milk 500 ml"))
    tiny = match.score(q, P("Toned Milk 200 ml"))
    other_dim = match.score(q, P("Toned Milk 1 kg"))
    assert exact == pytest.approx(1.0)
    assert exact - half == pytest.approx(0.25 * 0.4)
    assert exact - tiny == pytest.approx(0.25 * 0.8)
    assert exact - other_dim == pytest.approx(0.25)


def test_score_uses_brand_tokens():
    q = match.parse_query(0, "Amul Butter", 1)
    assert match.score(q, P("Butter 100 g", brand="Amul")) >= match.SURE


# --- match_item --------------------------------------------------------------------------------

def test_buys_two_half_litre_packs_for_one_litre():
    m = match.match_item(match.parse_query(0, "Amul Taaza Milk 1L", 1),
                         [P("Amul Taaza Toned Milk 500 ml", 29), P("Amul Masti Buttermilk 200 ml", 15)])
    assert (m["status"], m["product"]["name"], m["packs"], m["line_total"]) == \
        ("matched", "Amul Taaza Toned Milk 500 ml", 2, 58.0)
    assert m["confidence"] == "sure" and m["swap_from"] is None and m["idx"] == 0


def test_highest_score_wins_over_cheaper_lower_score():
    # controller ruling (fix round 1): price only breaks ties among equal scores
    m = match.match_item(match.parse_query(0, "Amul Taaza Milk 1L", 1),
                         [P("Amul Taaza Toned Milk 500 ml", 29), P("Amul Taaza Toned Milk 1 L", 60)])
    assert (m["product"]["name"], m["packs"], m["line_total"]) == ("Amul Taaza Toned Milk 1 L", 1, 60.0)


def test_plain_butter_never_priced_as_garlic_butter():
    m = match.match_item(match.parse_query(0, "Amul Butter 100 g", 1),
                         [P("Amul Butter 100 g", 56), P("Amul Garlic Butter 100 g", 50)])
    assert m["product"]["name"] == "Amul Butter 100 g"


def test_quantity_multiplies_the_wanted_amount():
    m = match.match_item(match.parse_query(0, "Amul Taaza Milk 1L ×2", 1), [P("Amul Taaza Toned Milk 500 ml", 29)])
    assert (m["product"]["name"], m["packs"], m["line_total"]) == ("Amul Taaza Toned Milk 500 ml", 4, 116.0)


def test_score_at_sure_boundary_counts_as_sure():
    q = match.parse_query(0, "Amul Taaza Milk 1L", 1)
    p = P("Amul Milk 1 L", 60)
    assert match.score(q, p) < match.SURE                      # 0.7999999999999999 in floating point
    assert match.score(q, p) == pytest.approx(match.SURE)
    m = match.match_item(q, [p], judge=never_judge)
    assert (m["status"], m["confidence"]) == ("matched", "sure")


def test_never_buys_more_than_one_and_a_half_times():
    m = match.match_item(match.parse_query(0, "Aashirvaad Atta 500 g", 1), [P("Aashirvaad Atta 1 kg", 60)])
    assert m["status"] == "pick" and m["product"] is None and m["line_total"] is None
    assert [c["name"] for c in m["candidates"]] == ["Aashirvaad Atta 1 kg"]
    assert m["candidates"][0]["packs"] == 1


def test_ambiguous_dimensions_ask_user():
    m = match.match_item(match.parse_query(0, "milk", 1),
                         [P("Amul Taaza Toned Milk 1 L", 60), P("Milk Bikis Biscuits 200 g", 30)])
    assert m["status"] == "pick" and len(m["candidates"]) == 2
    assert m["product"] is None


def test_no_size_picks_cheapest_equivalent():
    m = match.match_item(match.parse_query(0, "bread", 2),
                         [P("White Bread 400 g", 50, pid="a"), P("White Bread 400 g", 45, pid="b"),
                          P("White Bread 800 g", 80, pid="c")])
    assert (m["status"], m["product"]["product_id"], m["packs"], m["line_total"]) == ("matched", "b", 2, 90.0)


def test_tie_on_cost_prefers_higher_score_then_lower_id():
    q = match.parse_query(0, "Amul Butter 100 g", 1)
    m = match.match_item(q, [P("Amul Butter Salted 100 g", 56, pid="z"), P("Amul Butter 100 g", 56, pid="y")])
    assert m["product"]["product_id"] == "y"
    m = match.match_item(q, [P("Amul Butter 100 g", 56, pid="b"), P("Amul Butter 100 g", 56, pid="a")])
    assert m["product"]["product_id"] == "a"


def test_swap_same_brand_when_out_of_stock():
    m = match.match_item(match.parse_query(0, "Lay's Classic 52 g", 1),
                         [P("Lay's Classic Salted Chips 52 g", 20, brand="Lay's", in_stock=False),
                          P("Lay's Classic Salted Potato Chips Value Party Pack 60 g", 25, brand="Lay's"),
                          P("Lay's Magic Masala Chips 52 g", 20, brand="Lay's")])
    assert (m["status"], m["product"]["name"], m["packs"], m["line_total"]) == \
        ("swapped", "Lay's Classic Salted Potato Chips Value Party Pack 60 g", 1, 25.0)
    assert m["swap_from"]["name"] == "Lay's Classic Salted Chips 52 g"
    assert m["swap_from"]["in_stock"] is False


def test_swap_must_keep_every_non_brand_query_word():
    # Magic Masala is the same brand, dimension and size, but it is not "Classic": ask instead
    m = match.match_item(match.parse_query(0, "Lay's Classic", 1),
                         [P("Lay's Classic Salted Chips 52 g", 20, brand="Lay's", in_stock=False),
                          P("Lay's Magic Masala Chips 52 g", 20, brand="Lay's")])
    assert (m["status"], m["product"], m["swap_from"]) == ("pick", None, None)
    m = match.match_item(match.parse_query(0, "Amul Butter", 1),
                         [P("Butter 100 g", 56, brand="Amul", in_stock=False),
                          P("Cheese Slices 100 g", 60, brand="Amul")])
    assert (m["status"], m["product"]) == ("pick", None)


def test_swap_needs_a_brand_on_both_products():
    m = match.match_item(match.parse_query(0, "Organic Tomato", 1),
                         [P("Organic Tomato 500 g", 40, in_stock=False), P("Organic Onion 500 g", 30)])
    assert (m["status"], m["product"]) == ("pick", None)
    m = match.match_item(match.parse_query(0, "Lay's Classic 52 g", 1),
                         [P("Lay's Classic Salted Chips 52 g", 20, brand="Lay's", in_stock=False),
                          P("Lay's Classic Salted Potato Chips Value Party Pack 60 g", 25)])
    assert m["status"] == "pick"


def test_out_of_stock_without_substitute_or_alternative():
    m = match.match_item(match.parse_query(0, "Lay's Classic", 1),
                         [P("Lay's Classic Salted Chips 52 g", 20, brand="Lay's", in_stock=False),
                          P("Bingo Mad Angles 52 g", 20, brand="Bingo")])
    assert m["status"] == "out_of_stock" and m["swap_from"] is None and m["line_total"] is None
    assert m["product"]["name"] == "Lay's Classic Salted Chips 52 g"


def test_out_of_stock_with_other_candidates_asks_user():
    m = match.match_item(match.parse_query(0, "Lay's Classic", 1),
                         [P("Lay's Classic Salted Chips 52 g", 20, brand="Lay's", in_stock=False),
                          P("Bingo Classic Salted Chips 52 g", 20, brand="Bingo")])
    assert (m["status"], m["product"]) == ("pick", None)
    assert [c["name"] for c in m["candidates"]] == ["Lay's Classic Salted Chips 52 g",
                                                     "Bingo Classic Salted Chips 52 g"]


def test_near_match_without_judge_asks_user():
    m = match.match_item(match.parse_query(0, "Lay's Classic", 1), [P("Lay's Magic Masala Chips 52 g", 20)])
    assert (m["status"], m["confidence"], m["product"]) == ("pick", None, None)
    assert len(m["candidates"]) == 1


def test_near_match_confirmed_by_judge():
    m = match.match_item(match.parse_query(0, "Lay's Classic", 1), [P("Lay's Magic Masala Chips 52 g", 20)],
                         judge=lambda q, names: [True])
    assert (m["status"], m["confidence"], m["product"]["name"]) == ("matched", "confirmed", "Lay's Magic Masala Chips 52 g")


def test_judge_rejection_asks_user():
    m = match.match_item(match.parse_query(0, "Lay's Classic", 1), [P("Lay's Magic Masala Chips 52 g", 20)],
                         judge=lambda q, names: [False])
    assert m["status"] == "pick"


def test_judge_called_once_with_top_three():
    calls = []

    def judge(query, names):
        calls.append((query, names))
        return [False, True, False]

    products = [P("Lay's Chips Magic Masala 52 g", 20, pid="1"),       # 0.6*.5+.15*.25+.25
                P("Lay's Chips Tomato 52 g", 21, pid="2"),             # 0.6*.5+.15*.33+.25
                P("Lay's Chile Limon 52 g", 22, pid="3"),
                P("Lay's Cream Onion Chips Party 52 g", 23, pid="4"),  # lowest NEAR
                P("Lay's Masala 52 g", 5, pid="5", in_stock=False)]
    m = match.match_item(match.parse_query(0, "Lay's Classic", 1), products, judge=judge)
    assert len(calls) == 1
    query, names = calls[0]
    assert query == "Lay's Classic"
    assert len(names) == 3 and "Lay's Cream Onion Chips Party 52 g" not in names
    assert "Lay's Masala 52 g" not in names
    assert m["status"] == "matched" and m["confidence"] == "confirmed" and m["product"]["name"] == names[1]


def test_judge_not_called_when_sure_in_stock():
    m = match.match_item(match.parse_query(0, "Lay's Classic", 1),
                         [P("Lay's Classic Salted Chips 52 g", 20), P("Lay's Magic Masala Chips 52 g", 20)],
                         judge=never_judge)
    assert (m["status"], m["confidence"]) == ("matched", "sure")


def test_judge_not_called_without_near_candidates():
    m = match.match_item(match.parse_query(0, "Lay's Classic", 1), [P("Bingo Mad Angles 52 g", 20)],
                         judge=never_judge)
    assert m["status"] == "not_found"


def test_not_found_and_candidate_cap():
    m = match.match_item(match.parse_query(0, "Coriander Bunch", 1), [P("Amul Taaza Toned Milk 1 L", 60)])
    assert (m["status"], m["candidates"], m["product"], m["packs"], m["line_total"]) == \
        ("not_found", [], None, None, None)
    many = [P(f"Biscuits Variety{i} Pack {i}", 10 + i, pid=str(i)) for i in range(8)]
    m = match.match_item(match.parse_query(0, "Cream Biscuits", 1), many)
    assert m["status"] == "pick" and len(m["candidates"]) == match.MAX_CANDIDATES
    c = m["candidates"][0]
    assert set(c) == {"product_id", "name", "brand", "size_label", "price", "mrp", "in_stock", "packs",
                      "line_total", "score"}
    assert all(c["score"] >= match.SHOW for c in m["candidates"])
    json.dumps(m)


def test_constants():
    assert (match.SURE, match.NEAR, match.SHOW, match.MAX_CANDIDATES, match.JUDGE_TOP) == (0.8, 0.5, 0.3, 5, 3)


# --- provider neutrality ------------------------------------------------------------------------

def test_match_is_provider_neutral():
    src = pathlib.Path(match.__file__).read_text(encoding="utf-8")
    for name in ("agent", "metering", "openai", "anthropic"):
        assert not re.search(rf"^\s*(import|from)\s+{name}\b", src, re.M), name
    assert not hasattr(match, "llm_judge") and not hasattr(match, "count_openai")
    assert match.Judge is not None


# --- dishes ------------------------------------------------------------------------------------

def test_dish_traits():
    assert match.dish_traits("Chicken Biryani - Half")[1] == "half"
    assert match.dish_traits("Paneer Tikka (Serves 2)")[2] == 2
    assert match.dish_traits("Burger with Extra Cheese")[3] == frozenset({"cheese"})


def test_dish_traits_full_tuple():
    assert match.dish_traits("Chicken Biryani (Half)") == ("Chicken Biryani", "half", None, frozenset())
    assert match.dish_traits("Butter Chicken Full Plate") == ("Butter Chicken", "full", None, frozenset())
    assert match.dish_traits("Margherita Pizza [Large]") == ("Margherita Pizza", "large", None, frozenset())
    assert match.dish_traits("Family Pack Biryani for 4") == ("Family Pack Biryani", None, 4, frozenset())
    assert match.dish_traits("Veg Burger + Coke") == ("Veg Burger", None, None, frozenset({"coke"}))
    assert match.dish_traits("Chicken 65") == ("Chicken 65", None, None, frozenset())


def test_dish_from_raw():
    d = match.dish_from_raw({"dish_id": "1", "name": "Chicken Biryani", "variant": "Half Plate", "price": 220,
                             "veg": False, "in_stock": None, "packaging": 15})
    assert (d.variant, d.veg, d.in_stock, d.packaging, d.price) == ("half", False, True, 15.0, 220.0)
    assert match.dish_from_raw({"dish_id": "2", "name": "X", "price": 0}) is None
    assert match.dish_from_raw({"dish_id": "3", "price": 10}) is None


def test_variant_and_veg_must_match():
    q = match.parse_dish_query(0, "Chicken Biryani (Half)", 2, variant="half", veg=False)
    m = match.match_dish(q, [D("Chicken Biryani - Full", 395, veg=False), D("Chicken Biryani - Half", 235, veg=False),
                             D("Veg Biryani (Half)", 120, veg=True)])
    assert (m["status"], m["product"]["name"], m["line_total"]) == ("matched", "Chicken Biryani - Half", 470.0)
    assert (m["packs"], m["swap_from"], m["confidence"]) == (2, None, "sure")
    assert {c["name"] for c in m["candidates"]} == {"Chicken Biryani - Half"}


def test_half_query_never_priced_as_full():
    q = match.parse_dish_query(0, "Chicken Biryani (Half)", 1)
    m = match.match_dish(q, [D("Chicken Biryani - Full", 395, veg=False)])
    assert m["status"] == "not_found"


def test_dish_with_variants_asks_user():
    q = match.parse_dish_query(0, "Chicken Biryani", 1)
    m = match.match_dish(q, [D("Chicken Biryani - Half", 235), D("Chicken Biryani - Full", 395)])
    assert m["status"] == "pick" and m["product"] is None
    assert {c["name"] for c in m["candidates"]} == {"Chicken Biryani - Half", "Chicken Biryani - Full"}


def test_dish_without_variants_on_either_side_matches():
    q = match.parse_dish_query(0, "Paneer Biryani", 1, veg=True)
    m = match.match_dish(q, [D("Paneer Biryani", 260, veg=True, packaging=10), D("Veg Biryani", 200, veg=True)])
    assert (m["status"], m["product"]["name"], m["product"]["packaging"]) == ("matched", "Paneer Biryani", 10.0)


def test_unknown_dish_variant_is_not_sure_for_known_query_variant():
    q = match.parse_dish_query(0, "Chicken Biryani", 1, variant="half")
    m = match.match_dish(q, [D("Chicken Biryani", 300)])
    assert m["status"] == "pick"
    assert m["candidates"][0]["score"] == pytest.approx(match.SURE - 0.01)


def test_addon_mismatch_is_not_sure():
    q = match.parse_dish_query(0, "Classic Burger", 1)
    m = match.match_dish(q, [D("Classic Burger with Extra Cheese", 150)])
    assert m["status"] == "pick" and m["candidates"][0]["score"] <= match.SURE - 0.01
    q = match.parse_dish_query(0, "Classic Burger with Extra Cheese", 1)
    m = match.match_dish(q, [D("Classic Burger with Extra Cheese", 150), D("Classic Burger", 120)])
    assert (m["status"], m["product"]["name"]) == ("matched", "Classic Burger with Extra Cheese")


def test_serves_must_match():
    q = match.parse_dish_query(0, "Paneer Tikka (Serves 2)", 1)
    m = match.match_dish(q, [D("Paneer Tikka (Serves 4)", 400), D("Paneer Tikka (Serves 2)", 250)])
    assert m["product"]["name"] == "Paneer Tikka (Serves 2)"
    assert len(m["candidates"]) == 1


def test_unavailable_dish_is_out_of_stock():
    q = match.parse_dish_query(0, "Chicken 65", 1)
    m = match.match_dish(q, [D("Chicken 65", 240, in_stock=False), D("Chicken Lollipop", 260)])
    assert (m["status"], m["product"]["name"], m["line_total"]) == ("out_of_stock", "Chicken 65", None)


def test_dish_query_quantity_suffix():
    q = match.parse_dish_query(3, "Chicken 65 x2", 1)
    assert (q.idx, q.qty, q.tokens) == (3, 2, frozenset({"chicken", "65"}))
    m = match.match_dish(q, [D("Chicken 65", 240)])
    assert (m["status"], m["packs"], m["line_total"]) == ("matched", 2, 480.0)
    json.dumps(m)


def test_paneer_tikka_never_priced_as_paneer_tikka_roll():
    q = match.parse_dish_query(0, "Paneer Tikka", 1)
    m = match.match_dish(q, [D("Paneer Tikka", 280), D("Paneer Tikka Roll", 160)])
    assert (m["status"], m["product"]["name"]) == ("matched", "Paneer Tikka")
    m = match.match_dish(q, [D("Paneer Tikka Roll", 160)])
    assert (m["status"], m["product"]) == ("pick", None)


def test_chicken_biryani_never_priced_as_rice_bowl():
    q = match.parse_dish_query(0, "Chicken Biryani", 1)
    m = match.match_dish(q, [D("Chicken Biryani", 300), D("Chicken Biryani Rice Bowl", 199)])
    assert (m["status"], m["product"]["name"]) == ("matched", "Chicken Biryani")
    m = match.match_dish(q, [D("Chicken Biryani Rice Bowl", 199)])
    assert m["status"] == "pick"


def test_dish_missing_a_query_word_is_not_sure():
    q = match.parse_dish_query(0, "Hyderabadi Chicken Dum Biryani", 1)
    m = match.match_dish(q, [D("Chicken Dum Biryani", 300)])      # 3 of 4 words: 0.45 + 0.15 + 0.25 = 0.85
    assert m["status"] == "pick"


def test_dish_highest_score_wins_then_price():
    q = match.parse_dish_query(0, "Chicken Biryani", 1)
    m = match.match_dish(q, [D("Chicken Biryani", 300), D("Special Chicken Biryani", 250)])
    assert m["product"]["name"] == "Chicken Biryani"
    m = match.match_dish(q, [D("Chicken Biryani", 300, did="b"), D("Chicken Biryani", 280, did="a")])
    assert m["product"]["price"] == 280.0


def test_half_and_full_before_the_name():
    q = match.parse_dish_query(0, "Half Tandoori Chicken", 1)
    assert (q.variant, q.tokens) == ("half", frozenset({"tandoori", "chicken"}))
    assert match.match_dish(q, [D("Tandoori Chicken (Full)", 450)])["status"] == "not_found"
    assert match.dish_traits("Full Butter Chicken")[:2] == ("Butter Chicken", "full")
    assert match.dish_traits("Half Fry Egg")[1] is None             # "half fry" is the dish, not a portion


def test_leading_extra_is_not_an_addon():
    assert match.dish_traits("Extra Long Hot Dog") == ("Extra Long Hot Dog", None, None, frozenset())

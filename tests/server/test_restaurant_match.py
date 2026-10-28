"""Restaurant pairing across Swiggy and Zomato (Plan 5 R13): pure, no database."""
import json

import pytest

from server import match, restaurant_match as rm

GORWA = (22.3310, 73.1510)
ALKAPURI = (22.3100, 73.1800)
USER = (22.3300, 73.1500)


def L(site, name, lat=None, lon=None, locality=None, rid=None, distance_km=None, is_open=True):
    item = rm.listing_from_raw(site, {"restaurant_id": rid or f"{site}-{name}", "name": name, "locality": locality,
                                      "lat": lat, "lon": lon, "distance_km": distance_km, "is_open": is_open,
                                      "url": f"https://{site}.example/{rid or name}"})
    assert item is not None
    return item


def test_listing_from_raw():
    item = rm.listing_from_raw("zomato", {"restaurant_id": 42, "name": "Meghana Foods", "lat": "22.331",
                                          "lon": "73.151", "distance_km": "1.2", "is_open": None})
    assert (item.site, item.restaurant_id, item.lat, item.lon, item.distance_km, item.is_open, item.locality,
            item.url) == ("zomato", "42", 22.331, 73.151, 1.2, None, None, None)
    assert rm.listing_from_raw("zomato", {"name": "No id"}) is None
    assert rm.listing_from_raw("zomato", {"restaurant_id": "1", "name": " "}) is None
    assert rm.listing_from_raw("zomato", {"restaurant_id": "1", "name": "X", "lat": "abc"}).lat is None


def test_name_tokens_drop_business_words():
    assert rm.name_tokens("Meghana Foods Restaurant Pvt Ltd") == frozenset({"meghana", "foods"})


def test_haversine():
    assert rm.haversine_km(*GORWA, *GORWA) == 0
    assert rm.haversine_km(22.3320, 73.1480, *ALKAPURI) == pytest.approx(4.1, abs=0.1)


def test_same_restaurant_same_place_is_sure():
    a = L("swiggy", "Meghana Foods", *GORWA)
    b = L("zomato", "Meghana Foods", 22.3310 + 0.0009, 73.1510)   # ~0.1 km
    assert 0.05 < rm.haversine_km(a.lat, a.lon, b.lat, b.lon) < 0.15
    assert rm.score(a, b) >= match.SURE


def test_name_variant_same_place_asks():
    s = rm.score(L("swiggy", "Truffles", 22.3290, 73.1490), L("zomato", "Truffles Cafe", 22.3290, 73.1490))
    assert match.NEAR <= s < match.SURE


def test_place_steps_by_distance():
    a = L("swiggy", "Meghana Foods", 22.0, 73.0)

    def at(km):
        return L("zomato", "Meghana Foods", 22.0 + km / 111.195, 73.0)

    assert rm.score(a, at(0.2)) == pytest.approx(1.0)
    assert rm.score(a, at(0.8)) == pytest.approx(0.6 + 0.4 * 0.7)
    assert rm.score(a, at(1.5)) == pytest.approx(0.6 + 0.4 * 0.3)
    assert rm.score(a, at(2.5)) == pytest.approx(0.6)
    assert rm.score(a, at(3.2)) == 0


def test_same_name_far_apart_is_not_paired():
    sw = L("swiggy", "Behrouz Biryani", 22.3320, 73.1480, rid="s-b")
    zg = L("zomato", "Behrouz Biryani", 22.3320, 73.1480, rid="z-gorwa")
    za = L("zomato", "Behrouz Biryani", *ALKAPURI, rid="z-alkapuri")
    assert rm.score(sw, za) == 0
    pairs = rm.pair_results({"swiggy": [sw], "zomato": [za, zg]}, *USER, {"swiggy": "ok", "zomato": "ok"})
    assert len(pairs) == 2
    paired = [p for p in pairs if p["confidence"] == "sure"]
    assert len(paired) == 1
    assert paired[0]["listings"]["swiggy"]["restaurant_id"] == "s-b"
    assert paired[0]["listings"]["zomato"]["restaurant_id"] == "z-gorwa"
    single = [p for p in pairs if p["confidence"] == "single"][0]
    assert single["listings"] == {"swiggy": None, "zomato": single["listings"]["zomato"]}
    assert single["listings"]["zomato"]["restaurant_id"] == "z-alkapuri"
    assert single["note"] == "Only on Zomato"
    assert pairs[0] is paired[0]       # nearer first


def test_locality_used_without_coordinates():
    a = L("swiggy", "Meghana Foods", locality="Gorwa")
    assert rm.score(a, L("zomato", "Meghana Foods", locality="Gorwa, Vadodara")) == pytest.approx(0.6 + 0.4 * 0.7)
    assert rm.score(a, L("zomato", "Meghana Foods", locality="Alkapuri")) == pytest.approx(0.6)
    # an unknown place (0.6 + 0.4 × 0.5 = 0.8) is capped below SURE: ask, never pair silently
    assert rm.score(a, L("zomato", "Meghana Foods")) == pytest.approx(match.SURE - 0.01)
    # one side has coordinates only: fall back to localities, and one of them is unknown
    assert rm.score(L("swiggy", "Meghana Foods", *GORWA), L("zomato", "Meghana Foods", locality="Gorwa")) == \
        pytest.approx(match.SURE - 0.01)
    assert rm.score(L("swiggy", "Meghana Foods", *GORWA, locality="Gorwa"),
                    L("zomato", "Meghana Foods", locality="Gorwa")) == pytest.approx(0.6 + 0.4 * 0.7)


def test_unknown_place_is_ask_not_sure():
    s = rm.score(L("swiggy", "Behrouz Biryani"), L("zomato", "Behrouz Biryani"))
    assert match.NEAR <= s < match.SURE - 1e-9
    s = rm.score(L("swiggy", "Behrouz Biryani", distance_km=0.5), L("zomato", "Behrouz Biryani", distance_km=0.7))
    assert match.NEAR <= s < match.SURE - 1e-9
    [p] = rm.pair_results({"swiggy": [L("swiggy", "Behrouz Biryani", rid="s")],
                           "zomato": [L("zomato", "Behrouz Biryani", rid="z")]}, None, None,
                          {"swiggy": "ok", "zomato": "ok"})
    assert (p["confidence"], p["note"]) == ("ask", "Check this is the same restaurant")


def test_reported_distances_more_than_3_km_apart_never_pair():
    a = L("swiggy", "Behrouz Biryani", distance_km=0.5)
    assert rm.score(a, L("zomato", "Behrouz Biryani", distance_km=4.8)) == 0
    assert rm.score(a, L("zomato", "Behrouz Biryani", locality="Gorwa", distance_km=3.6)) == 0
    assert rm.score(a, L("zomato", "Behrouz Biryani", distance_km=3.4)) > 0


def test_differing_localities_cap_below_sure():
    a = L("swiggy", "Behrouz Biryani", locality="Sector 14, Gurgaon")
    assert match.NEAR <= rm.score(a, L("zomato", "Behrouz Biryani", locality="Sector 52, Gurgaon")) < match.SURE - 1e-9
    assert match.NEAR <= rm.score(L("swiggy", "Behrouz Biryani", locality="Sector 5"),
                                  L("zomato", "Behrouz Biryani", locality="Sector 9")) < match.SURE - 1e-9
    # same coordinates do not override a locality conflict
    assert rm.score(L("swiggy", "Behrouz Biryani", *GORWA, locality="Sector 14"),
                    L("zomato", "Behrouz Biryani", *GORWA, locality="Sector 52")) < match.SURE - 1e-9
    # a locality that only adds the city is not a conflict
    assert rm.score(L("swiggy", "Behrouz Biryani", locality="Sector 14"),
                    L("zomato", "Behrouz Biryani", locality="Sector 14, Gurgaon")) >= match.SURE


def test_pair_results_notes():
    by_site = {"swiggy": [L("swiggy", "Theobroma", 22.3295, 73.1495), L("swiggy", "Truffles", 22.3290, 73.1490),
                          L("swiggy", "Meghana Foods", *GORWA)],
               "zomato": [L("zomato", "Truffles Cafe", 22.3290, 73.1490), L("zomato", "Meghana Foods", *GORWA)]}
    pairs = rm.pair_results(by_site, *USER, {"swiggy": "ok", "zomato": "ok"})
    by_name = {p["name"]: p for p in pairs}
    assert by_name["Theobroma"]["confidence"] == "single" and by_name["Theobroma"]["note"] == "Only on Swiggy"
    assert by_name["Truffles"]["confidence"] == "ask"
    assert by_name["Truffles"]["note"] == "Check this is the same restaurant"
    assert by_name["Truffles"]["listings"]["zomato"]["name"] == "Truffles Cafe"
    assert by_name["Meghana Foods"]["confidence"] == "sure" and by_name["Meghana Foods"]["note"] is None
    json.dumps(pairs)

    only_swiggy = {"swiggy": [L("swiggy", "Theobroma", 22.3295, 73.1495)]}
    p = rm.pair_results(only_swiggy, *USER, {"swiggy": "ok", "zomato": "needs_login"})[0]
    assert (p["confidence"], p["note"]) == ("single", "Log in to Zomato again to compare")
    assert p["listings"] == {"swiggy": p["listings"]["swiggy"], "zomato": None}
    p = rm.pair_results(only_swiggy, *USER, {"swiggy": "ok", "zomato": "not_connected"})[0]
    assert p["note"] == "Connect Zomato to compare"


def test_pair_results_sorted_by_distance_then_name_and_capped():
    swiggy = [L("swiggy", f"Place {chr(65 + i)}", 22.33 + i * 0.01, 73.15) for i in range(12)]
    swiggy.append(L("swiggy", "Aaa Near", 22.3300, 73.1500))
    swiggy.append(L("swiggy", "Bbb Near", 22.3300, 73.1500))
    pairs = rm.pair_results({"swiggy": swiggy, "zomato": []}, *USER, {"swiggy": "ok", "zomato": "ok"})
    assert len(pairs) == 10
    assert [p["name"] for p in pairs[:3]] == ["Aaa Near", "Bbb Near", "Place A"]
    distances = [p["distance_km"] for p in pairs]
    assert distances == sorted(distances)


def test_pair_results_prefers_reported_distance_and_keys_are_unique():
    a = L("swiggy", "Meghana Foods", *GORWA, rid="1", distance_km=0.4)
    b = L("zomato", "Meghana Foods", *GORWA, rid="2", distance_km=0.6)
    [p] = rm.pair_results({"swiggy": [a], "zomato": [b]}, *USER, {"swiggy": "ok", "zomato": "ok"})
    assert p["distance_km"] == 0.4 and p["locality"] is None
    assert p["key"] == "swiggy:1|zomato:2"

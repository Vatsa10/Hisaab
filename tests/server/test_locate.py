"""Use my current location: the device's pin is matched to the user's saved address on each app (nearest first)."""
from helpers import A, add_user

from server import addresses_store

HOME = (22.3000, 73.1700)  # Testville (made up)


def _addr(pg, site, aid, lat, lon, label="Home"):
    pg.execute("insert into app_addresses (user_id, site, address_id, label, line, area, city, lat, lon) "
               "values (%s,%s,%s,%s,'1 Test Society','Gorwa','Testville',%s,%s)", (A, site, aid, label, lat, lon))


def test_the_nearest_saved_address_on_each_app_wins_and_far_ones_are_left_out(pg):
    add_user(pg, A, "a@example.com")
    _addr(pg, "swiggy", "s-near", HOME[0] + 0.0003, HOME[1])      # about 33 m
    _addr(pg, "swiggy", "s-far", HOME[0] + 0.004, HOME[1])        # about 440 m, the same app
    _addr(pg, "zomato", "z-mid", HOME[0] + 0.001, HOME[1] + 0.001)  # about 150 m
    _addr(pg, "zomato", "z-away", 23.0, 74.0)                     # another city
    got = addresses_store.nearby(pg, A, *HOME)
    assert [(r["site"], r["address_id"]) for r in got] == [("swiggy", "s-near"), ("zomato", "z-mid")]
    assert got[0]["distance_m"] < got[1]["distance_m"] and got[0]["distance_m"] < 60


def test_nothing_nearby_is_an_empty_list(pg):
    add_user(pg, A, "a@example.com")
    _addr(pg, "swiggy", "s1", 10.0, 10.0)
    assert addresses_store.nearby(pg, A, *HOME) == []


def test_the_locate_endpoint_returns_matches_and_stores_nothing(client, pg, token_a):
    add_user(pg, A, "a@example.com")
    _addr(pg, "swiggy", "s-near", HOME[0] + 0.0003, HOME[1])
    r = client.post("/profile/locate", json={"lat": HOME[0], "lon": HOME[1]}, headers=token_a)
    assert r.status_code == 200 and [m["address_id"] for m in r.json()["matches"]] == ["s-near"]
    assert pg.execute("select count(*) from profiles").fetchone()[0] == 0


def test_a_pin_outside_the_earth_is_refused(client, pg, token_a):
    add_user(pg, A, "a@example.com")
    assert client.post("/profile/locate", json={"lat": 123, "lon": 0}, headers=token_a).status_code == 422


def test_the_current_pin_can_be_saved_as_the_delivery_place(client, pg, token_a):
    add_user(pg, A, "a@example.com")
    r = client.put("/profile", json={"pin": {"lat": HOME[0], "lon": HOME[1]}}, headers=token_a)
    assert r.status_code == 200 and r.json()["address"] == "Current location"
    assert pg.execute("select address, lat, lon, chosen from profiles where user_id=%s", (A,)).fetchone() == (
        "Current location", HOME[0], HOME[1], None)


def test_exactly_one_of_address_choice_or_pin(client, pg, token_a):
    add_user(pg, A, "a@example.com")
    both = {"address": "Somewhere, Testville", "pin": {"lat": 1, "lon": 2}}
    assert client.put("/profile", json=both, headers=token_a).status_code == 422
    assert client.put("/profile", json={}, headers=token_a).status_code == 422

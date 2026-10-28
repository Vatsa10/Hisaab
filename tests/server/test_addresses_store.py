"""Saved delivery addresses: storage, options across apps, the profile choice, the worker refresh, and the
per-app address a compare or fill uses."""
import logging

import psycopg
import pytest
from helpers import A, B, order, settings_for_tests

import browser
from server import addresses_store, jobs, store, worker


@pytest.fixture(autouse=True)
def _clean(pg):
    pg.execute("truncate app_addresses")


def row(address_id, lat=12.9116, lon=77.6474, label="Home", line="Flat 12, Lakeview Residency",
        area="HSR Layout", city="Bengaluru", is_default=False):
    return {"address_id": address_id, "label": label, "line": line, "area": area, "city": city,
            "lat": lat, "lon": lon, "is_default": is_default}


HOME = (12.9116, 77.6474)
NEAR_HOME = (12.9120, 77.6478)  # ~60 m away
WORK = (12.9352, 77.6245)  # ~3.6 km away


def _ids(conn, user=A, site=None):
    q = "select site, address_id from app_addresses where user_id=%s"
    args = [user]
    if site:
        q += " and site=%s"
        args.append(site)
    return sorted(conn.execute(q + " order by 1, 2", args).fetchall())


# --- replace --------------------------------------------------------------------------------------------------

def test_replace_is_atomic_per_site(pg):
    addresses_store.replace(pg, A, "zepto", [row("z1"), row("z2", *WORK, label="Work")])
    addresses_store.replace(pg, A, "swiggy", [row("s1")])
    addresses_store.replace(pg, B, "zepto", [row("b1")])
    assert addresses_store.replace(pg, A, "zepto", [row("z3")]) == 1
    assert _ids(pg) == [("swiggy", "s1"), ("zepto", "z3")]  # the other site is untouched
    assert _ids(pg, B) == [("zepto", "b1")]  # and so is the other user

    # a bad row half-way through: nothing changes
    with pytest.raises(Exception):
        addresses_store.replace(pg, A, "zepto", [row("z4"), {"label": "no id"}])
    assert _ids(pg, site="zepto") == [("zepto", "z3")]

    # an app that now has no saved addresses clears its rows
    addresses_store.replace(pg, A, "zepto", [])
    assert _ids(pg) == [("swiggy", "s1")]


def test_replace_stores_instamart_under_swiggy(pg):
    addresses_store.replace(pg, A, "instamart", [row("s1")])
    assert _ids(pg) == [("swiggy", "s1")]
    assert pg.execute("select fetched_at is not null from app_addresses").fetchone() == (True,)


# --- rls ------------------------------------------------------------------------------------------------------

def test_users_read_only_their_own_addresses(pg, as_user):
    addresses_store.replace(pg, A, "zepto", [row("z1")])
    addresses_store.replace(pg, B, "zepto", [row("b1")])
    with as_user(pg, A) as c:
        assert c.execute("select address_id from app_addresses").fetchall() == [("z1",)]


@pytest.mark.parametrize("stmt", [
    "insert into app_addresses (user_id, site, address_id) values (auth.uid(), 'zepto', 'x')",
    "update app_addresses set line='x'",
    "delete from app_addresses",
    "truncate app_addresses",
])
def test_users_cannot_write_addresses(pg, as_user, stmt):
    addresses_store.replace(pg, A, "zepto", [row("z1")])
    with as_user(pg, A) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute(stmt)


def test_anon_cannot_read_addresses(pg):
    pg.execute("begin")
    try:
        pg.execute("set local role anon")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pg.execute("select * from app_addresses")
    finally:
        pg.execute("rollback")


# --- PUT /profile {choice} ------------------------------------------------------------------------------------

def test_choice_sets_profile_location(client, token_a, pg, monkeypatch):
    monkeypatch.setattr(browser, "resolve_location", lambda text: pytest.fail("a choice with a pin needs no geocoding"))
    addresses_store.replace(pg, A, "swiggy", [row("s-home", *HOME, line="Flat 12, Lakeview Residency")])
    r = client.put("/profile", json={"choice": {"site": "swiggy", "address_id": "s-home"}}, headers=token_a)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lat"] == HOME[0] and body["lon"] == HOME[1]
    assert body["address"] == "Flat 12, Lakeview Residency, HSR Layout, Bengaluru"
    assert body["display"] == "HSR Layout, Bengaluru"
    assert body["chosen"] == {"site": "swiggy", "address_id": "s-home"}
    p = store.get_profile(pg, A)
    assert (p["address"], p["lat"], p["lon"]) == (body["address"], HOME[0], HOME[1])
    assert p["chosen"] == {"site": "swiggy", "address_id": "s-home"}


def test_choice_without_a_pin_geocodes_area_and_city(client, token_a, pg, monkeypatch):
    asked = []
    monkeypatch.setattr(browser, "resolve_location",
                        lambda text: asked.append(text) or {"lat": 12.97, "lon": 77.64, "display": None})
    addresses_store.replace(pg, A, "zomato", [row("m1", None, None, area="Indiranagar")])
    r = client.put("/profile", json={"choice": {"site": "zomato", "address_id": "m1"}}, headers=token_a)
    assert r.status_code == 200
    assert asked == ["Indiranagar, Bengaluru"]
    assert (r.json()["lat"], r.json()["lon"]) == (12.97, 77.64)


def test_choice_must_be_one_of_your_own_saved_addresses(client, token_a, token_b, pg):
    addresses_store.replace(pg, A, "swiggy", [row("s-home")])
    bad = {"choice": {"site": "swiggy", "address_id": "s-home"}}
    assert client.put("/profile", json=bad, headers=token_b).status_code == 404  # not B's
    assert client.put("/profile", json={"choice": {"site": "swiggy", "address_id": "nope"}},
                      headers=token_a).status_code == 404
    assert client.put("/profile", json={"choice": {"site": "nowhere", "address_id": "s-home"}},
                      headers=token_a).status_code == 400
    assert client.put("/profile", json={}, headers=token_a).status_code == 422
    assert client.put("/profile", json={"address": "HSR Layout, Bengaluru", **bad}, headers=token_a).status_code == 422
    assert pg.execute("select count(*) from profiles").fetchone() == (0,)


def test_typing_an_address_clears_the_choice(client, token_a, pg, monkeypatch):
    monkeypatch.setattr(browser, "resolve_location", lambda text: {"lat": 12.9, "lon": 77.6})
    addresses_store.replace(pg, A, "swiggy", [row("s-home")])
    client.put("/profile", json={"choice": {"site": "swiggy", "address_id": "s-home"}}, headers=token_a)
    r = client.put("/profile", json={"address": "Somewhere else, Bengaluru"}, headers=token_a)
    assert r.status_code == 200 and "chosen" not in r.json()
    assert store.get_profile(pg, A)["chosen"] is None


def test_choice_is_never_logged(client, token_a, pg, caplog):
    addresses_store.replace(pg, A, "swiggy", [row("s-home", line="Flat 12, Lakeview Residency")])
    caplog.set_level(logging.DEBUG)
    client.put("/profile", json={"choice": {"site": "swiggy", "address_id": "s-home"}}, headers=token_a)
    for secret in ("Lakeview", "HSR", "12.91", "77.64"):
        assert secret not in caplog.text


# --- worker ---------------------------------------------------------------------------------------------------

def _run_sync(pg, site="zepto"):
    jobs.enqueue(pg, A, "sync", site)
    worker.run_job(jobs.claim(pg, "w", ("sync",)), pg, settings_for_tests())


def _addresses_recipe(monkeypatch, by_site):
    """An admin-learned `addresses` recipe per site (learning it is Plan 4's admin flow, not this task's)."""
    monkeypatch.setattr(worker.addresses_store, "recipe", lambda conn, site: by_site.get(site))


def test_worker_fetch_failure_never_fails_job(pg, monkeypatch, caplog):
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda site, since, **k: [order("1")])
    _addresses_recipe(monkeypatch, {"zepto": {"endpoint": "https://www.zepto.com/api/a", "method": "GET"}})
    addresses_store.replace(pg, A, "zepto", [row("z-old", line="Flat 12, Lakeview Residency")])

    def broken(*a, **k):
        raise RuntimeError("HTTP 500: Flat 12, Lakeview Residency")
    monkeypatch.setattr(worker.addresses, "fetch_for_site", broken)
    caplog.set_level(logging.DEBUG)
    _run_sync(pg)
    assert pg.execute("select status, error from jobs").fetchone() == ("done", None)
    assert pg.execute("select status from connections").fetchone() == ("connected",)
    assert _ids(pg) == [("zepto", "z-old")]  # last good list kept
    assert "Lakeview" not in caplog.text


def test_worker_refreshes_addresses_after_sync(pg, monkeypatch):
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda site, since, **k: [order("1")])
    _addresses_recipe(monkeypatch, {"swiggy": {"endpoint": "https://www.swiggy.com/x", "method": "GET"}})
    seen = []
    monkeypatch.setattr(worker.addresses, "fetch_for_site",
                        lambda site, recipe, **k: seen.append((site, recipe["endpoint"])) or [row("s1")])
    _run_sync(pg, "instamart")  # instamart rides on the swiggy account and its addresses
    assert seen == [("swiggy", "https://www.swiggy.com/x")]
    assert _ids(pg) == [("swiggy", "s1")]


def test_worker_without_an_addresses_recipe_opens_no_browser_for_them(pg, monkeypatch):
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda site, since, **k: [order("1")])
    monkeypatch.setattr(worker.addresses, "fetch_for_site", lambda *a, **k: pytest.fail("no recipe, no fetch"))
    _run_sync(pg)
    assert pg.execute("select status from jobs").fetchone() == ("done",)


def test_fake_worker_stores_two_addresses_per_site(pg, monkeypatch):
    monkeypatch.setenv("WORKER_FAKE", "1")
    jobs.enqueue(pg, A, "connect", "blinkit", {"phone": "9876543210"})
    job = jobs.claim(pg, "w", ("connect",))
    monkeypatch.setattr(worker.fake_sites, "login", lambda *a, **k: True)
    worker.run_job(job, pg, settings_for_tests())
    assert pg.execute("select status from jobs").fetchone() == ("done",)
    assert len(_ids(pg, site="blinkit")) == 2


# --- saved_for: the per-app address a compare or fill uses ---------------------------------------------------

def _choose(pg, site, address_id, lat, lon):
    pg.execute("insert into profiles (user_id, lat, lon, chosen) values (%s,%s,%s,%s) "
               "on conflict (user_id) do update set lat=excluded.lat, lon=excluded.lon, chosen=excluded.chosen",
               (A, lat, lon, psycopg.types.json.Jsonb({"site": site, "address_id": address_id})))


def test_compare_uses_matching_saved_address_per_app(pg):
    addresses_store.replace(pg, A, "zepto", [row("z-home", *HOME), row("z-near", *NEAR_HOME, label="Other",
                                                                       line="Gate 2")])
    addresses_store.replace(pg, A, "swiggy", [row("s-work", *WORK, line="Tower B"), row("s-home", *NEAR_HOME)])
    addresses_store.replace(pg, A, "zomato", [row("m-home", None, None, line="Flat 12, Lakeview Residency")])
    _choose(pg, "zepto", "z-home", *HOME)
    # the app the choice came from: that exact address, even with a closer one in the list
    assert addresses_store.saved_for(pg, A, "zepto", near=HOME)["address_id"] == "z-home"
    # another app: its own saved address within 150 m
    assert addresses_store.saved_for(pg, A, "swiggy", near=HOME)["address_id"] == "s-home"
    # Instamart prices at the Swiggy account's address
    assert addresses_store.saved_for(pg, A, "instamart", near=HOME)["address_id"] == "s-home"
    # no pin, but the same address line
    assert addresses_store.saved_for(pg, A, "zomato", near=HOME)["address_id"] == "m-home"
    # near defaults to the profile's saved location
    assert addresses_store.saved_for(pg, A, "swiggy")["address_id"] == "s-home"
    got = addresses_store.saved_for(pg, A, "swiggy", near=HOME)
    assert set(got) >= {"site", "address_id", "label", "line", "area", "city", "lat", "lon", "is_default"}


def test_no_nearby_saved_address_skips_app(pg):
    addresses_store.replace(pg, A, "zepto", [row("z-home", *HOME)])
    addresses_store.replace(pg, A, "blinkit", [row("b-work", *WORK, line="Tower B")])
    _choose(pg, "zepto", "z-home", *HOME)
    assert addresses_store.saved_for(pg, A, "blinkit", near=HOME) is None  # 3.6 km away: not priced
    assert addresses_store.saved_for(pg, A, "zomato", near=HOME) is None  # nothing saved at all
    assert addresses_store.saved_for(pg, B, "zepto", near=HOME) is None  # another user's list never counts

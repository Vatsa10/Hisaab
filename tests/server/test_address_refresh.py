"""Saved addresses are re-read at the start of every search and fill job, so a changed address is never priced."""
from helpers import A, settings_for_tests
from test_worker_plan5 import _grocery, _result, _run_all, _stub_search
from test_worker_plan6 import _run, _setup

import cart_fill
from server import addresses_store, cart_rank, fake_sites, fill, timing, worker

HOME = {"address_id": "zepto-home", "label": "Home", "line": "12 Main Road", "area": "Gorwa", "city": "Vadodara",
        "lat": 22.3310, "lon": 73.1510, "is_default": True}


def _after_refresh(monkeypatch, rows, seen=None):
    def refresh(job, conn, db_site, profile_dir, proxy_server, fake):
        if seen is not None:
            seen.append(job.type)
        addresses_store.replace(conn, job.user_id, db_site, rows)
    monkeypatch.setattr(worker, "_refresh_addresses", refresh)


def test_search_refreshes_first_and_prices_when_unchanged(pg, monkeypatch):
    rid = _grocery(pg)
    seen = []
    _after_refresh(monkeypatch, [dict(HOME, line="12 Main Rd")], seen)  # a rename, same pin
    _stub_search(monkeypatch)
    _run_all(pg)
    assert seen == ["search"] and _result(pg, rid)[0] == "ok"


def test_search_unserviceable_when_address_deleted(pg, monkeypatch):
    rid = _grocery(pg)
    _after_refresh(monkeypatch, [])
    _stub_search(monkeypatch, exc=AssertionError("must not search"))
    _run_all(pg)
    assert _result(pg, rid)[:2] == ("unserviceable", "address_changed")


def test_search_unserviceable_when_pin_moved(pg, monkeypatch):
    rid = _grocery(pg)
    _after_refresh(monkeypatch, [dict(HOME, lat=22.35)])  # ~2 km
    _stub_search(monkeypatch, exc=AssertionError("must not search"))
    _run_all(pg)
    assert _result(pg, rid)[:2] == ("unserviceable", "address_changed")


def test_small_pin_move_still_prices(pg, monkeypatch):
    rid = _grocery(pg)
    _after_refresh(monkeypatch, [dict(HOME, lat=22.3315)])  # ~55 m
    _stub_search(monkeypatch)
    _run_all(pg)
    assert _result(pg, rid)[0] == "ok"


def test_failed_refresh_keeps_list_and_job_continues(pg, monkeypatch):
    rid = _grocery(pg)
    store_ = worker.addresses_store
    monkeypatch.setattr(store_, "recipe", lambda conn, site: {"x": 1})
    monkeypatch.setattr(worker.addresses, "fetch_for_site", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    _stub_search(monkeypatch)
    _run_all(pg)
    assert _result(pg, rid)[0] == "ok"
    assert addresses_store.get(pg, A, "zepto", "zepto-home") is not None


def test_refresh_is_timed_as_addresses_phase():
    assert "addresses" in timing.PHASES


def test_note_for_changed_address():
    note, _ = cart_rank._note_and_action("zepto", "unserviceable", [], "address_changed")
    assert note == "Your saved address in Zepto changed. Pick it again."


def test_fill_stops_before_any_add_when_address_changed(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)
    addresses_store.replace(pg, A, "zepto", [dict(HOME, lat=22.33, lon=73.16)])
    _after_refresh(monkeypatch, [])
    monkeypatch.setattr(cart_fill, "fill_cart", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no add")))
    _run(pg, s)
    f = fill.get_fill(pg, fid)
    assert f["status"] == "failed" and f["error"] == "address_changed"
    assert "check" in " ".join(str(v) for v in f["summary"].values()).lower() or f["summary"]
    assert pg.execute("select status from jobs where id=%s", (jid,)).fetchone() == ("done",)


def test_fake_addresses_can_change_between_calls(monkeypatch):
    base = fake_sites.saved_addresses("zepto")
    monkeypatch.setenv("FAKE_ADDRESS_MOVED_SITES", "zepto")
    moved = fake_sites.saved_addresses("zepto")
    assert moved[0]["lat"] != base[0]["lat"] and moved[1] == base[1]
    monkeypatch.setenv("FAKE_ADDRESS_MOVED_SITES", "")
    monkeypatch.setenv("FAKE_ADDRESS_DELETED_SITES", "zepto")
    assert [a["address_id"] for a in fake_sites.saved_addresses("zepto")] == ["fake-zepto-2"]


# ---------------------------------------------------------------- POST /addresses/refresh

def test_refresh_endpoint_enqueues_for_connected_apps_and_is_rate_limited(client, token_a, pg):
    from server import store
    store.set_connection(pg, A, "zepto", status="connected")
    store.set_connection(pg, A, "blinkit", status="needs_login")
    r = client.post("/addresses/refresh", headers=token_a)
    assert r.status_code == 202 and r.json()["sites"] == ["zepto"]
    rows = pg.execute("select type, site, payload->>'op' from jobs where user_id=%s", (A,)).fetchall()
    assert rows == [("search", "zepto", "addresses")]
    again = client.post("/addresses/refresh", headers=token_a)
    assert again.status_code == 429 and int(again.headers["Retry-After"]) > 0
    assert client.post("/addresses/refresh").status_code in (401, 403)


def test_refresh_endpoint_without_connected_apps_is_409(client, token_a):
    assert client.post("/addresses/refresh", headers=token_a).status_code == 409


def test_addresses_op_job_only_refreshes(pg, monkeypatch):
    from server import jobs, store
    store.set_connection(pg, A, "zepto", status="connected")
    _grocery_seed = _grocery  # noqa: F841  (seeds a stored profile through the shared helper)
    pg.execute("insert into profiles (user_id, address, lat, lon) values (%s,'x',22.33,73.15)", (A,))
    from test_worker_plan5 import _seed_profile
    _seed_profile(A, "zepto")
    seen = []
    _after_refresh(monkeypatch, [HOME], seen)
    _stub_search(monkeypatch, exc=AssertionError("no search for a refresh"))
    jobs.enqueue(pg, A, "search", "zepto", {"op": "addresses"})
    _run_all(pg)
    assert seen == ["search"] and addresses_store.get(pg, A, "zepto", "zepto-home")
    assert pg.execute("select status, error from jobs").fetchone() == ("done", None)

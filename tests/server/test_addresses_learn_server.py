"""Learn kind "addresses": the worker runs addresses.learn_addresses on the admin's account, publishes a GET-only
recipe under the account's site, and queues the lightweight address refresh for every connected user of it. The
admin API starts it (Instamart through Swiggy's account) and site health lists it. Addresses are fake."""
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from helpers import A, B, add_user, auth_header, settings_for_tests

from server import admin, jobs, recipe_admin, store, vault, worker
from server.config import DEV_PROFILE_KEY

KEY = vault.master_key_from_env(DEV_PROFILE_KEY)
HOME = (10.1234, 20.5678)  # Testville (made up)
RECIPE = {"kind": "addresses", "site": "swiggy", "home_url": "https://www.swiggy.com/",
          "endpoint": "https://www.swiggy.com/api/saved_locations", "method": "GET", "query": {}, "body": None,
          "headers": {}, "read_post_allowlist": [],
          "field_map": {"list_path": "data.addresses", "address_id": "id", "line": "full_address",
                        "lat": "latitude", "lon": "longitude"}}


def _seed_profile(user_id, profile):
    d = Path(os.environ["PROFILE_ROOT"]).parent / f"seed-{user_id}-{profile}"
    vault.stamp(d, user_id, profile)
    (d / "Cookies").write_text("seeded")
    vault.store_from_url(os.environ["PROFILE_STORE_URL"]).put(
        vault.blob_key(user_id, profile), vault._seal(KEY, user_id, profile, vault._pack(d)))


def _admin(pg, site="swiggy"):
    add_user(pg, B, "admin@example.com")
    pg.execute("insert into admins(email) values ('admin@example.com')")
    store.set_connection(pg, B, site, status="connected", orders_page_url=f"https://www.{site}.com/my-account")
    pg.execute("insert into profiles (user_id, address, lat, lon) values (%s,%s,%s,%s)",
               (B, "Flat 7, Imaginary Towers, Testville", *HOME))
    _seed_profile(B, site)


def _run_learns(pg):
    while (job := jobs.claim(pg, "w", ("learn",))) is not None:
        worker.run_job(job, pg, settings_for_tests())


def _refreshes(pg, site="swiggy"):
    return sorted(str(r[0]) for r in pg.execute(
        "select user_id from jobs where type='search' and site=%s and payload->>'op'='addresses'", (site,)))


def test_an_addresses_learn_publishes_a_get_recipe_and_refreshes_every_connected_user(pg, monkeypatch):
    _admin(pg)
    store.set_connection(pg, A, "swiggy", status="connected")
    store.set_connection(pg, A, "zepto", status="connected")  # another app: not refreshed
    seen = {}

    def learn(site, profile_dir, **k):
        seen.update(site=site, **k)
        return RECIPE
    monkeypatch.setattr(worker.addresses, "learn_addresses", learn)
    jid = jobs.enqueue(pg, B, "learn", "swiggy", {"reason": "manual", "kind": "addresses"})
    _run_learns(pg)
    assert pg.execute("select status from jobs where id=%s", (jid,)).fetchone() == ("done",)
    assert seen["site"] == "swiggy" and callable(seen["llm"])
    assert seen["start_url"] == "https://www.swiggy.com/my-account"  # the admin's own orders page
    row = pg.execute("select version, recipe->>'method' from recipes where site='swiggy' and kind='addresses'")
    assert row.fetchone() == (1, "GET")
    assert _refreshes(pg) == sorted([A, B])
    assert _refreshes(pg, "zepto") == []


def test_a_learned_post_recipe_is_never_published(pg, monkeypatch):
    _admin(pg)
    monkeypatch.setattr(worker.addresses, "learn_addresses", lambda *a, **k: RECIPE | {"method": "POST"})
    jid = jobs.enqueue(pg, B, "learn", "swiggy", {"reason": "manual", "kind": "addresses"})
    _run_learns(pg)
    assert pg.execute("select status from jobs where id=%s", (jid,)).fetchone() == ("failed",)
    assert pg.execute("select count(*) from recipes where kind='addresses'").fetchone() == (0,)
    with pytest.raises(ValueError):
        recipe_admin.apply_learned(pg, "swiggy", "addresses", RECIPE | {"method": "POST"}, set(), None, "learned")


def test_approving_a_held_addresses_recipe_queues_the_refresh_and_skips_gone_users(pg, monkeypatch):
    add_user(pg, B, "admin@example.com")
    store.set_connection(pg, A, "swiggy", status="connected")
    store.set_connection(pg, B, "swiggy", status="needs_login")  # not connected: no refresh
    held = RECIPE | {"endpoint": "https://www.swiggy.com/users/someone/saved_locations"}  # account-specific: held
    out = recipe_admin.apply_learned(pg, "swiggy", "addresses", held, set(), None, "learned")
    assert out["status"] == "pending" and _refreshes(pg) == []
    real = jobs.enqueue

    def enqueue(conn, user_id, *a, **k):
        if user_id == A and not getattr(enqueue, "once", False):
            enqueue.once = True
            raise jobs.Conflict("busy")
        return real(conn, user_id, *a, **k)
    monkeypatch.setattr(recipe_admin.jobs, "enqueue", enqueue)
    assert recipe_admin.decide(pg, out["approval_id"], "approve", B) == "approved"
    assert _refreshes(pg) == []  # A was busy (Conflict skipped), B is not connected
    assert recipe_admin.enqueue_address_refresh(pg, "swiggy") == 1
    assert _refreshes(pg) == [A]


def test_admin_learn_route_for_addresses(client, pg):
    add_user(pg, A, "owner@example.com")
    pg.execute("insert into admins(email) values ('owner@example.com')")
    h = auth_header(A)
    store.set_connection(pg, A, "swiggy", status="connected")
    pg.execute("insert into profiles (user_id, address, lat, lon) values (%s,%s,%s,%s)", (A, "Testville", *HOME))
    r = client.post("/admin/learn/instamart?kind=addresses", headers=h)  # Instamart: through Swiggy's account
    assert r.status_code == 202
    assert pg.execute("select site, payload->>'kind' from jobs where id=%s", (r.json()["job_id"],)).fetchone() == (
        "swiggy", "addresses")
    assert client.post("/admin/learn/swiggy?kind=addresses", headers=h).status_code == 409  # one already runs
    assert client.post("/admin/learn/zepto?kind=addresses", headers=h).status_code == 409  # not connected
    assert client.post("/admin/learn/nowhere?kind=addresses", headers=h).status_code == 400


def test_site_health_lists_an_addresses_row_per_connectable_site(pg):
    add_user(pg, B, "admin@example.com")
    store.set_connection(pg, B, "zomato", status="connected")
    store.put_recipe(pg, "zomato", RECIPE | {"site": "zomato"}, kind="addresses")
    rows = [r for r in admin.site_health(pg, datetime.now(timezone.utc), B) if r["kind"] == "addresses"]
    assert [r["site"] for r in rows] == ["zepto", "blinkit", "swiggy", "zomato"]
    zomato = rows[-1]
    assert zomato["version"] == 1 and zomato["op"] == "addresses" and zomato["admin_connected"] is True

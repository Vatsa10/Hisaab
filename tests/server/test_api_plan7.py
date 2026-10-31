"""Plan 7 T11: disconnect and delete routes, cool-down refusals, start-up check, health fields, verify-erased."""
import os
import re

import helpers
import pytest
from fastapi.testclient import TestClient
from helpers import A, B
from typer.testing import CliRunner

from server import erase, site_health, vault
from server.config import Settings, settings

EA, EB = "a7@e2e.test", "b7@e2e.test"


@pytest.fixture(autouse=True)
def _fee_rules(fee_rules_snapshot):
    """seed_everything touches fee_rules (not truncated per test): restore it afterwards."""


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(erase, "_sleep", lambda s: None)


def _count(pg, sql, *p):
    return pg.execute(sql, p).fetchone()[0]


def test_disconnect_and_delete_routes(client, token_a, pg):
    helpers.seed_everything(pg, A, EA)
    helpers.seed_everything(pg, B, EB)
    r = client.post("/connections/swiggy/disconnect", headers=token_a)
    assert r.status_code == 200 and r.json() == {"site": "swiggy", "orders_deleted": 1}
    assert client.post("/connections/swiggy/disconnect", headers=token_a).json() == {"site": "swiggy",
                                                                                      "orders_deleted": 0}
    r = client.post("/connections/instamart/disconnect", headers=token_a)
    assert r.status_code == 400 and r.json()["detail"] == "Instamart is part of your Swiggy connection."
    r = client.post("/connections/nope/disconnect", headers=token_a)
    assert r.status_code == 400 and r.json()["detail"] == "unknown site"
    assert client.post("/connections/swiggy/disconnect").status_code == 401

    assert client.request("DELETE", "/account", headers=token_a, json={}).status_code == 422
    assert client.request("DELETE", "/account", headers=token_a, json={"confirm": "delete"}).status_code == 422
    assert _count(pg, "select count(*) from auth.users where id = %s", A) == 1
    r = client.request("DELETE", "/account", headers=token_a, json={"confirm": "DELETE"})
    assert r.status_code == 200 and r.json() == {"deleted": True}
    store = vault.store_from_url(settings().profile_store_url)
    assert erase.remaining(pg, store, A, EA) == []
    assert erase.remaining(pg, store, B, EB) != []
    # idempotent
    assert client.request("DELETE", "/account", headers=token_a, json={"confirm": "DELETE"}).json() == {"deleted": True}


def test_delete_route_blob_failure_is_503_and_retryable(client, token_a, pg, monkeypatch):
    helpers.seed_everything(pg, A, EA)
    real = vault.delete_user_profiles

    def boom(store, uid):
        raise OSError("down")
    monkeypatch.setattr(vault, "delete_user_profiles", boom)
    r = client.request("DELETE", "/account", headers=token_a, json={"confirm": "DELETE"})
    assert r.status_code == 503
    assert r.json()["detail"] == "Your data is deleted, but stored sessions couldn't be removed yet. Tap Delete again."
    monkeypatch.setattr(vault, "delete_user_profiles", real)
    assert client.request("DELETE", "/account", headers=token_a, json={"confirm": "DELETE"}).status_code == 200
    assert vault.store_from_url(settings().profile_store_url).list_prefix(f"{A}/") == []


def test_connect_refresh_refused_while_cooling(client, token_a, pg):
    pg.execute("insert into connections(user_id, site, status) values (%s, 'zepto', 'connected')", (A,))
    pg.execute("insert into consents(user_id, site, text_version) values (%s, 'zepto', %s)",
               (A, Settings().consent_version))
    site_health.record_throttle(pg, "zepto", "rate_limited", None, Settings())
    msg = re.compile(r"^Zepto is limiting requests right now\. Try again in about \d+ minutes\.$")
    for path, body in (("/connections/zepto/refresh", None), ("/connections/zepto/connect", {"phone": "9876543210"})):
        r = client.post(path, headers=token_a, json=body) if body else client.post(path, headers=token_a)
        assert r.status_code == 503, path
        assert msg.match(r.json()["detail"])
        assert int(r.headers["Retry-After"]) >= 1
    assert _count(pg, "select count(*) from jobs") == 0
    assert _count(pg, "select count(*) from usage") == 0
    # the existing 404 still comes first, and another site is unaffected
    assert client.post("/connections/blinkit/refresh", headers=token_a).status_code == 404
    site_health.record_success(pg, "zepto")
    assert client.post("/connections/zepto/refresh", headers=token_a).status_code == 202


def test_cooling_swiggy_refuses_instamart(client, token_a, pg):
    pg.execute("insert into connections(user_id, site, status) values (%s, 'swiggy', 'connected')", (A,))
    pg.execute("insert into connections(user_id, site, status) values (%s, 'instamart', 'connected')", (A,))
    site_health.record_throttle(pg, "swiggy", "challenge", None, Settings())
    r = client.post("/connections/instamart/refresh", headers=token_a)
    assert r.status_code == 503 and r.headers["Retry-After"].isdigit()


def test_api_startup_rejects_hosted_db_without_sslmode(monkeypatch):
    from server.api import app
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres")
    with pytest.raises(ValueError, match="sslmode"):
        with TestClient(app):
            pass


def test_site_health_fields_report_cooldown_and_failures(pg):
    """The admin site-health rows take these four fields (admin.py itself arrives with the admin API branch)."""
    s = Settings()
    quiet = site_health.health_fields(pg, s, "zepto")
    assert quiet == {"cooldown_until": None, "backoff_level": 0, "failures_window": 0, "jobs_window": 0}
    for i in range(3):
        pg.execute("insert into jobs(user_id, type, site, profile, status, error, finished_at) "
                   "values (%s, 'sync', 'zepto', 'zepto', %s, %s, now())",
                   (A, "failed" if i == 0 else "done", "boom" if i == 0 else None))
        pg.execute("update jobs set created_at = created_at - interval '1 minute' where false")
    until = site_health.record_throttle(pg, "zepto", "challenge", None, s)
    got = site_health.health_fields(pg, s, "zepto")
    assert got["cooldown_until"] == until and got["backoff_level"] == 1
    assert (got["failures_window"], got["jobs_window"]) == (1, 3)
    assert site_health.health_fields(pg, s, "blinkit")["backoff_level"] == 0


def test_cart_check_refused_while_every_app_is_cooling(client, token_a, pg):
    from test_cart_store import ITEMS, grocery_user
    grocery_user(pg)
    for site in ("zepto", "blinkit", "swiggy"):     # instamart rides on swiggy's host
        site_health.record_throttle(pg, site, "rate_limited", None, Settings())
    r = client.post("/cart/checks", json={"items": ITEMS}, headers=token_a)
    assert r.status_code == 503 and r.headers["Retry-After"].isdigit()
    assert "is limiting requests right now" in r.json()["detail"]
    assert _count(pg, "select count(*) from cart_requests") == 0
    assert _count(pg, "select count(*) from jobs") == 0
    assert _count(pg, "select count(*) from usage") == 0


def test_cart_check_runs_while_only_some_apps_are_cooling(client, token_a, pg):
    from test_cart_store import ITEMS, grocery_user
    grocery_user(pg)
    site_health.record_throttle(pg, "zepto", "rate_limited", None, Settings())
    r = client.post("/cart/checks", json={"items": ITEMS}, headers=token_a)
    assert r.status_code == 202


def test_admin_site_health_rows_carry_cooldown_fields(pg):
    from datetime import datetime, timezone

    from server import admin
    helpers.add_user(pg, A, EA)
    helpers.add_user(pg, B, EB)
    until = site_health.record_throttle(pg, "zepto", "challenge", None, Settings())
    pg.execute("insert into jobs(user_id, type, site, profile, status, error, finished_at) "
               "values (%s, 'sync', 'zepto', 'zepto', 'failed', 'boom', now())", (A,))
    pg.execute("insert into jobs(user_id, type, site, profile, status, finished_at) "
               "values (%s, 'sync', 'zepto', 'zepto', 'done', now())", (B,))
    now = datetime.now(timezone.utc)
    rows = {(r["site"], r["kind"]): r for r in admin.site_health(pg, now, A)}
    z = rows[("zepto", "orders")]
    assert z["cooldown_until"] == until and z["backoff_level"] == 1
    assert (z["failures_window"], z["jobs_window"]) == (1, 2)
    b = rows[("blinkit", "orders")]
    assert (b["cooldown_until"], b["backoff_level"], b["failures_window"], b["jobs_window"]) == (None, 0, 0, 0)


def test_ask_after_account_deletion_is_401_and_writes_no_usage(client, token_a, pg):
    from server.api import app, get_ask_conn
    app.dependency_overrides[get_ask_conn] = lambda: None
    try:
        helpers.seed_everything(pg, A, EA)
        client.request("DELETE", "/account", headers=token_a, json={"confirm": "DELETE"})
        r = client.post("/ask", headers=token_a, json={"question": "how much did I spend?"})
        assert r.status_code == 401
        assert _count(pg, "select count(*) from usage where user_id = %s", A) == 0
    finally:
        app.dependency_overrides.pop(get_ask_conn, None)


def test_enqueue_for_a_deleted_user_is_401_not_500(client, token_a, pg, monkeypatch):
    """A deleted user's token stays valid until it expires: every route that enqueues answers 401, never 500."""
    from datetime import timedelta

    from test_cart_store import ITEMS, grocery_user
    from test_food_cart import BOTH, NOW, cache_menus, cache_restaurants, setup_user

    from server import food_api
    monkeypatch.setattr(food_api, "_now", lambda: NOW)
    grocery_user(pg, sites=("zepto",))                 # profile, connection, search recipes
    pg.execute("insert into consents(user_id, site, text_version) values (%s, 'zepto', %s)",
               (A, Settings().consent_version))
    pg.execute("delete from profiles")                 # the food setup prices at its own home address
    setup_user(pg)
    cache_restaurants(pg)
    cache_menus(pg, ago=timedelta(minutes=31))
    pg.execute("delete from auth.users where id = %s", (A,))
    from test_food_cart import ITEMS as FOOD_ITEMS
    food = {"restaurant": dict(BOTH), "items": FOOD_ITEMS}
    calls = [("/connections/zepto/connect", {"phone": "9876543210"}), ("/connections/zepto/refresh", None),
             ("/cart/checks", {"items": ITEMS}), ("/cart/food/checks", food)]
    for path, body in calls:
        r = client.post(path, headers=token_a, json=body) if body else client.post(path, headers=token_a)
        assert r.status_code == 401, (path, r.status_code, r.text)
        assert r.json()["detail"] == "invalid token"
    assert _count(pg, "select count(*) from jobs") == 0


def test_cli_verify_erased(pg, tmp_path, monkeypatch):
    import cli
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    store = vault.store_from_url(settings().profile_store_url)
    helpers.seed_everything(pg, A, EA, store)
    r = CliRunner().invoke(cli.app, ["verify-erased", A, EA])
    assert r.exit_code == 1 and "public.orders" in r.output and "auth.users" in r.output
    assert f"blob:{A}/swiggy.tar.gz.enc" in r.output
    erase.delete_account(pg, store, A)
    r = CliRunner().invoke(cli.app, ["verify-erased", A, EA])
    assert r.exit_code == 0 and "NOTHING REMAINS" in r.output

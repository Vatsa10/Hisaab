"""Admin data helpers: emails, invites, per-user usage for a month and per-site health."""
from datetime import date, datetime, timedelta, timezone

import pytest
from helpers import A, B, add_user

from server import admin, jobs, store

NOW = datetime(2026, 10, 28, 6, 0, tzinfo=timezone.utc)  # 11:30 IST


def test_normalize_email():
    assert admin.normalize_email("  Ananya@Example.COM ") == "ananya@example.com"
    for bad in ("", "no-at-sign", "a@b", "a b@example.com", "@example.com", "a@@example.com",
                "x" * 250 + "@example.com"):
        with pytest.raises(ValueError):
            admin.normalize_email(bad)


def test_user_email_and_add_admin(pg):
    add_user(pg, A, "Owner@Example.com")
    assert admin.user_email(pg, A) == "owner@example.com"
    assert admin.user_email(pg, B) is None
    admin.add_admin(pg, "Owner@Example.com")
    admin.add_admin(pg, "owner@example.com")  # idempotent
    assert pg.execute("select email from admins").fetchall() == [("owner@example.com",)]
    assert pg.execute("select email from invites").fetchall() == [("owner@example.com",)]


def test_invite_revoke_and_list(pg):
    pg.execute("insert into waitlist(email) values ('wait@example.com')")
    assert admin.invite(pg, "Wait@Example.com", A) is True
    assert admin.invite(pg, "wait@example.com", A) is False  # already invited
    assert pg.execute("select invited_at is not null from waitlist").fetchone() == (True,)
    lists = admin.list_invites(pg)
    assert [i["email"] for i in lists["invites"]] == ["wait@example.com"]
    assert lists["invites"][0]["accepted_at"] is None
    assert lists["waitlist"][0]["email"] == "wait@example.com" and lists["waitlist"][0]["invited_at"]
    assert admin.revoke_invite(pg, "wait@example.com") is True
    assert admin.revoke_invite(pg, "wait@example.com") is False
    admin.invite(pg, "joined@example.com", A)
    pg.execute("update invites set accepted_at = now() where email = 'joined@example.com'")
    with pytest.raises(admin.InviteAccepted):
        admin.revoke_invite(pg, "joined@example.com")


def test_usage_by_user_sums_ist_month(pg):
    add_user(pg, A, "a@example.com")
    for day, mb in ((date(2026, 9, 30), 999), (date(2026, 10, 1), 100), (date(2026, 10, 31), 50),
                    (date(2026, 11, 1), 999)):
        pg.execute("insert into usage(user_id, day, proxy_mb, ai_tokens, worker_seconds, syncs, cart_checks) "
                   "values (%s, %s, %s, 10, 60, 1, 2)", (A, day, mb))
    pg.execute("insert into usage(user_id, day, ai_tokens) values (%s, '2026-10-15', 5)", (B,))  # no auth row
    rows = {r["user_id"]: r for r in admin.usage_by_user(pg, "2026-10")}
    assert rows[A] == {"user_id": A, "email": "a@example.com", "proxy_mb": 150.0, "ai_tokens": 20,
                       "worker_seconds": 120.0, "syncs": 2, "cart_checks": 4}
    assert rows[B]["email"] is None and rows[B]["ai_tokens"] == 5
    assert admin.usage_by_user(pg, "2026-08") == []


def _sync_job(pg, user, site, status, created_at, error=None, type="sync", finished_at=None):
    jid = pg.execute("insert into jobs(user_id, type, site, profile, status, error, created_at, finished_at) "
                     "values (%s, %s, %s, %s, %s, %s, %s, %s) returning id::text",
                     (user, type, site, jobs.SITE_PROFILE.get(site, site), status, error, created_at,
                      finished_at or created_at)).fetchone()[0]
    return jid


def test_site_health_counts_last_7_days(pg):
    add_user(pg, A, "owner@example.com")
    store.put_recipe(pg, "zepto", {"endpoint": "https://www.zepto.com/api/orders"})
    pg.execute("update recipes set healthy=false, paused=true, health_note='drift' where site='zepto'")
    store.set_connection(pg, A, "zepto", status="connected")
    store.set_connection(pg, A, "blinkit", status="needs_login")
    recent = NOW - timedelta(days=1)
    _sync_job(pg, A, "zepto", "done", recent, finished_at=recent)
    _sync_job(pg, B, "zepto", "done", NOW - timedelta(days=2))
    _sync_job(pg, B, "zepto", "failed", NOW - timedelta(days=3), error="recipe_drift")
    _sync_job(pg, B, "zepto", "done", NOW - timedelta(days=8))  # too old
    _sync_job(pg, B, "zepto", "done", NOW - timedelta(hours=1), type="connect")  # a connect counts as a sync run
    _sync_job(pg, B, "zepto", "failed", NOW - timedelta(hours=2), type="learn")  # a learn does not
    _sync_job(pg, B, "blinkit", "queued", NOW - timedelta(hours=1))  # not finished: not counted
    rows = {r["site"]: r for r in admin.site_health(pg, NOW, A) if r["kind"] == "orders"}
    assert "instamart" not in rows and set(rows) == {"zepto", "blinkit", "swiggy", "zomato"}
    z = rows["zepto"]
    assert (z["kind"], z["version"], z["healthy"], z["paused"], z["health_note"]) == \
        ("orders", 1, False, True, "drift")
    assert (z["syncs_ok_7d"], z["syncs_failed_7d"], z["admin_connected"]) == (3, 1, True)
    assert z["last_ok_at"] == NOW - timedelta(hours=1)
    b = rows["blinkit"]
    assert (b["version"], b["learned_at"], b["healthy"], b["paused"]) == (None, None, True, False)
    assert (b["syncs_ok_7d"], b["syncs_failed_7d"], b["last_ok_at"], b["admin_connected"]) == (0, 0, None, False)
    assert admin.site_health(pg, NOW, B)[0]["admin_connected"] is False

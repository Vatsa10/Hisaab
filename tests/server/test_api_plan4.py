"""Plan 4 API: refresh limits, /me, push subscriptions, Ask metering and the /admin routes."""
import os
import threading
import time

import pytest
from helpers import A, B, add_user, auth_header
from psycopg.types.json import Jsonb
from test_api_plan3 import StubLLM, ask_env, ask_url  # noqa: F401  (fixtures)

from server import ask, jobs, recipe_admin, store, usage

LIMIT_DETAIL = "You can refresh Zepto 3 times a day. Try again tomorrow."
POST = {"url_regex": "^https://www\\.zepto\\.com/api/orders$", "why": "order list"}


def _connected(pg, user=A, site="zepto", status="connected"):
    store.set_connection(pg, user, site, status=status)


def _manual_job(pg, user=A, site="zepto", trigger="manual", status="done"):
    pg.execute("insert into jobs(user_id, type, site, profile, status, payload) values (%s,'sync',%s,%s,%s,%s)",
               (user, site, jobs.SITE_PROFILE.get(site, site), status, Jsonb({"trigger": trigger})))


def _refresh(client, site="zepto", token=None):
    return client.post(f"/connections/{site}/refresh", headers=token or auth_header(A))


def _finish_all(pg):
    pg.execute("update jobs set status='done', finished_at=now() where status in ('queued','running')")


# ---------------------------------------------------------------- refresh limits

def test_refresh_limit_is_three_per_app_per_day(client, pg):
    _connected(pg)
    for left in (2, 1, 0):
        r = _refresh(client)
        assert r.status_code == 202 and r.json()["refresh_left"] == left
        assert pg.execute("select payload from jobs where id=%s", (r.json()["job_id"],)).fetchone()[0] == \
            {"trigger": "manual"}
        _finish_all(pg)
    r = _refresh(client)
    assert r.status_code == 429 and r.json()["detail"] == LIMIT_DETAIL
    assert int(r.headers["Retry-After"]) > 0 and r.headers["Retry-After"].isdigit()


def test_refresh_limit_is_per_app(client, pg):
    _connected(pg)
    _connected(pg, site="blinkit")
    for _ in range(3):
        _manual_job(pg)
    assert _refresh(client).status_code == 429
    assert _refresh(client, "blinkit").status_code == 202
    _manual_job(pg, user=B)  # another user's refreshes never count
    assert pg.execute("select count(*) from jobs where user_id=%s", (A,)).fetchone()[0] == 4


def test_refresh_conflict_does_not_consume_limit(client, pg):
    _connected(pg)
    assert _refresh(client).status_code == 202
    r = _refresh(client)  # a second tap while the first sync is queued
    assert r.status_code == 409
    assert pg.execute("select count(*) from jobs").fetchone()[0] == 1
    pg.execute("update jobs set status='done'")
    assert _refresh(client).json()["refresh_left"] == 1


def test_refresh_limit_is_serialised_with_the_enqueue(client, pg, pg2):
    """Check-then-enqueue under a per-user-and-site lock: a request that started while another one was enqueueing
    the last refresh sees it and is refused, instead of both passing the check (R4)."""
    _connected(pg)
    _manual_job(pg)
    _manual_job(pg)
    pg2.execute("begin")
    pg2.execute("select pg_advisory_xact_lock(%s)", (recipe_lock_key(),))
    pg2.execute("insert into jobs(user_id, type, site, profile, status, payload) "
                "values (%s,'sync','zepto','zepto','done',%s)", (A, Jsonb({"trigger": "manual"})))
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", _refresh(client)))
    t.start()
    time.sleep(0.5)
    assert "r" not in out  # waiting for the other request's transaction
    pg2.execute("commit")
    t.join(10)
    assert out["r"].status_code == 429


def recipe_lock_key():
    from server import limits
    return limits.refresh_lock_key(A, "zepto")


def test_scheduled_syncs_do_not_count(client, pg):
    _connected(pg)
    for _ in range(5):
        _manual_job(pg, trigger="schedule")
    r = _refresh(client)
    assert r.status_code == 202 and r.json()["refresh_left"] == 2


def test_refresh_needs_login_is_409(client, pg):
    _connected(pg, status="needs_login")
    r = _refresh(client)
    assert r.status_code == 409 and r.json()["detail"] == "Reconnect Zepto first."
    assert pg.execute("select count(*) from jobs").fetchone()[0] == 0


def test_refresh_paused_site_is_503(client, pg):
    _connected(pg)
    store.put_recipe(pg, "zepto", {"x": 1})
    recipe_admin.pause_site(pg, "zepto", "orders", "drift")
    r = _refresh(client)
    assert r.status_code == 503 and r.json()["detail"] == "Zepto sync is paused while we fix it."
    recipe_admin.mark_drift(pg, "zepto", "orders", "x")
    recipe_admin.resume_site(pg, "zepto", "orders")
    recipe_admin.mark_drift(pg, "zepto", "orders", "unhealthy but not paused")
    assert _refresh(client).status_code == 202


def test_refresh_allowed_while_budget_paused(client, pg):
    _connected(pg)
    pg.execute("insert into budget_state(month, level, spent_inr, budget_inr) values (%s,'paused',6000,5000)",
               (usage.month_of(tz="Asia/Kolkata"),))
    assert _refresh(client).status_code == 202


# ---------------------------------------------------------------- me, limits, push

def test_me_and_limits(client, pg):
    add_user(pg, A, "Owner@Example.com")
    assert client.get("/me", headers=auth_header(A)).json() == \
        {"user_id": A, "email": "owner@example.com", "is_admin": False}
    pg.execute("insert into admins(email) values ('owner@example.com')")
    assert client.get("/me", headers=auth_header(A)).json()["is_admin"] is True
    _manual_job(pg)
    usage.record(pg, A, usage.today(tz="Asia/Kolkata"), cart_checks=4)
    body = client.get("/me/limits", headers=auth_header(A)).json()
    assert body["refresh_left"] == {"zepto": 2, "blinkit": 3, "swiggy": 3, "instamart": 3, "zomato": 3}
    assert (body["refresh_limit"], body["cart_checks_left"], body["cart_checks_limit"]) == (3, 6, 10)
    assert body["budget_paused"] is False
    assert body["resets_at"].endswith("T00:00:00+05:30")
    pg.execute("insert into budget_state(month, level, spent_inr, budget_inr) values (%s,'paused',6000,5000)",
               (usage.month_of(tz="Asia/Kolkata"),))
    assert client.get("/me/limits", headers=auth_header(A)).json()["budget_paused"] is True
    assert client.get("/me").status_code == 401


def test_push_subscribe_and_unsubscribe(client, pg):
    sub = {"endpoint": "https://push.example.com/abc", "keys": {"p256dh": "pk", "auth": "ak"}}
    assert client.post("/push/subscribe", json=sub, headers=auth_header(A)).status_code == 204
    assert pg.execute("select user_id::text, p256dh, auth from push_subscriptions").fetchall() == [(A, "pk", "ak")]
    # another user cannot remove it
    assert client.post("/push/unsubscribe", json={"endpoint": sub["endpoint"]},
                       headers=auth_header(B)).status_code == 204
    assert pg.execute("select count(*) from push_subscriptions").fetchone()[0] == 1
    assert client.post("/push/unsubscribe", json={"endpoint": sub["endpoint"]},
                       headers=auth_header(A)).status_code == 204
    assert pg.execute("select count(*) from push_subscriptions").fetchone()[0] == 0


def test_push_subscribe_rejects_non_https(client, pg):
    sub = {"endpoint": "http://push.example.com/abc", "keys": {"p256dh": "pk", "auth": "ak"}}
    assert client.post("/push/subscribe", json=sub, headers=auth_header(A)).status_code == 422
    assert client.post("/push/subscribe", json={"endpoint": "https://x"}, headers=auth_header(A)).status_code == 422
    assert pg.execute("select count(*) from push_subscriptions").fetchone()[0] == 0


def test_vapid_key_404_when_unset(client, monkeypatch):
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    assert client.get("/push/vapid-key", headers=auth_header(A)).status_code == 404
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BPublicKey")
    assert client.get("/push/vapid-key", headers=auth_header(A)).json() == {"key": "BPublicKey"}


def test_ask_records_ai_tokens(client, pg, ask_env, monkeypatch):  # noqa: F811
    store.upsert_orders(pg, A, "zepto", [{"order_id": "1", "placed_at": "2025-01-05T10:00:00",
                                          "total_amount": 100.0, "currency": "INR", "status": "delivered",
                                          "merchant": "Store", "items": [], "raw": {}}])
    import metering
    monkeypatch.setattr(ask, "default_llm", StubLLM)
    real_answer = ask.answer
    monkeypatch.setattr(ask, "answer", lambda *a, **k: (metering.count_jev(), real_answer(*a, **k))[1])
    r = client.post("/ask", json={"question": "How much did I spend?"}, headers=auth_header(A))
    assert r.status_code == 200
    # two calls of 100 input + 10 output tokens each, plus one Jev call at JEV_TOKENS_PER_CALL=1000
    assert pg.execute("select user_id::text, ai_tokens from usage").fetchall() == [(A, 1220)]


# ---------------------------------------------------------------- admin

ADMIN_ROUTES = [("get", "/admin/costs"), ("get", "/admin/sites"), ("post", "/admin/sites/zepto/pause"),
                ("post", "/admin/sites/zepto/resume"), ("post", "/admin/learn/zepto"), ("get", "/admin/invites"),
                ("post", "/admin/invites"), ("delete", "/admin/invites/x@example.com"),
                ("get", "/admin/approvals?status=pending"),
                ("post", "/admin/approvals/00000000-0000-0000-0000-000000000001/decide")]


def _admin(pg, user=A, email="owner@example.com"):
    add_user(pg, user, email)
    pg.execute("insert into admins(email) values (%s)", (email,))
    return auth_header(user)


def test_admin_routes_forbidden_for_non_admin(client, pg):
    add_user(pg, B, "user@example.com")
    for method, path in ADMIN_ROUTES:
        r = client.request(method, path, headers=auth_header(B), json={})
        assert (r.status_code, r.json()["detail"]) == (403, "admin only"), path
        assert client.request(method, path).status_code == 401, path


def test_admin_costs_converts_usage_to_inr(client, pg):
    h = _admin(pg)
    add_user(pg, B, "user@example.com")
    month = usage.month_of(tz="Asia/Kolkata")
    day = usage.today(tz="Asia/Kolkata")
    usage.record(pg, B, day, proxy_mb=1000, ai_tokens=10_000, worker_seconds=600, syncs=2, cart_checks=1)
    usage.record(pg, A, day, ai_tokens=1000)
    body = client.get(f"/admin/costs?month={month}", headers=h).json()
    assert body["month"] == month and body["budget_inr"] == 5000 and body["fixed_inr"] == 0
    assert body["unit_costs"] == {"proxy_inr_per_gb": 400, "ai_inr_per_1k_tokens": 0.05,
                                  "worker_inr_per_minute": 0.2}
    first = body["users"][0]
    assert first == {"user_id": B, "email": "user@example.com", "proxy_gb": 1.0, "ai_tokens": 10_000,
                     "worker_minutes": 10.0, "syncs": 2, "cart_checks": 1, "inr": 402.5}
    assert [u["user_id"] for u in body["users"]] == [B, A]
    assert body["spent_inr"] == pytest.approx(402.55)
    assert (body["pct"], body["level"]) == (8, "ok")
    assert client.get("/admin/costs", headers=h).json()["month"] == month
    assert client.get("/admin/costs?month=2026-13", headers=h).status_code == 422


def test_admin_invite_moves_waitlist_and_emails(client, pg):
    h = _admin(pg)
    pg.execute("insert into waitlist(email) values ('new@example.com')")
    r = client.post("/admin/invites", json={"email": " New@Example.com "}, headers=h)
    assert r.status_code == 201
    assert pg.execute("select invited_by::text from invites where email='new@example.com'").fetchone() == (A,)
    assert pg.execute("select invited_at is not null from waitlist").fetchone() == (True,)
    row = pg.execute("select email, kind, title, body, dedupe_key from notifications").fetchone()
    assert row == ("new@example.com", "invite", "You're invited to Hisaab",
                   "Sign up with this email at http://localhost:3000/login.", "invite:new@example.com")
    assert client.post("/admin/invites", json={"email": "new@example.com"}, headers=h).status_code == 201
    assert pg.execute("select count(*) from notifications").fetchone()[0] == 1  # deduped
    assert client.post("/admin/invites", json={"email": "not-an-email"}, headers=h).status_code == 422
    body = client.get("/admin/invites", headers=h).json()
    assert [i["email"] for i in body["invites"]] == ["new@example.com"]
    assert body["waitlist"][0]["email"] == "new@example.com" and body["waitlist"][0]["invited_at"]
    assert client.delete("/admin/invites/new@example.com", headers=h).status_code == 204
    assert client.delete("/admin/invites/new@example.com", headers=h).status_code == 404


def test_admin_revoke_accepted_invite_is_409(client, pg):
    h = _admin(pg)
    pg.execute("insert into invites(email, accepted_at) values ('joined@example.com', now())")
    r = client.delete("/admin/invites/Joined@Example.com", headers=h)
    assert r.status_code == 409
    assert pg.execute("select count(*) from invites").fetchone()[0] == 1


def test_admin_learn_requires_connected_admin_account(client, pg):
    h = _admin(pg)
    r = client.post("/admin/learn/zepto", headers=h)
    assert (r.status_code, r.json()["detail"]) == (409, "Connect Zepto on your own account first.")
    _connected(pg, status="needs_login")
    assert client.post("/admin/learn/zepto", headers=h).status_code == 409
    _connected(pg, user=B)  # someone else's connection does not count
    assert client.post("/admin/learn/zepto", headers=h).status_code == 409
    _connected(pg)
    r = client.post("/admin/learn/zepto", headers=h)
    assert r.status_code == 202
    assert pg.execute("select user_id::text, type, site, payload from jobs where id=%s",
                      (r.json()["job_id"],)).fetchone() == (A, "learn", "zepto", {"reason": "manual"})
    assert client.post("/admin/learn/zepto", headers=h).status_code == 409  # a learn is active
    sites = {s["site"]: s for s in client.get("/admin/sites", headers=h).json()}
    assert sites["zepto"]["admin_connected"] is True and sites["blinkit"]["admin_connected"] is False
    assert client.post("/admin/learn/nowhere", headers=h).status_code == 400


def test_admin_decide_post_allowlist_notifies_waiting_job(client, pg, pg2, pg_listen):
    h = _admin(pg)
    jid = jobs.enqueue(pg, A, "learn", "zepto", {"reason": "manual"})
    assert jobs.claim(pg, "w1", ("learn",)).id == jid
    approve = recipe_admin.make_approver(pg_listen, pg2, jid, "zepto", "orders", timeout_s=10)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("ok", approve([POST])))
    t.start()
    deadline = time.monotonic() + 10
    pending = []
    while not pending and time.monotonic() < deadline:
        pending = client.get("/admin/approvals?status=pending", headers=h).json()
        time.sleep(0.05)
    assert len(pending) == 1
    a = pending[0]
    assert (a["site"], a["kind"], a["reason"], a["status"], a["job_id"]) == \
        ("zepto", "orders", "post_allowlist", "pending", jid)
    assert a["proposed"] == {"read_post_allowlist": [POST]} and a["live"] is True
    r = client.post(f"/admin/approvals/{a['id']}/decide", json={"decision": "approve"}, headers=h)
    assert r.status_code == 200 and r.json() == {"status": "approved"}
    t.join(10)
    assert out["ok"] is True
    assert client.post(f"/admin/approvals/{a['id']}/decide", json={"decision": "approve"},
                       headers=h).status_code == 409
    assert client.get("/admin/approvals?status=pending", headers=h).json() == []
    assert len(client.get("/admin/approvals?status=all", headers=h).json()) == 1
    assert client.post("/admin/approvals/not-a-uuid/decide", json={"decision": "approve"},
                       headers=h).status_code == 404
    assert client.post(f"/admin/approvals/{a['id']}/decide", json={"decision": "maybe"},
                       headers=h).status_code == 422


def test_admin_decide_stale_recipe_is_409(client, pg):
    h = _admin(pg)
    store.put_recipe(pg, "zepto", {"read_post_allowlist": []})
    held = recipe_admin.apply_learned(pg, "zepto", "orders", {"read_post_allowlist": [POST]}, set(), None,
                                      "learned")
    store.put_recipe(pg, "zepto", {"other": True})  # someone published in between
    r = client.post(f"/admin/approvals/{held['approval_id']}/decide", json={"decision": "approve"}, headers=h)
    assert r.status_code == 409 and "changed" in r.json()["detail"]
    assert store.get_recipe(pg, "zepto") == {"other": True}


def test_admin_pause_resume_site(client, pg):
    h = _admin(pg)
    assert client.post("/admin/sites/zepto/pause", headers=h).status_code == 404  # no recipe yet
    store.put_recipe(pg, "zepto", {"x": 1})
    assert client.post("/admin/sites/zepto/pause", headers=h).status_code == 204
    assert recipe_admin.is_paused(pg, "zepto") is True
    site = next(s for s in client.get("/admin/sites", headers=h).json() if s["site"] == "zepto")
    assert site["paused"] is True and site["health_note"] == "Paused by the admin"
    assert client.post("/admin/sites/zepto/resume", headers=h).status_code == 204
    assert recipe_admin.is_paused(pg, "zepto") is False


def test_cli_add_admin_and_invite(pg, monkeypatch):
    from typer.testing import CliRunner

    import cli
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    r = CliRunner().invoke(cli.app, ["add-admin", "Owner@Example.com"])
    assert r.exit_code == 0, r.output
    assert pg.execute("select email from admins").fetchall() == [("owner@example.com",)]
    assert pg.execute("select email from invites").fetchall() == [("owner@example.com",)]
    r = CliRunner().invoke(cli.app, ["invite", "friend@example.com"])
    assert r.exit_code == 0, r.output
    assert pg.execute("select count(*) from invites where email='friend@example.com'").fetchone()[0] == 1
    assert pg.execute("select kind, email from notifications").fetchall() == [("invite", "friend@example.com")]
    assert CliRunner().invoke(cli.app, ["invite", "bad"]).exit_code == 1
    monkeypatch.delenv("DATABASE_URL")
    assert CliRunner().invoke(cli.app, ["invite", "x@example.com"]).exit_code == 1

"""Scheduler: daily syncs for due connections, budget alerts once per month, notification dispatch."""
import os
import uuid
from datetime import datetime, timedelta, timezone

from server import scheduler
from server.config import Settings

NOW = datetime(2026, 10, 28, 6, 0, tzinfo=timezone.utc)
U = str(uuid.uuid4())


def S(**kw):
    base = dict(smtp_host="mailpit", monthly_budget_inr=1000.0)
    base.update(kw)
    return Settings(**base)


def conn_row(pg, site="zepto", status="connected", age_h: float | None = 25, user=U):
    last = None if age_h is None else NOW - timedelta(hours=age_h)
    pg.execute("insert into auth.users(id) values (%s) on conflict do nothing", (user,))  # enqueue needs the user
    pg.execute("insert into connections (user_id, site, status, last_synced_at) values (%s,%s,%s,%s)",
               (user, site, status, last))


def jobs(pg):
    return pg.execute("select type, site, payload from jobs order by created_at").fetchall()


def test_tick_enqueues_daily_sync_for_due_connection(pg):
    conn_row(pg)
    r = scheduler.tick(pg, S(), NOW)
    assert r["enqueued"] == 1 and not r["skipped"]
    rows = jobs(pg)
    assert len(rows) == 1 and rows[0][0] == "sync" and rows[0][2]["trigger"] == "schedule"


def test_tick_skips_recent_and_active(pg):
    conn_row(pg, "zepto", age_h=5)
    conn_row(pg, "blinkit", age_h=30)
    conn_row(pg, "zomato", age_h=30)
    pg.execute("insert into jobs (user_id, type, site, profile, status) values (%s,'sync','blinkit','blinkit','queued')", (U,))
    pg.execute("insert into jobs (user_id, type, site, profile, status) values (%s,'connect','zomato','zomato','running')", (U,))
    assert scheduler.tick(pg, S(), NOW)["enqueued"] == 0


def test_tick_skips_needs_login_disconnected_and_never_synced_error(pg):
    conn_row(pg, "zepto", "needs_login")
    conn_row(pg, "blinkit", "disconnected")
    conn_row(pg, "zomato", "error", age_h=None)
    conn_row(pg, "swiggy", "syncing")
    assert scheduler.due_connections(pg, NOW, 24) == []
    conn_row(pg, "instamart", "error", age_h=30)
    assert scheduler.due_connections(pg, NOW, 24) == [(U, "instamart")]


def test_never_synced_connected_is_due(pg):
    conn_row(pg, "zepto", "connected", age_h=None)
    assert scheduler.due_connections(pg, NOW, 24) == [(U, "zepto")]


def test_tick_skips_unhealthy_or_paused_recipe(pg):
    conn_row(pg, "zepto")
    conn_row(pg, "blinkit")
    conn_row(pg, "zomato")
    pg.execute("insert into recipes (site, kind, recipe, healthy) values ('zepto','orders','{}',false)")
    pg.execute("insert into recipes (site, kind, recipe, paused) values ('blinkit','orders','{}',true)")
    pg.execute("insert into recipes (site, kind, recipe) values ('zomato','orders','{}')")
    assert scheduler.due_connections(pg, NOW, 24) == [(U, "zomato")]
    # an unhealthy search recipe does not matter, and a missing orders recipe does not exclude
    pg.execute("delete from recipes")
    pg.execute("insert into recipes (site, kind, recipe, healthy) values ('zepto','search','{}',false)")
    assert len(scheduler.due_connections(pg, NOW, 24)) == 3


def test_failed_scheduled_sync_not_retried_same_day(pg):
    conn_row(pg, "zepto", "error", age_h=30)
    pg.execute("insert into jobs (user_id, type, site, profile, status, created_at) "
               "values (%s,'sync','zepto','zepto','failed',%s)", (U, NOW - timedelta(hours=2)))
    assert scheduler.tick(pg, S(), NOW)["enqueued"] == 0
    assert scheduler.tick(pg, S(), NOW + timedelta(hours=23))["enqueued"] == 1


def test_paused_budget_syncs_every_three_days(pg):
    s = S(fixed_monthly_inr=1000.0)
    conn_row(pg, age_h=48)
    r = scheduler.tick(pg, s, NOW)
    assert r["level"] == "paused" and r["enqueued"] == 0
    pg.execute("update connections set last_synced_at = %s", (NOW - timedelta(hours=73),))
    assert scheduler.tick(pg, s, NOW)["enqueued"] == 1


def test_budget_alerts_sent_once_per_month(pg):
    pg.execute("insert into admins (email) values ('a@x.com'), ('b@x.com')")
    s = S(fixed_monthly_inr=850.0)
    scheduler.tick(pg, s, NOW)
    scheduler.tick(pg, s, NOW)
    rows = pg.execute("select kind, title, body from notifications order by email").fetchall()
    assert [r[0] for r in rows] == ["budget_alert"] * 2
    assert rows[0][1] == "Budget at 85% for October 2026"
    assert rows[0][2] == "₹850 of ₹1,000 used. Sign-ups and cart checks pause at 100%."
    s2 = S(fixed_monthly_inr=1000.0)
    scheduler.tick(pg, s2, NOW)
    scheduler.tick(pg, s2, NOW)
    kinds = sorted(r[0] for r in pg.execute("select kind from notifications").fetchall())
    assert kinds == ["budget_alert"] * 2 + ["budget_paused"] * 2
    t = pg.execute("select title, body from notifications where kind='budget_paused' limit 1").fetchone()
    assert t[0] == "Budget reached for October 2026"
    assert t[1].endswith("automatic syncs run every 3 days until 1 November.")


def test_concurrent_ticks_enqueue_once(pg, pg2):
    conn_row(pg)
    pg2.execute("select pg_advisory_lock(727274)")
    assert scheduler.tick(pg, S(), NOW) == {"skipped": True}
    assert jobs(pg) == []
    pg2.execute("select pg_advisory_unlock(727274)")
    assert scheduler.tick(pg, S(), NOW)["enqueued"] == 1
    assert scheduler.tick(pg, S(), NOW)["enqueued"] == 0
    assert pg2.execute("select pg_try_advisory_lock(727274)").fetchone()[0] is True  # released after each tick


def test_tick_dispatches_pending_notifications(pg, monkeypatch):
    pg.execute("insert into admins (email) values ('a@x.com')")
    sent = []
    monkeypatch.setattr("server.notify.mail_send", lambda to, subj, text, s: sent.append((to, subj)))
    r = scheduler.tick(pg, S(fixed_monthly_inr=850.0), NOW)
    assert r["sent"] == 1 and sent == [("a@x.com", "Budget at 85% for October 2026")]


def test_main_once_runs_one_tick(pg, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    conn_row(pg, age_h=None)
    calls = []
    real = scheduler.tick
    monkeypatch.setattr(scheduler, "tick", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert scheduler.main(once=True) == 0
    assert len(calls) == 1 and len(jobs(pg)) == 1


def _failed_syncs(pg, n, site="zepto"):
    for _ in range(n):
        pg.execute("insert into jobs (user_id, type, site, profile, status, error, created_at, finished_at) "
                   "values (%s,'sync',%s,%s,'failed','RuntimeError: x',%s,%s)",
                   (U, site, site, NOW - timedelta(hours=1), NOW - timedelta(hours=1)))


def test_tick_pauses_site_over_error_budget_and_emails_admins(pg):
    pg.execute("insert into admins (email) values ('a@x.com')")
    pg.execute("insert into recipes (site, kind, recipe) values ('zepto','orders','{}')")
    _failed_syncs(pg, 10)
    r = scheduler.tick(pg, S(), NOW)
    assert r["paused_sites"] == [["zepto", "orders"]]
    assert pg.execute("select paused from recipes").fetchone() == (True,)
    assert pg.execute("select kind from notifications where kind='error_budget'").fetchall() == [("error_budget",)]
    assert scheduler.tick(pg, S(), NOW)["paused_sites"] == []


def test_tick_sends_needs_login_reminder_once(pg):
    pg.execute("insert into connections (user_id, site, status, needs_login_at) values (%s,'zepto','needs_login',%s)",
               (U, NOW - timedelta(hours=73)))
    pg.execute("insert into connections (user_id, site, status, needs_login_at) values (%s,'blinkit','needs_login',%s)",
               (U, NOW - timedelta(hours=10)))
    assert scheduler.tick(pg, S(), NOW)["reminders"] == 1
    assert scheduler.tick(pg, S(), NOW + timedelta(hours=1))["reminders"] == 0
    assert scheduler.tick(pg, S(), NOW + timedelta(hours=100))["reminders"] == 1  # blinkit, now past 72h
    assert pg.execute("select count(*) from notifications where kind='needs_login_reminder'").fetchone() == (2,)


def test_tick_skips_cooling_site(pg):
    from server import site_health
    conn_row(pg, "zepto")
    conn_row(pg, "blinkit")
    site_health.record_throttle(pg, "zepto", "rate_limited", 0, S(), now=NOW, rand=lambda: 0)
    assert scheduler.due_connections(pg, NOW, 24) == [(U, "blinkit")]
    assert scheduler.tick(pg, S(), NOW)["enqueued"] == 1
    assert scheduler.due_connections(pg, NOW + timedelta(minutes=5), 24) == [(U, "zepto")]


def test_tick_result_has_new_keys(pg):
    r = scheduler.tick(pg, S(), NOW)
    assert r["paused_sites"] == [] and r["reminders"] == 0
    assert {"skipped", "level", "enqueued", "sent"} <= set(r)


def test_main_checks_hosted_config(monkeypatch):
    seen = []
    monkeypatch.setattr("server.config.check_hosted", lambda s, role: seen.append(role))
    monkeypatch.setattr(scheduler, "settings", lambda: S())
    monkeypatch.setattr(scheduler.psycopg, "connect", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert scheduler.main(once=True) == 1
    assert seen == ["scheduler"]


def test_user_deleted_between_due_and_enqueue_does_not_abort_the_tick(pg, monkeypatch):
    """due_connections listed the user, then the account was deleted: skip them, enqueue the rest, still dispatch."""
    gone, other = str(uuid.uuid4()), str(uuid.uuid4())
    conn_row(pg, user=gone)
    conn_row(pg, user=other)
    real = scheduler.due_connections

    def due_then_delete(c, now, interval):
        rows = real(c, now, interval)
        pg.execute("delete from auth.users where id=%s", (gone,))
        return rows
    monkeypatch.setattr(scheduler, "due_connections", due_then_delete)
    sent = []
    monkeypatch.setattr(scheduler.notify, "dispatch", lambda c, s: sent.append(1) or 0)
    r = scheduler.tick(pg, S(), NOW)
    assert r["enqueued"] == 1 and sent == [1]
    assert [row[0] for row in pg.execute("select user_id::text from jobs").fetchall()] == [other]


def test_tick_finalizes_stale_carts_and_purges_food_cache(pg):
    from server import food_cart
    pg.execute("insert into restaurant_cache (user_id, site, geo_key, query_norm, results, fetched_at) "
               "values (%s,'swiggy','22.33,73.15','pizza','[]', %s)", (U, NOW - timedelta(hours=30)))
    pg.execute("insert into restaurant_cache (user_id, site, geo_key, query_norm, results, fetched_at) "
               "values (%s,'swiggy','22.33,73.15','dosa','[]', %s)", (U, NOW - timedelta(hours=1)))
    r = scheduler.tick(pg, S(), NOW)
    assert r["carts_finalized"] == 0
    assert pg.execute("select query_norm from restaurant_cache").fetchall() == [("dosa",)]
    assert food_cart.PURGE_AFTER_S == 86400

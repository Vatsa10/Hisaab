"""Speed budgets (Plan 7 Task 16): phase timing, slow-job flag, capped parallel fetch, warm Chrome reuse."""
import logging

from helpers import A, order, settings_for_tests
from test_worker_plan7 import _claim

import browser
from server import jobs, session, store, timing, worker
from server.config import Settings


def _raise(exc):
    def f(*a, **k):
        raise exc
    return f


def test_phase_records_ms():
    clock = iter([10.0, 10.25, 11.0, 11.5])
    rec = timing.Recorder(clock=lambda: next(clock))
    with rec.phase("open"):
        pass
    with rec.phase("fetch"):
        pass
    assert rec.phases == [{"phase": "open", "ms": 250}, {"phase": "fetch", "ms": 500}]
    assert rec.phase_ms() == {"open": 250, "fetch": 500}


def test_phase_without_recorder_is_a_noop():
    with timing.Phase("open"):
        pass


def test_over_budget_flags_slow_job(caplog):
    rec = timing.Recorder(clock=lambda: 0.0)
    rec.phases = [{"phase": "open", "ms": 1000}, {"phase": "fetch", "ms": 31000}]
    with caplog.at_level(logging.WARNING):
        out = rec.summary("zepto", budget_s=30)
    assert out["slow"] is True and out["total_ms"] == 32000
    assert "slow_job site=zepto phase=fetch" in caplog.text
    assert rec.summary("zepto", budget_s=60)["slow"] is False


def test_budgets_in_settings():
    s = Settings()
    assert (s.sync_budget_s, s.compare_budget_s, s.food_compare_budget_s, s.fill_budget_s) == (30, 20, 20, 30)


class _Page:
    def __init__(self):
        self.batches = []

    def evaluate(self, js, arg):
        self.batches.append(len(arg["reqs"]))
        return [{"status": 200, "url": "u", "text": "{}", "retryAfter": None} for _ in arg["reqs"]]


def test_parallel_fetch_caps_at_4():
    page = _Page()
    reqs = [{"url": f"https://x/{i}", "method": "GET", "headers": {}} for i in range(10)]
    out = browser.fetch_many_in_page(page, reqs)
    assert len(out) == 10 and page.batches == [4, 4, 2]
    assert browser.MAX_PARALLEL_FETCH == 4


def test_chrome_reused_for_same_profile():
    started, closed, t = [], [], [0.0]
    pool = browser.ChromePool(idle_s=120, clock=lambda: t[0])

    def start(key):
        started.append(key)
        return {"key": key}
    a = pool.get("p1", start, closed.append)
    b = pool.get("p1", start, closed.append)
    assert a is b and started == ["p1"]
    c = pool.get("p2", start, closed.append)
    assert c is not a and closed == [a]
    t[0] = 500.0
    pool.close_idle(closed.append)
    assert closed == [a, c]
    pool.get("p2", start, closed.append)
    assert started == ["p1", "p2", "p2"]


def test_goto_has_no_fixed_sleep():
    waits = []

    class P:
        url = "https://x/"

        def goto(self, url, wait_until):
            self.wait_until = wait_until

        def wait_for_timeout(self, ms):
            waits.append(ms)

        def wait_for_load_state(self, state, timeout=None):
            pass
    p = P()
    browser.goto(p, "https://x/")
    assert waits == [] and p.wait_until == "commit"


# ---------------------------------------------------------------- carry-overs

def test_expire_session_falls_back_when_mark_needs_login_raises(pg, monkeypatch):
    store.set_connection(pg, A, "zepto", status="connected")
    monkeypatch.setattr(session, "mark_needs_login", _raise(RuntimeError("db hiccup")))
    monkeypatch.setattr(worker.sync, "fetch_orders", _raise(browser.SessionExpired("401")))
    jid, job = _claim(pg)
    worker.run_job(job, pg, settings_for_tests())
    assert store.get_connection(pg, A, "zepto")["status"] == "needs_login"


def test_throttled_out_fails_site_busy_when_requeue_declines(pg, monkeypatch):
    monkeypatch.setattr(jobs, "requeue", lambda *a, **k: None)
    monkeypatch.setattr(worker.sync, "fetch_orders", _raise(browser.SiteThrottled("rate_limited", 120)))
    jid, job = _claim(pg)
    worker.run_job(job, pg, settings_for_tests())
    assert pg.execute("select status, error from jobs where id=%s", (jid,)).fetchone() == ("failed", "site_busy")


def test_job_timings_are_stored(pg, monkeypatch):
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: [order("1")])
    jid, job = _claim(pg)
    worker.run_job(job, pg, settings_for_tests())
    t = pg.execute("select timings from jobs where id=%s", (jid,)).fetchone()[0]
    assert t["slow"] is False and "total_ms" in t and isinstance(t["phases"], list)


def test_site_health_reports_p50_p95_and_slow_count(pg):
    from datetime import datetime, timezone

    from server import site_health
    for ms, slow in ((1000, False), (2000, False), (40000, True)):
        jid = jobs.enqueue(pg, A, "sync", "zepto")
        jobs.claim(pg, "w", ("sync",))
        jobs.finish(pg, jid, worker_id="w")
        jobs.set_timings(pg, jid, {"phases": [], "total_ms": ms, "slow": slow})
    out = site_health.speed_fields(pg, settings_for_tests(), "zepto", datetime.now(timezone.utc))
    assert out == {"p50_ms": 2000, "p95_ms": 40000, "slow_jobs_window": 1}

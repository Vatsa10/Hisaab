import threading

import pytest
from helpers import A, order, settings_for_tests

from browser import SessionExpired
from server import jobs, store, worker


def test_prompt_otp_round_trip(pg, pg_listen, pg2):
    jid = jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    ask = worker.make_otp_prompt(pg_listen, pg, jid, timeout_s=5)
    threading.Timer(0.5, lambda: pg2.execute("select pg_notify(%s, %s)", (f"otp_{jid.replace('-', '')}", "4321"))).start()
    assert ask("Enter OTP") == "4321"
    assert pg.execute("select progress->>'stage' from jobs").fetchone() == ("otp_received",)


def test_prompt_otp_times_out(pg, pg_listen):
    jid = jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    with pytest.raises(TimeoutError):
        worker.make_otp_prompt(pg_listen, pg, jid, timeout_s=1)("Enter OTP")


def test_sync_job_stores_orders(pg, monkeypatch):
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda site, since, **k: [order("1"), order("2")])
    jobs.enqueue(pg, A, "sync", "zepto")
    job = jobs.claim(pg, "w", ("sync",))
    worker.run_job(job, pg, settings_for_tests())
    assert pg.execute("select count(*) from orders where user_id=%s", (A,)).fetchone() == (2,)
    assert pg.execute("select status from connections").fetchone() == ("connected",)
    assert pg.execute("select last_synced_at is not null from connections").fetchone() == (True,)


def test_expired_session_marks_needs_login(pg, monkeypatch):
    def expired(*a, **k):
        raise SessionExpired("HTTP 401")
    monkeypatch.setattr(worker.sync, "fetch_orders", expired)
    jobs.enqueue(pg, A, "sync", "zepto")
    worker.run_job(jobs.claim(pg, "w", ("sync",)), pg, settings_for_tests())
    assert pg.execute("select status from connections").fetchone() == ("needs_login",)
    assert pg.execute("select status, error from jobs").fetchone() == ("failed", "needs_login")


def test_connect_job_happy_path(pg, monkeypatch):
    seen = {}

    def login(site, phone=None, **k):
        seen.update(site=site, phone=phone, prompt=k["prompt"], profile_dir=k["profile_dir"])
        return True

    def fetch(site, since, **k):
        seen["allow_learn"] = k["allow_learn"]
        return [order("1")]
    monkeypatch.setattr(worker.sync, "login", login)
    monkeypatch.setattr(worker.sync, "fetch_orders", fetch)
    jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    worker.run_job(jobs.claim(pg, "w", ("connect",)), pg, settings_for_tests())
    assert seen["phone"] == "9876543210" and callable(seen["prompt"]) and seen["allow_learn"] is False
    assert pg.execute("select status from connections").fetchone() == ("connected",)
    assert pg.execute("select count(*) from orders").fetchone() == (1,)
    assert pg.execute("select status, error, payload ? 'phone' from jobs").fetchone() == ("done", None, False)


def test_connect_login_failed(pg, monkeypatch):
    monkeypatch.setattr(worker.sync, "login", lambda *a, **k: False)
    jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    worker.run_job(jobs.claim(pg, "w", ("connect",)), pg, settings_for_tests())
    assert pg.execute("select status from connections").fetchone() == ("error",)
    assert pg.execute("select status, error from jobs").fetchone() == ("failed", "login_failed")


def test_connect_otp_timeout(pg, monkeypatch):
    def login(*a, **k):
        raise TimeoutError("no otp")
    monkeypatch.setattr(worker.sync, "login", login)
    jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    worker.run_job(jobs.claim(pg, "w", ("connect",)), pg, settings_for_tests())
    assert pg.execute("select status from connections").fetchone() == ("error",)
    assert pg.execute("select error from jobs").fetchone() == ("otp_timeout",)


def test_recipe_error_keeps_connection_status(pg, monkeypatch):
    def drift(*a, **k):
        raise worker.sync.RecipeError("no recipe for zepto")
    monkeypatch.setattr(worker.sync, "fetch_orders", drift)
    store.set_connection(pg, A, "zepto", status="connected")
    jobs.enqueue(pg, A, "sync", "zepto")
    worker.run_job(jobs.claim(pg, "w", ("sync",)), pg, settings_for_tests())
    assert pg.execute("select status from connections").fetchone() == ("connected",)
    assert pg.execute("select status, error from jobs").fetchone() == ("failed", "no_recipe: no recipe for zepto")


def test_exception_message_masks_phone(pg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("page failed for 9876543210 at step 3\nsecond line")
    monkeypatch.setattr(worker.sync, "login", boom)
    jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    worker.run_job(jobs.claim(pg, "w", ("connect",)), pg, settings_for_tests())
    err = pg.execute("select error from jobs").fetchone()[0]
    assert err.startswith("RuntimeError: ") and "9876543210" not in err and "second line" not in err
    assert pg.execute("select status from connections").fetchone() == ("error",)


def test_main_survives_a_crashing_job(pg, monkeypatch):
    jobs.enqueue(pg, A, "sync", "zepto")
    calls = []

    def run(job, conn, s, **k):
        calls.append(job.id)
        raise RuntimeError("kaboom")
    monkeypatch.setattr(worker, "run_job", run)
    monkeypatch.setattr(worker, "settings", settings_for_tests)

    def stop(_):
        raise KeyboardInterrupt
    monkeypatch.setattr(worker.time, "sleep", stop)
    worker.main("w1", poll_s=0.01)
    assert len(calls) == 1
    assert pg.execute("select status, error from jobs").fetchone() == ("failed", "RuntimeError: kaboom")


def test_main_reconnects_after_a_db_error(pg, monkeypatch):
    import psycopg
    jobs.enqueue(pg, A, "sync", "zepto")
    opened, ran = [], []
    real_connect, real_claim = worker._connect, jobs.claim
    monkeypatch.setattr(worker, "_connect", lambda url: opened.append(1) or real_connect(url))
    state = {"n": 0}

    def flaky_claim(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise psycopg.OperationalError("server closed the connection")
        return real_claim(*a, **k)
    monkeypatch.setattr(worker.jobs, "claim", flaky_claim)
    monkeypatch.setattr(worker, "run_job", lambda job, conn, s, **k: (ran.append(job.id), jobs.finish(conn, job.id)))
    monkeypatch.setattr(worker, "settings", settings_for_tests)
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    worker.main("w1", poll_s=0.01, max_loops=3)
    assert len(opened) == 4  # two connections, then both reopened after the error
    assert len(ran) == 1
    assert pg.execute("select status from jobs").fetchone() == ("done",)


def test_main_uses_stale_job_setting(pg, monkeypatch):
    jid = jobs.enqueue(pg, A, "sync", "zepto")
    jobs.claim(pg, "other", ("sync",))
    pg.execute("update jobs set started_at = now() - interval '20 minutes' where id=%s", (jid,))
    monkeypatch.setattr(worker, "settings", settings_for_tests)
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    worker.main("w1", poll_s=0.01, max_loops=1)
    assert pg.execute("select status from jobs").fetchone() == ("running",)
    assert settings_for_tests().stale_job_s == 2400

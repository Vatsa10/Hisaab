import dataclasses
import threading
import time

import pytest
from helpers import A, fake_login_env, order, settings_for_tests

import sync
from browser import SessionExpired
from server import jobs, store, worker


def _notify_later(conn, jid, payload, delay=0.5):
    threading.Timer(delay, lambda: conn.execute("select pg_notify(%s, %s)",
                                                (f"otp_{jid.replace('-', '')}", payload))).start()


def test_prompt_otp_round_trip(pg, pg_listen, pg2):
    jid = jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    ask = worker.make_prompt(pg_listen, pg, jid, timeout_s=5)
    _notify_later(pg2, jid, "4321")
    assert ask("Enter OTP", "otp") == "4321"
    assert pg.execute("select progress->>'stage' from jobs").fetchone() == ("otp_received",)


def test_prompt_otp_times_out(pg, pg_listen):
    jid = jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    with pytest.raises(sync.OtpTimeout) as e:
        worker.make_prompt(pg_listen, pg, jid, timeout_s=1)("Enter OTP", "otp")
    assert e.value.kind == "otp"
    assert pg.execute("select progress->>'stage' from jobs").fetchone() == ("otp_needed",)


def test_prompt_question_publishes_question_needed(pg, pg_listen):
    jid = jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    with pytest.raises(sync.OtpTimeout) as e:
        worker.make_prompt(pg_listen, pg, jid, timeout_s=1)("Solve the captcha?", "question")
    assert e.value.kind == "question"
    assert pg.execute("select progress->>'stage', progress->>'question' from jobs").fetchone() ==         ("question_needed", "Solve the captcha?")


def test_prompt_question_round_trip(pg, pg_listen, pg2):
    jid = jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    _notify_later(pg2, jid, "done")
    assert worker.make_prompt(pg_listen, pg, jid, timeout_s=5)("Solve the captcha?", "question") == "done"
    assert pg.execute("select progress->>'stage' from jobs").fetchone() == ("answer_received",)


def _counting_prompts(monkeypatch):
    waits = []
    real = worker.make_prompt

    def make(*a, **k):
        inner = real(*a, **k)

        def prompt(text, kind="otp"):
            waits.append(kind)
            return inner(text, kind)
        return prompt
    monkeypatch.setattr(worker, "make_prompt", make)
    return waits


def test_real_login_otp_timeout_waits_once(pg, monkeypatch):
    def run(self):  # the real LoginAgent.enter_otp, as when the OTP field appears
        self.enter_otp()
        return self.done or {"success": False, "note": "stuck", "url": self.page.url}
    fake_login_env(monkeypatch, run)
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: pytest.fail("must not sync"))
    waits = _counting_prompts(monkeypatch)
    jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    t0 = time.monotonic()
    worker.run_job(jobs.claim(pg, "w", ("connect",)), pg, dataclasses.replace(settings_for_tests(), otp_timeout_s=1))
    assert waits == ["otp"]
    assert time.monotonic() - t0 < 3
    assert pg.execute("select status, error, payload ? 'phone' from jobs").fetchone() == ("failed", "otp_timeout", False)
    assert pg.execute("select status from connections").fetchone() == ("error",)


def test_real_login_question_timeout(pg, monkeypatch):
    def run(self):
        self.ask_user("Is this your account?")
        return self.done or {"success": False, "note": "stuck", "url": self.page.url}
    fake_login_env(monkeypatch, run)
    waits = _counting_prompts(monkeypatch)
    jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    worker.run_job(jobs.claim(pg, "w", ("connect",)), pg, dataclasses.replace(settings_for_tests(), otp_timeout_s=1))
    assert waits == ["question"]
    assert pg.execute("select status, error from jobs").fetchone() == ("failed", "question_timeout")


def test_real_login_agent_failure_is_login_failed(pg, monkeypatch):
    fake_login_env(monkeypatch, lambda self: {"success": False, "note": "stuck", "url": self.page.url})
    waits = _counting_prompts(monkeypatch)
    jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    worker.run_job(jobs.claim(pg, "w", ("connect",)), pg, settings_for_tests())
    assert waits == []
    assert pg.execute("select status, error from jobs").fetchone() == ("failed", "login_failed")
    assert pg.execute("select status from connections").fetchone() == ("error",)


def test_sync_socket_timeout_is_a_plain_error(pg, monkeypatch):
    def slow(*a, **k):
        raise TimeoutError("timed out")
    monkeypatch.setattr(worker.sync, "fetch_orders", slow)
    store.set_connection(pg, A, "zepto", status="connected")
    jobs.enqueue(pg, A, "sync", "zepto")
    worker.run_job(jobs.claim(pg, "w", ("sync",)), pg, settings_for_tests())
    assert pg.execute("select status, error from jobs").fetchone() == ("failed", "TimeoutError: timed out")
    assert pg.execute("select status from connections").fetchone() == ("connected",)


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
        raise sync.OtpTimeout("otp")
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
    assert len(opened) == 2  # one job connection, reopened after the error (listen connections are per job)
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


def test_prompt_unlisten_failure_does_not_mask_the_error(pg):
    jid = jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})

    class Broken:
        def execute(self, q, *a):
            if "UNLISTEN" in q.as_string(None):
                raise RuntimeError("connection is closed")

        def notifies(self, **k):
            raise ValueError("listen connection died")
    with pytest.raises(ValueError, match="listen connection died"):
        worker.make_prompt(Broken(), pg, jid, timeout_s=1)("Enter OTP", "otp")


def test_broken_listen_connection_does_not_break_the_next_connect_job(pg, pg2, monkeypatch):
    opened = []
    real_connect = worker._connect
    monkeypatch.setattr(worker, "_connect", lambda url: opened.append(real_connect(url)) or opened[-1])
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: [order("1")])
    s = settings_for_tests()

    def login_with_dead_listener(site, phone=None, prompt=None, **k):
        opened[-1].close()  # the listen connection drops mid-job
        prompt("Enter OTP", "otp")
        return True
    monkeypatch.setattr(worker.sync, "login", login_with_dead_listener)
    first = jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    worker.run_job(jobs.claim(pg, "w", ("connect",)), pg, s)
    assert pg.execute("select status from jobs where id=%s", (first,)).fetchone() == ("failed",)

    def login(site, phone=None, prompt=None, **k):
        _notify_later(pg2, second, "4321")
        return prompt("Enter OTP", "otp") == "4321"
    monkeypatch.setattr(worker.sync, "login", login)
    second = jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    worker.run_job(jobs.claim(pg, "w", ("connect",)), pg, s)
    assert pg.execute("select status, error from jobs where id=%s", (second,)).fetchone() == ("done", None)
    assert len(opened) == 2 and all(c.closed for c in opened)


def test_run_job_does_not_overwrite_a_reclaimed_job(pg, pg2, monkeypatch):
    jid = jobs.enqueue(pg, A, "sync", "zepto")
    job = jobs.claim(pg, "w1", ("sync",))

    def slow_fetch(*a, **k):  # meanwhile the job was declared stale and another worker took it
        pg2.execute("update jobs set status='running', worker_id='w2', started_at=now() where id=%s", (jid,))
        return [order("1")]
    monkeypatch.setattr(worker.sync, "fetch_orders", slow_fetch)
    worker.run_job(job, pg, settings_for_tests())
    assert pg.execute("select status, worker_id, finished_at from jobs").fetchone() == ("running", "w2", None)


def test_worker_passes_empty_location_without_profile_coordinates(pg, monkeypatch):
    seen = []
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda site, since, **k: seen.append(k["location"]) or [order("1")])
    jobs.enqueue(pg, A, "sync", "zepto")
    worker.run_job(jobs.claim(pg, "w", ("sync",)), pg, settings_for_tests())
    pg.execute("insert into profiles (user_id, address) values (%s, 'Somewhere, City, State')", (A,))
    jobs.enqueue(pg, A, "sync", "zepto")
    worker.run_job(jobs.claim(pg, "w", ("sync",)), pg, settings_for_tests())
    pg.execute("update profiles set lat=12.9, lon=77.6 where user_id=%s", (A,))
    jobs.enqueue(pg, A, "sync", "zepto")
    worker.run_job(jobs.claim(pg, "w", ("sync",)), pg, settings_for_tests())
    assert seen == [{}, {}, {"query": "Somewhere, City, State", "lat": 12.9, "lon": 77.6}]

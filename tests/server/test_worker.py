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
    ask = worker.make_otp_prompt(pg_listen, pg, jid, timeout_s=5)  # the Plan 1 name of make_prompt
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
    pg.execute("update jobs set started_at = now() - interval '20 minutes', heartbeat_at = now() - interval '20 minutes' where id=%s", (jid,))
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


# ---------------------------------------------------------------- vault, proxy relay, heartbeat (Task 7)

import os
import socket
from pathlib import Path

from server import vault
from server.config import DEV_PROFILE_KEY

KEY = vault.master_key_from_env(DEV_PROFILE_KEY)


def _store():
    return vault.store_from_url(os.environ["PROFILE_STORE_URL"])


def _sync_job(pg, site="zepto"):
    jobs.enqueue(pg, A, "sync", site)
    return jobs.claim(pg, "w", ("sync",))


def test_startup_wipes_leftover_profiles(pg, monkeypatch):
    root = Path(os.environ["PROFILE_ROOT"])
    dead = root / "6f1c3d52-8a0e-4c57-9b1a-0d2f4e6a8b10"
    (dead / "Default").mkdir(parents=True)
    (dead / "Default" / "Cookies").write_text("secret")
    monkeypatch.setattr(worker, "settings", settings_for_tests)
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    worker.main("w1", poll_s=0.01, max_loops=1)
    assert not dead.exists()


def test_successful_job_saves_encrypted_profile(pg, monkeypatch):
    seen = {}

    def fetch(site, since, **k):
        seen["dir"] = Path(k["profile_dir"])
        (seen["dir"] / "Cookies").write_text("SESSION-COOKIE-PLAINTEXT")
        return [order("1")]
    monkeypatch.setattr(worker.sync, "fetch_orders", fetch)
    worker.run_job(_sync_job(pg), pg, settings_for_tests())
    key = vault.blob_key(A, "zepto")
    blob = _store().get(key)
    assert blob and b"SESSION-COOKIE-PLAINTEXT" not in blob
    assert pg.execute("select profile_blob_key from connections").fetchone() == (key,)
    assert not seen["dir"].exists()
    out = Path(os.environ["PROFILE_ROOT"]).parent / "restored"
    assert vault.restore_profile(_store(), KEY, A, "zepto", out) is True
    assert (out / "Cookies").read_text() == "SESSION-COOKIE-PLAINTEXT"


def test_job_restores_previous_profile(pg, monkeypatch):
    def first(site, since, **k):
        (Path(k["profile_dir"]) / "Cookies").write_text("v1")
        return []
    monkeypatch.setattr(worker.sync, "fetch_orders", first)
    worker.run_job(_sync_job(pg), pg, settings_for_tests())
    seen = {}

    def second(site, since, **k):
        seen["cookies"] = (Path(k["profile_dir"]) / "Cookies").read_text()
        return []
    monkeypatch.setattr(worker.sync, "fetch_orders", second)
    worker.run_job(_sync_job(pg), pg, settings_for_tests())
    assert seen["cookies"] == "v1"


def test_instamart_job_uses_swiggy_profile_name(pg, monkeypatch):
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda site, since, **k: [])
    worker.run_job(_sync_job(pg, "instamart"), pg, settings_for_tests())
    assert _store().get(vault.blob_key(A, "swiggy")) is not None
    assert _store().get(vault.blob_key(A, "instamart")) is None


def test_crash_mid_job_keeps_last_good_profile(pg, monkeypatch):
    def good(site, since, **k):
        (Path(k["profile_dir"]) / "Cookies").write_text("good")
        return []
    monkeypatch.setattr(worker.sync, "fetch_orders", good)
    worker.run_job(_sync_job(pg), pg, settings_for_tests())
    before = _store().get(vault.blob_key(A, "zepto"))
    seen = {}

    def crash(site, since, **k):
        seen["dir"] = Path(k["profile_dir"])
        (seen["dir"] / "Cookies").write_text("half-written")
        raise RuntimeError("chrome died")
    monkeypatch.setattr(worker.sync, "fetch_orders", crash)
    worker.run_job(_sync_job(pg), pg, settings_for_tests())
    assert _store().get(vault.blob_key(A, "zepto")) == before
    assert not seen["dir"].exists()
    assert pg.execute("select error from jobs order by created_at desc limit 1").fetchone() == ("RuntimeError: chrome died",)


def test_corrupt_profile_marks_needs_login(pg, monkeypatch):
    _store().put(vault.blob_key(A, "zepto"), b"x" * 64)
    store.set_connection(pg, A, "zepto", status="connected")
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: pytest.fail("must not sync"))
    worker.run_job(_sync_job(pg), pg, settings_for_tests())
    assert pg.execute("select status from connections").fetchone() == ("needs_login",)
    assert pg.execute("select status, error from jobs").fetchone() == ("failed", "needs_login")


def test_corrupt_profile_connect_job_starts_empty(pg, monkeypatch):
    _store().put(vault.blob_key(A, "zepto"), b"x" * 64)
    seen = {}

    def login(site, phone=None, **k):
        seen["files"] = list(Path(k["profile_dir"]).iterdir())
        return True
    monkeypatch.setattr(worker.sync, "login", login)
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: [])
    jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    worker.run_job(jobs.claim(pg, "w", ("connect",)), pg, settings_for_tests())
    assert seen["files"] == []
    assert pg.execute("select status from jobs").fetchone() == ("done",)


def _closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_proxy_unavailable_fails_fast(pg, monkeypatch):
    s = dataclasses.replace(settings_for_tests(),
                            proxy_url_template=f"http://user-{{session}}:topsecret@127.0.0.1:{_closed_port()}")
    store.set_connection(pg, A, "zepto", status="connected")
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: pytest.fail("must not sync"))
    t0 = time.monotonic()
    worker.run_job(_sync_job(pg), pg, s)
    assert time.monotonic() - t0 < 30
    assert pg.execute("select status, error from jobs").fetchone() == ("failed", "proxy_unavailable")
    assert pg.execute("select status from connections").fetchone() == ("connected",)


def test_proxy_session_id_created_once(pg, monkeypatch):
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: [])
    worker.run_job(_sync_job(pg), pg, settings_for_tests())
    first = pg.execute("select proxy_session_id from connections").fetchone()[0]
    assert first and len(first) == 12 and int(first, 16) >= 0
    worker.run_job(_sync_job(pg), pg, settings_for_tests())
    assert pg.execute("select proxy_session_id from connections").fetchone() == (first,)


def test_proxy_relay_url_is_passed_to_the_browser(pg, monkeypatch):
    log = {}

    class FakeRelay:
        def __init__(self, upstream):
            log["upstream"] = upstream
            self.url = "http://127.0.0.1:5555"

        def __enter__(self):
            log["entered"] = True
            return self

        def __exit__(self, *e):
            log["exited"] = True

        def check(self, *a, **k):
            log["checked"] = True
    monkeypatch.setattr(worker, "Relay", FakeRelay)
    s = dataclasses.replace(settings_for_tests(), proxy_url_template="http://u-{session}:pw@gw.example.com:7000")
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda site, since, **k: log.update(k=k) or [])
    worker.run_job(_sync_job(pg), pg, s)
    sid = pg.execute("select proxy_session_id from connections").fetchone()[0]
    assert log["upstream"] == f"http://u-{sid}:pw@gw.example.com:7000"
    assert log["k"]["proxy_server"] == "http://127.0.0.1:5555"
    assert log["checked"] and log["exited"]


def test_no_proxy_template_passes_no_proxy(pg, monkeypatch):
    seen = {}
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda site, since, **k: seen.update(k) or [])
    worker.run_job(_sync_job(pg), pg, settings_for_tests())
    assert seen["proxy_server"] is None


def test_heartbeat_runs_during_job(pg, monkeypatch):
    s = dataclasses.replace(settings_for_tests(), heartbeat_s=1)
    job = _sync_job(pg)
    pg.execute("update jobs set heartbeat_at = now() - interval '1 hour'")

    def slow(site, since, **k):
        time.sleep(2.5)
        return []
    monkeypatch.setattr(worker.sync, "fetch_orders", slow)
    worker.run_job(job, pg, s)
    assert pg.execute("select heartbeat_at > now() - interval '1 minute' from jobs").fetchone() == (True,)
    assert not [t for t in threading.enumerate() if t.name == "job-heartbeat"]


def test_orders_url_stored_on_connection(pg, monkeypatch):
    def login(site, phone=None, **k):
        k["on_orders_url"]("https://www.zepto.com/orders")
        return True
    monkeypatch.setattr(worker.sync, "login", login)
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: [])
    jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    worker.run_job(jobs.claim(pg, "w", ("connect",)), pg, settings_for_tests())
    assert pg.execute("select orders_page_url from connections").fetchone() == ("https://www.zepto.com/orders",)


def test_worker_passes_recipes_from_postgres(pg, monkeypatch):
    store.put_recipe(pg, "zepto", {"orders_page_url": "https://www.zepto.com/orders"})
    seen = {}

    def fetch(site, since, **k):
        seen["r"] = k["recipe_loader"]("zepto")
        seen["none"] = k["recipe_loader"]("zomato")
        return []
    monkeypatch.setattr(worker.sync, "fetch_orders", fetch)
    worker.run_job(_sync_job(pg), pg, settings_for_tests())
    assert seen["r"] == {"orders_page_url": "https://www.zepto.com/orders"} and seen["none"] is None


def test_runs_dir_is_temporary_and_removed(pg, monkeypatch):
    seen = {}

    def login(site, phone=None, **k):
        seen["runs"] = Path(k["runs_dir"])
        assert seen["runs"].is_dir()
        return True
    monkeypatch.setattr(worker.sync, "login", login)
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: [])
    jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    worker.run_job(jobs.claim(pg, "w", ("connect",)), pg, settings_for_tests())
    assert not seen["runs"].exists()


def test_sigterm_during_connect_fails_job_with_worker_shutdown(pg, monkeypatch):
    jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})

    def login(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(worker.sync, "login", login)
    monkeypatch.setattr(worker, "settings", settings_for_tests)
    worker.main("w1", poll_s=0.01, max_loops=3)
    assert pg.execute("select status, error, payload ? 'phone' from jobs").fetchone() == ("failed", "worker_shutdown", False)
    assert pg.execute("select status from connections").fetchone() == ("error",)


def test_sigterm_during_sync_leaves_job_running(pg, monkeypatch):
    jobs.enqueue(pg, A, "sync", "zepto")

    def fetch(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(worker.sync, "fetch_orders", fetch)
    monkeypatch.setattr(worker, "settings", settings_for_tests)
    worker.main("w1", poll_s=0.01, max_loops=3)
    assert pg.execute("select status from jobs").fetchone() == ("running",)


def test_settings_require_master_key_for_remote_store(monkeypatch):
    from server.config import settings
    monkeypatch.setenv("PROFILE_STORE_URL", "DefaultEndpointsProtocol=https;AccountName=x")
    monkeypatch.delenv("PROFILE_MASTER_KEY", raising=False)
    with pytest.raises(ValueError, match="PROFILE_MASTER_KEY"):
        settings(for_worker=True)
    assert settings().profile_master_key is None  # the API never needs it


def test_settings_file_store_needs_key_or_dev_flag(monkeypatch):
    from server.config import settings
    monkeypatch.setenv("PROFILE_STORE_URL", "file:///tmp/v")
    monkeypatch.delenv("PROFILE_MASTER_KEY", raising=False)
    monkeypatch.delenv("ALLOW_DEV_PROFILE_KEY", raising=False)
    with pytest.raises(ValueError, match="ALLOW_DEV_PROFILE_KEY"):
        settings(for_worker=True)
    monkeypatch.setenv("ALLOW_DEV_PROFILE_KEY", "1")
    assert len(settings(for_worker=True).profile_master_key) == 32



def _start(monkeypatch):
    monkeypatch.setattr(worker, "settings", settings_for_tests)
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    worker.main("w1", poll_s=0.01, max_loops=1)


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_profile_root_uses_default(monkeypatch, value):
    monkeypatch.setenv("PROFILE_ROOT", value)
    assert worker._profiles_root() == Path("profiles_users")


def test_startup_wipe_only_removes_job_dirs(pg, monkeypatch):
    root = Path(os.environ["PROFILE_ROOT"])
    job_dir = root / "6f1c3d52-8a0e-4c57-9b1a-0d2f4e6a8b10"
    job_dir.mkdir(parents=True)
    (job_dir / "x").write_text("1")
    keep_dir, keep_file = root / "keep-me", root / "notes.txt"
    keep_dir.mkdir()
    keep_file.write_text("1")
    _start(monkeypatch)
    assert not job_dir.exists() and keep_dir.is_dir() and keep_file.is_file() and root.is_dir()


def test_startup_refuses_cwd_parent_and_vault(pg, monkeypatch, tmp_path):
    guarded = tmp_path / "keep"
    guarded.mkdir()
    (guarded / "6f1c3d52-8a0e-4c57-9b1a-0d2f4e6a8b10").mkdir()
    monkeypatch.chdir(guarded)
    for bad in (guarded, tmp_path, guarded.anchor, Path.home()):
        monkeypatch.setenv("PROFILE_ROOT", str(bad))
        with pytest.raises(ValueError, match="PROFILE_ROOT"):
            _start(monkeypatch)
    assert (guarded / "6f1c3d52-8a0e-4c57-9b1a-0d2f4e6a8b10").is_dir()
    vault_dir = Path(os.environ["PROFILE_STORE_URL"].removeprefix("file:///"))
    vault_dir.mkdir(parents=True, exist_ok=True)
    (vault_dir / "6f1c3d52-8a0e-4c57-9b1a-0d2f4e6a8b10").mkdir()
    monkeypatch.setenv("PROFILE_ROOT", str(vault_dir))
    with pytest.raises(ValueError, match="PROFILE_ROOT"):
        _start(monkeypatch)
    assert (vault_dir / "6f1c3d52-8a0e-4c57-9b1a-0d2f4e6a8b10").is_dir()


# ---------------------------------------------------------------- proxy credentials never leak (final review)

LEAKY_PASSWORD = "s3cr/et#pw"  # unencoded "/" and "#" make urlsplit fail with a message quoting the password
LEAKY_TEMPLATE = "http://user-{session}:" + LEAKY_PASSWORD + "@gw.example.com:7000"


def _leaks(text: str) -> bool:
    return any(part in text for part in (LEAKY_PASSWORD, "s3cr", "et#pw"))


def test_worker_start_refuses_bad_proxy_template_without_echoing_it(monkeypatch, caplog):
    import logging
    monkeypatch.setenv("PROXY_URL_TEMPLATE", LEAKY_TEMPLATE)
    monkeypatch.setenv("ALLOW_DEV_PROFILE_KEY", "1")
    monkeypatch.delenv("PROFILE_MASTER_KEY", raising=False)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ValueError) as e:
        worker.main("w1", poll_s=0.01, max_loops=1)
    from server.config import PROXY_TEMPLATE_INVALID
    assert str(e.value) == PROXY_TEMPLATE_INVALID
    assert e.value.__cause__ is None and e.value.__suppress_context__
    assert not _leaks(str(e.value)) and not _leaks(caplog.text)


def test_relay_construction_error_is_proxy_unavailable_without_credentials(pg, monkeypatch, caplog):
    import json
    import logging
    caplog.set_level(logging.DEBUG)
    s = dataclasses.replace(settings_for_tests(), proxy_url_template=LEAKY_TEMPLATE)
    store.set_connection(pg, A, "zepto", status="connected")
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: pytest.fail("must not sync"))
    worker.run_job(_sync_job(pg), pg, s)
    error, progress = pg.execute("select error, progress from jobs").fetchone()
    assert error == "proxy_unavailable"
    assert not _leaks(error) and not _leaks(json.dumps(progress)) and not _leaks(caplog.text)
    assert pg.execute("select status from connections").fetchone() == ("connected",)


def test_relay_check_crash_is_proxy_unavailable(pg, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.DEBUG)

    class CrashingRelay:
        def __init__(self, upstream):
            self.url = "http://127.0.0.1:5555"

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def check(self, *a, **k):
            raise OSError(f"boom talking to {LEAKY_TEMPLATE}")
    monkeypatch.setattr(worker, "Relay", CrashingRelay)
    s = dataclasses.replace(settings_for_tests(), proxy_url_template="http://u-{session}:pw@gw.example.com:7000")
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: pytest.fail("must not sync"))
    worker.run_job(_sync_job(pg), pg, s)
    assert pg.execute("select error from jobs").fetchone() == ("proxy_unavailable",)
    assert not _leaks(caplog.text)


# ---------------------------------------------------------------- hung jobs: MAX_JOB_S (final review)

def test_job_past_max_job_s_stops_heartbeat_and_exits(pg, pg2, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    exited = threading.Event()
    codes = []
    s = dataclasses.replace(settings_for_tests(), heartbeat_s=1, max_job_s=2)
    job = _sync_job(pg)
    pg.execute("update jobs set heartbeat_at = now() - interval '1 hour'")
    seen = {}

    def hung(site, since, **k):  # a job that never finishes on its own
        assert exited.wait(10), "the overrun exit was never called"
        seen["hb1"] = pg2.execute("select heartbeat_at from jobs").fetchone()[0]
        time.sleep(1.5)  # longer than heartbeat_s: a live heartbeat would have touched the row again
        seen["hb2"] = pg2.execute("select heartbeat_at from jobs").fetchone()[0]
        return []
    monkeypatch.setattr(worker.sync, "fetch_orders", hung)
    t0 = time.monotonic()
    worker.run_job(job, pg, s, exit_process=lambda code: (codes.append(code), exited.set()))
    assert codes and codes[0] != 0
    assert 2 <= time.monotonic() - t0 < 10
    assert seen["hb1"] == seen["hb2"]
    assert pg.execute("select %s > now() - interval '1 minute'", (seen["hb1"],)).fetchone() == (True,)
    assert "MAX_JOB_S" in caplog.text


def test_job_within_max_job_s_does_not_exit(pg, monkeypatch):
    codes = []
    s = dataclasses.replace(settings_for_tests(), heartbeat_s=1, max_job_s=60)
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: time.sleep(1.2) or [])
    worker.run_job(_sync_job(pg), pg, s, exit_process=codes.append)
    assert codes == []
    assert pg.execute("select status from jobs").fetchone() == ("done",)


def test_max_job_s_setting(monkeypatch):
    from server.config import settings
    monkeypatch.delenv("MAX_JOB_S", raising=False)
    assert settings().max_job_s == 1200
    monkeypatch.setenv("MAX_JOB_S", "90")
    assert settings().max_job_s == 90


def test_heartbeat_connection_has_connect_timeout(monkeypatch):
    seen = []

    def connect(url, **k):
        seen.append(k)
        raise OSError("db down")
    monkeypatch.setattr(worker.psycopg, "connect", connect)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    worker._heartbeat_loop("postgresql://x", "j", "w", 0.05, stop)
    assert seen and all(k.get("connect_timeout") == 10 for k in seen)


def test_in_page_fetch_has_a_timeout():
    import browser
    assert "signal: AbortSignal.timeout(60000)" in browser._FETCH_JS

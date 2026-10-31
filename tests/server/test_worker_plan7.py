"""Worker, Plan 7: write fencing by job ownership, site backoff, one session-expiry path, safe drain."""
import _thread
import dataclasses
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers import A, add_user, order, settings_for_tests
from test_worker_plan6 import _job, _run, _setup

import browser
import cart_fill
import cart_learn
import metering
import sync
from server import erase, fill, jobs, notify, site_health, store, vault, worker
from server.config import DEV_PROFILE_KEY

KEY = vault.master_key_from_env(DEV_PROFILE_KEY)


@pytest.fixture(autouse=True)
def no_real_senders(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "mail_send", lambda to, subject, text, s: sent.append((to, subject)))
    monkeypatch.setattr(notify, "push_send", lambda sub, payload, s: sent.append(payload))
    return sent


@pytest.fixture
def fake_mode(monkeypatch):
    monkeypatch.setenv("WORKER_FAKE", "1")
    monkeypatch.setattr(sync, "login", lambda *a, **k: pytest.fail("real login must not run"))
    monkeypatch.setattr(sync, "fetch_orders", lambda *a, **k: pytest.fail("real sync must not run"))


def _vault_store():
    return vault.store_from_url(os.environ["PROFILE_STORE_URL"])


def _claim(pg, kind="sync", site="zepto", payload=None, user=A):
    jid = jobs.enqueue(pg, user, kind, site, payload)
    return jid, jobs.claim(pg, "w", (kind,))


def _run_sync(pg, site="zepto", s=None):
    jid, job = _claim(pg, "sync", site)
    worker.run_job(job, pg, s or settings_for_tests())
    return jid


# ---------------------------------------------------------------- write fencing

def test_sync_finishing_after_account_deletion_writes_nothing(pg, pg2, monkeypatch):
    def fetch(site, since, **k):
        erase.delete_account(pg2, _vault_store(), A)  # the account goes while Chrome is still working
        return [order("1")]
    monkeypatch.setattr(worker.sync, "fetch_orders", fetch)
    jid, job = _claim(pg)
    worker.run_job(job, pg, settings_for_tests())
    for table in ("orders", "connections", "usage", "jobs"):
        assert pg.execute(f"select count(*) from {table} where " + ("id" if table == "jobs" else "user_id") +
                          "=%s", (jid if table == "jobs" else A,)).fetchone() == (0,), table
    assert _vault_store().list_prefix(f"{A}/") == []


def test_requeued_job_does_not_save_profile(pg, monkeypatch):
    jid, job = _claim(pg)

    def do_sync(j, conn, *a, **k):  # the job is taken away from us after the sync, before the profile is saved
        assert jobs.requeue(conn, jid, "w")
        return 0, 0.0
    monkeypatch.setattr(worker, "_do_sync", do_sync)
    monkeypatch.setattr(vault, "save_profile", lambda *a, **k: pytest.fail("a job not ours must not save"))
    worker.run_job(job, pg, settings_for_tests())
    assert pg.execute("select status from jobs where id=%s", (jid,)).fetchone() == ("queued",)
    assert store.get_connection(pg, A, "zepto")["profile_blob_key"] is None


def test_save_profile_stamps_and_checks_owner(pg, monkeypatch, tmp_path):
    def fetch(site, since, **k):
        (Path(k["profile_dir"]) / "Cookies").write_text("session")
        return [order("1")]
    monkeypatch.setattr(worker.sync, "fetch_orders", fetch)
    _run_sync(pg)
    assert store.get_connection(pg, A, "zepto")["profile_blob_key"] == vault.blob_key(A, "zepto")
    dest = tmp_path / "restored"
    assert vault.restore_profile(_vault_store(), KEY, A, "zepto", dest)
    assert json.loads((dest / vault.CANARY_NAME).read_text())["user_id"] == A
    assert (dest / "Cookies").read_text() == "session"
    vault.check_owner(dest, A, "zepto")
    with pytest.raises(vault.ProfileOwnerMismatch):
        vault.check_owner(dest, "00000000-0000-0000-0000-00000000000b", "zepto")


# ---------------------------------------------------------------- per-user recipe (Zomato)

def _templated(pg):
    store.put_recipe(pg, "zomato", {"orders_page_url": "{orders_page_url}", "root": "{account}/orders"})


def test_zomato_sync_binds_users_orders_page(pg, monkeypatch):
    _templated(pg)
    store.set_connection(pg, A, "zomato", status="connected",
                         orders_page_url="https://www.zomato.com/users/me-1/ordering")
    seen = []

    def fetch(site, since, **k):
        seen.append(k["recipe_loader"](site))
        return []
    monkeypatch.setattr(worker.sync, "fetch_orders", fetch)
    jid = _run_sync(pg, "zomato")
    assert seen == [{"orders_page_url": "https://www.zomato.com/users/me-1/ordering",
                     "root": "/users/me-1/orders"}]
    assert "{orders_page_url}" not in json.dumps(seen) and "{account}" not in json.dumps(seen)
    assert pg.execute("select status from jobs where id=%s", (jid,)).fetchone() == ("done",)


def test_zomato_without_orders_page_is_needs_login(pg, monkeypatch):
    # needs_login, not no_recipe: the recipe is fine, the user's own orders page is unknown and a reconnect finds it
    _templated(pg)
    store.set_connection(pg, A, "zomato", status="connected")
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda site, since, **k: k["recipe_loader"](site))
    jid = _run_sync(pg, "zomato")
    assert pg.execute("select status, error from jobs where id=%s", (jid,)).fetchone() == ("failed", "needs_login")
    assert store.get_connection(pg, A, "zomato")["status"] == "needs_login"


# ---------------------------------------------------------------- one session-expiry path

def test_session_expired_notifies_once_across_two_jobs(pg, monkeypatch):
    store.set_connection(pg, A, "zepto", status="connected")

    def expired(*a, **k):
        raise browser.SessionExpired("HTTP 401")
    monkeypatch.setattr(worker.sync, "fetch_orders", expired)
    _run_sync(pg)
    _run_sync(pg)
    assert pg.execute("select count(*) from notifications where kind='needs_login'").fetchone() == (1,)
    assert pg.execute("select count(*) from jobs where status='failed' and error='needs_login'").fetchone() == (2,)
    assert pg.execute("select needs_login_at is not null from connections").fetchone() == (True,)


def test_connect_success_clears_needs_login_at(pg, monkeypatch):
    store.set_connection(pg, A, "zepto", status="needs_login")
    pg.execute("update connections set needs_login_at = now()")
    monkeypatch.setattr(worker.sync, "login", lambda *a, **k: True)
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: [order("1")])
    jid, job = _claim(pg, "connect", payload={"phone": "9876543210"})
    worker.run_job(job, pg, settings_for_tests())
    assert pg.execute("select status, needs_login_at from connections").fetchone() == ("connected", None)


# ---------------------------------------------------------------- site backoff (R5)

def _throttle(*a, **k):
    raise browser.SiteThrottled("rate_limited", 120)


def test_throttled_sync_requeues_with_run_after_and_cools_site(pg, monkeypatch):
    monkeypatch.setattr(worker.sync, "fetch_orders", _throttle)
    jid = _run_sync(pg)
    status, run_after, progress = pg.execute("select status, run_after, progress from jobs where id=%s",
                                             (jid,)).fetchone()
    assert status == "queued" and progress["stage"] == "cooling_down" and progress["until"]
    assert run_after > datetime.now(timezone.utc) + timedelta(seconds=100)
    assert site_health.cooldown_until(pg, "zepto") is not None


def test_throttled_connect_fails_site_busy(pg, monkeypatch):
    monkeypatch.setattr(worker.sync, "login", _throttle)
    jid, job = _claim(pg, "connect", payload={"phone": "9876543210"})
    worker.run_job(job, pg, settings_for_tests())
    assert pg.execute("select status, error from jobs where id=%s", (jid,)).fetchone() == ("failed", "site_busy")
    assert pg.execute("select status from connections").fetchone() == ("error",)
    assert site_health.cooldown_until(pg, "zepto") is not None


def test_cooling_search_is_unavailable_without_browser(pg, monkeypatch):
    monkeypatch.setattr(browser, "open_site", lambda *a, **k: pytest.fail("no browser while cooling"))
    monkeypatch.setattr(worker, "_do_search", lambda *a, **k: pytest.fail("no search while cooling"))
    site_health.record_throttle(pg, "zepto", "rate_limited", 120, settings_for_tests())
    jid, job = _claim(pg, "search", payload={"op": "products"})
    worker.run_job(job, pg, settings_for_tests())
    assert pg.execute("select status, error from jobs where id=%s", (jid,)).fetchone() == ("failed", "cooling_down")


def test_throttled_search_is_failed_blocked(pg, monkeypatch):
    seen = []
    monkeypatch.setattr(worker, "_do_search", _throttle)
    monkeypatch.setattr(worker.cart, "record_site_result",
                        lambda conn, rid, site, result, items=(), meta=None, error=None: seen.append((result, error)))
    store.set_connection(pg, A, "zepto", status="connected")
    from test_worker_plan6 import _seed_profile
    _seed_profile(A, "zepto")
    jid, job = _claim(pg, "search", payload={"op": "products", "request_id": "11111111-1111-1111-1111-111111111111"})
    worker.run_job(job, pg, settings_for_tests())
    assert seen == [("failed", "blocked")]


def test_success_resets_backoff(pg, monkeypatch):
    jid, job = _claim(pg)  # claimed before the site cools: a cooling site holds new jobs back
    site_health.record_throttle(pg, "zepto", "rate_limited", 0, settings_for_tests())
    assert pg.execute("select level from site_backoff where site='zepto'").fetchone() == (1,)
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: [order("1")])
    worker.run_job(job, pg, settings_for_tests())
    assert pg.execute("select level, until from site_backoff where site='zepto'").fetchone() == (0, None)


# ---------------------------------------------------------------- drain (R10)

def _drain(drain_s=0.05):
    s = dataclasses.replace(settings_for_tests(), drain_s=drain_s)
    return worker._Drain(s, "w", lambda code: None), s


def test_drain_does_not_requeue_sync_before_unwind(pg, monkeypatch):
    interrupts = []
    monkeypatch.setattr(worker._thread, "interrupt_main", lambda: interrupts.append(1))
    jid, job = _claim(pg)
    drain, _ = _drain()
    drain.start_job(job)
    unwound = []
    monkeypatch.setattr(drain.idle, "wait", lambda t=None: unwound.append(pg.execute(
        "select status, worker_id from jobs where id=%s", (jid,)).fetchone()) or True)
    drain._watch()
    assert interrupts == [1] and drain.aborted
    assert unwound == [("running", "w")]  # still ours: the main thread requeues it after run_job unwinds


def test_drain_interrupt_requeues_sync_after_unwind_in_main(pg, monkeypatch):
    jid, job = _claim(pg)
    assert worker._abort_on_shutdown(job, pg, "w")
    assert pg.execute("select status from jobs where id=%s", (jid,)).fetchone() == ("queued",)


def test_watchdog_never_interrupts_after_main_returned(pg, monkeypatch):
    interrupts = []
    monkeypatch.setattr(worker._thread, "interrupt_main", lambda: interrupts.append(1))
    monkeypatch.setattr(worker.psycopg, "connect", lambda *a, **k: None)
    drain, _ = _drain()
    job = jobs.Job(id="j1", user_id=A, type="connect", site="zepto", payload={}, worker_id="w", attempts=1) \
        if "attempts" in jobs.Job.__dataclass_fields__ else None
    if job is None:
        job = jobs.Job(id="j1", user_id=A, type="connect", site="zepto", payload={}, worker_id="w")
    drain.start_job(job)

    def abort(*a, **k):  # the main loop ends while the watchdog is aborting the job
        drain.end_job()
        drain.finish()
        return True
    monkeypatch.setattr(worker, "_abort_on_shutdown", abort)
    drain._watch()
    assert interrupts == []


def _interrupt_while_finishing(drain):
    """Deliver the watchdog's interrupt while the main thread is inside drain.finish() (waiting for the lock)."""
    import threading
    import time

    drain._lock.acquire()

    def watchdog():
        time.sleep(0.2)
        _thread.interrupt_main()
        time.sleep(0.1)
        drain._lock.release()
    t = threading.Thread(target=watchdog)
    t.start()
    try:
        drain.finish()
    finally:
        t.join()


def test_pending_interrupt_absorbed_by_finish(pg):
    drain, _ = _drain()
    drain.aborted = True
    _interrupt_while_finishing(drain)  # the watchdog's interrupt lands as the loop ends: swallowed
    assert drain.stopped.is_set()


def test_unaborted_interrupt_is_not_swallowed_by_finish(pg):
    drain, _ = _drain()
    with pytest.raises(KeyboardInterrupt):
        _interrupt_while_finishing(drain)
    assert drain.stopped.is_set()


def test_worker_refuses_hosted_db_without_sslmode(pg, monkeypatch):
    hosted = dataclasses.replace(settings_for_tests(), database_url="postgresql://u:p@db.example.com:5432/postgres")
    monkeypatch.setattr(worker, "settings", lambda **k: hosted)
    monkeypatch.setattr(worker, "_wipe_profiles_root", lambda url: pytest.fail("must refuse before starting"))
    with pytest.raises(ValueError, match="sslmode"):
        worker.main("w1", poll_s=0.01, max_loops=1)


def test_stale_requeue_uses_max_job_attempts(pg, monkeypatch):
    seen = []
    monkeypatch.setattr(jobs, "requeue_stale", lambda conn, older, max_attempts, on_lost=None: seen.append(
        (older, max_attempts)) or 0)
    s = dataclasses.replace(settings_for_tests(), stale_job_s=77, max_job_attempts=5)
    worker._requeue_stale(pg, s)
    assert seen == [(77, 5)]


# ---------------------------------------------------------------- fake mode

def test_fake_expired_and_throttle_urls(pg, fake_mode):
    store.set_connection(pg, A, "zepto", status="connected",
                         orders_page_url="https://fake.zepto.test/orders/expired")
    jid = _run_sync(pg)
    assert pg.execute("select status, error from jobs where id=%s", (jid,)).fetchone() == ("failed", "needs_login")
    assert store.get_connection(pg, A, "zepto")["status"] == "needs_login"
    store.set_connection(pg, A, "blinkit", status="connected",
                         orders_page_url="https://fake.blinkit.test/orders/throttle")
    jid = _run_sync(pg, "blinkit")
    assert pg.execute("select status, progress->>'stage' from jobs where id=%s", (jid,)).fetchone() == \
        ("queued", "cooling_down")


# ---------------------------------------------------------------- carry-overs

def test_late_usage_for_a_deleted_user_is_not_recorded(pg, pg2):
    jid, job = _claim(pg)
    with metering.meter() as meter:
        worker._record_usage(pg, job, settings_for_tests(), meter, None, 3.0)
    assert pg.execute("select count(*), sum(worker_seconds) from usage where user_id=%s", (A,)).fetchone() == (1, 3.0)
    erase.delete_account(pg2, _vault_store(), A)
    with metering.meter() as meter:
        worker._record_usage(pg, job, settings_for_tests(), meter, None, 3.0)
    assert pg.execute("select count(*) from usage where user_id=%s", (A,)).fetchone() == (0,)


def test_unexpected_error_mid_fill_closes_the_fill_interrupted(pg, monkeypatch):
    rid, fid, jid, s = _setup(pg)

    def engine(site, plan, priced, recipe, search_recipe, **k):
        raise RuntimeError("boom")  # after mark_started: adds may already be in the app's cart
    monkeypatch.setattr(cart_fill, "fill_cart", engine)
    _run(pg, s)
    f = fill.get_fill(pg, fid)
    assert f["status"] == "interrupted" and f["counted"] is True
    assert {i["status"] for i in f["items"]} == {"pending"}  # never marked not_added
    assert _job(pg, jid)[:2] == ("failed", "RuntimeError: boom")


def test_cart_mode_is_only_for_fill_and_cart_learn(pg, monkeypatch):
    """Chrome opens with a cart policy for a cart learn only; connect, sync, search and orders learn do not."""
    from psycopg.types.json import Jsonb
    from test_worker_plan6 import RECIPE, SEARCH, _admin_cart, _seed_profile  # noqa: F401

    modes = {}

    class Stop(Exception):
        pass

    def open_site(site, **k):
        modes.setdefault(current[0], []).append(k.get("cart_policy") is not None)
        raise Stop

    current = [None]
    monkeypatch.setattr(browser, "open_site", open_site)
    monkeypatch.setattr(sync, "open_site", open_site)
    _admin_cart(pg)  # an admin (user B) connected on zepto with a search recipe, an address and a stored profile
    from helpers import B
    store.put_recipe(pg, "zepto", {"kind": "orders", "orders_url": "https://www.zepto.com/orders"})

    def run(kind, payload=None, user=B):
        current[0] = kind
        _jid, job = _claim(pg, kind.split(":")[0], payload=payload, user=user)
        worker.run_job(job, pg, settings_for_tests())

    run("learn:cart", {"reason": "manual", "kind": "cart"})
    run("learn:orders", {"reason": "manual"})
    run("sync")
    run("connect", {"phone": "9876543210"})
    run("search", {"op": "products", "request_id": None})
    assert modes["learn:cart"] == [True]
    for kind in ("learn:orders", "sync", "connect", "search"):
        assert modes.get(kind, [False]) and not any(modes.get(kind, [])), kind

"""Worker, Plan 4: usage metering, notifications, recipe drift, supervised learn jobs; vault v1 wiring."""
import dataclasses
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import A, B, add_user, order, settings_for_tests

import llm
import metering
import sync
from browser import SessionExpired
from server import jobs, notify, recipe_admin, store, vault, worker
from server.config import DEV_PROFILE_KEY

KEY = vault.master_key_from_env(DEV_PROFILE_KEY)
POST = {"url_regex": "^https://www\\.zepto\\.com/api/orders$", "why": "order list"}
NEW_POST = {"url_regex": "^https://www\\.zepto\\.com/api/v2/orders$", "why": "order list v2"}
BASE = {"orders_page_url": "https://www.zepto.com/orders", "endpoint": "https://www.zepto.com/api/orders",
        "method": "POST", "read_post_allowlist": [POST]}


@pytest.fixture(autouse=True)
def no_real_senders(monkeypatch):
    """No real email or push from any test here: dispatch uses these recorders."""
    sent = []
    monkeypatch.setattr(notify, "mail_send", lambda to, subject, text, s: sent.append((to, subject)))
    monkeypatch.setattr(notify, "push_send", lambda sub, payload, s: sent.append(payload))
    return sent


def _store():
    return vault.store_from_url(os.environ["PROFILE_STORE_URL"])


def _seed_profile(user_id, profile="zepto", owner=None):
    """A saved profile in the vault for user_id (its canary stamped for owner, default user_id)."""
    d = Path(os.environ["PROFILE_ROOT"]).parent / f"seed-{user_id}-{profile}"
    vault.stamp(d, owner or user_id, profile)
    (d / "Cookies").write_text("seeded")
    _store().put(vault.blob_key(user_id, profile), vault._seal(KEY, user_id, profile, vault._pack(d)))


def _admin(pg, user_id=B, email="admin@example.com", connected_site="zepto"):
    add_user(pg, user_id, email)
    pg.execute("insert into admins(email) values (%s)", (email,))
    if connected_site:
        store.set_connection(pg, user_id, connected_site, status="connected")


def _run(pg, user_id, type, site="zepto", payload=None, s=None):
    jid = jobs.enqueue(pg, user_id, type, site, payload)
    job = jobs.claim(pg, "w", (type,))
    assert job.id == jid
    worker.run_job(job, pg, s or settings_for_tests())
    return jid


def _job(pg, jid):
    return pg.execute("select status, error, progress from jobs where id=%s", (jid,)).fetchone()


def _usage(pg, user_id=A):
    return pg.execute("select proxy_mb, ai_tokens, worker_seconds, syncs from usage where user_id=%s",
                      (user_id,)).fetchone()


def _notes(pg):
    return pg.execute("select kind, title, body, url, user_id::text, email, dedupe_key from notifications "
                      "order by created_at").fetchall()


# ---------------------------------------------------------------- metering

def test_sync_job_records_usage(pg, monkeypatch):
    def fetch(site, since, **k):
        llm._record("t", SimpleNamespace(usage=SimpleNamespace(input_tokens=1000, output_tokens=234),
                                         model="claude-haiku-5-5", stop_reason="end_turn"))
        metering.count_jev()
        time.sleep(0.01)
        return [order("1")]
    monkeypatch.setattr(worker.sync, "fetch_orders", fetch)
    jid = _run(pg, A, "sync")
    assert _job(pg, jid)[:2] == ("done", None)
    proxy_mb, tokens, seconds, syncs = _usage(pg)
    assert (float(proxy_mb), tokens, syncs) == (0.0, 2234, 1)
    assert float(seconds) > 0


def test_sync_passes_block_media(pg, monkeypatch):
    seen = []
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda site, since, **k: seen.append(k["block_media"]) or [])
    _run(pg, A, "sync")
    _run(pg, A, "sync", s=dataclasses.replace(settings_for_tests(), sync_block_media=False))
    monkeypatch.setattr(worker.sync, "login", lambda *a, **k: True)
    _run(pg, A, "connect", payload={"phone": "9876543210"})  # a connect's first sync never blocks media
    assert seen == [True, False, False]


def test_metering_failure_does_not_fail_job(pg, monkeypatch, caplog):
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda site, since, **k: [order("1")])

    def broken(*a, **k):
        raise RuntimeError("usage table on fire 9876543210")
    monkeypatch.setattr(worker.usage, "record", broken)
    jid = _run(pg, A, "sync")
    assert _job(pg, jid)[:2] == ("done", None)
    assert "RuntimeError" in caplog.text and "9876543210" not in caplog.text


# ---------------------------------------------------------------- notifications

def test_connect_success_enqueues_first_sync_notification(pg, monkeypatch, no_real_senders):
    add_user(pg, A, "user@example.com")
    monkeypatch.setattr(worker.sync, "login", lambda *a, **k: True)
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda site, since, **k: [order("1"), order("2")])
    jid = _run(pg, A, "connect", payload={"phone": "9876543210"})
    assert _job(pg, jid)[:2] == ("done", None)
    assert _notes(pg) == [("first_sync_done", "Zepto is connected", "2 orders · ₹200 found", "/spend", A, None,
                           f"first_sync:{jid}")]
    assert pg.execute("select status, channel from notifications").fetchone() == ("sent", "email")
    assert no_real_senders == [("user@example.com", "Zepto is connected")]


def test_needs_login_notifies_once(pg, monkeypatch):
    def expired(*a, **k):
        raise SessionExpired("HTTP 401")
    monkeypatch.setattr(worker.sync, "fetch_orders", expired)
    store.set_connection(pg, A, "zepto", status="connected")
    _run(pg, A, "sync")
    _run(pg, A, "sync")
    assert pg.execute("select status from connections").fetchone() == ("needs_login",)
    notes = _notes(pg)  # one per episode, from session.mark_needs_login
    assert len(notes) == 1 and notes[0][:6] == (
        "needs_login", "Zepto logged you out", "Reconnect to keep your spending up to date.",
        "/accounts?reconnect=zepto", A, None) and notes[0][6].startswith(f"needs_login:{A}:zepto:")


# ---------------------------------------------------------------- drift

def _drift(*a, **k):
    raise sync.RecipeDrift("recipe drift for zepto: fast path and replay both failed; an admin must re-learn it")


def test_drift_marks_unhealthy_and_enqueues_admin_learn(pg, monkeypatch):
    store.put_recipe(pg, "zepto", BASE)
    _admin(pg)
    store.set_connection(pg, A, "zepto", status="connected")
    monkeypatch.setattr(worker.sync, "fetch_orders", _drift)
    jid = _run(pg, A, "sync")
    assert _job(pg, jid)[:2] == ("failed", "recipe_drift")
    assert pg.execute("select healthy, paused from recipes").fetchone() == (False, False)
    assert pg.execute("select user_id::text, site, status, payload from jobs where type='learn'").fetchall() == \
        [(B, "zepto", "queued", {"reason": "drift", "auto": True})]
    _run(pg, A, "sync")  # already unhealthy: no second learn, no error
    assert pg.execute("select count(*) from jobs where type='learn'").fetchone() == (1,)
    assert pg.execute("select status from connections where user_id=%s", (A,)).fetchone() == ("connected",)


def test_drift_without_admin_connection_pauses_and_alerts(pg, monkeypatch):
    store.put_recipe(pg, "zepto", BASE)
    _admin(pg, connected_site=None)
    monkeypatch.setattr(worker.sync, "fetch_orders", _drift)
    jid = _run(pg, A, "sync")
    assert _job(pg, jid)[:2] == ("failed", "recipe_drift")
    assert pg.execute("select healthy, paused from recipes").fetchone() == (False, True)
    assert pg.execute("select count(*) from jobs where type='learn'").fetchone() == (0,)
    from server import usage
    today = usage.today(tz=settings_for_tests().app_tz)
    assert _notes(pg) == [("site_paused", "Zepto sync paused",
                           "Its order recipe stopped working and no admin account is connected to re-learn it.",
                           "/", None, "admin@example.com", f"site_paused:zepto:{today}:admin@example.com")]


# ---------------------------------------------------------------- learn jobs

def _learner(calls, recipe=None, new_entries=()):
    """A sync.learn stand-in: asks approve() for new_entries (like the agent), then returns recipe."""
    def learn(site, approve=None, preapproved=(), save=True, **k):
        cookies = Path(k["profile_dir"]) / "Cookies"
        calls.append({"site": site, "preapproved": list(preapproved), "save": save, "approve": approve,
                      "cookies": cookies.read_text() if cookies.exists() else None, **k})
        pending = [e for e in new_entries if e["url_regex"] not in set(preapproved)]
        if pending and not approve(pending):
            raise sync.RecipeError("POST allowlist rejected")
        return recipe or dict(BASE)
    return learn


def test_learn_job_requires_admin(pg, monkeypatch):
    add_user(pg, A, "user@example.com")
    _seed_profile(A)
    calls = []
    monkeypatch.setattr(worker.sync, "learn", _learner(calls))
    jid = _run(pg, A, "learn")
    assert _job(pg, jid)[:2] == ("failed", "not_admin")
    assert calls == []


def test_learn_job_missing_profile_is_needs_login(pg, monkeypatch):
    _admin(pg, connected_site=None)
    calls = []
    monkeypatch.setattr(worker.sync, "learn", _learner(calls))
    jid = _run(pg, B, "learn")
    assert _job(pg, jid)[:2] == ("failed", "needs_login")
    assert calls == []
    # ruling: the learn job fails, but no connection row is written and nobody is told "logged out"
    assert pg.execute("select count(*) from connections").fetchone() == (0,)
    assert pg.execute("select count(*) from notifications").fetchone() == (0,)


def test_learn_job_missing_profile_keeps_admin_connection(pg, monkeypatch):
    _admin(pg)
    monkeypatch.setattr(worker.sync, "learn", _learner([]))
    _run(pg, B, "learn")
    assert pg.execute("select status from connections").fetchone() == ("connected",)
    assert pg.execute("select count(*) from notifications").fetchone() == (0,)


def test_learn_job_auto_applies_recipe_without_new_posts(pg, monkeypatch):
    store.put_recipe(pg, "zepto", BASE)
    recipe_admin.mark_drift(pg, "zepto", "orders", "drift")
    _admin(pg)
    _seed_profile(B)
    calls = []
    learned = {**BASE, "fast_path_ok": True}
    monkeypatch.setattr(worker.sync, "learn", _learner(calls, learned))
    jid = _run(pg, B, "learn", payload={"reason": "drift", "auto": True})
    status, error, progress = _job(pg, jid)
    assert (status, error) == ("done", None)
    assert calls[0]["preapproved"] == [POST["url_regex"]] and calls[0]["save"] is False
    assert calls[0]["approve"] is recipe_admin.deny_new_posts
    assert calls[0]["cookies"] == "seeded"  # the admin's own restored profile
    assert progress["stage"] == "learned" and progress["status"] == "auto_applied" and progress["version"] == 2
    assert pg.execute("select version, healthy, paused, recipe from recipes").fetchone() == (2, True, False, learned)
    assert pg.execute("select status, reason, id::text from recipe_approvals").fetchone() == \
        ("auto_applied", "drift_relearn", progress["approval_id"])
    assert _usage(pg, B)[3] == 0  # a learn is not a sync


def test_learn_job_waits_for_approval(pg, pg2, monkeypatch):
    store.put_recipe(pg, "zepto", BASE)
    _admin(pg)
    _seed_profile(B)
    calls = []
    learned = {**BASE, "read_post_allowlist": [POST, NEW_POST]}
    monkeypatch.setattr(worker.sync, "learn", _learner(calls, learned, [NEW_POST]))
    decided = []

    def admin_decides():
        for _ in range(100):
            row = pg2.execute("select progress->>'stage', progress->>'approval_id' from jobs where type='learn'"
                              ).fetchone()
            if row and row[0] == "approval_needed":
                decided.append(recipe_admin.decide(pg2, row[1], "approve", B))
                return
            time.sleep(0.1)
    t = threading.Thread(target=admin_decides)
    t.start()
    jid = _run(pg, B, "learn", s=dataclasses.replace(settings_for_tests(), approval_timeout_s=10))
    t.join(15)
    assert decided == ["approved"]
    status, error, progress = _job(pg, jid)
    assert (status, error) == ("done", None)
    assert progress["stage"] == "learned" and progress["status"] == "auto_applied"
    assert pg.execute("select version, recipe from recipes").fetchone() == (2, learned)
    assert pg.execute("select reason, status from recipe_approvals order by created_at").fetchall() == \
        [("post_allowlist", "approved"), ("learned", "auto_applied")]


def test_unanswered_approval_expires_and_keeps_recipe(pg, monkeypatch):
    store.put_recipe(pg, "zepto", BASE)
    _admin(pg)
    _seed_profile(B)
    monkeypatch.setattr(worker.sync, "learn", _learner([], {**BASE, "read_post_allowlist": [POST, NEW_POST]},
                                                        [NEW_POST]))
    jid = _run(pg, B, "learn", s=dataclasses.replace(settings_for_tests(), approval_timeout_s=1))
    status, error, _ = _job(pg, jid)
    assert status == "failed" and "POST allowlist rejected" in error
    assert pg.execute("select version, recipe from recipes").fetchone() == (1, BASE)
    assert pg.execute("select reason, status from recipe_approvals").fetchall() == [("post_allowlist", "expired")]
    assert pg.execute("select paused from recipes").fetchone() == (False,)  # a supervised learn never pauses


def test_auto_learn_failure_pauses_site(pg, monkeypatch):
    store.put_recipe(pg, "zepto", BASE)
    recipe_admin.mark_drift(pg, "zepto", "orders", "drift")
    _admin(pg)
    _seed_profile(B)

    def broken(*a, **k):
        raise sync.RecipeError("discovery found no order list")
    monkeypatch.setattr(worker.sync, "learn", broken)
    jid = _run(pg, B, "learn", payload={"reason": "drift", "auto": True})
    assert _job(pg, jid)[0] == "failed"
    assert pg.execute("select version, paused from recipes").fetchone() == (1, True)
    assert [(k, t, e) for k, t, _, _, _, e, _ in _notes(pg)] == \
        [("site_paused", "Zepto sync paused", "admin@example.com")]


def test_auto_learn_pending_result_pauses_site(pg, monkeypatch):
    store.put_recipe(pg, "zepto", BASE)
    _admin(pg)
    _seed_profile(B)
    flagged = {**BASE, "orders_page_url": "https://www.zepto.com/users/12345678/orders"}
    monkeypatch.setattr(worker.sync, "learn", _learner([], flagged))
    jid = _run(pg, B, "learn", payload={"reason": "drift", "auto": True})
    status, _, progress = _job(pg, jid)
    assert status == "done" and progress["status"] == "pending"
    assert pg.execute("select version, paused from recipes").fetchone() == (1, True)
    assert [n[0] for n in _notes(pg)] == ["site_paused"]


def test_learn_job_uses_learn_timeout_for_heartbeat(pg, monkeypatch):
    seen = []
    real = worker._heartbeat_loop

    def spy(database_url, job_id, worker_id, every_s, stop, max_job_s=None, on_overrun=None):
        seen.append(max_job_s)
        return real(database_url, job_id, worker_id, every_s, stop, max_job_s, on_overrun)
    monkeypatch.setattr(worker, "_heartbeat_loop", spy)
    _admin(pg)
    _seed_profile(B)
    monkeypatch.setattr(worker.sync, "learn", _learner([]))
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: [])
    s = dataclasses.replace(settings_for_tests(), max_job_s=111, max_learn_job_s=222)
    _run(pg, B, "learn", payload={"auto": True}, s=s)
    _run(pg, A, "sync", s=s)
    assert seen == [222, 111]


def test_learn_is_a_worker_job_type():
    assert worker.JOB_TYPES == ("connect", "sync", "learn", "search", "fill_cart")


def test_shutdown_fails_a_learn_job(pg):
    _admin(pg)
    jobs.enqueue(pg, B, "learn", "zepto")
    job = jobs.claim(pg, "w1", ("learn",))
    assert worker._abort_on_shutdown(job, pg, "w1") is True
    assert pg.execute("select status, error from jobs").fetchone() == ("failed", "worker_shutdown")
    assert pg.execute("select status from connections").fetchone() == ("connected",)


# ---------------------------------------------------------------- vault v1 wiring

def test_fresh_profile_is_stamped_before_save(pg, monkeypatch):
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda site, since, **k: [])
    jid = _run(pg, A, "sync")
    assert _job(pg, jid)[:2] == ("done", None)
    out = Path(os.environ["PROFILE_ROOT"]).parent / "restored"
    assert vault.restore_profile(_store(), KEY, A, "zepto", out) is True
    vault.check_owner(out, A, "zepto")


def test_vault_key_mismatch_is_a_config_error(pg, monkeypatch):
    other = vault.master_key_from_env("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo3ODkwMTI=")
    d = Path(os.environ["PROFILE_ROOT"]).parent / "seed"
    vault.stamp(d, A, "zepto")
    vault.save_profile(_store(), other, A, "zepto", d)
    before = _store().get(vault.blob_key(A, "zepto"))
    store.set_connection(pg, A, "zepto", status="connected")
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: pytest.fail("must not sync"))
    jid = _run(pg, A, "sync")
    assert _job(pg, jid)[:2] == ("failed", "vault_key_mismatch")
    assert pg.execute("select status from connections").fetchone() == ("connected",)
    assert _store().get(vault.blob_key(A, "zepto")) == before
    assert _notes(pg) == []


def test_restored_profile_of_another_owner_fails_without_upload(pg, monkeypatch):
    _seed_profile(A, owner=B)
    before = _store().get(vault.blob_key(A, "zepto"))
    store.set_connection(pg, A, "zepto", status="connected")
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda *a, **k: pytest.fail("must not sync"))
    jid = _run(pg, A, "sync")
    assert _job(pg, jid)[:2] == ("failed", "profile_owner_mismatch")
    assert _store().get(vault.blob_key(A, "zepto")) == before
    assert pg.execute("select status from connections").fetchone() == ("connected",)


def test_profile_restamped_mid_job_fails_without_upload(pg, monkeypatch):
    _seed_profile(A)
    before = _store().get(vault.blob_key(A, "zepto"))

    def fetch(site, since, **k):
        vault.stamp(k["profile_dir"], B, "zepto")
        return []
    monkeypatch.setattr(worker.sync, "fetch_orders", fetch)
    jid = _run(pg, A, "sync")
    assert _job(pg, jid)[:2] == ("failed", "profile_owner_mismatch")
    assert _store().get(vault.blob_key(A, "zepto")) == before


# ---------------------------------------------------------------- review fixes (round 1)

def _stale_all(pg):
    pg.execute("update jobs set started_at = now() - interval '20 minutes', "
               "heartbeat_at = now() - interval '20 minutes' where status='running'")


def _running_learn(pg, payload):
    store.put_recipe(pg, "zepto", BASE)
    _admin(pg)
    jobs.enqueue(pg, B, "learn", "zepto", payload)
    return jobs.claim(pg, "w1", ("learn",))


def test_lost_auto_learn_pauses_site_and_alerts(pg):
    job = _running_learn(pg, {"reason": "drift", "auto": True})
    _stale_all(pg)
    assert worker._requeue_stale(pg, dataclasses.replace(settings_for_tests(), stale_job_s=600)) == 1
    assert _job(pg, job.id)[:2] == ("failed", "worker_lost")
    assert pg.execute("select paused from recipes").fetchone() == (True,)
    assert [(n[0], n[5]) for n in _notes(pg)] == [("site_paused", "admin@example.com")]


def test_lost_supervised_learn_does_not_pause(pg):
    _running_learn(pg, {})
    _stale_all(pg)
    assert worker._requeue_stale(pg, dataclasses.replace(settings_for_tests(), stale_job_s=600)) == 1
    assert pg.execute("select paused from recipes").fetchone() == (False,)
    assert _notes(pg) == []


def test_main_loop_pauses_a_lost_auto_learn(pg, monkeypatch):
    job = _running_learn(pg, {"reason": "drift", "auto": True})
    _stale_all(pg)
    monkeypatch.setattr(worker, "settings", lambda **k: dataclasses.replace(settings_for_tests(), stale_job_s=600))
    monkeypatch.setattr(worker.time, "sleep", lambda _: None)
    worker.main("w2", poll_s=0.01, max_loops=1)
    assert _job(pg, job.id)[:2] == ("failed", "worker_lost")
    assert pg.execute("select paused from recipes").fetchone() == (True,)


def test_shutdown_of_auto_learn_pauses_site_and_alerts(pg):
    job = _running_learn(pg, {"reason": "drift", "auto": True})
    assert worker._abort_on_shutdown(job, pg, "w1", settings_for_tests()) is True
    assert _job(pg, job.id)[:2] == ("failed", "worker_shutdown")
    assert pg.execute("select paused from recipes").fetchone() == (True,)
    assert [(n[0], n[5]) for n in _notes(pg)] == [("site_paused", "admin@example.com")]


def test_shutdown_of_supervised_learn_does_not_pause(pg):
    job = _running_learn(pg, {})
    assert worker._abort_on_shutdown(job, pg, "w1", settings_for_tests()) is True
    assert pg.execute("select paused from recipes").fetchone() == (False,)
    assert _notes(pg) == []


def test_learn_profile_save_failure_never_publishes(pg, monkeypatch):
    store.put_recipe(pg, "zepto", BASE)
    _admin(pg)
    _seed_profile(B)
    monkeypatch.setattr(worker.sync, "learn", _learner([], {**BASE, "fast_path_ok": True}))

    def broken_save(*a, **k):
        raise OSError("vault unreachable")
    monkeypatch.setattr(worker.vault, "save_profile", broken_save)
    jid = _run(pg, B, "learn", payload={"reason": "drift", "auto": True})
    assert _job(pg, jid)[0] == "failed"
    # the profile is saved before apply_learned, so a failed save never pauses a live recipe
    assert pg.execute("select version, recipe from recipes").fetchone() == (1, BASE)
    assert pg.execute("select count(*) from recipe_approvals").fetchone() == (0,)
    assert pg.execute("select paused from recipes").fetchone() == (True,)


def test_meter_close_failure_never_escapes_run_job(pg, monkeypatch, caplog):
    import contextlib

    @contextlib.contextmanager
    def bad_meter():
        yield metering.Meter()
        raise RuntimeError("meter exit 9876543210")
    monkeypatch.setattr(worker.metering, "meter", bad_meter)
    monkeypatch.setattr(worker.sync, "fetch_orders", lambda site, since, **k: [order("1")])
    jid = _run(pg, A, "sync")
    assert _job(pg, jid)[:2] == ("done", None)
    assert "RuntimeError" in caplog.text and "9876543210" not in caplog.text

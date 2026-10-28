import pytest

from server import jobs
from helpers import A


def test_two_workers_never_claim_the_same_job(pg, pg2):
    jobs.enqueue(pg, A, "sync", "zepto")
    first, second = jobs.claim(pg, "w1", ("sync",)), jobs.claim(pg2, "w2", ("sync",))
    assert first is not None and second is None


def test_enqueue_conflict_while_active(pg):
    jobs.enqueue(pg, A, "connect", "swiggy", {"phone": "9876543210"})
    with pytest.raises(jobs.Conflict):
        jobs.enqueue(pg, A, "connect", "swiggy", {"phone": "9876543210"})
    # connection must remain usable after the failed insert
    assert pg.execute("select count(*) from jobs").fetchone()[0] == 1


def test_finish_strips_phone(pg):
    jid = jobs.enqueue(pg, A, "connect", "swiggy", {"phone": "9876543210", "x": 1})
    jobs.finish(pg, jid, error="otp_timeout")
    assert pg.execute("select status, payload, error from jobs").fetchone() == ("failed", {"x": 1}, "otp_timeout")


def test_requeue_stale_running_jobs(pg):
    jid = jobs.enqueue(pg, A, "sync", "zepto")
    jobs.claim(pg, "w1", ("sync",))
    pg.execute("update jobs set started_at = now() - interval '11 minutes', heartbeat_at = now() - interval '11 minutes'")
    assert jobs.requeue_stale(pg, 600) == 1
    assert jobs.claim(pg, "w2", ("sync",)).id == jid


def test_set_progress_merges(pg):
    jid = jobs.enqueue(pg, A, "sync", "zepto")
    jobs.set_progress(pg, jid, stage="syncing")
    jobs.set_progress(pg, jid, orders_found=12)
    assert pg.execute("select progress from jobs").fetchone()[0] == {"stage": "syncing", "orders_found": 12}


def test_claim_skips_a_row_locked_by_another_transaction(pg, pg2):
    jobs.enqueue(pg, A, "sync", "zepto")
    pg.execute("set lock_timeout = '2s'")  # fail fast instead of hanging if SKIP LOCKED is missing
    with pg2.transaction():
        pg2.execute("select id from jobs where status='queued' for update")
        assert jobs.claim(pg, "w1", ("sync",)) is None
    assert jobs.claim(pg, "w1", ("sync",)) is not None


def test_claim_takes_next_unlocked_job(pg, pg2):
    jobs.enqueue(pg, A, "sync", "zepto")
    newer = jobs.enqueue(pg, A, "sync", "blinkit")
    pg.execute("set lock_timeout = '2s'")
    with pg2.transaction():
        pg2.execute("select id from jobs where site='zepto' for update")
        got = jobs.claim(pg, "w1", ("sync",))
    assert got is not None and got.id == newer


def test_claim_skips_a_job_whose_profile_is_busy(pg):
    from helpers import B
    jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    assert jobs.claim(pg, "w1", ("connect",)) is not None
    jobs.enqueue(pg, A, "sync", "zepto")
    other = jobs.enqueue(pg, B, "sync", "zepto")
    got = jobs.claim(pg, "w2", ("sync",))
    assert got is not None and got.id == other
    assert jobs.claim(pg, "w2", ("sync",)) is None
    assert pg.execute("select count(*) from jobs where status='running'").fetchone() == (2,)


def test_claim_survives_a_unique_violation_race(pg, pg2):
    import threading
    import time
    connect = jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210"})
    jobs.enqueue(pg, A, "sync", "zepto")
    result = {}
    with pg2.transaction():
        # Another worker has started the connect job but not committed yet: claim's NOT EXISTS check cannot
        # see it, so its UPDATE waits on the unique index and then fails.
        pg2.execute("update jobs set status='running', worker_id='w1' where id=%s", (connect,))
        t = threading.Thread(target=lambda: result.update(job=jobs.claim(pg, "w2", ("sync",))))
        t.start()
        time.sleep(0.5)
    t.join(10)
    assert not t.is_alive() and result == {"job": None}
    assert pg.execute("select type, status from jobs order by type").fetchall() == \
        [("connect", "running"), ("sync", "queued")]


def test_requeue_stale_fails_dead_connect_jobs(pg):
    from server import store
    connect = jobs.enqueue(pg, A, "connect", "zepto", {"phone": "9876543210", "x": 1})
    sync_job = jobs.enqueue(pg, A, "sync", "blinkit")
    jobs.claim(pg, "w1", ("connect",))
    jobs.claim(pg, "w1", ("sync",))
    store.set_connection(pg, A, "zepto", status="syncing")
    pg.execute("update jobs set started_at = now() - interval '11 minutes', heartbeat_at = now() - interval '11 minutes'")
    assert jobs.requeue_stale(pg, 600) == 2
    row = pg.execute("select status, error, payload, finished_at is not null from jobs where id=%s",
                     (connect,)).fetchone()
    assert row == ("failed", "worker_lost", {"x": 1}, True)
    assert pg.execute("select status from jobs where id=%s", (sync_job,)).fetchone() == ("queued",)
    assert pg.execute("select status from connections where site='zepto'").fetchone() == ("error",)
    assert jobs.claim(pg, "w2", ("connect",)) is None


def test_stale_worker_cannot_finish_a_reclaimed_job(pg):
    jid = jobs.enqueue(pg, A, "sync", "zepto")
    first = jobs.claim(pg, "w1", ("sync",))
    assert first.worker_id == "w1"
    pg.execute("update jobs set started_at = now() - interval '11 minutes', heartbeat_at = now() - interval '11 minutes'")
    jobs.requeue_stale(pg, 600)
    assert jobs.claim(pg, "w2", ("sync",)).id == jid
    jobs.finish(pg, jid, error="late failure", worker_id="w1")  # the old worker wakes up
    assert pg.execute("select status, error, worker_id from jobs").fetchone() == ("running", None, "w2")
    jobs.finish(pg, jid, worker_id="w2")
    assert pg.execute("select status, error from jobs").fetchone() == ("done", None)


def test_heartbeating_job_is_not_requeued(pg):
    jid = jobs.enqueue(pg, A, "sync", "zepto"); jobs.claim(pg, "w1", ("sync",))
    pg.execute("update jobs set started_at = now() - interval '2 hours'")
    assert jobs.heartbeat(pg, jid, "w1") is True
    assert jobs.requeue_stale(pg, 2400) == 0


def test_silent_job_is_requeued(pg):
    jobs.enqueue(pg, A, "sync", "zepto"); jobs.claim(pg, "w1", ("sync",))
    pg.execute("update jobs set heartbeat_at = now() - interval '41 minutes'")
    assert jobs.requeue_stale(pg, 2400) == 1


def test_heartbeat_from_other_worker_is_ignored(pg):
    jid = jobs.enqueue(pg, A, "sync", "zepto"); jobs.claim(pg, "w1", ("sync",))
    assert jobs.heartbeat(pg, jid, "w2") is False


def test_shared_profile_blocks_second_running_job(pg):
    jobs.enqueue(pg, A, "sync", "swiggy"); jobs.enqueue(pg, A, "sync", "instamart")
    assert jobs.claim(pg, "w1", ("sync",)) is not None
    assert jobs.claim(pg, "w2", ("sync",)) is None


def test_job_without_profile_is_rejected(pg):
    import psycopg
    with pytest.raises(psycopg.errors.NotNullViolation):
        pg.execute("insert into jobs (user_id, type, site) values (%s, 'sync', 'zepto')", (A,))
    jid = jobs.enqueue(pg, A, "sync", "instamart")
    assert pg.execute("select profile from jobs where id=%s", (jid,)).fetchone() == ("swiggy",)


def test_requeue_only_touches_our_running_job(pg):
    jid = jobs.enqueue(pg, A, "sync", "zepto"); jobs.claim(pg, "w1", ("sync",))
    assert jobs.requeue(pg, jid, "w2") is False
    assert pg.execute("select status, worker_id from jobs").fetchone() == ("running", "w1")
    assert jobs.requeue(pg, jid, "w1") is True
    assert pg.execute("select status, worker_id, started_at, heartbeat_at from jobs").fetchone() == \
        ("queued", None, None, None)
    assert jobs.requeue(pg, jid, "w1") is False


def test_search_waits_behind_running_sync(pg):
    jobs.enqueue(pg, A, "sync", "swiggy")
    jobs.claim(pg, "w1", ("sync",))
    jobs.enqueue(pg, A, "search", "instamart", {"request_id": "r"})
    assert jobs.claim(pg, "w2", ("search",)) is None  # same profile 'swiggy' is busy: stays queued
    assert pg.execute("select status from jobs where type='search'").fetchone() == ("queued",)


def test_queued_search_claimed_before_queued_sync(pg):
    from helpers import B
    jobs.enqueue(pg, A, "sync", "zepto")
    jobs.enqueue(pg, B, "search", "zepto", {"request_id": "r"})
    assert jobs.claim(pg, "w", ("sync", "search")).type == "search"


def test_connect_claimed_before_older_sync(pg):
    from helpers import B
    jobs.enqueue(pg, A, "sync", "zepto")
    jobs.enqueue(pg, B, "connect", "zepto", {"phone": "9876543210"})
    assert jobs.claim(pg, "w", ("sync", "connect")).type == "connect"


def test_finish_reports_whether_it_touched_the_job(pg):
    jid = jobs.enqueue(pg, A, "sync", "zepto"); jobs.claim(pg, "w1", ("sync",))
    assert jobs.finish(pg, jid, worker_id="w2") is False
    assert jobs.finish(pg, jid, worker_id="w1") is True
    assert jobs.finish(pg, jid, worker_id="w1") is False

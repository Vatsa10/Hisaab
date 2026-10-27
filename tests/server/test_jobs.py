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
    pg.execute("update jobs set started_at = now() - interval '11 minutes'")
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

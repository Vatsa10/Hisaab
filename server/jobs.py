"""Postgres job queue: workers claim rows with FOR UPDATE SKIP LOCKED."""
from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb


class Conflict(Exception):
    """An active (queued/running) job already exists for this user, site and type."""


@dataclass
class Job:
    id: str
    user_id: str
    type: str
    site: str
    payload: dict
    worker_id: str | None = None


# Sites that share a Chrome profile (and so must never run a browser at the same time).
SITE_PROFILE: dict[str, str] = {"instamart": "swiggy"}


def enqueue(conn, user_id: str, type: str, site: str, payload: dict | None = None) -> str:
    try:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO jobs (user_id, type, site, profile, payload) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (user_id, type, site, SITE_PROFILE.get(site, site), Jsonb(payload or {})),
            ).fetchone()
    except psycopg.errors.UniqueViolation as e:
        if e.diag.constraint_name == "one_active_job":
            raise Conflict(f"{type} job for {site} already active") from e
        raise
    return str(row[0])


RUNNING_INDEX = "one_running_job_per_profile"
MAX_CLAIM_TRIES = 10


def claim(conn, worker_id: str, types: tuple[str, ...]) -> Job | None:
    """Claim the oldest queued job (interactive connect/search jobs first, then the rest) of the given types whose user and Chrome profile have no running job (one
    browser per profile). The row lock lasts only for this transaction. If another worker starts a job for the
    same user and profile concurrently, the unique index one_running_job_per_profile rejects our UPDATE: that candidate
    is skipped (inside a savepoint, so the transaction stays usable) and the next one is tried."""
    skipped: list = []
    with conn.transaction():
        for _ in range(MAX_CLAIM_TRIES):
            row = conn.execute(
                "SELECT j.id FROM jobs j WHERE j.status='queued' AND j.type = ANY(%s) AND NOT (j.id = ANY(%s::uuid[])) "
                "AND NOT EXISTS (SELECT 1 FROM jobs r WHERE r.status='running' AND r.user_id=j.user_id "
                "AND r.profile=j.profile) "
                "ORDER BY CASE j.type WHEN 'connect' THEN 0 WHEN 'search' THEN 0 ELSE 1 END, j.created_at "
                "LIMIT 1 FOR UPDATE OF j SKIP LOCKED",
                (list(types), skipped),
            ).fetchone()
            if row is None:
                return None
            try:
                with conn.transaction():
                    r = conn.execute(
                        "UPDATE jobs SET status='running', started_at=now(), heartbeat_at=now(), worker_id=%s WHERE id=%s "
                        "RETURNING id, user_id, type, site, payload",
                        (worker_id, row[0]),
                    ).fetchone()
            except psycopg.errors.UniqueViolation as e:
                if e.diag.constraint_name != RUNNING_INDEX:
                    raise
                skipped.append(row[0])
                continue
            return Job(id=str(r[0]), user_id=str(r[1]), type=r[2], site=r[3], payload=r[4], worker_id=worker_id)
    return None


def set_progress(conn, job_id: str, **progress) -> None:
    conn.execute("UPDATE jobs SET progress = progress || %s WHERE id=%s", (Jsonb(progress), job_id))


def heartbeat(conn, job_id: str, worker_id: str) -> bool:
    """Record that the worker is alive. Only a job still running under worker_id is touched; returns whether
    a row was updated (False means the job was finished, requeued or taken by another worker)."""
    return conn.execute(
        "UPDATE jobs SET heartbeat_at=now() WHERE id=%s AND status='running' AND worker_id=%s",
        (job_id, worker_id),
    ).rowcount == 1


def finish(conn, job_id: str, error: str | None = None, worker_id: str | None = None) -> bool:
    """Mark done/failed and drop the phone number from the payload. With worker_id, only a job that is still
    running under that worker is touched, so a worker whose job was declared stale and re-claimed by another
    worker cannot overwrite it. Returns whether the row was updated."""
    query = "UPDATE jobs SET status=%s, error=%s, finished_at=now(), payload = payload - 'phone' WHERE id=%s"
    params = ["failed" if error else "done", error, job_id]
    if worker_id is not None:
        query += " AND status='running' AND worker_id=%s"
        params.append(worker_id)
    return conn.execute(query, params).rowcount == 1


def requeue(conn, job_id: str, worker_id: str) -> bool:
    """Put a job this worker is running back in the queue at once (e.g. a sync aborted at shutdown), instead of
    waiting for it to go stale. Only a job still running under worker_id is touched; returns whether it was."""
    return conn.execute(
        "UPDATE jobs SET status='queued', worker_id=NULL, started_at=NULL, heartbeat_at=NULL "
        "WHERE id=%s AND status='running' AND worker_id=%s",
        (job_id, worker_id),
    ).rowcount == 1


def requeue_stale(conn, older_than_s: int = 600) -> int:
    """Handle running jobs silent (no heartbeat, or none and no start) for more than older_than_s seconds (their worker is presumed dead).
    A connect job cannot be resumed (its OTP exchange is gone): it fails with "worker_lost", its phone is
    dropped and a connection left "syncing" becomes "error". Every other job goes back to queued.
    Returns how many jobs were handled."""
    with conn.transaction():
        lost = conn.execute(
            """WITH lost AS (
                 UPDATE jobs SET status='failed', error='worker_lost', finished_at=now(), payload = payload - 'phone'
                 WHERE status='running' AND type='connect' AND coalesce(heartbeat_at, started_at) < now() - make_interval(secs => %s)
                 RETURNING user_id, site),
               conns AS (
                 UPDATE connections c SET status='error' FROM lost
                 WHERE c.user_id=lost.user_id AND c.site=lost.site AND c.status='syncing'
                 RETURNING 1)
               SELECT count(*) FROM lost""",
            (older_than_s,),
        ).fetchone()[0]
        requeued = conn.execute(
            "UPDATE jobs SET status='queued', worker_id=NULL, started_at=NULL, heartbeat_at=NULL "
            "WHERE status='running' AND type <> 'connect' AND coalesce(heartbeat_at, started_at) < now() - make_interval(secs => %s)",
            (older_than_s,),
        ).rowcount
    return lost + requeued

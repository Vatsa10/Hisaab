"""Postgres job queue: workers claim rows with FOR UPDATE SKIP LOCKED."""
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from server import sites


class Conflict(Exception):
    """An active (queued/running) job already exists for this user, site and type."""


class JobGone(Exception):
    """The job is no longer running under this worker (finished, requeued, re-claimed or deleted)."""


@dataclass
class Job:
    id: str
    user_id: str
    type: str
    site: str
    payload: dict
    worker_id: str | None = None


# Sites that share a Chrome profile (and so must never run a browser at the same time): the sites module's map.
SITE_PROFILE = sites.SITE_PROFILE

# Job types that cannot resume after their worker is lost (OTP exchange, admin session, add-to-cart in progress).
NO_RETRY_TYPES = ("connect", "learn", "fill_cart")


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _profile_sql(col: str) -> str:
    """SQL for the Chrome profile of the site in col, from SITE_PROFILE (single-quoted literals only)."""
    if not SITE_PROFILE:
        return col
    whens = " ".join(f"when {_sql_str(s)} then {_sql_str(p)}" for s, p in sorted(SITE_PROFILE.items()))
    return f"(case {col} {whens} else {col} end)"


# A queued row is due when it has no run_after or it has passed. A sync row is cooling while any site sharing its
# Chrome profile (swiggy and instamart share one) is in back-off. claim() and scaler_query() both use these.
_DUE_SQL = "(j.run_after is null or j.run_after <= now())"
_COOLING_SQL = ("(j.type = 'sync' and exists (select 1 from site_backoff b where b.until > now() "
                f"and {_profile_sql('b.site')} = j.profile))")


def enqueue(conn, user_id: str, type: str, site: str, payload: dict | None = None) -> str:
    try:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO jobs (user_id, type, site, profile, payload) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (user_id, type, site, sites.profile_for(site), Jsonb(payload or {})),
            ).fetchone()
    except psycopg.errors.UniqueViolation as e:
        if e.diag.constraint_name == "one_active_job":
            raise Conflict(f"{type} job for {site} already active") from e
        raise
    return str(row[0])


RUNNING_INDEX = "one_running_job_per_profile"
MAX_CLAIM_TRIES = 10


def claim(conn, worker_id: str, types: tuple[str, ...]) -> Job | None:
    """Claim the oldest due queued job (interactive connect/search jobs first, then the rest) of the given types
    whose user and Chrome profile have no running job (one browser per profile). A row with a future run_after
    waits, and a sync whose site is cooling down (site_backoff) is skipped without using an attempt. Claiming
    counts an attempt. The row lock lasts only for this transaction. If another worker starts a job for the
    same user and profile concurrently, the unique index one_running_job_per_profile rejects our UPDATE: that candidate
    is skipped (inside a savepoint, so the transaction stays usable) and the next one is tried."""
    skipped: list = []
    with conn.transaction():
        for _ in range(MAX_CLAIM_TRIES):
            row = conn.execute(
                "SELECT j.id FROM jobs j WHERE j.status='queued' AND j.type = ANY(%s) AND NOT (j.id = ANY(%s::uuid[])) "
                f"AND {_DUE_SQL} AND NOT {_COOLING_SQL} "
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
                        "UPDATE jobs SET status='running', started_at=now(), heartbeat_at=now(), worker_id=%s, "
                        "attempts = attempts + 1 WHERE id=%s "
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


@contextmanager
def owned(conn, job_id: str, worker_id: str) -> Iterator[None]:
    """The write fence: a transaction holding this job's row lock, entered only while the job is still running
    under worker_id. Deleting the job (account deletion, disconnect) waits for the body to commit, and a body
    entered after the row is gone, requeued or re-claimed never runs: JobGone is raised instead."""
    with conn.transaction():
        if conn.execute("SELECT 1 FROM jobs WHERE id=%s AND status='running' AND worker_id=%s FOR UPDATE",
                        (job_id, worker_id)).fetchone() is None:
            raise JobGone(job_id)
        yield


def requeue(conn, job_id: str, worker_id: str, run_after: datetime | None = None,
            max_attempts: int = 3) -> str | None:
    """Put a job this worker is running back in the queue, claimable from run_after (None: at once). A job that
    has used max_attempts claims fails instead with "too_many_attempts" (phone dropped). Only a job still running
    under worker_id is touched. Returns "queued", "failed", or None when the job is not ours."""
    with conn.transaction():
        row = conn.execute("SELECT attempts FROM jobs WHERE id=%s AND status='running' AND worker_id=%s FOR UPDATE",
                           (job_id, worker_id)).fetchone()
        if row is None:
            return None
        if row[0] >= max_attempts:
            conn.execute("UPDATE jobs SET status='failed', error='too_many_attempts', finished_at=now(), "
                         "payload = payload - 'phone' WHERE id=%s", (job_id,))
            return "failed"
        conn.execute("UPDATE jobs SET status='queued', worker_id=NULL, started_at=NULL, heartbeat_at=NULL, "
                     "run_after=%s WHERE id=%s", (run_after, job_id))
        return "queued"


def requeue_stale(conn, older_than_s: int = 600, max_attempts: int = 3) -> int:
    """Handle running jobs silent (no heartbeat, or none and no start) for more than older_than_s seconds (their
    worker is presumed dead). A job in NO_RETRY_TYPES cannot be resumed: it fails with "worker_lost", its phone
    is dropped and, for a connect, a connection left "syncing" becomes "error". Any other job goes back to queued,
    unless it has used max_attempts claims: then it fails with "too_many_attempts". Returns how many were handled."""
    silent = "status='running' AND coalesce(heartbeat_at, started_at) < now() - make_interval(secs => %s)"
    with conn.transaction():
        lost = conn.execute(
            f"""WITH lost AS (
                 UPDATE jobs SET status='failed', error='worker_lost', finished_at=now(), payload = payload - 'phone'
                 WHERE {silent} AND type = ANY(%s)
                 RETURNING user_id, site, type),
               conns AS (
                 UPDATE connections c SET status='error' FROM lost
                 WHERE lost.type='connect' AND c.user_id=lost.user_id AND c.site=lost.site AND c.status='syncing'
                 RETURNING 1)
               SELECT count(*) FROM lost""",
            (older_than_s, list(NO_RETRY_TYPES)),
        ).fetchone()[0]
        capped = conn.execute(
            "UPDATE jobs SET status='failed', error='too_many_attempts', finished_at=now(), payload = payload - 'phone' "
            f"WHERE {silent} AND NOT (type = ANY(%s)) AND attempts >= %s",
            (older_than_s, list(NO_RETRY_TYPES), max_attempts),
        ).rowcount
        requeued = conn.execute(
            "UPDATE jobs SET status='queued', worker_id=NULL, started_at=NULL, heartbeat_at=NULL "
            f"WHERE {silent} AND NOT (type = ANY(%s))",
            (older_than_s, list(NO_RETRY_TYPES)),
        ).rowcount
    return lost + capped + requeued


_TYPE_NAME = re.compile(r"[a-z_]+")


def scaler_query(types: Sequence[str]) -> str:
    """The KEDA postgres scaler's query (its only source): how many jobs of these types need a worker, meaning
    running ones plus queued ones that are due and not a cooling sync. Literal SQL with single quotes only."""
    types = tuple(types)
    if not types or not all(_TYPE_NAME.fullmatch(t) for t in types):
        raise ValueError("scaler_query needs one or more lower-case job type names")
    listed = ",".join(_sql_str(t) for t in types)
    return (f"select count(*) from jobs j where j.type in ({listed}) and (j.status = 'running' or "
            f"(j.status = 'queued' and {_DUE_SQL} and not {_COOLING_SQL}))")

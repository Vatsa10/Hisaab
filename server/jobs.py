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


def enqueue(conn, user_id: str, type: str, site: str, payload: dict | None = None) -> str:
    try:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO jobs (user_id, type, site, payload) VALUES (%s,%s,%s,%s) RETURNING id",
                (user_id, type, site, Jsonb(payload or {})),
            ).fetchone()
    except psycopg.errors.UniqueViolation as e:
        if e.diag.constraint_name == "one_active_job":
            raise Conflict(f"{type} job for {site} already active") from e
        raise
    return str(row[0])


def claim(conn, worker_id: str, types: tuple[str, ...]) -> Job | None:
    """Claim the oldest queued job of the given types; the row lock lasts only for this transaction."""
    with conn.transaction():
        row = conn.execute(
            "SELECT id FROM jobs WHERE status='queued' AND type = ANY(%s) "
            "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED",
            (list(types),),
        ).fetchone()
        if row is None:
            return None
        r = conn.execute(
            "UPDATE jobs SET status='running', started_at=now(), worker_id=%s WHERE id=%s "
            "RETURNING id, user_id, type, site, payload",
            (worker_id, row[0]),
        ).fetchone()
    return Job(id=str(r[0]), user_id=str(r[1]), type=r[2], site=r[3], payload=r[4])


def set_progress(conn, job_id: str, **progress) -> None:
    conn.execute("UPDATE jobs SET progress = progress || %s WHERE id=%s", (Jsonb(progress), job_id))


def finish(conn, job_id: str, error: str | None = None) -> None:
    """Mark done/failed and drop the phone number from the payload."""
    conn.execute(
        "UPDATE jobs SET status=%s, error=%s, finished_at=now(), payload = payload - 'phone' WHERE id=%s",
        ("failed" if error else "done", error, job_id),
    )


def requeue_stale(conn, older_than_s: int = 600) -> int:
    """Reset running jobs started more than older_than_s seconds ago back to queued."""
    cur = conn.execute(
        "UPDATE jobs SET status='queued', worker_id=NULL, started_at=NULL "
        "WHERE status='running' AND started_at < now() - make_interval(secs => %s)",
        (older_than_s,),
    )
    return cur.rowcount

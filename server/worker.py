"""Worker: claims connect/sync jobs from Postgres and runs the browser login/sync code per user."""
import logging
import os
import re
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql

import browser
import sync
from server import jobs, store
from server.config import Settings, settings

log = logging.getLogger("worker")

JOB_TYPES = ("connect", "sync")
MAX_ERROR_LEN = 300


def make_otp_prompt(listen_conn, job_conn, job_id: str, timeout_s: int):
    """Return a prompt(question) callable that waits for the OTP relayed via NOTIFY otp_<job id>."""
    channel = "otp_" + job_id.replace("-", "")

    def prompt(question: str) -> str:
        # LISTEN first so a NOTIFY sent right after the app sees "otp_needed" is not missed.
        listen_conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
        try:
            jobs.set_progress(job_conn, job_id, stage="otp_needed", question=question)
            deadline = time.monotonic() + timeout_s
            while (remaining := deadline - time.monotonic()) > 0:
                for note in listen_conn.notifies(timeout=remaining, stop_after=1):
                    jobs.set_progress(job_conn, job_id, stage="otp_received")
                    return note.payload
            raise TimeoutError("no OTP received in time")
        finally:
            listen_conn.execute(sql.SQL("UNLISTEN {}").format(sql.Identifier(channel)))

    return prompt


def _profile_dir(user_id: str, site: str) -> Path:
    return Path(os.getenv("PROFILE_ROOT", "profiles_users")) / user_id / browser.SITES[site]["profile"]


def _location(conn, user_id: str) -> dict | None:
    p = store.get_profile(conn, user_id)
    if p and p.get("lat") is not None and p.get("lon") is not None:
        return {"query": p.get("address"), "lat": p["lat"], "lon": p["lon"]}
    return None


def _clean_error(e: Exception, phone: str | None) -> str:
    """'<Type>: <first line>' truncated, with the phone and any long digit run (phone/OTP) masked."""
    line = (str(e).splitlines() or [""])[0]
    if phone:
        line = line.replace(phone, "***")
    line = re.sub(r"\d{4,}", "***", line)
    return f"{type(e).__name__}: {line}"[:MAX_ERROR_LEN]


def _status(conn, user_id: str, site: str) -> str | None:
    row = conn.execute("SELECT status FROM connections WHERE user_id=%s AND site=%s", (user_id, site)).fetchone()
    return row[0] if row else None


def _do_sync(job: jobs.Job, conn, db_site: str, sub_platform: str | None) -> None:
    orders = sync.fetch_orders(job.site, None, profile_dir=_profile_dir(job.user_id, job.site),
                               location=_location(conn, job.user_id), allow_learn=False)
    n = store.upsert_orders(conn, job.user_id, db_site, orders, sub_platform=sub_platform)
    jobs.set_progress(conn, job.id, stage="synced", orders_found=n)
    store.set_connection(conn, job.user_id, db_site, status="connected",
                         last_synced_at=datetime.now(timezone.utc))


def run_job(job: jobs.Job, conn, s: Settings, listen_conn=None) -> None:
    cfg = browser.SITES[job.site]
    db_site, sub_platform = cfg.get("db_site", job.site), cfg.get("sub_platform")
    phone = (job.payload or {}).get("phone")

    def fail(error: str, status: str | None = None) -> None:
        if status is None and _status(conn, job.user_id, db_site) == "syncing":
            status = "error"
        if status:
            store.set_connection(conn, job.user_id, db_site, status=status)
        jobs.finish(conn, job.id, error=error)

    own_listen = None
    try:
        if job.type == "connect":
            store.set_connection(conn, job.user_id, db_site, status="syncing")
            if listen_conn is None:
                listen_conn = own_listen = psycopg.connect(s.database_url, autocommit=True)
            prompt = make_otp_prompt(listen_conn, conn, job.id, s.otp_timeout_s)
            ok = sync.login(job.site, phone, prompt=prompt, profile_dir=_profile_dir(job.user_id, job.site),
                            location=_location(conn, job.user_id))
            if not ok:
                return fail("login_failed", "error")
            store.set_connection(conn, job.user_id, db_site, status="connected")
        _do_sync(job, conn, db_site, sub_platform)
        jobs.finish(conn, job.id)
    except browser.SessionExpired:
        fail("needs_login", "needs_login")
    except TimeoutError:
        fail("otp_timeout", "error")
    except sync.RecipeError as e:
        fail(f"no_recipe: {(str(e).splitlines() or [''])[0]}"[:MAX_ERROR_LEN])
    except Exception as e:
        log.exception("job %s failed", job.id)
        fail(_clean_error(e, phone))
    finally:
        if own_listen is not None:
            own_listen.close()


def main(worker_id: str | None = None, poll_s: float = 2.0) -> None:
    worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
    s = settings()
    log.info("worker %s started", worker_id)
    with psycopg.connect(s.database_url, autocommit=True) as conn, \
            psycopg.connect(s.database_url, autocommit=True) as listen_conn:
        try:
            while True:
                jobs.requeue_stale(conn)
                job = jobs.claim(conn, worker_id, JOB_TYPES)
                if job is None:
                    time.sleep(poll_s)
                    continue
                try:
                    run_job(job, conn, s, listen_conn=listen_conn)
                except Exception as e:
                    log.exception("job %s crashed", job.id)
                    jobs.finish(conn, job.id, error=_clean_error(e, (job.payload or {}).get("phone")))
        except KeyboardInterrupt:
            log.info("worker %s stopping", worker_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()

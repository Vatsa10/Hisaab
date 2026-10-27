"""Worker: claims connect/sync jobs from Postgres and runs the browser login/sync code per user."""
import contextlib
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

import browser
import sync
from server import jobs, store, vault
from server.config import Settings, settings
from server.proxy_relay import ProxyUnavailable, Relay, proxy_url_for

log = logging.getLogger("worker")

JOB_TYPES = ("connect", "sync")
MAX_ERROR_LEN = 300


# prompt kind -> (stage while waiting, stage once answered)
PROMPT_STAGES = {"otp": ("otp_needed", "otp_received"), "question": ("question_needed", "answer_received")}


def make_prompt(listen_conn, job_conn, job_id: str, timeout_s: int):
    """Return a prompt(text, kind="otp") callable that publishes the question on the job's progress and waits
    for the answer relayed via NOTIFY otp_<job id>. kind is "otp" or "question"; both use the same channel.
    Raises sync.OtpTimeout(kind) when nothing arrives within timeout_s."""
    channel = "otp_" + job_id.replace("-", "")

    def prompt(text: str, kind: str = "otp") -> str:
        waiting, answered = PROMPT_STAGES[kind]
        # LISTEN first so a NOTIFY sent right after the app sees the question is not missed.
        listen_conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
        try:
            jobs.set_progress(job_conn, job_id, stage=waiting, question=text)
            deadline = time.monotonic() + timeout_s
            while (remaining := deadline - time.monotonic()) > 0:
                for note in listen_conn.notifies(timeout=remaining, stop_after=1):
                    jobs.set_progress(job_conn, job_id, stage=answered)
                    return note.payload
            raise sync.OtpTimeout(kind)
        finally:
            try:  # best effort: a broken listen connection must not mask the original error
                listen_conn.execute(sql.SQL("UNLISTEN {}").format(sql.Identifier(channel)))
            except Exception as e:
                log.warning("UNLISTEN failed (%s)", type(e).__name__)

    return prompt


# Plan 1 name (it only asked for OTPs then); kept so code and tests written against Plan 1 still work.
make_otp_prompt = make_prompt


def _profiles_root() -> Path:
    return Path((os.getenv("PROFILE_ROOT") or "").strip() or "profiles_users")


def _check_profiles_root(root: Path, store_url: str) -> Path:
    """Refuse a PROFILE_ROOT that could make the start-up wipe (or a job) touch something that is not ours:
    the filesystem root, the home directory, the working directory or a parent of it, or the vault."""
    resolved = root.resolve()
    cwd = Path.cwd().resolve()
    forbidden = {Path(resolved.anchor), Path.home().resolve(), cwd, *cwd.parents}
    if store_url.startswith("file://"):
        forbidden.add(vault.store_from_url(store_url).root.resolve())
    if resolved in forbidden:
        raise ValueError(f"PROFILE_ROOT {str(resolved)!r} is not a safe scratch directory "
                         "(filesystem root, home, working directory, a parent of it, or the vault)")
    return resolved


def _is_job_dir(p: Path) -> bool:
    try:
        uuid.UUID(p.name)
    except ValueError:
        return False
    return p.is_dir() and not p.is_symlink()


def _wipe_profiles_root(store_url: str) -> None:
    """Plaintext profiles never outlive a job; per-job directories (UUID names) a dead worker left behind go
    at start-up. Nothing else under the root, and never the root itself, is deleted."""
    root = _check_profiles_root(_profiles_root(), store_url)
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if _is_job_dir(child):
            shutil.rmtree(child, ignore_errors=True)


def _location(conn, user_id: str) -> dict:
    """The user's delivery location, or {} (never None: None would mean the CLI owner's saved location)."""
    p = store.get_profile(conn, user_id)
    if p and p.get("lat") is not None and p.get("lon") is not None:
        return {"query": p.get("address") or "", "lat": p["lat"], "lon": p["lon"]}
    return {}


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


def _heartbeat_loop(database_url: str, job_id: str, worker_id: str | None, every_s: float,
                    stop: threading.Event) -> None:
    """Touch the job every every_s seconds on its own connection until stop is set or the job is no longer ours."""
    conn = None
    try:
        while not stop.wait(every_s):
            try:
                if conn is None:
                    conn = psycopg.connect(database_url, autocommit=True)
                if not jobs.heartbeat(conn, job_id, worker_id):
                    return
            except Exception as e:
                log.warning("heartbeat failed (%s)", type(e).__name__)
                _close(conn)
                conn = None
    finally:
        _close(conn)


def _ensure_proxy_session(conn, user_id: str, site: str) -> str:
    """The user's sticky proxy session for this site: created once (12 hex chars), then reused."""
    return conn.execute(
        "INSERT INTO connections (user_id, site, proxy_session_id) VALUES (%s,%s,%s) "
        "ON CONFLICT (user_id, site) DO UPDATE SET "
        "proxy_session_id = coalesce(connections.proxy_session_id, excluded.proxy_session_id) "
        "RETURNING proxy_session_id",
        (user_id, site, secrets.token_hex(6))).fetchone()[0]


def _do_sync(job: jobs.Job, conn, db_site: str, sub_platform: str | None, profile_dir: Path,
             proxy_server: str | None) -> None:
    orders = sync.fetch_orders(job.site, None, profile_dir=profile_dir, location=_location(conn, job.user_id),
                               allow_learn=False, proxy_server=proxy_server,
                               recipe_loader=lambda site: store.get_recipe(conn, site))
    n = store.upsert_orders(conn, job.user_id, db_site, orders, sub_platform=sub_platform)
    jobs.set_progress(conn, job.id, stage="synced", orders_found=n)
    store.set_connection(conn, job.user_id, db_site, status="connected",
                         last_synced_at=datetime.now(timezone.utc))


def run_job(job: jobs.Job, conn, s: Settings) -> None:
    """Run one claimed job. The user's encrypted profile is restored into a fresh per-job directory, the browser
    goes out through the user's sticky proxy session (if configured), the job heartbeats while it runs, and the
    profile is saved back only after a success. The plaintext directory is always deleted. A connect job gets
    its own fresh LISTEN connection, closed when the job ends, so one that died never affects the next job."""
    cfg = browser.SITES[job.site]
    db_site, sub_platform = cfg.get("db_site", job.site), cfg.get("sub_platform")
    phone = (job.payload or {}).get("phone")
    profile_name = jobs.SITE_PROFILE.get(job.site, job.site)
    profile_dir = _profiles_root() / job.id
    runs_dir = Path(tempfile.mkdtemp(prefix="ea-runs-"))
    stop_hb = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat_loop, name="job-heartbeat", daemon=True,
                                 args=(s.database_url, job.id, job.worker_id, s.heartbeat_s, stop_hb))
    vault_store = None

    def fail(error: str, status: str | None = None) -> None:
        if status is None and _status(conn, job.user_id, db_site) == "syncing":
            status = "error"
        if status:
            store.set_connection(conn, job.user_id, db_site, status=status)
        jobs.finish(conn, job.id, error=error, worker_id=job.worker_id)

    def save_profile() -> None:
        key = vault.save_profile(vault_store, s.profile_master_key, job.user_id, profile_name, profile_dir)
        store.set_connection(conn, job.user_id, db_site, profile_blob_key=key)

    listen_conn = None
    heartbeat.start()
    try:
        shutil.rmtree(profile_dir, ignore_errors=True)
        vault_store = vault.store_from_url(s.profile_store_url)
        try:
            vault.restore_profile(vault_store, s.profile_master_key, job.user_id, profile_name, profile_dir)
        except vault.VaultError as e:
            log.warning("job %s: stored profile unusable (%s); starting empty", job.id, type(e).__name__)
            shutil.rmtree(profile_dir, ignore_errors=True)
            if job.type == "sync":
                return fail("needs_login", "needs_login")
        profile_dir.mkdir(parents=True, exist_ok=True)
        session_id = _ensure_proxy_session(conn, job.user_id, db_site)

        with contextlib.ExitStack() as stack:
            proxy_server = None
            if s.proxy_url_template:
                upstream = proxy_url_for(s.proxy_url_template, session_id)
                relay = stack.enter_context(Relay(upstream))
                try:
                    relay.check()
                except ProxyUnavailable as e:
                    # log the upstream host only: the URL carries the proxy credentials
                    log.warning("job %s: proxy %s unavailable (%s)", job.id, urlsplit(upstream).hostname, e)
                    return fail("proxy_unavailable")
                proxy_server = relay.url

            if job.type == "connect":
                store.set_connection(conn, job.user_id, db_site, status="syncing")
                listen_conn = _connect(s.database_url)
                prompt = make_prompt(listen_conn, conn, job.id, s.otp_timeout_s)
                ok = sync.login(job.site, phone, prompt=prompt, profile_dir=profile_dir,
                                location=_location(conn, job.user_id), runs_dir=runs_dir,
                                on_orders_url=lambda url: store.set_connection(conn, job.user_id, db_site,
                                                                               orders_page_url=url),
                                proxy_server=proxy_server,
                                recipe_loader=lambda site: store.get_recipe(conn, site))
                if not ok:
                    return fail("login_failed", "error")
                store.set_connection(conn, job.user_id, db_site, status="connected")
                save_profile()  # a good login is kept even if the first sync below fails
            _do_sync(job, conn, db_site, sub_platform, profile_dir, proxy_server)
            save_profile()
        jobs.finish(conn, job.id, worker_id=job.worker_id)
    except browser.SessionExpired:
        fail("needs_login", "needs_login")
    except sync.OtpTimeout as e:  # only an unanswered prompt; any other TimeoutError is a normal error below
        fail(f"{e.kind}_timeout", "error")
    except sync.RecipeError as e:
        fail(f"no_recipe: {(str(e).splitlines() or [''])[0]}"[:MAX_ERROR_LEN])
    except Exception as e:
        msg = _clean_error(e, phone)
        log.error("job %s failed: %s", job.id, msg)
        fail(msg)
    finally:
        stop_hb.set()
        heartbeat.join(5)
        _close(listen_conn)
        shutil.rmtree(profile_dir, ignore_errors=True)
        shutil.rmtree(runs_dir, ignore_errors=True)


def _connect(url: str):
    return psycopg.connect(url, autocommit=True)


def _close(*conns) -> None:
    for c in conns:
        try:
            if c is not None:
                c.close()
        except Exception:
            pass


def _abort_on_shutdown(job: jobs.Job, conn, worker_id: str) -> None:
    """SIGTERM mid-job. A connect job cannot be resumed (its OTP exchange dies with us): fail it. A sync job is
    left running; with no more heartbeats it is requeued once stale."""
    if job.type != "connect":
        return
    try:
        db_site = browser.SITES[job.site].get("db_site", job.site)
        if _status(conn, job.user_id, db_site) == "syncing":
            store.set_connection(conn, job.user_id, db_site, status="error")
        jobs.finish(conn, job.id, error="worker_shutdown", worker_id=worker_id)
    except Exception as e:
        log.error("could not fail job %s on shutdown (%s)", job.id, type(e).__name__)


def _on_sigterm(signum, frame):
    raise KeyboardInterrupt


def main(worker_id: str | None = None, poll_s: float = 2.0, max_loops: int | None = None) -> None:
    """Poll for jobs forever. Any error outside run_job (e.g. a dropped DB connection) is logged,
    the connection is reopened after a backoff, and the loop continues. SIGTERM is handled like Ctrl-C.
    max_loops is for tests."""
    worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
    s = settings(for_worker=True)
    _wipe_profiles_root(s.profile_store_url)
    log.info("worker started id=%s", worker_id)
    old_handler = None
    if threading.current_thread() is threading.main_thread():
        old_handler = signal.signal(signal.SIGTERM, _on_sigterm)
    conn = None
    failures = loops = 0
    try:
        while max_loops is None or loops < max_loops:
            loops += 1
            try:
                if conn is None:
                    conn = _connect(s.database_url)
                jobs.requeue_stale(conn, s.stale_job_s)
                job = jobs.claim(conn, worker_id, JOB_TYPES)
                if job is None:
                    failures = 0
                    time.sleep(poll_s)
                    continue
                try:
                    run_job(job, conn, s)
                except KeyboardInterrupt:
                    _abort_on_shutdown(job, conn, worker_id)
                    raise
                except Exception as e:
                    phone = (job.payload or {}).get("phone")
                    log.error("job %s crashed: %s", job.id, _clean_error(e, phone))
                    jobs.finish(conn, job.id, error=_clean_error(e, phone), worker_id=worker_id)
                failures = 0
            except Exception as e:
                failures += 1
                delay = min(30.0, poll_s * 2 ** failures)
                log.error("worker loop error (%s), reconnecting in %.1fs: %s", type(e).__name__, delay,
                          _clean_error(e, None))
                _close(conn)
                conn = None
                time.sleep(delay)
    except KeyboardInterrupt:
        log.info("worker %s stopping", worker_id)
    finally:
        _close(conn)
        if old_handler is not None:
            signal.signal(signal.SIGTERM, old_handler)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()

"""Worker: claims connect/sync jobs from Postgres and runs the browser login/sync code per user."""
import _thread
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
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

import browser
import sync
from server import fake_sites, jobs, store, vault
from server.config import Settings, settings
from server.local_db import is_local_db
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


def _fake_mode() -> bool:
    """Only WORKER_FAKE=1 swaps the browser login/sync for server.fake_sites (no browser, vault or proxy)."""
    return os.getenv("WORKER_FAKE") == "1"


def _check_fake_mode(database_url: str) -> None:
    """Fake data must never be written into a real project: refuse unless the database is on this machine."""
    if _fake_mode() and not is_local_db(database_url):
        raise RuntimeError("WORKER_FAKE=1 refused: DATABASE_URL is not a local database")


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


HEARTBEAT_CONNECT_TIMEOUT_S = 10
EXIT_JOB_OVERRUN = 70  # process exit code when a job ran past MAX_JOB_S


def _heartbeat_loop(database_url: str, job_id: str, worker_id: str | None, every_s: float,
                    stop: threading.Event, max_job_s: float | None = None,
                    on_overrun: Callable[[], None] | None = None) -> None:
    """Touch the job every every_s seconds on its own connection until stop is set or the job is no longer ours.

    A job still running after max_job_s is presumed hung (a browser call that never returns): heartbeats stop,
    so the row goes stale and requeue_stale reclaims it, and on_overrun() is called (the worker exits)."""
    conn = None
    start = time.monotonic()
    deadline = start + max_job_s if max_job_s else None
    next_beat = start + every_s
    try:
        while True:
            wake = next_beat if deadline is None else min(next_beat, deadline)
            if stop.wait(max(0.0, wake - time.monotonic())):
                return
            if deadline is not None and time.monotonic() >= deadline:
                log.error("job %s ran past MAX_JOB_S (%ss): heartbeats stopped; worker exiting so the job is "
                          "reclaimed", job_id, max_job_s)
                if on_overrun is not None:
                    on_overrun()
                return
            if time.monotonic() < next_beat:
                continue
            next_beat = time.monotonic() + every_s
            try:
                if conn is None:
                    conn = psycopg.connect(database_url, autocommit=True,
                                           connect_timeout=HEARTBEAT_CONNECT_TIMEOUT_S)
                if not jobs.heartbeat(conn, job_id, worker_id):
                    return
            except Exception as e:
                log.warning("heartbeat failed (%s)", type(e).__name__)
                _close(conn)
                conn = None
    finally:
        _close(conn)


def _proxy_host(upstream: str | None) -> str:
    """The upstream proxy's host for a log line (never the user/password), or "?" if it does not parse."""
    try:
        parts = urlsplit(upstream or "")
        host = parts.hostname or "?"
        return host if "@" in parts.netloc else "?"  # no "@": what looks like a host may be the credentials
    except Exception:
        return "?"


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
    own = store.get_connection(conn, job.user_id, db_site) or {}  # the orders page this user landed on at login
    orders = (fake_sites.fetch_orders if _fake_mode() else sync.fetch_orders)(job.site, None, profile_dir=profile_dir, location=_location(conn, job.user_id),
                               allow_learn=False, proxy_server=proxy_server,
                               recipe_loader=lambda site: store.get_recipe(conn, site),
                               orders_page_url=own.get("orders_page_url"))
    n = store.upsert_orders(conn, job.user_id, db_site, orders, sub_platform=sub_platform)
    jobs.set_progress(conn, job.id, stage="synced", orders_found=n)
    store.set_connection(conn, job.user_id, db_site, status="connected",
                         last_synced_at=datetime.now(timezone.utc))


def run_job(job: jobs.Job, conn, s: Settings, exit_process: Callable[[int], None] | None = None) -> None:
    """Run one claimed job. The user's encrypted profile is restored into a fresh per-job directory, the browser
    goes out through the user's sticky proxy session (if configured), the job heartbeats while it runs, and the
    profile is saved back only after a success. The plaintext directory is always deleted. A connect job gets
    its own fresh LISTEN connection, closed when the job ends, so one that died never affects the next job.

    A job still running after s.max_job_s is presumed hung: its heartbeat stops and exit_process(non-zero) is
    called from the heartbeat thread (default os._exit), so the container restarts and the job is reclaimed."""
    exit_process = exit_process or os._exit
    cfg = browser.SITES[job.site]
    db_site, sub_platform = cfg.get("db_site", job.site), cfg.get("sub_platform")
    phone = (job.payload or {}).get("phone")
    profile_name = jobs.SITE_PROFILE.get(job.site, job.site)
    profile_dir = _profiles_root() / job.id
    runs_dir = Path(tempfile.mkdtemp(prefix="ea-runs-"))
    stop_hb = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat_loop, name="job-heartbeat", daemon=True,
                                 args=(s.database_url, job.id, job.worker_id, s.heartbeat_s, stop_hb,
                                       s.max_job_s, lambda: exit_process(EXIT_JOB_OVERRUN)))
    vault_store = None
    fake = _fake_mode()

    def fail(error: str, status: str | None = None) -> None:
        if status is None and _status(conn, job.user_id, db_site) == "syncing":
            status = "error"
        if status:
            store.set_connection(conn, job.user_id, db_site, status=status)
        jobs.finish(conn, job.id, error=error, worker_id=job.worker_id)

    def save_profile() -> None:
        if fake:
            return
        key = vault.save_profile(vault_store, s.profile_master_key, job.user_id, profile_name, profile_dir)
        store.set_connection(conn, job.user_id, db_site, profile_blob_key=key)

    listen_conn = None
    heartbeat.start()
    try:
        shutil.rmtree(profile_dir, ignore_errors=True)
        vault_store = None if fake else vault.store_from_url(s.profile_store_url)
        try:
            if not fake:
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
            if s.proxy_url_template and not fake:
                # Any failure here (a template that does not parse, a relay that will not start, an unreachable
                # or refusing upstream) is "proxy_unavailable". Exception text is never used: it can quote the
                # upstream URL, which carries the proxy credentials.
                upstream = None
                try:
                    upstream = proxy_url_for(s.proxy_url_template, session_id)
                    relay = stack.enter_context(Relay(upstream))
                    relay.check()
                except Exception as e:
                    reason = str(e) if isinstance(e, ProxyUnavailable) else type(e).__name__
                    log.warning("job %s: proxy %s unavailable (%s)", job.id, _proxy_host(upstream), reason)
                    return fail("proxy_unavailable")
                proxy_server = relay.url

            if job.type == "connect":
                store.set_connection(conn, job.user_id, db_site, status="syncing")
                listen_conn = _connect(s.database_url)
                prompt = make_prompt(listen_conn, conn, job.id, s.otp_timeout_s)
                ok = (fake_sites.login if fake else sync.login)(job.site, phone, prompt=prompt, profile_dir=profile_dir,
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


def _abort_on_shutdown(job: jobs.Job, conn, worker_id: str) -> bool:
    """Abort a job this worker is still running because the worker is going away. A connect job cannot be
    resumed (its OTP exchange dies with us): it fails with "worker_shutdown" and a connection left "syncing"
    becomes "error". A sync job goes straight back to the queue for another worker. Both updates only touch a
    job still running under worker_id. Returns whether the job was aborted (False: already finished, or the
    database could not be reached; then the stale-job requeue deals with it)."""
    try:
        if job.type == "connect":
            if not jobs.finish(conn, job.id, error="worker_shutdown", worker_id=worker_id):
                return False
            db_site = browser.SITES[job.site].get("db_site", job.site)
            if _status(conn, job.user_id, db_site) == "syncing":
                store.set_connection(conn, job.user_id, db_site, status="error")
            return True
        return jobs.requeue(conn, job.id, worker_id)
    except Exception as e:
        log.error("could not abort job %s on shutdown (%s)", job.id, type(e).__name__)
        return False


DRAIN_UNWIND_S = 10  # after an abort at DRAIN_S, how long the job gets to unwind before the process exits
EXIT_DRAIN_TIMEOUT = 71  # process exit code when an aborted job did not unwind


class _Drain:
    """SIGTERM handling. The worker stops claiming jobs; the current job may finish for up to drain_s seconds.
    If it is still running then, a watchdog thread aborts it in the database (_abort_on_shutdown, on its own
    connection), interrupts the main thread so the job unwinds and cleans up, and exits the process if the job
    has not unwound within DRAIN_UNWIND_S."""

    def __init__(self, s: Settings, worker_id: str, exit_process: Callable[[int], None]):
        self.s, self.worker_id, self.exit_process = s, worker_id, exit_process
        self.requested = threading.Event()  # SIGTERM received: claim nothing more
        self.stopped = threading.Event()  # the main loop has ended
        self.idle = threading.Event()  # no job is running
        self.idle.set()
        self.aborted = False  # the watchdog aborted the current job
        self._job: jobs.Job | None = None
        self._lock = threading.Lock()

    def start_job(self, job: jobs.Job) -> None:
        with self._lock:
            self._job = job
            self.idle.clear()

    def end_job(self) -> None:
        with self._lock:
            self._job = None
            self.idle.set()

    def on_sigterm(self, signum=None, frame=None) -> None:
        if self.requested.is_set():
            return
        self.requested.set()
        log.info("worker %s: SIGTERM, draining (current job may run %ss more)", self.worker_id, self.s.drain_s)
        threading.Thread(target=self._watch, name="drain-watchdog", daemon=True).start()

    def _watch(self) -> None:
        if self.stopped.wait(self.s.drain_s):
            return
        with self._lock:
            job = self._job
        if job is None:
            return
        conn = None
        try:
            conn = psycopg.connect(self.s.database_url, autocommit=True, connect_timeout=HEARTBEAT_CONNECT_TIMEOUT_S)
            done = _abort_on_shutdown(job, conn, self.worker_id)
        except Exception as e:
            log.error("could not reach the database to abort job %s (%s)", job.id, type(e).__name__)
            done = False
        finally:
            _close(conn)
        self.aborted = True
        log.warning("job %s still running after DRAIN_S (%ss): %s", job.id, self.s.drain_s,
                    "aborted" if done else "left for the stale-job requeue")
        if self.stopped.is_set():  # the main loop ended meanwhile: nothing left to interrupt
            return
        _thread.interrupt_main()
        if not self.idle.wait(DRAIN_UNWIND_S) and not self.stopped.is_set():
            log.error("job %s did not unwind after the abort; worker exiting", job.id)
            self.exit_process(EXIT_DRAIN_TIMEOUT)


def main(worker_id: str | None = None, poll_s: float = 2.0, max_loops: int | None = None,
         exit_process: Callable[[int], None] | None = None) -> None:
    """Poll for jobs until SIGTERM (or Ctrl-C). Any error outside run_job (e.g. a dropped DB connection) is
    logged, the connection is reopened after a backoff, and the loop continues.

    SIGTERM drains: no new job is claimed and the current one may finish within s.drain_s; past that it is
    aborted (see _Drain). Ctrl-C aborts the current job at once. exit_process (default os._exit) ends the
    process when a job hangs (MAX_JOB_S) or will not unwind after a drain abort; max_loops is for tests."""
    worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
    exit_process = exit_process or os._exit
    s = settings(for_worker=True)
    _check_fake_mode(s.database_url)
    _wipe_profiles_root(s.profile_store_url)
    log.info("worker started id=%s", worker_id)
    drain = _Drain(s, worker_id, exit_process)
    installed, old_handler = False, None
    if threading.current_thread() is threading.main_thread():
        old_handler = signal.signal(signal.SIGTERM, drain.on_sigterm)
        installed = True
    conn = None
    failures = loops = 0
    try:
        while (max_loops is None or loops < max_loops) and not drain.requested.is_set():
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
                if drain.requested.is_set():  # SIGTERM arrived while claiming: hand the job straight back
                    jobs.requeue(conn, job.id, worker_id)
                    break
                drain.start_job(job)
                try:
                    run_job(job, conn, s, exit_process=exit_process)
                except KeyboardInterrupt:
                    if not drain.aborted:  # Ctrl-C (the drain watchdog has already aborted its job)
                        _abort_on_shutdown(job, conn, worker_id)
                    raise
                except Exception as e:
                    phone = (job.payload or {}).get("phone")
                    log.error("job %s crashed: %s", job.id, _clean_error(e, phone))
                    jobs.finish(conn, job.id, error=_clean_error(e, phone), worker_id=worker_id)
                finally:
                    drain.end_job()
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
        pass
    finally:
        drain.stopped.set()
        _close(conn)
        if installed:
            signal.signal(signal.SIGTERM, old_handler if old_handler is not None else signal.SIG_DFL)
    log.info("worker %s stopping", worker_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()

"""Worker: claims connect/sync/learn jobs from Postgres and runs the browser login/sync/learn code per user."""
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
import llm
import metering
import sync
from server import categorize_pg, fake_sites, jobs, notify, recipe_admin, store, usage, vault
from server.config import SITE_NAMES, Settings, settings
from server.local_db import is_local_db
from server.proxy_relay import ProxyUnavailable, Relay, proxy_url_for

log = logging.getLogger("worker")

JOB_TYPES = ("connect", "sync", "learn")
MAX_ERROR_LEN = 300


# prompt kind -> (stage while waiting, stage once answered)
PROMPT_STAGES = {"otp": ("otp_needed", "otp_received"), "question": ("question_needed", "answer_received")}


def make_prompt(listen_conn, job_conn, job_id: str, timeout_s: int):
    """Return a prompt(text, kind="otp") callable that publishes the question on the job's progress and waits
    for the answer relayed via NOTIFY otp_<job id>. kind is "otp" or "question"; both use the same channel.
    The progress also carries prompt_at (ISO-8601 UTC), when the wait began, so the app can count the same
    timeout_s window down exactly. Raises sync.OtpTimeout(kind) when nothing arrives within timeout_s."""
    channel = "otp_" + job_id.replace("-", "")

    def prompt(text: str, kind: str = "otp") -> str:
        waiting, answered = PROMPT_STAGES[kind]
        # LISTEN first so a NOTIFY sent right after the app sees the question is not missed.
        listen_conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
        try:
            prompt_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            jobs.set_progress(job_conn, job_id, stage=waiting, question=text, prompt_at=prompt_at)
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


_categorize_skip_logged = False
_NOT_COUNTED = sync.NOT_COUNTED  # same statuses the spend views leave out
PROGRESS_EVERY_S = 1.0  # at most one "found so far" write per second during a sync


def throttled(write: Callable[[int, float], None], min_interval_s: float = PROGRESS_EVERY_S,
              clock: Callable[[], float] = time.monotonic) -> Callable[[int, float], None]:
    """Wrap write(count, amount) so it runs for the first call and then at most once per min_interval_s; calls in
    between are dropped (the final count is written when the sync ends). A failing write is logged by class only
    and never fails the sync."""
    last: float | None = None

    def on_progress(count: int, amount: float) -> None:
        nonlocal last
        now = clock()
        if last is not None and now - last < min_interval_s:
            return
        last = now
        try:
            write(count, amount)
        except Exception as e:
            log.warning("progress write failed (%s)", type(e).__name__)

    return on_progress


def _categorize(conn) -> None:
    """Categorise items the sync just stored. Off with CATEGORIZE=0 or without ANTHROPIC_API_KEY (logged once per
    process). A failure is logged by exception class only (the text can quote a key) and never fails the job."""
    global _categorize_skip_logged
    if os.getenv("CATEGORIZE") == "0" or not (os.getenv("ANTHROPIC_API_KEY") or "").strip():
        if not _categorize_skip_logged:
            _categorize_skip_logged = True
            log.info("categorisation disabled (CATEGORIZE=0 or ANTHROPIC_API_KEY not set)")
        return
    try:
        categorize_pg.categorize_new_items(conn)  # default classifier: Claude via llm.py
    except Exception as e:
        log.warning("categorisation failed (%s)", type(e).__name__)


def _do_sync(job: jobs.Job, conn, db_site: str, sub_platform: str | None, profile_dir: Path,
             proxy_server: str | None, fetch: Callable | None = None,
             block_media: bool = False) -> tuple[int, float]:
    """Fetch and store the user's orders; returns (orders stored, their counted total)."""
    own = store.get_connection(conn, job.user_id, db_site) or {}  # the orders page this user landed on at login
    jobs.set_progress(conn, job.id, stage="syncing")
    # The app shows "Found so far" from these, live; throttled so a fast fetch does not flood Realtime.
    found_so_far = throttled(lambda count, amount: jobs.set_progress(
        conn, job.id, stage="syncing", orders_found=count, amount_found=round(float(amount), 2)))
    orders = (fetch or sync.fetch_orders)(job.site, None, profile_dir=profile_dir, location=_location(conn, job.user_id),
                   allow_learn=False, proxy_server=proxy_server,
                   recipe_loader=lambda site: store.get_recipe(conn, site),
                   orders_page_url=own.get("orders_page_url"), on_progress=found_so_far,
                   block_media=block_media)
    n = store.upsert_orders(conn, job.user_id, db_site, orders, sub_platform=sub_platform)
    total = conn.execute(
        "SELECT coalesce(sum(total_amount), 0) FROM orders WHERE user_id=%s AND site=%s AND order_id = ANY(%s) "
        "AND coalesce(status, '') <> ALL(%s)",
        (job.user_id, db_site, [o["order_id"] for o in orders], list(_NOT_COUNTED))).fetchone()[0]
    jobs.set_progress(conn, job.id, stage="synced", orders_found=n, amount_found=round(float(total), 2))
    store.set_connection(conn, job.user_id, db_site, status="connected",
                         last_synced_at=datetime.now(timezone.utc))
    return n, float(total)


def _app(site: str) -> str:
    return SITE_NAMES.get(site, site.capitalize())


def _dispatch(conn, s: Settings) -> None:
    """Send what is in the outbox now; best effort (the scheduler retries whatever is left)."""
    try:
        notify.dispatch(conn, s)
    except Exception as e:
        log.warning("notification dispatch failed (%s)", type(e).__name__)


def _notify_user(conn, s: Settings, **row) -> None:
    """Enqueue one user notification and dispatch; a failure never fails the job."""
    try:
        notify.enqueue(conn, **row)
    except Exception as e:
        log.warning("notification enqueue failed (%s)", type(e).__name__)
        return
    _dispatch(conn, s)


NO_LEARNER_BODY = "Its order recipe stopped working and no admin account is connected to re-learn it."
RELEARN_FAILED_BODY = ("Its order recipe stopped working and the automatic re-learn did not publish a new one. "
                       "Review it in the admin view.")


def _pause_and_alert(conn, s: Settings, site: str, note: str, body: str) -> None:
    """Pause the site's orders recipe and email every admin (once per site per IST day)."""
    try:
        recipe_admin.pause_site(conn, site, "orders", note)
        notify.alert_admins(conn, kind="site_paused", title=f"{_app(site)} sync paused", body=body,
                            dedupe_key=f"site_paused:{site}:{usage.today(tz=s.app_tz)}")
    except Exception as e:
        log.error("could not pause %s after drift (%s)", site, type(e).__name__)
        return
    _dispatch(conn, s)


def _on_drift(conn, s: Settings, job: jobs.Job, db_site: str, note: str) -> None:
    """Ruling R8: on the healthy -> unhealthy transition, enqueue an unattended learn on a connected admin
    account, or, with none, pause the site and alert the admins. Later drifted syncs do nothing more."""
    try:
        if not recipe_admin.mark_drift(conn, job.site, "orders", note[:MAX_ERROR_LEN]):
            return
        learner = recipe_admin.admin_learner(conn, db_site)
    except Exception as e:
        log.error("drift handling for %s failed (%s)", job.site, type(e).__name__)
        return
    if learner is None:
        return _pause_and_alert(conn, s, job.site, "drift: no admin account connected", NO_LEARNER_BODY)
    try:
        jobs.enqueue(conn, learner, "learn", job.site, {"reason": "drift", "auto": True})
    except jobs.Conflict:
        pass
    except Exception as e:
        log.error("could not enqueue a re-learn for %s (%s)", job.site, type(e).__name__)


def _record_usage(conn, job: jobs.Job, s: Settings, meter: metering.Meter, relay, seconds: float) -> None:
    """Add this job's proxy, AI and worker time to the user's usage for today (IST). Never fails the job."""
    try:
        usage.record(conn, job.user_id, usage.today(tz=s.app_tz),
                     proxy_mb=(relay.bytes_total if relay is not None else 0) / 1e6,
                     ai_tokens=meter.ai_tokens(s.jev_tokens_per_call),
                     worker_seconds=seconds,
                     syncs=1 if job.type in ("connect", "sync") else 0)
    except Exception as e:
        log.warning("job %s: usage not recorded (%s)", job.id, type(e).__name__)


def _do_learn(job: jobs.Job, conn, s: Settings, profile_dir: Path, proxy_server: str | None,
              listen: Callable[[], object], fake: bool) -> dict:
    """Learn the site's orders recipe on the admin's account and publish or file it (recipe_admin.apply_learned).
    An auto (drift) learn never lets a new POST through; a supervised one asks the admin for each."""
    auto = bool((job.payload or {}).get("auto"))
    jobs.set_progress(conn, job.id, stage="learning")
    if auto:
        approve = recipe_admin.deny_new_posts
    else:
        approve = recipe_admin.make_approver(listen(), conn, job.id, job.site, "orders", s.approval_timeout_s)
    current = store.get_recipe(conn, job.site) or {}
    preapproved = [e["url_regex"] for e in current.get("read_post_allowlist") or []
                   if isinstance(e, dict) and e.get("url_regex")]
    recipe = (fake_sites.learn if fake else sync.learn)(
        job.site, profile_dir=profile_dir, location=_location(conn, job.user_id), proxy_server=proxy_server,
        approve=approve, preapproved=preapproved, save=False)
    result = recipe_admin.apply_learned(conn, job.site, "orders", recipe, set(getattr(approve, "approved", ())),
                                        job.id, reason="drift_relearn" if auto else "learned")
    jobs.set_progress(conn, job.id, stage="learned", approval_id=result["approval_id"], status=result["status"],
                      version=result["version"])
    return result


def run_job(job: jobs.Job, conn, s: Settings, exit_process: Callable[[int], None] | None = None) -> None:
    """Run one claimed job. The user's encrypted profile is restored into a fresh per-job directory, the browser
    goes out through the user's sticky proxy session (if configured), the job heartbeats while it runs, and the
    profile is saved back only after a success. The plaintext directory is always deleted. A connect job gets
    its own fresh LISTEN connection, closed when the job ends, so one that died never affects the next job.

    A job still running after s.max_job_s is presumed hung: its heartbeat stops and exit_process(non-zero) is
    called from the heartbeat thread (default os._exit), so the container restarts and the job is reclaimed."""
    exit_process = exit_process or os._exit
    started = time.monotonic()
    cfg = browser.SITES[job.site]
    db_site, sub_platform = cfg.get("db_site", job.site), cfg.get("sub_platform")
    phone = (job.payload or {}).get("phone")
    auto_learn = job.type == "learn" and bool((job.payload or {}).get("auto"))
    profile_name = jobs.SITE_PROFILE.get(job.site, job.site)
    profile_dir = _profiles_root() / job.id
    runs_dir = Path(tempfile.mkdtemp(prefix="ea-runs-"))
    stop_hb = threading.Event()
    max_s = s.max_learn_job_s if job.type == "learn" else s.max_job_s
    heartbeat = threading.Thread(target=_heartbeat_loop, name="job-heartbeat", daemon=True,
                                 args=(s.database_url, job.id, job.worker_id, s.heartbeat_s, stop_hb,
                                       max_s, lambda: exit_process(EXIT_JOB_OVERRUN)))
    vault_store = None
    fake = _fake_mode()
    restored = False
    relay = None
    metered = contextlib.ExitStack()
    meter = metered.enter_context(metering.meter())

    def fail(error: str, status: str | None = None) -> None:
        before = _status(conn, job.user_id, db_site)
        if status is None and before == "syncing":
            status = "error"
        if status:
            store.set_connection(conn, job.user_id, db_site, status=status)
        jobs.finish(conn, job.id, error=error, worker_id=job.worker_id)
        if status == "needs_login" and before != "needs_login":
            _notify_user(conn, s, kind="needs_login", title=f"{_app(job.site)} logged you out",
                         body="Reconnect to keep your spending up to date.", url="/accounts",
                         user_id=job.user_id, dedupe_key=f"needs_login:{job.id}")
        if auto_learn:
            _pause_and_alert(conn, s, job.site, f"drift re-learn failed: {error}"[:MAX_ERROR_LEN],
                             RELEARN_FAILED_BODY)

    def save_profile() -> None:
        if fake:
            return
        if not restored:  # a fresh profile (nothing restored, or the stored one was unusable) is ours now
            vault.stamp(profile_dir, job.user_id, profile_name)
        key = vault.save_profile(vault_store, s.profile_master_key, job.user_id, profile_name, profile_dir)
        store.set_connection(conn, job.user_id, db_site, profile_blob_key=key)

    def listen():
        nonlocal listen_conn
        listen_conn = _connect(s.database_url)
        return listen_conn

    listen_conn = None
    heartbeat.start()
    try:
        if job.type == "learn" and not recipe_admin.is_admin(conn, job.user_id):
            return fail("not_admin")
        shutil.rmtree(profile_dir, ignore_errors=True)
        vault_store = None if fake else vault.store_from_url(s.profile_store_url)
        try:
            if not fake:
                restored = vault.restore_profile(vault_store, s.profile_master_key, job.user_id, profile_name,
                                                 profile_dir)
        except (vault.VaultKeyMismatch, vault.ProfileOwnerMismatch):
            raise  # handled below: never "start empty" over a profile we cannot or may not read
        except vault.VaultError as e:
            log.warning("job %s: stored profile unusable (%s); starting empty", job.id, type(e).__name__)
            shutil.rmtree(profile_dir, ignore_errors=True)
            if job.type in ("sync", "learn"):
                return fail("needs_login", "needs_login")
        if job.type == "learn" and not restored and not fake:
            return fail("needs_login", "needs_login")  # a learn runs on the admin's logged-in account
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

            if job.type == "learn":
                result = _do_learn(job, conn, s, profile_dir, proxy_server, listen, fake)
                save_profile()
                jobs.finish(conn, job.id, worker_id=job.worker_id)
                if auto_learn and result["status"] == "pending":
                    _pause_and_alert(conn, s, job.site, "drift re-learn needs admin approval", RELEARN_FAILED_BODY)
                return

            if job.type == "connect":
                store.set_connection(conn, job.user_id, db_site, status="syncing")
                prompt = make_prompt(listen(), conn, job.id, s.otp_timeout_s)
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
            n, total = _do_sync(job, conn, db_site, sub_platform, profile_dir, proxy_server,
                                fetch=fake_sites.fetch_orders if fake else sync.fetch_orders,
                                block_media=job.type == "sync" and s.sync_block_media)  # R12: user syncs only
            save_profile()
            _categorize(conn)
        jobs.finish(conn, job.id, worker_id=job.worker_id)
        if job.type == "connect":
            _notify_user(conn, s, kind="first_sync_done", title=f"{_app(job.site)} is connected",
                         body=f"{n} orders · {notify.format_inr(total)} found", url="/spend",
                         user_id=job.user_id, dedupe_key=f"first_sync:{job.id}")
    except browser.SessionExpired:
        fail("needs_login", "needs_login")
    except sync.OtpTimeout as e:  # only an unanswered prompt; any other TimeoutError is a normal error below
        fail(f"{e.kind}_timeout", "error")
    except sync.RecipeDrift as e:
        fail("recipe_drift")
        if job.type != "learn":
            _on_drift(conn, s, job, db_site, (str(e).splitlines() or [""])[0])
    except sync.RecipeError as e:
        label = "learn_failed" if job.type == "learn" else "no_recipe"
        fail(f"{label}: {(str(e).splitlines() or [''])[0]}"[:MAX_ERROR_LEN])
    except vault.VaultKeyMismatch:
        # A configuration error (PROFILE_MASTER_KEY changed), not the user's session: nothing is saved over the
        # stored profile and the connection keeps its status.
        log.error("job %s: PROFILE_MASTER_KEY does not match the stored profile; check the worker's key", job.id)
        fail("vault_key_mismatch")
    except vault.ProfileOwnerMismatch:
        log.error("job %s: profile owner check failed; nothing uploaded", job.id)
        fail("profile_owner_mismatch")
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
        metered.close()
        _record_usage(conn, job, s, meter, relay, time.monotonic() - started)


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
    becomes "error"; so does a learn job (its approval exchange dies with us). A sync job goes straight back to
    the queue for another worker. Both updates only touch a job still running under worker_id. Returns whether
    the job was aborted (False: already finished, or the database could not be reached; then the stale-job
    requeue deals with it)."""
    try:
        if job.type in ("connect", "learn"):
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
    llm.model()  # the LLM cost policy: a model at or above $1 / MTok stops the worker before anything else
    worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
    exit_process = exit_process or os._exit
    _check_fake_mode(os.getenv("DATABASE_URL", ""))  # before settings(): no other config error may mask it
    # Fake mode has no vault and no proxy, so their key / template validation (for_worker) does not apply.
    s = settings(for_worker=not _fake_mode())
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

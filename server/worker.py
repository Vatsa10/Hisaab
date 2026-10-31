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

import addresses
import browser
import cart_fill
import food_learn
import food_search
import llm
import metering
import recipes
import search
import search_learn
import sync
from server import (addresses_store, cart, categorize_pg, fake_sites, food_cart, jobs, match, notify, recipe_admin,
                    session, site_health, sites, store, usage, vault)
from server import fill as fills
from server import fill_summary
from server.config import OPEN_APP_URLS, SITE_NAMES, Settings, check_hosted, settings
from server.local_db import is_local_db
from server.proxy_relay import ProxyUnavailable, Relay, proxy_url_for

log = logging.getLogger("worker")

JOB_TYPES = ("connect", "sync", "learn", "search", "fill_cart")
MAX_ERROR_LEN = 300


# prompt kind -> (stage while waiting, stage once answered)
PROMPT_STAGES = {"otp": ("otp_needed", "otp_received"), "question": ("question_needed", "answer_received")}


def make_prompt(listen_conn, job_conn, job_id: str, timeout_s: int):
    """Return a prompt(text, kind="otp") callable that publishes the question on the job's progress and waits
    for the answer relayed via NOTIFY otp_<job id>. kind is "otp" or "question"; both use the same channel.
    The progress also carries prompt_at (ISO-8601 UTC), when the wait began, so the app can count the same
    timeout_s window down exactly. Raises sync.OtpTimeout(kind) when nothing arrives within timeout_s."""
    channel = "otp_" + job_id.replace("-", "")

    def prompt(text: str, kind: str = "otp", choices: list | None = None) -> str:
        waiting, answered = PROMPT_STAGES[kind]
        # LISTEN first so a NOTIFY sent right after the app sees the question is not missed.
        listen_conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
        try:
            prompt_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            extra = {} if choices is None else {"choices": choices}
            jobs.set_progress(job_conn, job_id, stage=waiting, question=text, prompt_at=prompt_at, **extra)
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
                   recipe_loader=lambda site: _bound_recipe(conn, site, own.get("orders_page_url")),
                   orders_page_url=own.get("orders_page_url"), on_progress=found_so_far,
                   block_media=block_media)
    with jobs.owned(conn, job.id, job.worker_id):  # nothing is written for a job that is no longer ours
        n = store.upsert_orders(conn, job.user_id, db_site, orders, sub_platform=sub_platform)
        total = conn.execute(
            "SELECT coalesce(sum(total_amount), 0) FROM orders WHERE user_id=%s AND site=%s AND order_id = ANY(%s) "
            "AND coalesce(status, '') <> ALL(%s)",
            (job.user_id, db_site, [o["order_id"] for o in orders], list(_NOT_COUNTED))).fetchone()[0]
        store.set_connection(conn, job.user_id, db_site, status="connected",
                             last_synced_at=datetime.now(timezone.utc))
    jobs.set_progress(conn, job.id, stage="synced", orders_found=n, amount_found=round(float(total), 2))
    return n, float(total)


def _bound_recipe(conn, site: str, orders_page_url: str | None) -> dict | None:
    """The site's orders recipe with a templated one (Zomato) bound to this user's own orders page. Raises
    recipes.TemplateError when the page is unknown; run_job turns that into needs_login (a reconnect re-captures it)."""
    recipe = store.get_recipe(conn, site)
    return recipes.bind(recipe, orders_page_url) if recipe else recipe


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


PRICE_NO_LEARNER_BODY = "Its price recipe stopped working and no admin account is connected to re-learn it."
PRICE_RELEARN_FAILED_BODY = ("Its price recipe stopped working and the automatic re-learn did not publish a new one. "
                             "Review it in the admin view.")
# learn kind -> the recipe kinds it publishes
LEARN_RECIPE_KINDS = {"orders": ("orders",), "search": ("search",), "food": ("restaurants", "menu")}


def _pause_and_alert(conn, s: Settings, site: str, note: str, body: str, kind: str = "orders") -> None:
    """Pause the site's recipe(s) of this learn kind (orders, search or food) and email every admin (once per
    site and kind per IST day)."""
    try:
        for recipe_kind in LEARN_RECIPE_KINDS[kind]:
            recipe_admin.pause_site(conn, site, recipe_kind, note, reason="auto")
        if kind == "orders":
            title, key = f"{_app(site)} sync paused", f"site_paused:{site}:{usage.today(tz=s.app_tz)}"
        else:
            title, key = f"{_app(site)} prices paused", f"site_paused:{kind}:{site}:{usage.today(tz=s.app_tz)}"
        notify.alert_admins(conn, kind="site_paused", title=title, body=body, dedupe_key=key)
    except Exception as e:
        log.error("could not pause %s after drift (%s)", site, type(e).__name__)
        return
    _dispatch(conn, s)


def _is_auto_learn(job: jobs.Job) -> bool:
    return job.type == "learn" and bool((job.payload or {}).get("auto"))


def _learn_kind(job: jobs.Job) -> str:
    """What a learn job learns: the payload's kind ("orders" when absent or unknown)."""
    kind = (job.payload or {}).get("kind")
    return kind if kind in LEARN_RECIPE_KINDS else "orders"


def _pause_after_failed_relearn(conn, s: Settings, job: jobs.Job, error: str) -> None:
    """Ruling R8: an unattended (drift) learn that did not publish a recipe, however it ended, pauses the site
    and alerts the admins. A supervised learn never pauses."""
    if _is_auto_learn(job):
        kind = _learn_kind(job)
        _pause_and_alert(conn, s, job.site, f"drift re-learn failed: {error}"[:MAX_ERROR_LEN],
                         RELEARN_FAILED_BODY if kind == "orders" else PRICE_RELEARN_FAILED_BODY, kind)


def _requeue_stale(conn, s: Settings) -> int:
    """jobs.requeue_stale, with an unattended learn lost with its worker pausing its site like any failed one."""
    def on_lost(lost: list[jobs.Job]) -> None:
        for job in lost:
            _pause_after_failed_relearn(conn, s, job, "worker_lost")
    return jobs.requeue_stale(conn, s.stale_job_s, s.max_job_attempts, on_lost=on_lost)


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


def _on_search_drift(conn, s: Settings, job: jobs.Job, db_site: str, recipe_kind: str, note: str) -> None:
    """Ruling R11, as _on_drift for a price recipe: on the healthy -> unhealthy transition, enqueue an unattended
    learn of that kind ("search", or "food" for a restaurants / menu recipe) on a connected admin account, or,
    with none, pause the site's recipes of that kind and alert the admins."""
    kind = "search" if recipe_kind == "search" else "food"
    try:
        if not recipe_admin.mark_drift(conn, job.site, recipe_kind, note[:MAX_ERROR_LEN]):
            return
        learner = recipe_admin.admin_learner(conn, db_site)
    except Exception as e:
        log.error("drift handling for %s %s failed (%s)", job.site, kind, type(e).__name__)
        return
    if learner is None:
        return _pause_and_alert(conn, s, job.site, "drift: no admin account connected", PRICE_NO_LEARNER_BODY, kind)
    try:
        jobs.enqueue(conn, learner, "learn", job.site, {"reason": "drift", "auto": True, "kind": kind})
    except jobs.Conflict:
        pass
    except Exception as e:
        log.error("could not enqueue a %s re-learn for %s (%s)", kind, job.site, type(e).__name__)


def _record_usage(conn, job: jobs.Job, s: Settings, meter: metering.Meter, relay, seconds: float) -> None:
    """Add this job's proxy, AI and worker time to the user's usage for today (IST). Never fails the job."""
    try:
        with conn.transaction():
            # A deleted (or being deleted) account gets no usage row: the user row is held while we write.
            if conn.execute("SELECT 1 FROM auth.users WHERE id=%s FOR KEY SHARE", (job.user_id,)).fetchone() is None:
                return
            usage.record(conn, job.user_id, usage.today(tz=s.app_tz),
                         proxy_mb=(relay.bytes_total if relay is not None else 0) / 1e6,
                         ai_tokens=meter.ai_tokens(s.jev_tokens_per_call),
                         worker_seconds=seconds,
                         syncs=1 if job.type in ("connect", "sync") else 0)
    except Exception as e:
        log.warning("job %s: usage not recorded (%s)", job.id, type(e).__name__)


def _do_learn(job: jobs.Job, conn, s: Settings, profile_dir: Path, proxy_server: str | None,
              listen: Callable[[], object], fake: bool, save_profile: Callable[[], None]) -> dict:
    """Learn the site's orders recipe on the admin's account and publish or file it (recipe_admin.apply_learned).
    An auto (drift) learn never lets a new POST through; a supervised one asks the admin for each. The admin's
    profile is saved before anything is published, so a failed save never pauses a site whose recipe is live."""
    auto = bool((job.payload or {}).get("auto"))
    kind = _learn_kind(job)
    recipe_kinds = LEARN_RECIPE_KINDS[kind]
    jobs.set_progress(conn, job.id, stage="learning")
    if auto:
        approve = recipe_admin.deny_new_posts
    else:
        approve = recipe_admin.make_approver(listen(), conn, job.id, job.site, recipe_kinds[0], s.approval_timeout_s)
    preapproved = []
    for recipe_kind in recipe_kinds:  # what the site's current recipes already allow needs no new approval
        current = store.get_recipe(conn, job.site, recipe_kind) or {}
        preapproved += [e["url_regex"] for e in current.get("read_post_allowlist") or []
                        if isinstance(e, dict) and e.get("url_regex")]
    location = _location(conn, job.user_id)
    if kind == "orders":
        learned = {"orders": (fake_sites.learn if fake else sync.learn)(
            job.site, profile_dir=profile_dir, location=location, proxy_server=proxy_server, approve=approve,
            preapproved=preapproved, save=False)}
    elif fake:
        out = fake_sites.learn(job.site, approve, preapproved, kind=kind)
        learned = {"search": out} if kind == "search" else out
    elif kind == "search":
        learned = {"search": search_learn.learn_search(job.site, location=location, profile_dir=profile_dir,
                                                       proxy_server=proxy_server, approve=approve,
                                                       preapproved=preapproved, llm=_learn_llm)}
    else:
        learned = food_learn.learn_food(job.site, location=location, profile_dir=profile_dir,
                                        proxy_server=proxy_server, approve=approve, preapproved=preapproved)
    save_profile()
    approved = set(getattr(approve, "approved", ()))
    reason = "drift_relearn" if auto else "learned"
    results = [recipe_admin.apply_learned(conn, job.site, recipe_kind, learned[recipe_kind], approved, job.id,
                                          reason=reason) for recipe_kind in recipe_kinds]
    result = next((r for r in results if r["status"] == "pending"), results[-1])
    jobs.set_progress(conn, job.id, stage="learned", approval_id=result["approval_id"], status=result["status"],
                      version=result["version"])
    return result


def _learn_llm(prompt: str) -> dict:
    """The search learner's LLM: one JSON-object reply from llm.py (Claude Haiku, under its cost policy)."""
    resp = llm.create("search_learn", effort="medium", max_tokens=8000, messages=[{"role": "user", "content": prompt}])
    return llm.json_reply(resp)


JUDGE_PROMPT = ('Shopping-list line: "{text}"\nFor each product below, is it the same item the line asks for '
                '(same kind, brand if one is named, and size)? Reply with JSON only: {{"answers": [true or false, '
                'one per product, in order]}}.\n{products}')


def _judge():
    """The near-match judge for match.match_item: one cheap LLM call per uncertain item, listing at most 3
    product names. Any failure is "unsure" (None), which never accepts a product."""
    def judge(text: str, names: list[str]) -> list:
        try:
            listing = "\n".join(f"{i + 1}. {n}" for i, n in enumerate(names))
            resp = llm.create("match_judge", effort="low", max_tokens=400,
                              messages=[{"role": "user", "content": JUDGE_PROMPT.format(text=text, products=listing)}])
            answers = llm.json_reply(resp).get("answers")
            if isinstance(answers, list) and len(answers) == len(names):
                return [a if isinstance(a, bool) else None for a in answers]
        except Exception as e:
            log.warning("match judge failed (%s)", type(e).__name__)
        return [None] * len(names)
    return judge


def _refresh_addresses(job: jobs.Job, conn, db_site: str, profile_dir: Path, proxy_server: str | None,
                       fake: bool) -> None:
    """Best effort, after a successful login or sync: re-read the user's saved addresses on this app (a GET of
    the list the app already has; never a change to it) and replace the stored ones. Skipped when no admin has
    learned the site's `addresses` recipe. Any failure keeps the last good list and never fails the job; only
    the exception class and a count are logged, never an address."""
    try:
        if fake:
            rows = fake_sites.saved_addresses(db_site)
        else:
            recipe = addresses_store.recipe(conn, db_site)
            if not recipe:
                return
            rows = addresses.fetch_for_site(db_site, recipe, profile_dir=profile_dir,
                                            location=_location(conn, job.user_id), proxy_server=proxy_server)
        n = addresses_store.replace(conn, job.user_id, db_site, rows)
        log.info("job %s: %d saved addresses on %s", job.id, n, db_site)
    except Exception as e:
        log.warning("job %s: saved addresses not refreshed (%s)", job.id, type(e).__name__)


class _SearchEnd(Exception):
    """A search job that ends without a price: result is the app's cart_results status, error its short code."""

    def __init__(self, result: str, error: str | None = None):
        super().__init__(result, error)
        self.result, self.error = result, error


def _address_query(row: dict) -> str:
    """The address text the browser layer reads a city from ('society, area, city, state'). Saved addresses carry no
    state, so the country fills that slot."""
    return ", ".join(x for x in (row.get("line"), row.get("area"), row.get("city"), "India") if x)


def _search_location(conn, job: jobs.Job) -> dict | None:
    """Where this job searches: the app's own saved address (the job's address_id, else the one for the user's
    chosen place), as a browser location. None when the app has no usable saved address."""
    address_id = (job.payload or {}).get("address_id")
    row = (addresses_store.get(conn, job.user_id, job.site, str(address_id)) if address_id
           else addresses_store.saved_for(conn, job.user_id, job.site))
    if not row or row.get("lat") is None or row.get("lon") is None:
        return None
    return {"query": _address_query(row), "lat": row["lat"], "lon": row["lon"]}


def _search_recipe(conn, site: str, kind: str) -> dict:
    row = conn.execute("SELECT recipe, healthy, paused FROM recipes WHERE site=%s AND kind=%s", (site, kind)).fetchone()
    if row is None or not row[1] or row[2]:
        raise _SearchEnd("unavailable", "unavailable")
    return row[0]


def _do_search(job: jobs.Job, conn, s: Settings, profile_dir: Path, proxy_server: str | None,
               fake: bool = False) -> None:
    """Run one compare job on the user's own session and store what it finds. op "products" prices a grocery
    check on this app; "restaurants" fills the user's restaurant cache; "menu" fills the menu cache and, for a
    check, prices it. Raises _SearchEnd for an outcome without a price, browser.SessionExpired for a logged-out
    session and the search errors for run_job to turn into results. Never logs items, queries or addresses."""
    p = job.payload or {}
    op, request_id = p.get("op"), p.get("request_id")
    if op not in ("products", "restaurants", "menu"):
        raise _SearchEnd("failed", "bad_op")
    request = None
    if request_id:
        request = cart.get_request(conn, job.user_id, request_id)
        if request is None:
            raise _SearchEnd("failed", "request_gone")
    recipe = _search_recipe(conn, job.site, "search" if op == "products" else op)
    location = _search_location(conn, job)
    if location is None and not request_id:  # a lookup with no saved address falls back to the profile's place
        location = _location(conn, job.user_id)
    if not location:
        raise _SearchEnd("unserviceable", cart.NO_SAVED_ADDRESS)
    block_media = s.sync_block_media
    jobs.set_progress(conn, job.id, stage="searching")

    if op == "products":
        qs = [match.parse_query(i, str(it.get("text") or ""), it.get("qty") or 1)
              for i, it in enumerate(request["items"])]
        queries = list(dict.fromkeys(q.text for q in qs))
        if fake:
            found = fake_sites.search(job.site, queries)
        else:
            found = search.search_products(job.site, queries, location, recipe, profile_dir,
                                           proxy_server=proxy_server, block_media=block_media)
        judge = _judge() if s.match_judge and not fake else None
        by_query = {q: [x for r in rows if (x := match.from_raw(r))] for q, rows in found["products"].items()}
        matched = [match.match_item(q, by_query[q.text], judge) for q in qs if q.text in by_query]
        jobs.set_progress(conn, job.id, stage="matched", count=len(matched))
        cart.record_site_result(conn, request_id, job.site, "ok", matched,
                                meta={"fee_info": found.get("fee_info") or {}})
        return

    profile = store.get_profile(conn, job.user_id) or {}
    if op == "restaurants":
        query = str(p.get("query") or "")
        if fake:
            found = fake_sites.search_restaurants(job.site, query)
        else:
            found = food_search.search_restaurants(job.site, query, location, recipe, profile_dir,
                                                   proxy_server=proxy_server, block_media=block_media)
        if profile.get("lat") is not None and profile.get("lon") is not None:
            food_cart.put_restaurants(conn, job.user_id, job.site,
                                      food_cart.geo_key(profile["lat"], profile["lon"]), query,
                                      found["restaurants"])
        jobs.set_progress(conn, job.id, stage="found", count=len(found["restaurants"]))
        return

    restaurant_id = str(p.get("restaurant_id") or "")
    if fake:
        menu = fake_sites.fetch_menu(job.site, restaurant_id)
    else:
        menu = food_search.fetch_menu(job.site, restaurant_id, location, recipe, profile_dir,
                                      proxy_server=proxy_server, url=p.get("url"), block_media=block_media)
    food_cart.put_menu(conn, job.user_id, job.site, restaurant_id, menu)
    jobs.set_progress(conn, job.id, stage="found", count=len(menu.get("dishes") or []))
    if request is not None:
        listing = ((request.get("restaurant") or {}).get("listings") or {}).get(job.site) or {}
        food_cart.record_menu(conn, request_id, job.site,
                              {**menu, "fetched_at": datetime.now(timezone.utc).isoformat()}, request["items"],
                              listing.get("distance_km"))


def _fill_id(job: jobs.Job) -> str | None:
    v = (job.payload or {}).get("fill_id")
    return str(v) if v and fills._is_uuid(v) else None


def _fill_precheck(job: jobs.Job, conn, s: Settings, db_site: str):
    """The fill job's gates. Returns (fill, cart recipe, search recipe), or None after finishing the job: a missing
    fill fails the job (fill_gone); an unusable recipe or connection fails the fill (the job itself is done)."""
    f = fills.get_fill(conn, _fill_id(job)) if _fill_id(job) else None
    if f is None:
        jobs.finish(conn, job.id, error="fill_gone", worker_id=job.worker_id)
        return None
    recipe = store.get_recipe(conn, job.site, "cart")
    if recipe is None or not fills.cart_recipe_ready(conn, job.site):
        fills.fail_fill(conn, f["id"], "cart_unavailable", s)
    elif _status(conn, job.user_id, db_site) != "connected":
        fills.fail_fill(conn, f["id"], "needs_login", s)
    else:
        return f, recipe, store.get_recipe(conn, job.site, "search")
    jobs.finish(conn, job.id, worker_id=job.worker_id)
    return None


def _do_fill(job: jobs.Job, conn, s: Settings, ctx, profile_dir: Path, proxy_server: str | None,
             listen: Callable[[], object], fake: bool, save_profile: Callable[[], None]) -> None:
    """Add the fill's plan to the user's cart on their own session (add-only, through the CartGuard). Questions
    reach the app on the job's progress; an unanswered one is a skip. Never logs in, never logs cart contents."""
    f, recipe, search_recipe = ctx
    fid, site = f["id"], job.site
    fills.mark_started(conn, fid)
    jobs.set_progress(conn, job.id, stage="starting", done=0, total=len(f["plan"]))
    prompt = make_prompt(listen(), conn, job.id, s.otp_timeout_s)
    reported = 0

    def ask(question, choices):
        try:
            return prompt(question, "question", choices)
        except sync.OtpTimeout:
            return None

    def on_item(item: dict) -> None:
        nonlocal reported
        reported += 1
        fills.record_item(conn, fid, item)
        jobs.set_progress(conn, job.id, stage="adding", done=reported, total=len(f["plan"]))

    kw = dict(profile_dir=profile_dir, ask=ask, on_item=on_item,
              on_stage=lambda st: jobs.set_progress(conn, job.id, stage=st), proxy_server=proxy_server,
              confirm_location=f.get("confirm_location"), max_location_m=s.cart_location_max_m)
    if fake:
        outcome = fake_sites.fill_cart(site, f["plan"], f["priced_location"], recipe, search_recipe,
                                       user_id=job.user_id, state_dir=os.environ.get("FAKE_STATE_DIR"), **kw)
        open_url = f"https://fake.{site}.test/cart"
    else:
        outcome = cart_fill.fill_cart(site, f["plan"], f["priced_location"], recipe, search_recipe, **kw)
        open_url = OPEN_APP_URLS[site]
    summary = fills.finish_fill(conn, fid, outcome, open_url, s)
    save_profile()
    app, final = fill_summary.app_name(site), (outcome.get("status") or "")
    if final in ("done", "partial"):
        _notify_user(conn, s, kind="cart_filled", title=summary.get("title") or f"Your {app} cart is filled",
                     body=f"Open {app} to review and pay.", url=f"/cart/{f['request_id']}?fill={fid}",
                     user_id=job.user_id, dedupe_key=f"cart_filled:{fid}")
    elif final == "needs_confirmation":
        _notify_user(conn, s, kind="cart_reprice", title=f"Check {app} prices",
                     body=((summary.get("reprice") or {}).get("total_line") or f"{app} prices changed."),
                     url=f"/cart/{f['request_id']}?fill={fid}", user_id=job.user_id,
                     dedupe_key=f"cart_reprice:{fid}")


def _on_cart_drift(conn, s: Settings, site: str, note: str) -> None:
    """On the healthy -> unhealthy transition of the cart recipe: pause cart filling and tell the admins (once a day)."""
    try:
        if not recipe_admin.mark_drift(conn, site, "cart", note[:MAX_ERROR_LEN]):
            return
        recipe_admin.pause_site(conn, site, "cart", note[:MAX_ERROR_LEN], reason="auto")
        notify.alert_admins(conn, kind="site_paused", title=f"{_app(site)} cart filling paused",
                            body="Its cart recipe stopped working. Learn it again from the admin view and approve it.",
                            dedupe_key=f"site_paused:cart:{site}:{usage.today(tz=s.app_tz)}")
    except Exception as e:
        log.error("cart drift handling for %s failed (%s)", site, type(e).__name__)
        return
    _dispatch(conn, s)


def _cart_learn_precheck(conn, job: jobs.Job, db_site: str) -> str | None:
    """Why a cart learn cannot start, or None: the admin's connection, the site's search recipe and an address."""
    if _status(conn, job.user_id, db_site) != "connected":
        return "needs_login"
    if not store.get_recipe(conn, job.site, "search"):
        return "no_search_recipe"
    if not _location(conn, job.user_id):
        return "no_address"
    return None


def _do_learn_cart(job: jobs.Job, conn, s: Settings, profile_dir: Path, proxy_server: str | None,
                   listen: Callable[[], object], fake: bool, save_profile: Callable[[], None]) -> dict:
    """Learn the site's cart recipe on the admin's account. Every new endpoint needs the admin's live approval and
    the recipe is always filed as pending (apply_learned). The learn adds items to the admin's real cart."""
    import cart_learn
    jobs.set_progress(conn, job.id, stage="learning")
    approve = recipe_admin.make_approver(listen(), conn, job.id, job.site, "cart", s.approval_timeout_s)
    current = store.get_recipe(conn, job.site, "cart") or {}
    preapproved = [dict(e, op=e.get("op") or op)
                   for op, key in (("cart", "cart_allowlist"), ("location", "location_allowlist"),
                                   ("read", "read_post_allowlist"))
                   for e in current.get(key) or [] if isinstance(e, dict) and e.get("url_regex")]
    if fake:
        learned = fake_sites.learn(job.site, approve=approve, preapproved=preapproved, kind="cart")
    else:
        learned = cart_learn.learn_cart(job.site, profile_dir=profile_dir, location=_location(conn, job.user_id),
                                        search_recipe=store.get_recipe(conn, job.site, "search"), approve=approve,
                                        preapproved=preapproved, proxy_server=proxy_server)
    save_profile()
    result = recipe_admin.apply_learned(conn, job.site, "cart", learned["recipe"], set(approve.approved), job.id,
                                        reason="learned")
    app = _app(job.site)
    jobs.set_progress(conn, job.id, stage="learned", status=result["status"], approval_id=result["approval_id"],
                      note=f"Learning added 2 × {learned['probe_item']} to your {app} cart. "
                           f"Remove it in {app} if you don't want it.")
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
    db_site, sub_platform = sites.db_site(job.site), cfg.get("sub_platform")
    phone = (job.payload or {}).get("phone")
    auto_learn = job.type == "learn" and bool((job.payload or {}).get("auto"))
    profile_name = sites.profile_for(job.site)
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

    def fail(error: str, status: str | None = None, touch_connection: bool = True,
             interrupted: bool = False) -> None:
        """Finish the job as failed. touch_connection=False leaves the connection row (and its "logged you out"
        notification) alone: a learn that never started on the admin's account is not about their session."""
        if job.type == "search":
            return end_search("needs_login" if error == "needs_login" else "failed", error=error)
        if job.type == "fill_cart" and _fill_id(job):  # the fill row never outlives its job unfinished
            try:
                if interrupted:  # a started fill may have adds in the app's cart: kept, never refunded
                    fills.interrupt_fill(conn, _fill_id(job), error, s)
                else:
                    fills.fail_fill(conn, _fill_id(job), error, s)
            except Exception as e:
                log.error("job %s: fill not closed (%s)", job.id, type(e).__name__)
        if touch_connection:
            before = _status(conn, job.user_id, db_site)
            if status is None and before == "syncing":
                status = "error"
            if status == "needs_login":
                _expire_session()
            elif status:
                store.set_connection(conn, job.user_id, db_site, status=status)
        jobs.finish(conn, job.id, error=error, worker_id=job.worker_id)
        _pause_after_failed_relearn(conn, s, job, error)

    def end_search(result: str, error: str | None = None) -> None:
        """Finish a search job without a price: file the app's result for its check, finish the job as failed,
        and, for a logged-out session, mark the connection and tell the user once. A search never changes the
        connection's status otherwise."""
        request_id = (job.payload or {}).get("request_id")
        if request_id:
            try:
                cart.record_site_result(conn, request_id, job.site, result, error=error)
            except cart.NotFound:
                pass
            except Exception as e:
                log.warning("job %s: result not recorded (%s)", job.id, type(e).__name__)
        if result == "needs_login":
            _expire_session()
        jobs.finish(conn, job.id, error=error or result, worker_id=job.worker_id)

    def _expire_session() -> None:
        """The one needs_login path: flip the connection once per episode, tell the user once, then dispatch."""
        try:
            session.mark_needs_login(conn, job.user_id, db_site)
        except Exception as e:
            log.error("job %s: session expiry not recorded (%s)", job.id, type(e).__name__)
            return
        _dispatch(conn, s)

    def throttled_out(e: browser.SiteThrottled) -> None:
        """R5: the site rate-limited us. Cool the site down; a sync waits and retries, a search reports blocked,
        any other job fails site_busy."""
        until = site_health.record_throttle(conn, job.site, e.kind, e.retry_after_s, s)
        if job.type == "sync":
            if jobs.requeue(conn, job.id, job.worker_id, run_after=until, max_attempts=s.max_job_attempts):
                jobs.set_progress(conn, job.id, stage="cooling_down", until=until.isoformat())
            return
        if job.type == "search":
            return end_search("failed", "blocked")
        fail("site_busy")

    def save_profile() -> None:
        if fake:
            return
        try:
            with jobs.owned(conn, job.id, job.worker_id):  # a job that is no longer ours saves nothing
                key = vault.save_profile(vault_store, s.profile_master_key, job.user_id, profile_name, profile_dir)
                store.set_connection(conn, job.user_id, db_site, profile_blob_key=key)
        except jobs.JobGone:
            log.info("job %s no longer ours; profile not saved", job.id)

    def listen():
        nonlocal listen_conn
        listen_conn = _connect(s.database_url)
        return listen_conn

    listen_conn = None
    heartbeat.start()
    try:
        if job.type == "learn" and not recipe_admin.is_admin(conn, job.user_id):
            return fail("not_admin")
        if job.type == "search" and site_health.cooldown_until(conn, job.site) is not None:
            return end_search("unavailable", "cooling_down")  # no browser while the site is cooling down
        is_cart_learn = job.type == "learn" and (job.payload or {}).get("kind") == "cart"
        if is_cart_learn and (err := _cart_learn_precheck(conn, job, db_site)):
            return fail(err, touch_connection=False)
        fill_ctx = None
        if job.type == "fill_cart":
            fill_ctx = _fill_precheck(job, conn, s, db_site)
            if fill_ctx is None:
                return
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
            if job.type in ("sync", "search", "fill_cart"):
                return fail("needs_login", "needs_login")
        if job.type == "fill_cart" and not restored and not fake:
            return fail("needs_login", "needs_login")  # a fill never logs in: no stored session, no fill
        if job.type == "search" and not restored and not fake:
            return fail("needs_login", "needs_login")  # compares run on the user's own session: none stored
        if job.type == "learn" and not restored and not fake:
            # A learn runs on the admin's logged-in account; with none stored the job fails, but the admin's
            # connection row is neither written nor reported as logged out.
            return fail("needs_login", touch_connection=False)
        profile_dir.mkdir(parents=True, exist_ok=True)
        if not fake:  # restored or fresh, the directory is stamped with its owner before Chrome and the save
            vault.stamp(profile_dir, job.user_id, profile_name)
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

            if job.type == "fill_cart":
                _do_fill(job, conn, s, fill_ctx, profile_dir, proxy_server, listen, fake, save_profile)
                jobs.finish(conn, job.id, worker_id=job.worker_id)
                return

            if is_cart_learn:
                _do_learn_cart(job, conn, s, profile_dir, proxy_server, listen, fake, save_profile)
                jobs.finish(conn, job.id, worker_id=job.worker_id)
                return

            if job.type == "learn":
                result = _do_learn(job, conn, s, profile_dir, proxy_server, listen, fake, save_profile)
                jobs.finish(conn, job.id, worker_id=job.worker_id)
                if auto_learn and result["status"] == "pending":
                    kind = _learn_kind(job)
                    _pause_and_alert(conn, s, job.site, "drift re-learn needs admin approval",
                                     RELEARN_FAILED_BODY if kind == "orders" else PRICE_RELEARN_FAILED_BODY, kind)
                return

            if job.type == "search":
                _do_search(job, conn, s, profile_dir, proxy_server, fake=fake)
                site_health.record_success(conn, job.site)
                save_profile()  # the session is the user's own; a good run keeps what the site refreshed
                jobs.finish(conn, job.id, worker_id=job.worker_id)
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
                with jobs.owned(conn, job.id, job.worker_id):
                    store.set_connection(conn, job.user_id, db_site, status="connected", needs_login_at=None)
                save_profile()  # a good login is kept even if the first sync below fails
            n, total = _do_sync(job, conn, db_site, sub_platform, profile_dir, proxy_server,
                                fetch=fake_sites.fetch_orders if fake else sync.fetch_orders,
                                block_media=job.type == "sync" and s.sync_block_media)  # R12: user syncs only
            site_health.record_success(conn, job.site)
            save_profile()
            _refresh_addresses(job, conn, db_site, profile_dir, proxy_server, fake)
            _categorize(conn)
        jobs.finish(conn, job.id, worker_id=job.worker_id)
        if job.type == "connect":
            _notify_user(conn, s, kind="first_sync_done", title=f"{_app(job.site)} is connected",
                         body=f"{n} orders · {notify.format_inr(total)} found", url="/spend",
                         user_id=job.user_id, dedupe_key=f"first_sync:{job.id}")
    except jobs.JobGone:
        log.info("job %s no longer ours; ending quietly", job.id)
    except (browser.SessionExpired, recipes.TemplateError):  # TemplateError: the user's orders page is unknown
        fail("needs_login", "needs_login")
    except browser.SiteThrottled as e:
        throttled_out(e)
    except _SearchEnd as e:
        end_search(e.result, e.error)
    except search.Unserviceable:
        end_search("unserviceable")
    except search.SearchDrift as e:
        end_search("failed", "recipe_drift")
        op = (job.payload or {}).get("op")
        _on_search_drift(conn, s, job, db_site, "search" if op == "products" else str(op),
                         (str(e).splitlines() or [""])[0])
    except sync.OtpTimeout as e:  # only an unanswered prompt; any other TimeoutError is a normal error below
        fail(f"{e.kind}_timeout", "error")
    except cart_fill.CartDrift as e:
        fail("recipe_drift")
        _on_cart_drift(conn, s, job.site, (str(e).splitlines() or [""])[0] or "cart drift")
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
        fail(msg, interrupted=True)
    finally:
        stop_hb.set()
        heartbeat.join(5)
        _close(listen_conn)
        shutil.rmtree(profile_dir, ignore_errors=True)
        shutil.rmtree(runs_dir, ignore_errors=True)
        try:
            metered.close()
        except Exception as e:  # metering must never fail or escape a job
            log.warning("job %s: meter close failed (%s)", job.id, type(e).__name__)
        if job.type == "search" and (job.payload or {}).get("request_id"):
            try:  # the check closes as soon as every app has an answer
                cart.finalize_if_complete(conn, job.payload["request_id"], s)
            except Exception as e:
                log.warning("job %s: check not finalized (%s)", job.id, type(e).__name__)
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


def _abort_on_shutdown(job: jobs.Job, conn, worker_id: str, s: Settings | None = None) -> bool:
    """Abort a job this worker is still running because the worker is going away. A connect job cannot be
    resumed (its OTP exchange dies with us): it fails with "worker_shutdown" and a connection left "syncing"
    becomes "error"; so does a learn job (its approval exchange dies with us). A sync job goes straight back to
    the queue for another worker. Both updates only touch a job still running under worker_id. Returns whether
    the job was aborted (False: already finished, or the database could not be reached; then the stale-job
    requeue deals with it). An unattended learn aborted this way pauses its site and alerts the admins (s, the
    worker's settings, default read from the environment)."""
    try:
        if job.type in ("connect", "learn", "fill_cart"):
            if not jobs.finish(conn, job.id, error="worker_shutdown", worker_id=worker_id):
                return False
            db_site = browser.SITES[job.site].get("db_site", job.site)
            if _status(conn, job.user_id, db_site) == "syncing":
                store.set_connection(conn, job.user_id, db_site, status="error")
            if _is_auto_learn(job):
                _pause_after_failed_relearn(conn, s or settings(), job, "worker_shutdown")
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

    def finish(self) -> None:
        """The main loop has ended: nothing may be interrupted after this. An interrupt the watchdog raised just
        as the loop ended is absorbed here."""
        try:
            with self._lock:
                self.stopped.set()
        except KeyboardInterrupt:
            self.stopped.set()
            if not self.aborted:
                raise

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
        if job.type == "sync":
            # A sync is requeued by the main thread once run_job has unwound (Chrome has exited by then), so a
            # new worker never starts a second Chrome on the same profile while this one is still closing.
            log.warning("job %s still running after DRAIN_S (%ss): interrupting", job.id, self.s.drain_s)
        else:
            conn = None
            try:
                conn = psycopg.connect(self.s.database_url, autocommit=True,
                                       connect_timeout=HEARTBEAT_CONNECT_TIMEOUT_S)
                done = _abort_on_shutdown(job, conn, self.worker_id, self.s)
            except Exception as e:
                log.error("could not reach the database to abort job %s (%s)", job.id, type(e).__name__)
                done = False
            finally:
                _close(conn)
            log.warning("job %s still running after DRAIN_S (%ss): %s", job.id, self.s.drain_s,
                        "aborted" if done else "left for the stale-job requeue")
        self.aborted = True
        with self._lock:  # only while this job is still the main thread's and the main loop has not ended
            if self._job is not job or self.stopped.is_set():
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
    check_hosted(s, "worker")
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
                _requeue_stale(conn, s)
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
                    # Ctrl-C aborts here; so does a drain abort of a sync (the watchdog only interrupted: Chrome has
                    # exited by now, so the requeue cannot start a second Chrome on the same profile).
                    if not drain.aborted or job.type == "sync":
                        _abort_on_shutdown(job, conn, worker_id, s)
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
        drain.finish()
        _close(conn)
        if installed:
            signal.signal(signal.SIGTERM, old_handler if old_handler is not None else signal.SIG_DFL)
    log.info("worker %s stopping", worker_id)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()

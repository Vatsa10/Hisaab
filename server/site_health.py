"""Per-site rate-limit backoff (R5) and the error budget that pauses a failing site (R6).

Backoff is kept per Chrome-profile group (swiggy and instamart share a host), so one throttle cools both.
"""
import math
import random
from datetime import datetime, timedelta

from fastapi import HTTPException

from server import notify, recipe_admin, sites, usage
from server.config import SITE_NAMES, Settings


class SiteCooling(Exception):
    def __init__(self, site: str, until: datetime, message: str, retry_after_s: int):
        super().__init__(message)
        self.site, self.until, self.message, self.retry_after_s = site, until, message, retry_after_s


def _app(site: str) -> str:
    return SITE_NAMES.get(site, site.title())


# ---------------------------------------------------------------- backoff

def record_throttle(conn, site: str, kind: str, retry_after_s: float | None, s: Settings,
                    now: datetime | None = None, rand=random.random) -> datetime:
    """A 429 or challenge page: raise the group's level, set `until` (doubling, capped, with a little jitter,
    never earlier than the server's Retry-After) and return it."""
    now = usage.as_utc(now)
    group = list(sites.same_profile(site))
    with conn.transaction():
        # Every row of a host group is written together, so they agree; take the most recently updated one
        # (and lock it) as the group's current state.
        row = conn.execute("SELECT level, updated_at, events FROM site_backoff WHERE site = ANY(%s) "
                           "ORDER BY updated_at DESC LIMIT 1 FOR UPDATE", (group,)).fetchone()
        level, events = 0, 0
        if row:
            level, events = row[0], row[2]
            if row[1] < now - timedelta(hours=s.backoff_reset_h):
                level = 0
        level += 1
        delay = min(s.backoff_base_s * 2 ** min(level - 1, 30), s.backoff_max_s)
        delay = min(delay + delay * 0.2 * rand(), s.backoff_max_s)
        delay = max(delay, min(float(retry_after_s or 0), s.backoff_max_s))
        until = now + timedelta(seconds=delay)
        for g in group:
            conn.execute(
                "INSERT INTO site_backoff (site, level, until, last_kind, events, updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (site) DO UPDATE SET level=excluded.level, "
                "until=excluded.until, last_kind=excluded.last_kind, events=excluded.events, "
                "updated_at=excluded.updated_at",
                (g, level, until, kind, events + 1, now))
    return until


def record_success(conn, site: str) -> None:
    conn.execute("UPDATE site_backoff SET level=0, until=NULL WHERE site = ANY(%s) AND level > 0",
                 (list(sites.same_profile(site)),))


def cooldown_until(conn, site: str, now: datetime | None = None) -> datetime | None:
    row = conn.execute("SELECT max(until) FROM site_backoff WHERE site = ANY(%s) AND until > %s",
                       (list(sites.same_profile(site)), usage.as_utc(now))).fetchone()
    return row[0] if row else None


def raise_if_cooling(conn, site: str, now: datetime | None = None) -> None:
    now = usage.as_utc(now)
    until = cooldown_until(conn, site, now)
    if until is None:
        return
    minutes = max(1, math.ceil((until - now).total_seconds() / 60))
    raise SiteCooling(site, until, f"{_app(site)} is limiting requests right now. "
                                   f"Try again in about {minutes} minutes.",
                      max(1, math.ceil((until - now).total_seconds())))


def to_http(e: SiteCooling) -> HTTPException:
    return HTTPException(503, detail=e.message, headers={"Retry-After": str(int(e.retry_after_s))})


# ---------------------------------------------------------------- error budget

# Failures the user or the platform caused, not the site: never counted for or against the budget.
NOT_SITE_FAULTS = ("needs_login", "worker_lost", "worker_shutdown", "site_busy", "too_many_attempts",
                   "login_failed")

_ORDERS_SQL = """
SELECT j.site, count(*)::int AS total, count(*) FILTER (WHERE j.status = 'failed')::int AS failed
FROM jobs j LEFT JOIN recipes r ON r.site = j.site AND r.kind = 'orders'
WHERE j.type = 'sync' AND j.status IN ('done', 'failed')
  AND j.finished_at >= greatest(%(since)s::timestamptz, coalesce(r.paused_changed_at, '-infinity'))
  AND j.finished_at <= %(now)s
  AND NOT (j.status = 'failed' AND (j.error = ANY(%(ignored)s)))
GROUP BY j.site"""

_RESULTS_SQL = """
SELECT x.site, x.kind, count(*)::int AS total, count(*) FILTER (WHERE x.status = 'failed')::int AS failed
FROM (SELECT c.site, c.status, c.priced_at,
             CASE WHEN q.mode = 'food' AND c.site IN ('swiggy', 'zomato') THEN 'menu' ELSE 'search' END AS kind
      FROM cart_results c JOIN cart_requests q ON q.id = c.request_id
      WHERE c.status IN ('ok', 'failed')) x
LEFT JOIN recipes r ON r.site = x.site AND r.kind = x.kind
WHERE x.priced_at >= greatest(%(since)s::timestamptz, coalesce(r.paused_changed_at, '-infinity'))
  AND x.priced_at <= %(now)s
GROUP BY x.site, x.kind"""


def failure_stats(conn, now: datetime, window_h: int) -> list[dict]:
    now = usage.as_utc(now)
    p = {"now": now, "since": now - timedelta(hours=window_h), "ignored": list(NOT_SITE_FAULTS)}
    rows = [(site, "orders", total, failed) for site, total, failed in conn.execute(_ORDERS_SQL, p).fetchall()]
    rows += conn.execute(_RESULTS_SQL, p).fetchall()
    return [{"site": site, "kind": kind, "total": total, "failed": failed, "pct": failed * 100 // total}
            for site, kind, total, failed in sorted(rows) if total]


def check_error_budget(conn, s: Settings, now: datetime) -> list[tuple[str, str]]:
    """Pause every (site, kind) whose failure rate in the window is over budget, and alert the admins once a day.
    Only sites with an unpaused recipe row are touched."""
    now = usage.as_utc(now)
    h = s.error_budget_window_h
    paused = []
    for st in failure_stats(conn, now, h):
        site, kind, total, failed = st["site"], st["kind"], st["total"], st["failed"]
        if total < s.error_budget_min_jobs or failed * 100 <= s.error_budget_max_failure_pct * total:
            continue
        row = conn.execute("SELECT paused FROM recipes WHERE site=%s AND kind=%s", (site, kind)).fetchone()
        if row is None or row[0]:
            continue
        what = "syncs" if kind == "orders" else "price checks"
        note = f"Paused automatically: {failed} of {total} {what} failed in the last {h} hours."
        recipe_admin.pause_site(conn, site, kind, note)
        notify.alert_admins(
            conn, kind="error_budget",
            title=f"{_app(site)} {'sync' if kind == 'orders' else 'prices'} paused: too many failures",
            body=note + " Check it in the admin view, then resume it.",
            dedupe_key=f"error_budget:{site}:{kind}:{usage.today(now, s.app_tz)}")
        paused.append((site, kind))
    return paused

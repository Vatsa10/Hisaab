"""Daily per-user limits (manual refresh per app, cart checks) and the budget pause, counted on IST days."""
from datetime import date, datetime, timezone

from fastapi import HTTPException

from server import budget, usage
from server.config import SITE_NAMES, Settings


class LimitExceeded(Exception):
    def __init__(self, kind: str, limit: int, retry_after_s: int, message: str):
        super().__init__(message)
        self.kind, self.limit, self.retry_after_s, self.message = kind, limit, retry_after_s, message


class BudgetPaused(Exception):
    def __init__(self, kind: str, resumes_on: date, message: str):
        super().__init__(message)
        self.kind, self.resumes_on, self.message = kind, resumes_on, message


def seconds_until_reset(now: datetime | None, tz: str) -> int:
    """Seconds until the next local midnight (at least 1)."""
    _, end = usage.day_bounds(now, tz)
    now = now or datetime.now(timezone.utc)
    return max(1, int((end - now).total_seconds()))


def refresh_left(conn, user_id: str, site: str, now: datetime | None, s: Settings) -> int:
    """Manual sync jobs enqueued today (IST) for this user and site count; scheduled ones never do (R4)."""
    start, end = usage.day_bounds(now, s.app_tz)
    used = conn.execute(
        "select count(*) from jobs where user_id = %s and site = %s and type = 'sync' "
        "and payload->>'trigger' = 'manual' and created_at >= %s and created_at < %s",
        (user_id, site, start, end)).fetchone()[0]
    return max(0, s.refresh_per_app_per_day - used)


def check_refresh(conn, user_id: str, site: str, now: datetime | None, s: Settings) -> None:
    if refresh_left(conn, user_id, site, now, s) > 0:
        return
    name = SITE_NAMES.get(site, site.title())
    raise LimitExceeded("refresh", s.refresh_per_app_per_day, seconds_until_reset(now, s.app_tz),
                        f"You can refresh {name} {s.refresh_per_app_per_day} times a day. Try again tomorrow.")


def cart_checks_left(conn, user_id: str, now: datetime | None, s: Settings) -> int:
    row = conn.execute("select cart_checks from usage where user_id = %s and day = %s",
                       (user_id, usage.today(now, s.app_tz))).fetchone()
    return max(0, s.cart_checks_per_day - (row[0] if row else 0))


def _first_of_next_month(now: datetime | None, tz: str) -> date:
    d = usage.today(now, tz)
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def consume_cart_check(conn, user_id: str, now: datetime | None, s: Settings) -> int:
    """Use one cart check atomically; returns how many are left today."""
    if budget.is_paused(conn, now, s.app_tz):
        resumes = _first_of_next_month(now, s.app_tz)
        raise BudgetPaused("cart", resumes, f"Cart checks are paused until {resumes.day} {resumes:%B}.")
    limit = s.cart_checks_per_day
    row = None
    if limit > 0:
        row = conn.execute(
            "insert into usage (user_id, day, cart_checks) values (%s, %s, 1) "
            "on conflict (user_id, day) do update set cart_checks = usage.cart_checks + 1 "
            "where usage.cart_checks < %s returning cart_checks",
            (user_id, usage.today(now, s.app_tz), limit)).fetchone()
    if row is None:
        raise LimitExceeded("cart", limit, seconds_until_reset(now, s.app_tz),
                            f"You can check {limit} carts a day. Try again tomorrow.")
    return limit - row[0]


def to_http(e: LimitExceeded | BudgetPaused) -> HTTPException:
    if isinstance(e, LimitExceeded):
        return HTTPException(status_code=429, detail=e.message, headers={"Retry-After": str(e.retry_after_s)})
    return HTTPException(status_code=503, detail=e.message)

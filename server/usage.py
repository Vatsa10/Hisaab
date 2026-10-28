"""Per-user, per-IST-day usage counters, and the day/month helpers every limit and budget check shares."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Kolkata"


def _local(now: datetime | None, tz: str) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(ZoneInfo(tz))


def today(now: datetime | None = None, tz: str = DEFAULT_TZ) -> date:
    return _local(now, tz).date()


def month_of(now: datetime | None = None, tz: str = DEFAULT_TZ) -> str:
    return _local(now, tz).strftime("%Y-%m")


def day_bounds(now: datetime | None = None, tz: str = DEFAULT_TZ) -> tuple[datetime, datetime]:
    """UTC start (inclusive) and end (exclusive) of the local day that contains `now`."""
    zone = ZoneInfo(tz)
    d = today(now, tz)
    start = datetime(d.year, d.month, d.day, tzinfo=zone)
    end = datetime(*(d + timedelta(days=1)).timetuple()[:3], tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def record(conn, user_id: str, day: date, *, proxy_mb: float = 0.0, ai_tokens: int = 0,
           worker_seconds: float = 0.0, syncs: int = 0, cart_checks: int = 0) -> None:
    """Add to the user's counters for `day` (creating the row on first use)."""
    conn.execute(
        "insert into usage (user_id, day, proxy_mb, ai_tokens, worker_seconds, syncs, cart_checks) "
        "values (%s, %s, %s, %s, %s, %s, %s) "
        "on conflict (user_id, day) do update set "
        "proxy_mb = usage.proxy_mb + excluded.proxy_mb, "
        "ai_tokens = usage.ai_tokens + excluded.ai_tokens, "
        "worker_seconds = usage.worker_seconds + excluded.worker_seconds, "
        "syncs = usage.syncs + excluded.syncs, "
        "cart_checks = usage.cart_checks + excluded.cart_checks",
        (user_id, day, proxy_mb, ai_tokens, worker_seconds, syncs, cart_checks))

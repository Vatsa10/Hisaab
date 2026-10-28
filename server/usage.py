"""Per-user, per-IST-day usage counters, and the day/month helpers every limit and budget check shares."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from server import config


def as_utc(now: datetime | None) -> datetime:
    """`now` (default: the current time) as an aware datetime; a naive one is taken as UTC."""
    now = now or datetime.now(timezone.utc)
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now


def _zone(tz: str | None) -> ZoneInfo:
    """tz=None means APP_TZ from config (the single source of the zone)."""
    return ZoneInfo(tz or config.settings().app_tz)


def _local(now: datetime | None, tz: str | None) -> datetime:
    return as_utc(now).astimezone(_zone(tz))


def today(now: datetime | None = None, tz: str | None = None) -> date:
    return _local(now, tz).date()


def month_of(now: datetime | None = None, tz: str | None = None) -> str:
    return _local(now, tz).strftime("%Y-%m")


def day_bounds(now: datetime | None = None, tz: str | None = None) -> tuple[datetime, datetime]:
    """UTC start (inclusive) and end (exclusive) of the local day that contains `now`."""
    zone = _zone(tz)
    d = today(now, tz)
    start = datetime(d.year, d.month, d.day, tzinfo=zone)
    end = datetime(*(d + timedelta(days=1)).timetuple()[:3], tzinfo=zone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def record(conn, user_id: str, day: date, *, proxy_mb: float = 0.0, ai_tokens: int = 0,
           worker_seconds: float = 0.0, syncs: int = 0, cart_checks: int = 0, cart_fills: int = 0) -> None:
    """Add to the user's counters for `day` (creating the row on first use)."""
    conn.execute(
        "insert into usage (user_id, day, proxy_mb, ai_tokens, worker_seconds, syncs, cart_checks, cart_fills) "
        "values (%s, %s, %s, %s, %s, %s, %s, %s) "
        "on conflict (user_id, day) do update set "
        "proxy_mb = usage.proxy_mb + excluded.proxy_mb, "
        "ai_tokens = usage.ai_tokens + excluded.ai_tokens, "
        "worker_seconds = usage.worker_seconds + excluded.worker_seconds, "
        "syncs = usage.syncs + excluded.syncs, "
        "cart_checks = usage.cart_checks + excluded.cart_checks, "
        "cart_fills = usage.cart_fills + excluded.cart_fills",
        (user_id, day, proxy_mb, ai_tokens, worker_seconds, syncs, cart_checks, cart_fills))

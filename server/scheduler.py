"""Scheduler: daily syncs, budget alerts and notification dispatch. Run `python -m server.scheduler` (loops) or
`--once` (one tick, for a cron-style job). Logs counts only."""
import logging
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone

import psycopg

from server import budget, jobs, notify
from server.config import Settings, settings

log = logging.getLogger("scheduler")

LOCK_KEY = 727274
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]


def due_connections(conn, now: datetime, interval_h: int) -> list[tuple[str, str]]:
    """Connections whose scheduled sync is due: connected (or errored after at least one sync), last synced more than
    interval_h ago, no sync/connect job active or created within interval_h (so a failed run is not retried the same
    interval), and an orders recipe that is not unhealthy or paused (no recipe row does not exclude)."""
    iv = timedelta(hours=interval_h)
    rows = conn.execute(
        "SELECT c.user_id, c.site FROM connections c "
        "WHERE (c.status = 'connected' OR (c.status = 'error' AND c.last_synced_at IS NOT NULL)) "
        "AND (c.last_synced_at IS NULL OR c.last_synced_at < %s) "
        "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.user_id = c.user_id AND j.site = c.site "
        "  AND j.type IN ('sync','connect') AND (j.status IN ('queued','running') OR j.created_at > %s)) "
        "AND NOT EXISTS (SELECT 1 FROM recipes r WHERE r.site = c.site AND r.kind = 'orders' "
        "  AND (NOT r.healthy OR r.paused)) "
        "ORDER BY c.last_synced_at NULLS FIRST, c.user_id, c.site",
        (now - iv, now - iv)).fetchall()
    return [(str(u), site) for u, site in rows]


def _month_names(month: str) -> tuple[str, str]:
    y, m = int(month[:4]), int(month[5:7])
    return f"{MONTHS[m - 1]} {y}", MONTHS[m % 12]


def tick(conn, s: Settings, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if not conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()[0]:
        return {"skipped": True}
    try:
        state = budget.refresh_state(conn, s, now)
        label, next_month = _month_names(state.month)
        spent, total = notify.format_inr(state.spent_inr), notify.format_inr(state.budget_inr)
        if state.level in ("alert", "paused"):
            notify.alert_admins(
                conn, kind="budget_alert", title=f"Budget at {state.pct}% for {label}",
                body=f"{spent} of {total} used. Sign-ups and cart checks pause at 100%.",
                dedupe_key=f"budget:{state.month}:alert")
        if state.level == "paused":
            notify.alert_admins(
                conn, kind="budget_paused", title=f"Budget reached for {label}",
                body=f"{spent} of {total} used. Sign-ups and cart checks are paused and automatic syncs run "
                     f"every 3 days until 1 {next_month}.",
                dedupe_key=f"budget:{state.month}:paused")
        interval = s.paused_sync_interval_h if state.level == "paused" else s.sync_interval_h
        enqueued = 0
        for user_id, site in due_connections(conn, now, interval):
            try:
                jobs.enqueue(conn, user_id, "sync", site, {"trigger": "schedule"})
                enqueued += 1
            except jobs.Conflict:
                pass
        sent = notify.dispatch(conn, s)
        return {"skipped": False, "level": state.level, "enqueued": enqueued, "sent": sent}
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def main(once: bool = False, max_loops: int | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    s = settings()
    stop = threading.Event()
    old = None
    if threading.current_thread() is threading.main_thread():
        old = signal.signal(signal.SIGTERM, lambda *_: stop.set())
    conn, loops = None, 0
    try:
        while not stop.is_set() and (max_loops is None or loops < max_loops):
            loops += 1
            try:
                if conn is None:
                    conn = psycopg.connect(s.database_url, autocommit=True)
                r = tick(conn, s)
                log.info("tick skipped=%s level=%s enqueued=%s sent=%s", r.get("skipped"), r.get("level"),
                         r.get("enqueued"), r.get("sent"))
            except Exception as e:
                log.error("tick failed (%s)", type(e).__name__)
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                if once:
                    return 1
            if once:
                break
            stop.wait(s.scheduler_tick_s)
    finally:
        if conn is not None:
            conn.close()
        if old is not None:
            signal.signal(signal.SIGTERM, old)
    return 0


if __name__ == "__main__":
    sys.exit(main(once="--once" in sys.argv[1:]))

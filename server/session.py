"""Session expiry: the single needs_login path. One transition, one notification, one reminder per episode."""
from datetime import datetime, timedelta

from server import notify, sites, usage
from server.config import SITE_NAMES, Settings


def mark_needs_login(conn, user_id: str, site: str, *, now: datetime | None = None) -> bool:
    """Flip an existing connection to needs_login. True only on the transition (not already needs_login);
    that is when queued syncs are dropped and the user is told. `site` is the connections.site value."""
    now = usage.as_utc(now)
    with conn.transaction():
        row = conn.execute(
            "UPDATE connections SET status='needs_login', needs_login_at=%s "
            "WHERE user_id=%s AND site=%s AND status <> 'needs_login' RETURNING needs_login_at",
            (now, user_id, site)).fetchone()
        if row is None:
            return False
        stamp = int(row[0].timestamp())
        conn.execute("DELETE FROM jobs WHERE user_id=%s AND type='sync' AND status='queued' AND site = ANY(%s)",
                     (user_id, list(sites.same_profile(site))))
        notify.enqueue(conn, kind="needs_login", title=f"{SITE_NAMES.get(site, site.title())} logged you out",
                       body="Reconnect to keep your spending up to date.", url=f"/accounts?reconnect={site}",
                       user_id=user_id, dedupe_key=f"needs_login:{user_id}:{site}:{stamp}")
    return True


def remind(conn, s: Settings, now: datetime) -> int:
    """One reminder per episode for connections logged out longer than needs_login_remind_h."""
    now = usage.as_utc(now)
    rows = conn.execute(
        "SELECT user_id, site, needs_login_at FROM connections "
        "WHERE status='needs_login' AND needs_login_at < %s", (now - timedelta(hours=s.needs_login_remind_h),)
    ).fetchall()
    n = 0
    for user_id, site, at in rows:
        user_id = str(user_id)
        if notify.enqueue(conn, kind="needs_login_reminder",
                          title=f"{SITE_NAMES.get(site, site.title())} is still logged out",
                          body="Your spending stops updating until you reconnect. It takes a minute.",
                          url=f"/accounts?reconnect={site}", user_id=user_id,
                          dedupe_key=f"needs_login_reminder:{user_id}:{site}:{int(at.timestamp())}"):
            n += 1
    return n

"""Admin data: emails, admins and invites, per-user usage for a month and per-site health.

Shared by the /admin API routes and the `add-admin` / `invite` CLI commands. Nothing here logs an email."""
import re
from datetime import datetime, timedelta

from psycopg.rows import dict_row

from server import budget, cart, notify, sites
from server.config import Settings

_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
MAX_EMAIL_LEN = 254
# Sites a user connects (and so an admin can learn on): Instamart arrives through the swiggy connection.
CONNECTABLE_SITES = sites.CONNECTABLE
SYNC_TYPES = ("connect", "sync")  # every connect/sync job run is a sync (R5)


class InviteAccepted(Exception):
    """The invite was already used to sign up: revoking it would not remove the account."""


def normalize_email(e: str) -> str:
    """The email trimmed and lower-cased; ValueError when it does not look like an address."""
    email = (e or "").strip().lower()
    if len(email) > MAX_EMAIL_LEN or not _EMAIL.match(email):
        raise ValueError("invalid email address")
    return email


def user_email(conn, user_id: str) -> str | None:
    row = conn.execute("SELECT lower(email) FROM auth.users WHERE id = %s", (str(user_id),)).fetchone()
    return row[0] if row else None


def add_admin(conn, email: str) -> None:
    """Make email an admin (R2); it is invited too, so the sign-up hook lets it in. Idempotent."""
    email = normalize_email(email)
    with conn.transaction():
        conn.execute("INSERT INTO admins (email) VALUES (%s) ON CONFLICT DO NOTHING", (email,))
        conn.execute("INSERT INTO invites (email) VALUES (%s) ON CONFLICT DO NOTHING", (email,))


def invite(conn, email: str, invited_by: str | None) -> bool:
    """Invite email (True when newly invited) and mark it invited on the waitlist."""
    email = normalize_email(email)
    with conn.transaction():
        new = conn.execute("INSERT INTO invites (email, invited_by) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                           (email, invited_by)).rowcount == 1
        conn.execute("UPDATE waitlist SET invited_at = now() WHERE email = %s AND invited_at IS NULL", (email,))
    return new


def send_invite(conn, email: str, invited_by: str | None, s: Settings) -> bool:
    """invite() plus the invite email in the outbox (deduplicated per address), in one transaction: an invite
    is never recorded without its email. Returns invite()'s result."""
    email = normalize_email(email)
    with conn.transaction():
        new = invite(conn, email, invited_by)
        notify.enqueue(conn, kind="invite", email=email, title="You're invited to Hisaab",
                       body=f"Sign up with this email at {s.app_url}/login.", dedupe_key=f"invite:{email}")
    return new


def revoke_invite(conn, email: str) -> bool:
    """Delete a pending invite (True) or report there was none (False). Raises InviteAccepted when it was used."""
    email = normalize_email(email)
    with conn.transaction():
        row = conn.execute("SELECT accepted_at FROM invites WHERE email = %s FOR UPDATE", (email,)).fetchone()
        if row is None:
            return False
        if row[0] is not None:
            raise InviteAccepted(email)
        conn.execute("DELETE FROM invites WHERE email = %s", (email,))
    return True


def list_invites(conn) -> dict:
    with conn.cursor(row_factory=dict_row) as cur:
        invites = cur.execute("SELECT email, invited_at, accepted_at FROM invites "
                              "ORDER BY invited_at DESC, email").fetchall()
        waitlist = cur.execute("SELECT email, joined_at, invited_at FROM waitlist "
                               "ORDER BY joined_at DESC, email").fetchall()
    return {"invites": invites, "waitlist": waitlist}


def usage_by_user(conn, month: str) -> list[dict]:
    """Each user's summed usage over the IST month "YYYY-MM" (usage days are IST days), with their email
    (None when the auth user is gone). Raw units; rupees are the caller's (budget.cost_inr)."""
    first, nxt = budget._month_range(month)
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT u.user_id::text AS user_id, lower(a.email) AS email, "
            "sum(u.proxy_mb)::float8 AS proxy_mb, sum(u.ai_tokens)::bigint AS ai_tokens, "
            "sum(u.worker_seconds)::float8 AS worker_seconds, sum(u.syncs)::int AS syncs, "
            "sum(u.cart_checks)::int AS cart_checks "
            "FROM usage u LEFT JOIN auth.users a ON a.id = u.user_id "
            "WHERE u.day >= %s AND u.day < %s GROUP BY u.user_id, a.email ORDER BY u.user_id",
            (first, nxt)).fetchall()
    return rows


def site_health(conn, now: datetime, admin_user_id: str) -> list[dict]:
    """One row per connectable site: its orders recipe (version, learned_at, healthy, paused, health_note; a
    site without a recipe is healthy, unpaused, version None), the outcomes of connect/sync jobs that finished
    in the 7 days before now, and whether the calling admin's own connection for it is connected."""
    since = now - timedelta(days=7)
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(
            "SELECT s.site, 'orders' AS kind, r.version, r.learned_at, coalesce(r.healthy, true) AS healthy, "
            "coalesce(r.paused, false) AS paused, r.health_note, "
            "coalesce(j.ok, 0)::int AS syncs_ok_7d, coalesce(j.failed, 0)::int AS syncs_failed_7d, "
            "j.last_ok_at, coalesce(c.status = 'connected', false) AS admin_connected "
            "FROM unnest(%s::text[]) WITH ORDINALITY AS s(site, n) "
            "LEFT JOIN recipes r ON r.site = s.site AND r.kind = 'orders' "
            "LEFT JOIN connections c ON c.site = s.site AND c.user_id = %s "
            "LEFT JOIN LATERAL (SELECT count(*) FILTER (WHERE status = 'done') AS ok, "
            "  count(*) FILTER (WHERE status = 'failed') AS failed, "
            "  max(finished_at) FILTER (WHERE status = 'done') AS last_ok_at "
            "  FROM jobs WHERE site = s.site AND type = ANY(%s) AND finished_at >= %s AND finished_at <= %s) j "
            "ON true ORDER BY s.n",
            (list(CONNECTABLE_SITES), str(admin_user_id), list(SYNC_TYPES), since, now)).fetchall()
    return [{**r, "op": None, "db_site": r["site"]} for r in rows] + _search_health(conn, since, now, admin_user_id)


def _search_health(conn, since: datetime, now: datetime, admin_user_id: str) -> list[dict]:
    """One row per grocery search site and per food site and kind (restaurants, menu): the recipe's state, the
    7 days' search-job outcomes for that op, and whether the admin's own connection for the site's account
    (Instamart: swiggy) is connected."""
    rows = [(s, "search", "products") for s in cart.SEARCH_SITES]
    rows += [(s, k, k) for s in cart.FOOD_SITES for k in cart.FOOD_KINDS]
    recipes = {(r[0], r[1]): r[2:] for r in conn.execute(
        "SELECT site, kind, version, learned_at, healthy, paused, health_note FROM recipes")}
    outcomes = {(r[0], r[1]): r[2:] for r in conn.execute(
        "SELECT site, coalesce(payload->>'op', ''), "
        "count(*) FILTER (WHERE status='done'), count(*) FILTER (WHERE status='failed'), "
        "max(finished_at) FILTER (WHERE status='done') "
        "FROM jobs WHERE type='search' AND finished_at >= %s AND finished_at <= %s GROUP BY 1, 2", (since, now))}
    connected = {r[0] for r in conn.execute(
        "SELECT site FROM connections WHERE user_id=%s AND status='connected'", (str(admin_user_id),))}
    out = []
    for site, kind, op in rows:
        version, learned_at, healthy, paused, note = recipes.get((site, kind), (None, None, True, False, None))
        ok, failed, last_ok = outcomes.get((site, op), (0, 0, None))
        out.append({"site": site, "kind": kind, "op": op, "db_site": cart.db_site(site), "version": version, "learned_at": learned_at,
                    "healthy": bool(healthy), "paused": bool(paused), "health_note": note,
                    "syncs_ok_7d": int(ok), "syncs_failed_7d": int(failed), "last_ok_at": last_ok,
                    "admin_connected": cart.db_site(site) in connected})
    return out

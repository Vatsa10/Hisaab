"""The setup journey: what each app still needs before it is fully usable, and a one-tap run that does the
next step by itself.

Everything is derived from tables that already exist (connections, recipes, recipe_approvals, jobs, orders,
app_addresses, profiles); the only new state is setup_runs, which says "the admin pressed Set up on this site".

advance(conn, site) is the whole engine: look at the first step that is not done and queue it, or stop with a
plain reason. It is called after every learn/sync/connect job ends, after an approval is decided, and by the
scheduler as a safety net, so a missed call only delays the next step. A learn that the site's rules hold for the
admin (account-specific values, new POSTs) is never approved here: the run pauses on "Needs your OK". The cart
learn adds items to a real cart and always needs the admin live, so it is never started by a run."""
import logging
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from server import admin, cart, jobs, notify
from server.config import SITE_NAMES

log = logging.getLogger("journey")

MAX_ATTEMPTS = 2  # a step that fails twice stops the run
STEP_LABELS = {
    "connect": "Connect your account",
    "orders": "Learn the order history",
    "imported": "Import your orders",
    "addresses": "Learn saved addresses",
    "food": "Learn restaurants and menus",
    "search": "Learn grocery prices",
    "cart": "Learn the cart",
}
# step id -> the recipe kinds that make it done, and the learn kind that produces them
RECIPE_KINDS = {"orders": ("orders",), "addresses": ("addresses",), "food": ("restaurants", "menu"),
                "search": ("search",), "cart": ("cart",)}
AUTO_STEPS = ("connect", "orders", "imported", "addresses", "food", "search")  # cart needs the admin live


def _app(site: str) -> str:
    return SITE_NAMES.get(site, site.title())


@dataclass
class Step:
    id: str
    label: str
    status: str  # done | running | waiting_approval | failed | blocked | todo
    detail: str | None = None
    op: str | None = None  # what the next click does: connect | learn | sync | approve
    kind: str | None = None  # the learn kind when op is learn

    def as_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "status": self.status, "detail": self.detail, "op": self.op,
                "kind": self.kind}


def step_ids(site: str) -> list[str]:
    ids = ["connect", "orders", "imported", "addresses"]
    if site in cart.FOOD_SITES:
        ids.append("food")
    if site in cart.SEARCH_SITES:
        ids += ["search", "cart"]
    return ids


def _learn_state(conn, site: str, step: str, learn_kind: str) -> tuple[str, str | None]:
    """done / waiting_approval / running / failed / todo for one learned recipe group on a site."""
    kinds = list(RECIPE_KINDS[step])
    have = {r[0] for r in conn.execute(
        "SELECT kind FROM recipes WHERE site=%s AND kind = ANY(%s)", (site, kinds))}
    if set(kinds) <= have:
        return "done", None
    if conn.execute("SELECT 1 FROM recipe_approvals WHERE site=%s AND kind = ANY(%s) AND status='pending' "
                    "AND live_request IS NOT TRUE", (site, kinds)).fetchone():
        return "waiting_approval", "A recipe is waiting for your OK in Approvals."
    row = conn.execute(
        "SELECT status, left(coalesce(error, ''), 160) FROM jobs WHERE site=%s AND type='learn' "
        "AND coalesce(payload->>'kind', 'orders') = %s ORDER BY created_at DESC LIMIT 1", (site, learn_kind)).fetchone()
    if row and row[0] in ("queued", "running"):
        return "running", None
    if row and row[0] == "failed":
        return "failed", row[1] or "the last try failed"
    return "todo", None


def _has_location(conn, user_id: str) -> bool:
    row = conn.execute("SELECT lat, lon FROM profiles WHERE user_id=%s", (user_id,)).fetchone()
    if row and row[0] is not None and row[1] is not None:
        return True
    return conn.execute("SELECT 1 FROM app_addresses WHERE user_id=%s AND lat IS NOT NULL AND lon IS NOT NULL "
                        "LIMIT 1", (user_id,)).fetchone() is not None


def site_journey(conn, site: str, user_id: str) -> dict:
    """The ordered steps for one site on the admin's account, the first step still to do, and the run state."""
    app = _app(site)
    connected = (conn.execute("SELECT status FROM connections WHERE user_id=%s AND site=%s",
                              (user_id, site)).fetchone() or [None])[0] == "connected"
    steps: list[Step] = []
    for sid in step_ids(site):
        label = STEP_LABELS[sid]
        if sid == "connect":
            running = conn.execute("SELECT 1 FROM jobs WHERE user_id=%s AND site=%s AND type='connect' "
                                   "AND status IN ('queued','running')", (user_id, site)).fetchone()
            steps.append(Step(sid, label, "done" if connected else "running" if running else "todo",
                              None if connected else f"Needs your phone number and OTP: Accounts, {app}.",
                              None if connected else "connect"))
            continue
        if sid == "imported":
            n = conn.execute("SELECT count(*) FROM orders WHERE user_id=%s AND site=%s", (user_id, site)).fetchone()[0]
            if n > 0:
                st, detail = "done", f"{n} orders"
            elif conn.execute("SELECT 1 FROM jobs WHERE user_id=%s AND site=%s AND type IN ('sync','connect') "
                              "AND status IN ('queued','running')", (user_id, site)).fetchone():
                st, detail = "running", None
            else:
                last = conn.execute("SELECT status, left(coalesce(error,''), 160) FROM jobs WHERE user_id=%s AND "
                                    "site=%s AND type='sync' ORDER BY created_at DESC LIMIT 1",
                                    (user_id, site)).fetchone()
                st, detail = ("failed", last[1]) if last and last[0] == "failed" else ("todo", None)
            steps.append(Step(sid, label, st, detail, "sync" if st in ("todo", "failed") else None))
            continue
        st, detail = _learn_state(conn, site, sid, sid)
        if sid == "addresses" and st == "done" and conn.execute(
                "SELECT 1 FROM jobs WHERE user_id=%s AND site=%s AND type='search' AND payload->>'op'='addresses' "
                "AND status IN ('queued','running')", (user_id, site)).fetchone():
            st, detail = "running", "Reading your saved addresses."  # the first read follows the recipe at once
        steps.append(Step(sid, label, st, detail, "learn" if st in ("todo", "failed") else
                          "approve" if st == "waiting_approval" else None, sid))
    # What an earlier step still blocks.
    seen_open: Step | None = None
    for s in steps:
        if s.status == "done":
            continue
        if seen_open is not None and s.status in ("todo", "failed"):
            s.status, s.op, s.detail = "blocked", None, f"{seen_open.label} first."
        elif seen_open is None:
            seen_open = s
        if s.status == "todo" and s.id in ("food", "search", "cart") and not _has_location(conn, user_id):
            s.status, s.op, s.detail = "blocked", None, "Add your delivery address first (Accounts)."
    nxt = next((s for s in steps if s.status != "done"), None)
    run = conn.execute("SELECT status, step, message, attempts, started_at FROM setup_runs WHERE site=%s",
                       (site,)).fetchone()
    return {"site": site, "name": app, "steps": [s.as_dict() for s in steps],
            "done": sum(1 for s in steps if s.status == "done"), "total": len(steps),
            "next": nxt.as_dict() if nxt else None,
            "run": {"status": run[0], "step": run[1], "message": run[2], "started_at": run[4]} if run else None}


# ---- the run ---------------------------------------------------------------------------------------------------

def _set_run(conn, site: str, status: str, step: str | None, message: str | None, attempts: dict | None = None):
    conn.execute(
        "UPDATE setup_runs SET status=%s, step=%s, message=%s, attempts=coalesce(%s, attempts), updated_at=now() "
        "WHERE site=%s", (status, step, message, Jsonb(attempts) if attempts is not None else None, site))


def _finish(conn, site: str, status: str, step: str | None, message: str, notify_title: str | None = None) -> str:
    _set_run(conn, site, status, step, message)
    if notify_title:
        try:
            started = conn.execute("SELECT started_at FROM setup_runs WHERE site=%s", (site,)).fetchone()[0]
            notify.alert_admins(conn, kind="setup_run", title=notify_title, body=message,
                                dedupe_key=f"setup:{site}:{status}:{step or ''}:{started.isoformat()}")
        except Exception as e:  # a failed notification never stops the run
            log.warning("setup notify failed (%s)", type(e).__name__)
    return status


def ensure_location(conn, user_id: str) -> bool:
    """The admin's profile gets a delivery place from one of their app's saved addresses (the default one first)
    when it has none, so the food and grocery learns can run. Never replaces a place already set."""
    row = conn.execute(
        "SELECT line, area, city, lat, lon FROM app_addresses WHERE user_id=%s AND lat IS NOT NULL AND lon IS NOT NULL "
        "ORDER BY is_default DESC, fetched_at DESC LIMIT 1", (user_id,)).fetchone()
    if row is None:
        return False
    parts: list[str] = []
    for p in row[:3]:  # "Society, Area, City" without repeating what an earlier part already says
        if p and not any(p.lower() in q.lower() for q in parts):
            parts.append(p)
    text = ", ".join(parts)
    conn.execute("INSERT INTO profiles (user_id, address, lat, lon) VALUES (%s, %s, %s, %s) "
                 "ON CONFLICT (user_id) DO UPDATE SET address=EXCLUDED.address, lat=EXCLUDED.lat, lon=EXCLUDED.lon "
                 "WHERE profiles.lat IS NULL", (user_id, text, row[3], row[4]))
    return True


def start_learn(conn, user_id: str, site: str, kind: str) -> str:
    """Queue a learn for the admin. Raises ValueError (plain sentence) when it cannot start now; the same rules the
    Learn button has. Returns the job id."""
    account = cart.db_site(site)
    if kind == "addresses":
        site = account
    row = conn.execute("SELECT status FROM connections WHERE user_id=%s AND site=%s", (user_id, account)).fetchone()
    if row is None or row[0] != "connected":
        raise ValueError(f"Connect {_app(account)} on your own account first.")
    if kind == "cart" and conn.execute("SELECT 1 FROM recipes WHERE site=%s AND kind='search' AND healthy",
                                       (site,)).fetchone() is None:
        raise ValueError(f"Learn {_app(site)} prices (search) first.")
    if kind not in ("orders", "addresses"):
        ensure_location(conn, user_id)
        prof = conn.execute("SELECT lat, lon FROM profiles WHERE user_id=%s", (user_id,)).fetchone()
        if not prof or prof[0] is None or prof[1] is None:
            raise ValueError("Add your delivery address first.")
    if conn.execute("SELECT 1 FROM jobs WHERE site=%s AND type='learn' AND status IN ('queued','running')",
                    (site,)).fetchone() is not None:
        raise ValueError("A learn for this app is already running.")
    try:
        return jobs.enqueue(conn, user_id, "learn", site, {"reason": "setup", "kind": kind})
    except jobs.Conflict:
        raise ValueError("A learn for this app is already running.") from None


def start_run(conn, site: str, user_id: str) -> dict:
    """Press Set up: (re)start the run for a site and take the first step now."""
    conn.execute(
        "INSERT INTO setup_runs (site, user_id, status, step, message, attempts) VALUES (%s, %s, 'running', NULL, NULL, '{}') "
        "ON CONFLICT (site) DO UPDATE SET user_id=EXCLUDED.user_id, status='running', step=NULL, message=NULL, "
        "attempts='{}'::jsonb, started_at=now(), updated_at=now()", (site, user_id))
    advance(conn, site)
    return site_journey(conn, site, user_id)


def stop_run(conn, site: str) -> bool:
    cur = conn.execute("UPDATE setup_runs SET status='stopped', message='Stopped by you.', updated_at=now() "
                       "WHERE site=%s AND status IN ('running','paused')", (site,))
    return cur.rowcount > 0


def advance(conn, site: str) -> str | None:
    """Take the next step of a running setup for this site. Returns the run's status after the call, or None when
    no run is going. Safe to call at any time and from several processes: a queue conflict just means 'wait'."""
    run = conn.execute("SELECT user_id, attempts FROM setup_runs WHERE site=%s AND status='running'",
                       (site,)).fetchone()
    if run is None:
        return None
    user_id, attempts = str(run[0]), dict(run[1] or {})
    app = _app(site)
    j = site_journey(conn, site, user_id)
    nxt = next((s for s in j["steps"] if s["status"] != "done"), None)
    if nxt is None or nxt["id"] not in AUTO_STEPS:
        tail = "" if nxt is None else f" The {nxt['label'].lower()} step needs you live, so it is left for you."
        return _finish(conn, site, "done", None, f"{app} is ready.{tail}", f"{app} is ready")
    sid, st = nxt["id"], nxt["status"]
    if st == "running":
        _set_run(conn, site, "running", sid, f"{nxt['label']}...")
        return "running"
    if st == "waiting_approval":
        return _finish(conn, site, "paused", sid, f"{nxt['label']}: needs your OK in Approvals. Setup continues after.",
                       f"{app} setup needs your OK")
    if st == "blocked" or (st == "todo" and sid == "connect"):
        return _finish(conn, site, "paused", sid, nxt["detail"] or f"{nxt['label']} first.",
                       f"{app} setup is waiting for you")
    if st == "failed":
        attempts[sid] = attempts.get(sid, 0) + 1
        _set_run(conn, site, "running", sid, None, attempts)
        if attempts[sid] >= MAX_ATTEMPTS:
            return _finish(conn, site, "paused", sid, f"{nxt['label']} failed twice: {nxt['detail']}",
                           f"{app} setup stopped")
    try:
        if sid == "imported":
            jobs.enqueue(conn, user_id, "sync", site)
        else:
            start_learn(conn, user_id, site, sid)
    except ValueError as e:
        if "already running" in str(e):
            return "running"
        return _finish(conn, site, "paused", sid, str(e), f"{app} setup stopped")
    except jobs.Conflict:
        return "running"
    _set_run(conn, site, "running", sid, f"{nxt['label']}...")
    return "running"


def advance_after(conn, site: str) -> None:
    """The hook after a job or an approval: advance this site's run (and the account site's, for Instamart)."""
    for s in {site, cart.db_site(site)}:
        try:
            advance(conn, s)
        except Exception as e:  # never let the engine break the job that called it
            log.warning("setup advance failed for %s (%s)", s, type(e).__name__)


def advance_all(conn) -> int:
    """Scheduler safety net: take the next step of every running setup. Returns how many runs there are."""
    sites = [r[0] for r in conn.execute("SELECT site FROM setup_runs WHERE status='running'").fetchall()]
    for s in sites:
        advance_after(conn, s)
    return len(sites)


def journey_all(conn, user_id: str) -> list[dict]:
    return [site_journey(conn, s, user_id) for s in admin.CONNECTABLE_SITES]


def approval_site(conn, approval_id: str) -> str | None:
    row = conn.execute("SELECT site FROM recipe_approvals WHERE id=%s", (str(approval_id),)).fetchone()
    return row[0] if row else None


"""Shared "login" recipes: a site's learned login steps, replayed by every user's login (login_replay).

Only an admin's own successful login proposes a script, and it is always held for the admin's approval
(recipe_admin.decide publishes it). A replay that drifted and was re-recorded marks the old script stale."""
import logging

from psycopg.types.json import Jsonb

import login_replay
from server import recipe_admin

log = logging.getLogger("login_recipes")
KIND = "login"


def load(conn, site: str) -> dict | None:
    """The approved login recipe while it is healthy, unpaused and valid; None otherwise."""
    row = conn.execute("SELECT recipe FROM recipes WHERE site=%s AND kind=%s AND healthy AND NOT paused",
                       (site, KIND)).fetchone()
    if row is None or login_replay.validate((row[0] or {}).get("steps")) is not None:
        return None
    return row[0]


def propose(conn, user_id: str, site: str, script: list[dict], drifted: bool, job_id: str | None) -> str | None:
    """File script as a pending login recipe for the admin; returns the approval id, or None when nothing is
    filed (not an admin, an invalid script, the same as the live one, or already waiting)."""
    if not recipe_admin.is_admin(conn, user_id):
        return None
    if (why := login_replay.validate(script)) is not None:
        log.info("%s login script not proposed: %s", site, why)
        return None
    proposed = login_replay.make_recipe(script)
    with conn.transaction():
        base = conn.execute("SELECT recipe, version FROM recipes WHERE site=%s AND kind=%s FOR UPDATE",
                            (site, KIND)).fetchone()
        current, base_version = (base[0], base[1]) if base else (None, None)
        if current is not None and current.get("steps") == script:
            return None
        if drifted and current is not None:
            recipe_admin.mark_drift(conn, site, KIND, "login replay drifted; a replacement waits for approval")
        if conn.execute("SELECT 1 FROM recipe_approvals WHERE site=%s AND kind=%s AND status='pending' "
                        "AND proposed=%s", (site, KIND, Jsonb(proposed))).fetchone():
            return None
        approval_id = conn.execute(
            "INSERT INTO recipe_approvals (site, kind, reason, proposed, current, diff, status, job_id, "
            "base_version) VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s) RETURNING id",
            (site, KIND, "login_drift" if drifted else "login_learned", Jsonb(proposed),
             Jsonb(current) if current is not None else None, Jsonb(recipe_admin.diff(current, proposed)),
             job_id, base_version),
        ).fetchone()[0]
    return str(approval_id)


def login_kwargs(conn, user_id: str, site: str, job_id: str | None) -> dict:
    """Extra sync.login arguments for a worker login: the approved script to replay and, for an admin's own
    login only, the hook that proposes what the login learned. Lookup failures leave the login as it was."""
    try:
        out = {"login_recipe": load(conn, site)}
        if recipe_admin.is_admin(conn, user_id):
            out["on_login_script"] = lambda script, drifted: propose(conn, user_id, site, script, drifted, job_id)
        return out
    except Exception as e:
        log.warning("%s login recipe unavailable (%s)", site, type(e).__name__)
        return {}


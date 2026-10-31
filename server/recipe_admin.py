"""Shared-recipe administration: who may learn, what a shared recipe may contain, and how learned recipes and
new POST-allowlist entries wait for the admin (ruling R9); plus per-site health and pause state (ruling R8).

A recipe in Postgres is read by every user's sync, so a learned recipe is published only when it is safe:
identity headers are stripped, nothing in it looks tied to the learning account, and every POST it lets
through was already allowed or was approved by the admin during that learn job."""
import logging
import re
import time
from urllib.parse import urlsplit

import recipes

from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from server import jobs, store

log = logging.getLogger("recipe_admin")


class NotPending(Exception):
    """The approval does not exist or was already decided (or expired)."""


class JobEnded(Exception):
    """A POST-allowlist approval whose learn job is no longer running: nobody is waiting for the answer."""


class RecipeChanged(Exception):
    """A held recipe was proposed against one version of the shared recipe, and another version has been
    published since: approving it would overwrite the newer recipe. The row is expired; a new learn proposes
    again against the current recipe."""

    def __init__(self, approval_id: str, base_version: int | None, current_version: int | None):
        super().__init__(f"recipe changed since approval {approval_id} was filed "
                         f"(version {base_version} -> {current_version})")
        self.approval_id, self.base_version, self.current_version = approval_id, base_version, current_version


class InvalidRecipe(Exception):
    """A held cart recipe fails check_cart_recipe at approval time: it is not published and the row is expired."""

    def __init__(self, approval_id: str, reason: str):
        super().__init__(f"approval {approval_id}: invalid cart recipe ({reason})")
        self.approval_id, self.reason = approval_id, reason


def _cart_recipe_error(recipe, site: str) -> str | None:
    import cart_recipe  # lazy: the cart guard pulls in the browser modules
    if not isinstance(recipe, dict):
        return "not a recipe"
    try:
        return cart_recipe.check_cart_recipe(recipe, site)
    except Exception as e:  # a malformed recipe is refused, never published
        return f"unreadable ({type(e).__name__})"


# ---------------------------------------------------------------- admins

def is_admin(conn, user_id: str) -> bool:
    """Whether this auth user's email (any case) is in admins. Never trusts a client claim, only auth.users."""
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM auth.users u JOIN admins a ON a.email = lower(u.email) WHERE u.id = %s)",
        (str(user_id),),
    ).fetchone()[0]


def admin_learner(conn, site: str) -> str | None:
    """The oldest admin whose own connection for site is connected (their account can run a learn), or None."""
    row = conn.execute(
        "SELECT u.id FROM admins a JOIN auth.users u ON lower(u.email) = a.email "
        "JOIN connections c ON c.user_id = u.id AND c.site = %s AND c.status = 'connected' "
        "ORDER BY a.added_at, a.email LIMIT 1",
        (site,),
    ).fetchone()
    return str(row[0]) if row else None


# ---------------------------------------------------------------- sanitising for sharing

# A published recipe is shared by every user: it must not carry the account it was learned on.
IDENTITY_HEADERS = {"referer", "origin", "cookie"}
_ACCOUNT_SEGMENT = re.compile(r"/(?:users|user|profile)/[^/?#]+", re.I)
_LONG_DIGITS = re.compile(r"\d{8,}")


def strip_identity_headers(obj):
    """A copy of obj with referer/origin/cookie (any case) removed from every "headers" mapping."""
    if isinstance(obj, dict):
        return {k: ({h: v for h, v in val.items() if h.lower() not in IDENTITY_HEADERS}
                    if k.lower() == "headers" and isinstance(val, dict) else strip_identity_headers(val))
                for k, val in obj.items()}
    if isinstance(obj, list):
        return [strip_identity_headers(v) for v in obj]
    return obj


def _url_path(s: str) -> str | None:
    """The path of an http(s) URL or of a "/..." path, else None."""
    if re.match(r"https?://", s, re.I):
        try:
            return urlsplit(s).path
        except ValueError:
            return s
    return re.split(r"[?#]", s, maxsplit=1)[0] if s.startswith("/") else None


def account_specific(obj, where: str = "$") -> list[str]:
    """JSON paths of string values that look tied to one account: a /users/<x>, /user/<x> or /profile/<x>
    segment, or a run of 8+ digits in a URL path."""
    if isinstance(obj, dict):
        return [p for k, v in obj.items() for p in account_specific(v, f"{where}.{k}")]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in account_specific(v, f"{where}[{i}]")]
    if isinstance(obj, str):
        path = _url_path(obj)
        if _ACCOUNT_SEGMENT.search(obj if path is None else path) or (path and _LONG_DIGITS.search(path)):
            return [where]
    return []


def sanitize_for_sharing(recipe: dict, site: str | None = None) -> tuple[dict, list[str]]:
    """(a copy of recipe safe to share, JSON paths of values that still look account-specific).
    Identity headers are removed; a per-account site (zomato) is templated first; other flagged values are
    reported, not rewritten. The input is not modified."""
    if site in recipes.PER_ACCOUNT_SITES:
        recipe = recipes.templatize(recipe)
    clean = strip_identity_headers(recipe)
    return clean, account_specific(clean)


# ---------------------------------------------------------------- diffs

def diff(current: dict | None, proposed: dict) -> list[dict]:
    """Leaf-level changes from current (None = nothing yet) to proposed: {"op": "add"|"remove"|"change",
    "path": "$.a.b[0]", "old"?, "new"?}, sorted by path (list indices numerically). A key or list item present
    on one side only is reported once, whole."""
    out: list[dict] = []
    _diff({} if current is None else current, proposed, "$", out)
    return sorted(out, key=lambda d: _path_key(d["path"]))


def _diff(old, new, path: str, out: list[dict]) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        for k in old.keys() | new.keys():
            p = f"{path}.{k}"
            if k not in new:
                out.append({"op": "remove", "path": p, "old": old[k]})
            elif k not in old:
                out.append({"op": "add", "path": p, "new": new[k]})
            else:
                _diff(old[k], new[k], p, out)
    elif isinstance(old, list) and isinstance(new, list):
        for i in range(max(len(old), len(new))):
            p = f"{path}[{i}]"
            if i >= len(new):
                out.append({"op": "remove", "path": p, "old": old[i]})
            elif i >= len(old):
                out.append({"op": "add", "path": p, "new": new[i]})
            else:
                _diff(old[i], new[i], p, out)
    elif old != new or type(old) is not type(new):
        out.append({"op": "change", "path": path, "old": old, "new": new})


def _path_key(path: str) -> list[tuple]:
    return [(1, int(i)) if i else (0, k) for k, i in re.findall(r"\.([^.\[]+)|\[(\d+)\]", path)]


# ---------------------------------------------------------------- live POST-allowlist approvals

def _channel(approval_id: str) -> str:
    return "approval_" + str(approval_id).replace("-", "")


class Approver:
    """approve(entries) -> bool, passed to sync.learn as its approve callback. Each call with new POST-allowlist
    entries files a post_allowlist approval, shows it on the job's progress (stage approval_needed) and waits up
    to timeout_s for the admin's decision relayed via NOTIFY approval_<id hex>. Entries approved in this job are
    collected in .approved (their url_regex) for apply_learned."""

    def __init__(self, listen_conn, job_conn, job_id: str, site: str, kind: str, timeout_s: float):
        self.listen_conn, self.job_conn = listen_conn, job_conn
        self.job_id, self.site, self.kind, self.timeout_s = job_id, site, kind, timeout_s
        self.approved: set[str] = set()

    def __call__(self, entries: list[dict]) -> bool:
        if not entries:
            return True
        proposed = {"read_post_allowlist": entries}
        approval_id = str(self.job_conn.execute(
            "INSERT INTO recipe_approvals (site, kind, reason, proposed, diff, job_id, live_request) "
            "VALUES (%s, %s, 'post_allowlist', %s, %s, %s, true) RETURNING id",
            (self.site, self.kind, Jsonb(proposed), Jsonb(diff(None, proposed)), self.job_id),
        ).fetchone()[0])
        channel = _channel(approval_id)
        # LISTEN before publishing the question so a decision made right after the admin sees it is not missed.
        self.listen_conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
        try:
            jobs.set_progress(self.job_conn, self.job_id, stage="approval_needed", approval_id=approval_id,
                              entries=entries)
            decision = self._wait(channel)
            if decision is None:
                expired = self.job_conn.execute(
                    "UPDATE recipe_approvals SET status='expired', decided_at=now() "
                    "WHERE id=%s AND status='pending'", (approval_id,)).rowcount == 1
                if expired:
                    return False
                # Decided in the instant before the expiry: the row holds the answer.
                decision = self.job_conn.execute("SELECT status FROM recipe_approvals WHERE id=%s",
                                                 (approval_id,)).fetchone()[0]
            ok = decision == "approved"
            jobs.set_progress(self.job_conn, self.job_id, stage="approval_received", approved=ok)
            if ok:
                self.approved.update(e["url_regex"] for e in entries if e.get("url_regex"))
            return ok
        finally:
            try:  # best effort: a broken listen connection must not mask the original error
                self.listen_conn.execute(sql.SQL("UNLISTEN {}").format(sql.Identifier(channel)))
            except Exception as e:
                log.warning("UNLISTEN failed (%s)", type(e).__name__)

    def _wait(self, channel: str) -> str | None:
        """The payload sent on channel within timeout_s, or None. Stray notifications for an earlier approval
        on the same listen connection are skipped."""
        deadline = time.monotonic() + self.timeout_s
        while (remaining := deadline - time.monotonic()) > 0:
            for note in self.listen_conn.notifies(timeout=remaining, stop_after=1):
                if note.channel == channel:
                    return note.payload
        return None


def make_approver(listen_conn, job_conn, job_id: str, site: str, kind: str, timeout_s: float) -> Approver:
    return Approver(listen_conn, job_conn, job_id, site, kind, timeout_s)


def deny_new_posts(entries: list[dict]) -> bool:
    """The approve callback of an unattended (drift) learn: no new POST entry is ever let through."""
    return False


# ---------------------------------------------------------------- applying and deciding

def _post_regexes(recipe: dict | None) -> set[str]:
    return {e["url_regex"] for e in (recipe or {}).get("read_post_allowlist") or []
            if isinstance(e, dict) and e.get("url_regex")}


def apply_learned(conn, site: str, kind: str, recipe: dict, approved: set[str], job_id: str | None,
                  reason: str) -> dict:
    """Publish a learned recipe, or file it for the admin. It waits (status pending) when kind is cart (reason
    cart), when a value looks account-specific (account_specific), or when it allows a POST that is neither in
    the current recipe nor in approved (post_allowlist); otherwise it is published at once and recorded as
    auto_applied with reason as given ("learned" or "drift_relearn"). Returns {"approval_id", "status",
    "version"} (version None while pending)."""
    clean, flagged = sanitize_for_sharing(recipe, site)
    with conn.transaction():
        # Locked so a publish cannot slip in between reading the base and writing over it.
        base = conn.execute("SELECT recipe, version FROM recipes WHERE site=%s AND kind=%s FOR UPDATE",
                            (site, kind)).fetchone()
        current, base_version = (base[0], base[1]) if base else (None, None)
        if kind == "cart":
            hold = "cart"
        elif flagged:
            hold = "account_specific"
        elif _post_regexes(clean) - _post_regexes(current) - set(approved):
            hold = "post_allowlist"
        else:
            hold = None
        version = None if hold else store.put_recipe(conn, site, clean, kind=kind)
        approval_id = conn.execute(
            "INSERT INTO recipe_approvals (site, kind, reason, proposed, current, diff, status, job_id, "
            "base_version, decided_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, CASE WHEN %s THEN NULL ELSE now() END) RETURNING id",
            (site, kind, hold or reason, Jsonb(clean), Jsonb(current) if current is not None else None,
             Jsonb(diff(current, clean)), "pending" if hold else "auto_applied", job_id, base_version,
             bool(hold)),
        ).fetchone()[0]
    return {"approval_id": str(approval_id), "status": "pending" if hold else "auto_applied", "version": version}


def decide(conn, approval_id: str, decision: str, admin_user_id: str) -> str:
    """Record the admin's decision on a pending approval and return its new status ("approved"/"rejected").
    A live POST-allowlist request (live_request, set only by an Approver): the waiting learn job is told via
    NOTIFY approval_<id hex>; if that job is no longer running the row becomes expired and JobEnded is raised.
    Any other row is a held-back recipe (including one held for new POSTs): approve publishes the proposed
    recipe, which also makes the site healthy and unpaused, but only while the shared recipe is still the
    version it was proposed against; otherwise the row becomes expired and RecipeChanged is raised (reject is
    always recorded). Raises NotPending when the row is missing or already decided."""
    if decision not in ("approve", "reject"):
        raise ValueError(f"decision must be approve or reject, not {decision!r}")
    status = "approved" if decision == "approve" else "rejected"
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT a.id, a.site, a.kind, a.reason, a.proposed, a.job_id, a.status, a.live_request, "
                "a.base_version, j.status AS job_status "
                "FROM recipe_approvals a LEFT JOIN jobs j ON j.id = a.job_id WHERE a.id = %s FOR UPDATE OF a",
                (str(approval_id),),
            ).fetchone()
        if row is None or row["status"] != "pending":
            raise NotPending(str(approval_id))
        failure: Exception | None = None
        if row["live_request"]:
            if row["job_status"] != "running":
                failure = JobEnded(str(approval_id))
            else:
                conn.execute("SELECT pg_notify(%s, %s)", (_channel(row["id"]), status))
        elif status == "approved":
            locked = conn.execute("SELECT version FROM recipes WHERE site=%s AND kind=%s FOR UPDATE",
                                  (row["site"], row["kind"])).fetchone()
            current_version = locked[0] if locked else None
            if current_version != row["base_version"]:
                log.warning("approval %s refused: %s/%s recipe moved from version %s to %s", row["id"],
                            row["site"], row["kind"], row["base_version"], current_version)
                failure = RecipeChanged(str(approval_id), row["base_version"], current_version)
            elif row["kind"] == "cart" and (err := _cart_recipe_error(row["proposed"], row["site"])):
                # The sanitized recipe that would be published is checked again: an invalid one never goes live.
                log.warning("approval %s refused: %s cart recipe is invalid (%s)", row["id"], row["site"], err)
                failure = InvalidRecipe(str(approval_id), err)
            else:
                store.put_recipe(conn, row["site"], row["proposed"], kind=row["kind"])
        if failure is not None:
            conn.execute("UPDATE recipe_approvals SET status='expired', decided_at=now() WHERE id=%s",
                         (row["id"],))
        else:
            conn.execute("UPDATE recipe_approvals SET status=%s, decided_at=now(), decided_by=%s WHERE id=%s",
                         (status, str(admin_user_id), row["id"]))
    if failure is not None:  # raised after the commit, so the expiry is kept
        raise failure
    return status


# ---------------------------------------------------------------- site health

def mark_drift(conn, site: str, kind: str, note: str) -> bool:
    """Mark the site's recipe unhealthy. True only when it was healthy until now (the caller then re-learns or
    pauses once, not on every drifted sync). A site without a recipe row is not marked."""
    return conn.execute(
        "UPDATE recipes SET healthy=false, health_note=%s WHERE site=%s AND kind=%s AND healthy",
        (note, site, kind),
    ).rowcount == 1


def pause_site(conn, site: str, kind: str, note: str, reason: str = "manual") -> bool:
    """Pause the site: the scheduler skips it and manual refresh is refused until it is resumed or a recipe
    is published. False when the site has no recipe row (never learned: nothing to pause; its jobs already
    fail as no_recipe). No placeholder row is ever created. `reason` is "manual" (an admin) or "auto" (the
    error budget or drift); admin site health reports it."""
    return conn.execute("UPDATE recipes SET paused=true, paused_reason=%s, health_note=%s WHERE site=%s AND kind=%s",
                        (reason, note, site, kind)).rowcount == 1


def resume_site(conn, site: str, kind: str) -> bool:
    """The admin resumes the site: unpaused and healthy again, so syncs retry the recipe (a new drift marks it
    unhealthy again). False when the site has no recipe row."""
    return conn.execute("UPDATE recipes SET paused=false, paused_reason=NULL, healthy=true, health_note=NULL WHERE site=%s AND kind=%s",
                        (site, kind)).rowcount == 1


def is_paused(conn, site: str, kind: str = "orders") -> bool:
    row = conn.execute("SELECT paused FROM recipes WHERE site=%s AND kind=%s", (site, kind)).fetchone()
    return bool(row and row[0])

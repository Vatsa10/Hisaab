"""The /admin routes behind the admin PWA view: costs, site health and pause, learn jobs, invites and recipe
approvals. Every route is guarded by require_admin (admins table joined on the token user's auth email), so
the PWA never needs the service-role key. Emails are returned to the admin but never logged.

build_router(current_user, get_conn) takes the API's own auth and connection dependencies, so this module
never imports server.api (which includes the router)."""
import re
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from browser import SITES
from server import admin, budget, cart, cart_rank, jobs, recipe_admin, store, usage
from server.config import SITE_NAMES, settings

ADMIN_PAUSE_NOTE = "Paused by the admin"
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
APPROVAL_COLUMNS = ("id, site, kind, reason, status, proposed, current, diff, job_id, created_at, decided_at, "
                    "live_request AS live, base_version")


class InviteBody(BaseModel):
    email: str = Field(max_length=320)


class FeeRulesBody(BaseModel):
    rules: dict
    verified: bool


# Which sites each learn kind covers: order history on connectable sites, grocery search, restaurants+menus.
LEARN_SITES = {"orders": admin.CONNECTABLE_SITES, "search": cart.SEARCH_SITES, "food": cart.FOOD_SITES,
               "cart": cart.SEARCH_SITES}


class DecideBody(BaseModel):
    decision: Literal["approve", "reject"]


def _app(site: str) -> str:
    return SITE_NAMES.get(site, site.title())


def _known(site: str) -> None:
    if site not in SITES:
        raise HTTPException(400, "unknown site")


def _no_recipe(site: str) -> HTTPException:
    """A never-learned site has nothing to pause or resume (its jobs already fail as no_recipe)."""
    return HTTPException(404, f"{_app(site)} has no recipe yet")


def build_router(current_user, get_conn) -> APIRouter:
    def require_admin(user: str = Depends(current_user), conn=Depends(get_conn)) -> str:
        if not recipe_admin.is_admin(conn, user):
            raise HTTPException(403, "admin only")
        return user

    router = APIRouter(prefix="/admin")

    @router.get("/costs")
    def costs(month: str | None = Query(None), _admin: str = Depends(require_admin), conn=Depends(get_conn)):
        s = settings()
        month = month or usage.month_of(tz=s.app_tz)
        if not _MONTH.match(month):
            raise HTTPException(422, "month must be YYYY-MM")
        users = [{"user_id": u["user_id"], "email": u["email"], "proxy_gb": round(u["proxy_mb"] / 1000, 4),
                  "ai_tokens": int(u["ai_tokens"]), "worker_minutes": round(u["worker_seconds"] / 60, 2),
                  "syncs": u["syncs"], "cart_checks": u["cart_checks"],
                  "cart_fills": u["cart_fills"],
                  "inr": round(budget.cost_inr(u["proxy_mb"], u["ai_tokens"], u["worker_seconds"], s), 2)}
                 for u in admin.usage_by_user(conn, month)]
        users.sort(key=lambda u: (-u["inr"], u["user_id"]))
        spent = budget.month_cost(conn, s, month)
        pct = int(spent * 100 // s.monthly_budget_inr) if s.monthly_budget_inr > 0 else 0
        return {"month": month, "budget_inr": s.monthly_budget_inr, "spent_inr": round(spent, 2),
                "fixed_inr": s.fixed_monthly_inr, "pct": pct, "level": budget.level_for(spent, s),
                "unit_costs": {"proxy_inr_per_gb": s.proxy_inr_per_gb,
                               "ai_inr_per_1k_tokens": s.ai_inr_per_1k_tokens,
                               "worker_inr_per_minute": s.worker_inr_per_minute},
                "users": users}

    @router.get("/sites")
    def sites(user: str = Depends(require_admin), conn=Depends(get_conn)):
        return admin.site_health(conn, datetime.now(timezone.utc), user)

    @router.post("/sites/{site}/pause", status_code=204)
    def pause(site: str, _admin: str = Depends(require_admin), conn=Depends(get_conn)):
        _known(site)
        if not recipe_admin.pause_site(conn, site, "orders", ADMIN_PAUSE_NOTE):
            raise _no_recipe(site)
        return Response(status_code=204)

    @router.post("/sites/{site}/resume", status_code=204)
    def resume(site: str, _admin: str = Depends(require_admin), conn=Depends(get_conn)):
        _known(site)
        if not recipe_admin.resume_site(conn, site, "orders"):
            raise _no_recipe(site)
        return Response(status_code=204)

    @router.post("/learn/{site}", status_code=202)
    def learn(site: str, kind: Literal["orders", "search", "food", "cart"] = "orders", user: str = Depends(require_admin),
              conn=Depends(get_conn)):
        _known(site)
        if site not in LEARN_SITES[kind]:
            if kind == "orders":
                raise HTTPException(400, "Instamart is learned through the swiggy connection")
            raise HTTPException(400, f"{_app(site)} has no {kind} recipe to learn")
        account = cart.db_site(site)
        row = conn.execute("SELECT status FROM connections WHERE user_id=%s AND site=%s", (user, account)).fetchone()
        if row is None or row[0] != "connected":
            raise HTTPException(409, f"Connect {_app(account)} on your own account first.")
        if kind == "cart" and conn.execute(
                "SELECT 1 FROM recipes WHERE site=%s AND kind='search' AND healthy", (site,)).fetchone() is None:
            raise HTTPException(409, f"Learn {_app(site)} prices (search) first.")
        if kind != "orders":
            prof = store.get_profile(conn, user) or {}
            if prof.get("lat") is None or prof.get("lon") is None:
                raise HTTPException(409, "Add your delivery address first.")
        busy = "A learn is already running." if kind == "cart" else f"A learn for {_app(site)} is already running."
        if conn.execute("SELECT 1 FROM jobs WHERE site=%s AND type='learn' AND status IN ('queued','running')",
                        (site,)).fetchone() is not None:
            raise HTTPException(409, busy)
        payload = {"reason": "manual", "kind": kind}
        try:
            job_id = jobs.enqueue(conn, user, "learn", site, payload)
        except jobs.Conflict:
            raise HTTPException(409, busy) from None
        return {"job_id": job_id}

    @router.get("/fees")
    def get_fees(_admin: str = Depends(require_admin), conn=Depends(get_conn)):
        rows = {r[0]: r[1:] for r in conn.execute(
            "SELECT site, rules, version, verified, source, updated_at FROM fee_rules")}
        out = []
        for site in cart_rank.SITE_ORDER:
            rules, version, verified, source, updated_at = rows.get(site, (None, 0, False, None, None))
            out.append({"site": site, "kind": "food" if site in cart.FOOD_SITES else "grocery", "rules": rules,
                        "version": version, "verified": bool(verified), "source": source, "updated_at": updated_at})
        return out

    @router.put("/fees/{site}")
    def put_fees(site: str, body: FeeRulesBody, user: str = Depends(require_admin), conn=Depends(get_conn)):
        if site not in cart_rank.SITE_ORDER:
            raise HTTPException(404, "unknown site")
        try:
            version = cart.put_fee_rules(conn, site, body.rules, body.verified, user)
        except cart.InvalidRules as e:
            raise HTTPException(422, e.errors) from None
        return {"version": version}

    @router.get("/invites")
    def get_invites(_admin: str = Depends(require_admin), conn=Depends(get_conn)):
        return admin.list_invites(conn)

    @router.post("/invites", status_code=201)
    def post_invite(body: InviteBody, user: str = Depends(require_admin), conn=Depends(get_conn)):
        try:
            email = admin.normalize_email(body.email)
        except ValueError:
            raise HTTPException(422, "Enter a valid email address.") from None
        return {"email": email, "invited": admin.send_invite(conn, email, user, settings())}

    @router.delete("/invites/{email}", status_code=204)
    def delete_invite(email: str, _admin: str = Depends(require_admin), conn=Depends(get_conn)):
        try:
            removed = admin.revoke_invite(conn, email)
        except ValueError:
            raise HTTPException(422, "Enter a valid email address.") from None
        except admin.InviteAccepted:
            raise HTTPException(409, "This invite was already used to sign up.") from None
        if not removed:
            raise HTTPException(404, "no such invite")
        return Response(status_code=204)

    @router.get("/approvals")
    def approvals(status: Literal["pending", "all"] = "pending", _admin: str = Depends(require_admin),
                  conn=Depends(get_conn)):
        where = "WHERE status = 'pending'" if status == "pending" else ""
        with conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(f"SELECT {APPROVAL_COLUMNS} FROM recipe_approvals {where} "
                               "ORDER BY created_at DESC LIMIT 200").fetchall()
        for r in rows:
            r["id"] = str(r["id"])
            r["job_id"] = str(r["job_id"]) if r["job_id"] else None
        return rows

    @router.post("/approvals/{approval_id}/decide")
    def decide(approval_id: str, body: DecideBody, user: str = Depends(require_admin), conn=Depends(get_conn)):
        try:
            aid = str(uuid.UUID(approval_id))
        except ValueError:
            raise HTTPException(404, "not found") from None
        try:
            status = recipe_admin.decide(conn, aid, body.decision, user)
        except recipe_admin.NotPending:
            raise HTTPException(409, "This approval was already decided or has expired.") from None
        except recipe_admin.JobEnded:
            raise HTTPException(409, "The learn job that asked has ended, so this request expired.") from None
        except recipe_admin.RecipeChanged as e:
            raise HTTPException(409, f"The recipe changed since this was proposed (version {e.base_version} → "
                                     f"{e.current_version}). Run a new learn to propose it again.") from None
        except recipe_admin.InvalidRecipe as e:
            raise HTTPException(409, f"This cart recipe failed its safety check ({e.reason}), so it was not "
                                     "published. Run a new learn.") from None
        return {"status": status}

    return router

"""Cart fills (Plan 6): the fill plan, the Fill button's state, starting and confirming a fill, and the fill row's
life cycle as the worker drives it.

A fill only ever adds what the user's cart check priced. It is created by the owner's tap (start_fill or
confirm_fill), never by the system, and counts against its own daily cap (R7). One fill per check and app holds the
slot (R8), within 30 minutes of pricing. All copy is built here and in fill_summary (R10).
Logs carry the site and counts only: never product names, coordinates or areas with a user id.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

import psycopg
from psycopg.types.json import Jsonb

from server import budget, fill_summary, jobs, limits, usage
from server.cart_rank import inr
from server.config import OPEN_APP_URLS, Settings

log = logging.getLogger("fill")

FILL_SITES = ("zepto", "blinkit", "instamart")
SLOT_STATUSES = ("queued", "filling", "done", "partial", "interrupted")
ACTIVE = ("queued", "filling")
FILLED = ("done", "partial", "interrupted")
CONNECTED = ("connected", "syncing")       # syncing: the login is good, a sync is just running
MAX_CHOICES = 5
ACCOUNTS = "/accounts"
FOOD_NOTE = "Filling isn't available for food orders."
STALE_NOTE = "Prices are over 30 minutes old. Check again first."
SLOT_INDEX = "one_fill_per_check_site"

FILL_COLUMNS = ("id", "user_id", "request_id", "site", "job_id", "status", "plan", "items", "priced_location",
                "confirm_location", "app_location", "location_selected", "repriced", "cart", "summary", "error",
                "counted", "confirmed_from", "open_url", "created_at", "started_at", "finished_at")


class NotFound(Exception):
    """No such check (or fill) for this user."""


class NotReady(Exception):
    """The check is still pricing (or failed)."""


class FoodNotFillable(Exception):
    """Food compares are never filled (R14)."""


class NoPrices(Exception):
    """That app has no priced card on this check."""


class Unavailable(Exception):
    """That app has no approved, healthy, unpaused cart recipe."""


class NeedsConnection(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class Stale(Exception):
    """The prices are older than cart_fill_max_age_s."""


class AlreadyFilled(Exception):
    """A fill already holds this check and app (R8)."""


class NothingToAdd(Exception):
    """Every item would be skipped."""


class FillRunning(Exception):
    """A fill on this app is already queued or running for this user."""


class NotConfirmable(Exception):
    """Not a re-priced fill of this user's that is still on offer."""


# --- helpers -----------------------------------------------------------------------------------------------------

def db_site(site: str) -> str:
    """The connection a site's fill uses: Instamart uses the Swiggy login (browser.SITES)."""
    import browser
    return browser.SITES.get(site, {}).get("db_site", site)


def _app(site: str) -> str:
    return fill_summary.app_name(site)


def _conn_name(site: str) -> str:
    return fill_summary.conn_name(site)


def cart_recipe_ready(conn, site: str) -> bool:
    """An approved cart recipe that is healthy and not paused (drift pauses filling; R6 of Plan 4)."""
    return conn.execute("select 1 from recipes where site = %s and kind = 'cart' and healthy and not paused",
                        (site,)).fetchone() is not None


def _is_uuid(v) -> bool:
    try:
        uuid.UUID(str(v))
    except ValueError:
        return False
    return True


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _qty(item: dict) -> int:
    q = item.get("qty")
    return q if isinstance(q, int) and not isinstance(q, bool) and q > 0 else 1


def _cand_id(c: dict):
    return c.get("product_id", c.get("dish_id"))


def _usable(c) -> bool:
    return isinstance(c, dict) and c.get("in_stock", True) is not False and _cand_id(c) is not None \
        and _is_num(c.get("price"))


def _packs(v, default: int) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 1 else default


def _choice(c: dict, qty: int, label: str) -> dict:
    return {"id": str(_cand_id(c)), "label": label, "product_id": str(_cand_id(c)), "name": c.get("name"),
            "packs": _packs(c.get("packs"), qty), "price": float(c["price"])}


SKIP_CHOICE = {"id": "skip", "label": "Skip this item"}


# --- the plan ----------------------------------------------------------------------------------------------------

def plan_items(request: dict, result: dict, site: str) -> list[dict]:
    """One plan item per request item, from the card's match cells and the user's choices (pure)."""
    app = _app(site)
    by_idx = {m["idx"]: m for m in (result.get("items") or [])
              if isinstance(m, dict) and isinstance(m.get("idx"), int)}
    choices = request.get("choices") or {}
    plan = []
    for idx, item in enumerate(request.get("items") or []):
        item = item if isinstance(item, dict) else {}
        qty = _qty(item)
        label = str(item.get("text") or item.get("name") or "")
        p = {"idx": idx, "label": label, "action": "skip", "product_id": None, "name": None, "packs": None,
             "price": None, "question": None, "choices": [], "reason": "not_found"}
        match = by_idx.get(idx) or {}
        status = match.get("status")
        cands = [c for c in match.get("candidates") or [] if _usable(c)]
        picked = choices.get(str(idx)) or choices.get(idx) or {}
        chosen_id = picked.get(site) if isinstance(picked, dict) else None
        chosen = next((c for c in cands if str(_cand_id(c)) == str(chosen_id)), None) if chosen_id is not None \
            else None
        product = match.get("product") if isinstance(match.get("product"), dict) else None
        if chosen is not None:
            p.update(action="add", product_id=str(_cand_id(chosen)), name=chosen.get("name"),
                     packs=_packs(chosen.get("packs"), qty), price=float(chosen["price"]), reason=None)
        elif status in ("matched", "swapped") and product and _usable(product):
            packs = _packs(match.get("packs"), qty)
            name, price = product.get("name"), float(product["price"])
            p.update(product_id=str(_cand_id(product)), name=name, packs=packs, price=price, reason=None)
            if status == "matched":
                p["action"] = "add"
            else:
                price_label = inr(packs * price)
                swap_from = _swap_name(match.get("swap_from"), label)
                p.update(action="ask",
                         question=f"{swap_from} is out of stock on {app}. Add {name} ({price_label}) instead?",
                         choices=[{"id": str(_cand_id(product)), "label": f"Add {name} · {price_label}",
                                   "product_id": str(_cand_id(product)), "name": name, "packs": packs,
                                   "price": price},
                                  dict(SKIP_CHOICE)])
        elif status == "pick" and cands:
            opts = []
            for c in cands[:MAX_CHOICES]:
                packs = _packs(c.get("packs"), qty)
                parts = [str(c.get("name") or "")]
                if c.get("size_label"):
                    parts.append(str(c["size_label"]))
                parts.append(inr(packs * float(c["price"])))
                opts.append(_choice(c, qty, " · ".join(parts) + (f" ×{packs}" if packs > 1 else "")))
            p.update(action="ask", question=f"Which {label} should we add?", choices=opts + [dict(SKIP_CHOICE)],
                     reason=None)
        elif status == "out_of_stock":
            p["reason"] = "out_of_stock"
        plan.append(p)
    return plan


def _swap_name(swap_from, fallback: str) -> str:
    if isinstance(swap_from, dict) and swap_from.get("name"):
        return str(swap_from["name"])
    if isinstance(swap_from, str) and swap_from.strip():
        return swap_from
    return fallback


def _preview(plan: list[dict]) -> dict:
    return {k: [p["label"] for p in plan if p["action"] == k] for k in ("add", "ask", "skip")}


# --- reads -------------------------------------------------------------------------------------------------------

def _request(conn, user_id: str, request_id: str) -> dict:
    row = None
    if _is_uuid(request_id):
        row = conn.execute(
            "select id, user_id, mode, items, choices, status, area, lat, lon, priced_at from cart_requests "
            "where id = %s and user_id = %s", (str(request_id), str(user_id))).fetchone()
    if row is None:
        raise NotFound()
    keys = ("id", "user_id", "mode", "items", "choices", "status", "area", "lat", "lon", "priced_at")
    req = dict(zip(keys, row))
    req["id"], req["user_id"] = str(req["id"]), str(req["user_id"])
    return req


def _results(conn, request_id: str) -> dict[str, dict]:
    rows = conn.execute("select site, status, items, priced_at from cart_results where request_id = %s",
                        (request_id,)).fetchall()
    return {r[0]: {"site": r[0], "status": r[1], "items": r[2] or [], "priced_at": r[3]} for r in rows}


def _row(r) -> dict:
    f = dict(zip(FILL_COLUMNS, r))
    for k in ("id", "user_id", "request_id", "job_id", "confirmed_from"):
        if f[k] is not None:
            f[k] = str(f[k])
    return f


def get_fill(conn, fill_id: str, user_id: str | None = None) -> dict | None:
    """The fill row (as a dict), or None; with user_id, only that user's."""
    sql = f"select {', '.join(FILL_COLUMNS)} from cart_fills where id = %s"
    args: tuple = (str(fill_id),)
    if user_id is not None:
        sql += " and user_id = %s"
        args += (str(user_id),)
    if not _is_uuid(fill_id):
        return None
    r = conn.execute(sql, args).fetchone()
    return _row(r) if r else None


def _fills_for(conn, request_id: str, site: str) -> list[dict]:
    rows = conn.execute(f"select {', '.join(FILL_COLUMNS)} from cart_fills where request_id = %s and site = %s "
                        "order by created_at desc, id desc", (request_id, site)).fetchall()
    return [_row(r) for r in rows]


def _connection_status(conn, user_id: str, site: str) -> str | None:
    row = conn.execute("select status from connections where user_id = %s and site = %s",
                       (str(user_id), db_site(site))).fetchone()
    return row[0] if row else None


def _priced_at(req: dict, card: dict):
    return card.get("priced_at") or req.get("priced_at")


def _stale(req: dict, card: dict, now: datetime | None, s: Settings) -> bool:
    priced = _priced_at(req, card)
    return priced is None or (usage.as_utc(now) - priced).total_seconds() > s.cart_fill_max_age_s


def _in_window(fill: dict, now: datetime | None, s: Settings) -> bool:
    done = fill.get("finished_at")
    return done is not None and usage.as_utc(now) - done <= timedelta(seconds=s.fill_confirm_window_s)


def _slot_taken(conn, request_id: str, site: str) -> bool:
    return conn.execute("select 1 from cart_fills where request_id = %s and site = %s and status = any(%s)",
                        (request_id, site, list(SLOT_STATUSES))).fetchone() is not None


# --- the Fill button ---------------------------------------------------------------------------------------------

def _card(state: str, label: str, sublabel: str | None = None, href: str | None = None, fill_id: str | None = None,
          preview: dict | None = None) -> dict:
    return {"state": state, "label": label, "sublabel": sublabel, "href": href, "fill_id": fill_id,
            "preview": preview}


def fill_options(conn, user_id: str, request_id: str, now: datetime | None, s: Settings) -> dict:
    """The Fill button of each ok card, in the precedence order of the plan (Plan 6 T5)."""
    req = _request(conn, user_id, request_id)
    if req["status"] != "done":
        raise NotReady()
    left = limits.cart_fills_left(conn, user_id, now, s)
    out = {"fills_left": left, "fills_limit": s.cart_fills_per_day, "sites": {}}
    cards = {site: c for site, c in _results(conn, req["id"]).items() if c["status"] == "ok"}
    paused = None
    for site, card in cards.items():
        app = _app(site)
        fill_label = f"Fill cart on {app}"
        if req["mode"] != "grocery":
            out["sites"][site] = _card("unavailable", fill_label, FOOD_NOTE)
            continue
        fills = _fills_for(conn, req["id"], site) if site in FILL_SITES else []
        active = next((f for f in fills if f["status"] in ACTIVE), None)
        filled = next((f for f in fills if f["status"] in FILLED), None)
        newest = fills[0] if fills else None
        if active:
            out["sites"][site] = _card("filling", "Filling your cart…", fill_id=active["id"])
        elif newest and newest["status"] == "needs_confirmation" and _in_window(newest, now, s):
            out["sites"][site] = _card("review", "See new prices", fill_id=newest["id"])
        elif filled:
            out["sites"][site] = _card("filled", "See what was added", fill_id=filled["id"])
        elif site not in FILL_SITES or not cart_recipe_ready(conn, site):
            out["sites"][site] = _card("unavailable", fill_label, f"Not available for {app} yet")
        else:
            if paused is None:
                paused = budget.is_paused(conn, now, s.app_tz)
            status = _connection_status(conn, user_id, site)
            if paused:
                out["sites"][site] = _card("paused", fill_label, limits.fill_paused(now, s).message)
            elif status == "needs_login":
                out["sites"][site] = _card("needs_login", f"Reconnect {_conn_name(site)} to fill", href=ACCOUNTS)
            elif status not in CONNECTED:
                out["sites"][site] = _card("connect", f"Connect {_conn_name(site)} to fill", href=ACCOUNTS)
            elif _stale(req, card, now, s):
                out["sites"][site] = _card("stale", fill_label, STALE_NOTE)
            elif left <= 0:
                out["sites"][site] = _card("limit", fill_label, "No fills left today")
            else:
                out["sites"][site] = _card("ready", fill_label,
                                           f"{left} fill left today" if left == 1 else f"{left} fills left today",
                                           preview=_preview(plan_items(req, card, site)))
    return out


# --- starting a fill ---------------------------------------------------------------------------------------------

def _check_connection(conn, user_id: str, site: str) -> None:
    status = _connection_status(conn, user_id, site)
    if status in CONNECTED:
        return
    name = _conn_name(site)
    if status == "needs_login":
        raise NeedsConnection(f"{name} logged you out. Reconnect it first.")
    if site == "instamart":
        raise NeedsConnection("Connect Swiggy first. Instamart uses your Swiggy login.")
    raise NeedsConnection(f"Connect {name} first.")


def _create(conn, user_id: str, request_id: str, site: str, plan: list[dict], priced_location: dict,
            now: datetime | None, s: Settings, confirm_location: dict | None = None,
            confirmed_from: str | None = None) -> dict:
    """Consume a fill, insert the row and enqueue its job, all or nothing."""
    try:
        with conn.transaction():
            left = limits.consume_cart_fill(conn, user_id, now, s)
            fill_id = str(conn.execute(
                "insert into cart_fills (user_id, request_id, site, plan, priced_location, confirm_location, "
                "confirmed_from, created_at) values (%s,%s,%s,%s,%s,%s,%s,%s) returning id",
                (str(user_id), request_id, site, Jsonb(plan), Jsonb(priced_location),
                 Jsonb(confirm_location) if confirm_location is not None else None, confirmed_from,
                 usage.as_utc(now))).fetchone()[0])
            job_id = jobs.enqueue(conn, str(user_id), "fill_cart", site, {"fill_id": fill_id})
            conn.execute("update cart_fills set job_id = %s where id = %s", (job_id, fill_id))
    except jobs.Conflict:
        raise FillRunning() from None
    except psycopg.errors.UniqueViolation as e:
        if e.diag.constraint_name == SLOT_INDEX:
            raise AlreadyFilled() from None
        raise
    log.info("fill queued on %s: %d planned items", site, len(plan))
    return {"fill_id": fill_id, "job_id": job_id, "fills_left": left}


def start_fill(conn, user_id: str, request_id: str, site: str, now: datetime | None, s: Settings) -> dict:
    """The owner's tap on "Fill cart on <app>". Returns {fill_id, job_id, fills_left}."""
    req = _request(conn, user_id, request_id)
    if req["status"] != "done":
        raise NotReady()
    if req["mode"] != "grocery":
        raise FoodNotFillable()
    card = _results(conn, req["id"]).get(site)
    if card is None or card["status"] != "ok":
        raise NoPrices()
    if site not in FILL_SITES or not cart_recipe_ready(conn, site):
        raise Unavailable()
    _check_connection(conn, user_id, site)
    if _stale(req, card, now, s):
        raise Stale()
    if _slot_taken(conn, req["id"], site):
        raise AlreadyFilled()
    plan = plan_items(req, card, site)
    if not any(p["action"] in ("add", "ask") for p in plan):
        raise NothingToAdd()
    priced = {"lat": req["lat"], "lon": req["lon"], "area": req["area"]}
    return _create(conn, user_id, req["id"], site, plan, priced, now, s)


def confirm_fill(conn, user_id: str, fill_id: str, now: datetime | None, s: Settings) -> dict:
    """ "Fill at these prices": a new fill pinned to the app's location (R6). Same shape as start_fill."""
    old = get_fill(conn, fill_id, user_id)
    if old is None or old["status"] != "needs_confirmation" or not _in_window(old, now, s):
        raise NotConfirmable()
    newest = _fills_for(conn, old["request_id"], old["site"])
    if not newest or newest[0]["id"] != old["id"]:
        raise NotConfirmable()
    site = old["site"]
    if not cart_recipe_ready(conn, site):
        raise Unavailable()
    _check_connection(conn, user_id, site)
    return _create(conn, user_id, old["request_id"], site, old["plan"], old["priced_location"], now, s,
                   confirm_location=old["app_location"], confirmed_from=old["id"])


# --- the worker's side -------------------------------------------------------------------------------------------

def _pending(p: dict) -> dict:
    return {"idx": p.get("idx"), "label": p.get("label"), "name": p.get("name"), "product_id": p.get("product_id"),
            "packs": p.get("packs"), "status": "pending", "added_qty": 0, "skip_reason": None}


def mark_started(conn, fill_id: str) -> None:
    f = get_fill(conn, fill_id)
    if f is None:
        return
    # Only a live fill starts: one that finalize_lost (or a finish) already ended stays ended.
    conn.execute("update cart_fills set status = 'filling', started_at = now(), items = %s "
                 "where id = %s and status in ('queued','filling')",
                 (Jsonb([_pending(p) for p in f["plan"]]), str(fill_id)))


def record_item(conn, fill_id: str, item: dict) -> None:
    """Replace the item with the same idx (or append it)."""
    with conn.transaction():
        row = conn.execute("select items from cart_fills where id = %s for update", (str(fill_id),)).fetchone()
        if row is None:
            return
        items = list(row[0] or [])
        at = next((n for n, i in enumerate(items) if isinstance(i, dict) and i.get("idx") == item.get("idx")), None)
        if at is None:
            items.append(item)
        else:
            items[at] = item
        conn.execute("update cart_fills set items = %s where id = %s", (Jsonb(items), str(fill_id)))


def _area(conn, request_id: str) -> str:
    row = conn.execute("select area from cart_requests where id = %s", (request_id,)).fetchone()
    return row[0] if row and row[0] else "your address"


def _refundable(outcome: dict) -> bool:
    """Nothing was added (not even partly). Interrupted fills (the cart may hold our adds) never get here."""
    if outcome.get("status") not in ("failed", "needs_confirmation"):
        return False
    return not any(isinstance(i, dict) and i.get("status") in ("added", "partial") for i in outcome.get("items") or [])


def _finish(conn, fill_id: str, outcome: dict, open_url: str | None, s: Settings,
            now: datetime | None = None) -> dict:
    if not _is_uuid(fill_id):
        return {}
    with conn.transaction():
        row = conn.execute("select status from cart_fills where id = %s for update", (str(fill_id),)).fetchone()
        if row is None:
            return {}
        f = get_fill(conn, fill_id)
        if row[0] not in ACTIVE:
            # Already ended (finalize_lost, or an earlier finish): a late finish never overwrites it.
            return f["summary"] or {}
        if outcome.get("status") == "failed" and outcome.get("error") in fill_summary.CART_UNKNOWN:
            # The engine stopped with adds possibly in the app's cart: interrupted keeps the slot (no re-fill)
            # and is never refunded.
            outcome = dict(outcome, status="interrupted")
        counted = f["counted"]
        if counted and _refundable(outcome):
            limits.refund_cart_fill(conn, f["user_id"], usage.today(f["created_at"], s.app_tz))
            counted = False
        merged = dict(f)
        merged.update(status=outcome.get("status") or "failed", error=outcome.get("error"),
                      items=outcome.get("items") or [], cart=outcome.get("cart"),
                      app_location=outcome.get("app_location"),
                      location_selected=bool(outcome.get("location_selected")), repriced=outcome.get("repriced"),
                      existing_changed=bool(outcome.get("existing_changed")), counted=counted, open_url=open_url)
        summary = fill_summary.build(merged, _area(conn, f["request_id"]), open_url)

        def j(v):
            return Jsonb(v) if v is not None else None
        conn.execute(
            "update cart_fills set status = %s, error = %s, items = %s, cart = %s, app_location = %s, "
            "location_selected = %s, repriced = %s, counted = %s, open_url = %s, summary = %s, "
            "finished_at = coalesce(%s, now()) where id = %s",
            (merged["status"], merged["error"], Jsonb(merged["items"]), j(merged["cart"]), j(merged["app_location"]),
             merged["location_selected"], j(merged["repriced"]), counted, open_url, Jsonb(summary),
             usage.as_utc(now) if now is not None else None, str(fill_id)))
    log.info("fill on %s finished: %s, %d items, counted=%s", f["site"], merged["status"], len(merged["items"]),
             counted)
    return summary


def finish_fill(conn, fill_id: str, outcome: dict, open_url: str | None, s: Settings) -> dict:
    """Write the engine's outcome and the summary; refund a fill that added nothing. Returns the summary."""
    return _finish(conn, fill_id, outcome, open_url, s)


def _failed_outcome(f: dict, error: str | None, status: str = "failed") -> dict:
    items = [dict(i, status="not_added") if isinstance(i, dict) and i.get("status") == "pending" and
             status == "failed" else i for i in f.get("items") or []]
    return {"status": status, "error": error, "existing_changed": False, "items": items, "cart": None,
            "app_location": f.get("app_location"), "location_selected": bool(f.get("location_selected")),
            "repriced": None}


def fail_fill(conn, fill_id: str, error: str, s: Settings) -> dict:
    """finish_fill with a failed outcome over the items recorded so far."""
    f = get_fill(conn, fill_id)
    if f is None:
        return {}
    return _finish(conn, fill_id, _failed_outcome(f, error), f.get("open_url") or OPEN_APP_URLS.get(f["site"]), s)


def finalize_lost(conn, s: Settings, now: datetime | None) -> int:
    """Fills whose job died (failed, finished or gone) while the fill was queued or filling (R9). A started fill may
    have added items: interrupted, still counted, and it keeps the slot. One never started is failed and refunded."""
    rows = conn.execute(
        "select f.id from cart_fills f left join jobs j on j.id = f.job_id "
        "where f.status in ('queued','filling') and (j.id is null or j.status in ('failed','done'))").fetchall()
    n = 0
    for (fid,) in rows:
        f = get_fill(conn, str(fid))
        if f is None or f["status"] not in ACTIVE:
            continue
        started = f["status"] == "filling" or f["started_at"] is not None
        outcome = _failed_outcome(f, "worker_lost", "interrupted" if started else "failed")
        _finish(conn, f["id"], outcome, f.get("open_url") or OPEN_APP_URLS.get(f["site"]), s, now)
        n += 1
    if n:
        log.warning("finalized %d lost fills", n)
    return n

"""Cart checks (Plan 5): which of the user's own connected apps can be priced, the list and its "usual" items,
starting a check (one daily check, one `search` job per priceable app), collecting per-app results, building the
ranked summary, "Pick one" choices, and the admin-edited fee rules.

Compares run only on apps the user has connected (their own session), each at that app's own saved address for
the user's chosen place (`addresses_store.saved_for`). A refused check (409/429/503) never uses one of the day's
checks; a check where every app failed is refunded. Nothing here logs item texts, addresses or coordinates."""
import logging
import re
import uuid
from datetime import datetime, timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from food_search import FOOD_SITES
from search import SEARCH_SITES
from server import addresses_store, cart_rank, fees, jobs, limits, store, usage
from server.config import SITE_NAMES, Settings

log = logging.getLogger("server.cart")

GROCERY_KINDS = ("search",)
FOOD_KINDS = ("restaurants", "menu")
CONNECTED = ("connected", "syncing")
QTY_MAX = 20
NO_SAVED_ADDRESS = "no_saved_address"


# --- errors -------------------------------------------------------------------------------------------------------

class CartError(Exception):
    status = 400
    message = "Bad request."

    def __init__(self, message: str | None = None):
        if message:
            self.message = message
        super().__init__(self.message)


class NoAddress(CartError):
    status, message = 409, "Add your delivery address first."


def _or_list(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"


class NoSavedAddress(NoAddress):
    """Every app that could be priced has no saved address at the user's chosen place."""

    def __init__(self, sites):
        self.sites = list(sites)
        super().__init__(f"Add this address in {_or_list([_app(s) for s in self.sites])} to compare here.")


class NoConnectedApp(CartError):
    status = 409

    def __init__(self, mode: str):
        self.mode = mode
        super().__init__("Connect Swiggy or Zomato to compare." if mode == "food"
                         else "Connect Blinkit, Zepto or Instamart to compare.")


class CheckRunning(CartError):
    status, message = 409, "A cart check is already running."


class Unavailable(CartError):
    status, message = 503, "Cart checks are unavailable right now."


class NotFound(CartError):
    status, message = 404, "not found"


class NotReady(CartError):
    status, message = 409, "Prices are still loading."


class BadChoice(ValueError):
    status, message = 422, "That isn't one of the options."


class InvalidRules(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


def _app(site: str) -> str:
    return SITE_NAMES.get(site, site.title())


def db_site(site: str) -> str:
    """The account a site belongs to (Instamart -> swiggy)."""
    return addresses_store.db_site(site)


# --- access -------------------------------------------------------------------------------------------------------

def site_access(conn, user_id: str, sites, kinds_by_site: dict) -> dict[str, str]:
    """{site: "ok" | "not_connected" | "needs_login" | "unavailable"} for the user's own connections.
    Connected = connected or syncing, or error after at least one sync. A connected site whose recipes (every kind in
    kinds_by_site[site]) are not all learned, healthy and unpaused is unavailable."""
    sites = list(sites)
    conns = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT site, status, last_synced_at FROM connections WHERE user_id=%s AND site = ANY(%s)",
        (user_id, sorted({db_site(x) for x in sites})))}
    recipes = {(r[0], r[1]): bool(r[2]) and not r[3] for r in conn.execute(
        "SELECT site, kind, healthy, paused FROM recipes WHERE site = ANY(%s)", (sorted(set(sites)),))}
    out = {}
    for site in sites:
        status, synced = conns.get(db_site(site), (None, None))
        if status in CONNECTED or (status == "error" and synced is not None):
            usable = all(recipes.get((site, k), False) for k in kinds_by_site.get(site) or ())
            out[site] = "ok" if usable else "unavailable"
        elif status == "needs_login":
            out[site] = "needs_login"
        else:
            out[site] = "not_connected"
    return out


def grocery_access(conn, user_id: str) -> dict[str, str]:
    return site_access(conn, user_id, SEARCH_SITES, {x: GROCERY_KINDS for x in SEARCH_SITES})


def food_access(conn, user_id: str) -> dict[str, str]:
    return site_access(conn, user_id, FOOD_SITES, {x: FOOD_KINDS for x in FOOD_SITES})


# --- items --------------------------------------------------------------------------------------------------------

def norm_name(text: str) -> str:
    """Same as db.norm_name and the SQL norm_name: lower case, non-word characters to spaces, collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def _qty(v) -> int:
    try:
        q = int(round(float(v)))
    except (TypeError, ValueError):
        q = 1
    return max(1, min(QTY_MAX, q))


def normalize_items(items, max_items: int) -> list[dict]:
    """Tidy the list: blank lines dropped, lines with the same norm_name (and food variant/veg) merged with their
    quantities summed (capped at 20), first spelling kept. ValueError when nothing is left or the list is too long."""
    out: list[dict] = []
    seen: dict[tuple, dict] = {}
    for it in items or ():
        text = " ".join(str(it.get("text") or "").split())
        name = norm_name(text)
        if not name:
            continue
        key = (name, it.get("variant"), it.get("veg"))
        if key in seen:
            seen[key]["qty"] = min(QTY_MAX, seen[key]["qty"] + _qty(it.get("qty")))
            continue
        row = {"text": text, "qty": _qty(it.get("qty"))}
        for extra in ("variant", "veg"):
            if it.get(extra) is not None:
                row[extra] = it[extra]
        seen[key] = row
        out.append(row)
    if not out:
        raise ValueError("the list is empty")
    if len(out) > max_items:
        raise ValueError(f"Keep the list to {max_items} items or fewer.")
    return out


def usual_items(conn, user_id: str, now: datetime | None, days: int = 90, min_times: int = 3,
                limit: int = 30, tz: str | None = None) -> list[dict]:
    """Grocery items (Blinkit, Zepto, Instamart) in min_times+ orders over the last `days` IST days; cancelled,
    refunded and failed orders never count. [{text, qty, times}]: the latest spelling and quantity."""
    since = (usage.today(now, tz) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT (array_agg(i.name ORDER BY o.placed_at DESC))[1],
                  (array_agg(i.qty ORDER BY o.placed_at DESC))[1],
                  count(DISTINCT o.site || ':' || o.order_id) AS times,
                  norm_name(i.name) AS n
             FROM orders o
             JOIN order_items i ON i.user_id = o.user_id AND i.site = o.site AND i.order_id = o.order_id
            WHERE o.user_id = %s
              AND (o.site IN ('blinkit', 'zepto') OR (o.site = 'swiggy' AND o.sub_platform = 'instamart'))
              AND coalesce(o.status, '') NOT IN ('cancelled', 'refunded', 'failed')
              AND o.placed_at >= %s
              AND norm_name(coalesce(i.name, '')) <> ''
            GROUP BY norm_name(i.name)
           HAVING count(DISTINCT o.site || ':' || o.order_id) >= %s
            ORDER BY times DESC, n
            LIMIT %s""",
        (user_id, since, min_times, limit)).fetchall()
    return [{"text": " ".join(r[0].split()), "qty": _qty(r[1]), "times": int(r[2])} for r in rows]


# --- starting a check ---------------------------------------------------------------------------------------------

def _check_running(conn, user_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM jobs WHERE user_id=%s AND type='search' AND status IN ('queued','running') "
        "AND payload->>'request_id' IS NOT NULL LIMIT 1", (user_id,)).fetchone() is not None


def create_check(conn, user_id: str, mode: str, items: list[dict], sites, access: dict[str, str],
                 now: datetime | None, s: Settings, restaurant: dict | None = None) -> dict:
    """Use one of today's checks and file the request. Returns {request_id, sites, cart_checks_left, enqueue,
    addresses}: `enqueue` are the apps to price, each at `addresses[site]` (that app's own saved address id).
    The caller enqueues their jobs inside its own transaction around this call, so a failed enqueue rolls the
    check back. Raises NoAddress / NoSavedAddress / NoConnectedApp / Unavailable / CheckRunning (nothing used),
    or limits.LimitExceeded / limits.BudgetPaused."""
    now = usage.as_utc(now)
    sites = list(sites)
    prof = store.get_profile(conn, user_id) or {}
    if prof.get("lat") is None or prof.get("lon") is None:
        raise NoAddress()
    ok = [x for x in sites if access.get(x) == "ok"]
    if not ok:
        if any(access.get(x) == "unavailable" for x in sites):
            raise Unavailable()
        raise NoConnectedApp(mode)
    if _check_running(conn, user_id):
        raise CheckRunning()
    places = {x: addresses_store.saved_for(conn, user_id, x) for x in ok}
    priced = [x for x in ok if places[x]]
    if not priced:
        raise NoSavedAddress(ok)
    with conn.transaction():
        left = limits.consume_cart_check(conn, user_id, now, s)
        request_id = str(conn.execute(
            "INSERT INTO cart_requests (user_id, mode, items, restaurant, sites, area, lat, lon, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (user_id, mode, Jsonb(items), Jsonb(restaurant) if restaurant is not None else None, sites,
             cart_rank.area_label(prof.get("address")), prof["lat"], prof["lon"], now)).fetchone()[0])
        for site in sites:
            if site in priced:
                continue
            status, error = (("unserviceable", NO_SAVED_ADDRESS) if site in ok
                             else (access.get(site) or "not_connected", None))
            conn.execute("INSERT INTO cart_results (request_id, user_id, site, status, error) VALUES (%s,%s,%s,%s,%s)",
                         (request_id, user_id, site, status, error))
    log.info("cart check %s: %d apps to price, %d not priced", request_id, len(priced), len(sites) - len(priced))
    return {"request_id": request_id, "sites": sites, "cart_checks_left": left, "enqueue": priced,
            "addresses": {x: places[x]["address_id"] for x in priced}}


def start_check(conn, user_id: str, items, now: datetime | None, s: Settings) -> dict:
    """A grocery check: one `search` job (op products) per priceable app, on that app's own profile."""
    items = normalize_items(items, s.cart_max_items)
    access = grocery_access(conn, user_id)
    try:
        with conn.transaction():
            out = create_check(conn, user_id, "grocery", items, SEARCH_SITES, access, now, s)
            for site in out["enqueue"]:
                jobs.enqueue(conn, user_id, "search", site, {"op": "products", "request_id": out["request_id"],
                                                             "address_id": out["addresses"][site]})
    except jobs.Conflict:
        raise CheckRunning() from None
    return out


# --- reading ------------------------------------------------------------------------------------------------------

def _uuid(v) -> str | None:
    try:
        return str(uuid.UUID(str(v)))
    except ValueError:
        return None


_REQUEST_COLUMNS = ("id, user_id, mode, items, restaurant, sites, status, area, choices, summary, error, "
                    "created_at, priced_at")


def _load(conn, request_id: str, lock: bool = False) -> dict | None:
    rid = _uuid(request_id)
    if rid is None:
        return None
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(f"SELECT {_REQUEST_COLUMNS} FROM cart_requests WHERE id=%s" + (" FOR UPDATE" if lock else ""),
                          (rid,)).fetchone()
    if row is not None:
        row["id"], row["user_id"] = str(row["id"]), str(row["user_id"])
    return row


def get_request(conn, user_id: str, request_id: str) -> dict | None:
    """The user's own request (never another user's), or None."""
    row = _load(conn, request_id)
    return row if row is not None and row["user_id"] == str(user_id) else None


def _results(conn, request_id: str) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT site, status, items, meta, error, priced_at FROM cart_results WHERE request_id=%s",
                           (request_id,)).fetchall()


# --- fee rules ----------------------------------------------------------------------------------------------------

def fee_rules(conn) -> dict[str, tuple[dict, bool]]:
    return {r[0]: (r[1], bool(r[2])) for r in conn.execute("SELECT site, rules, verified FROM fee_rules")}


def put_fee_rules(conn, site: str, rules, verified: bool, admin_user_id: str) -> int:
    """Validate and save one site's rules (food rules for Swiggy and Zomato, grocery rules for the rest); returns
    the new version. InvalidRules carries every problem."""
    if site not in cart_rank.SITE_ORDER:
        raise InvalidRules([f"unknown site {site!r}"])
    want = "food" if site in FOOD_SITES else "grocery"
    errors = []
    if isinstance(rules, dict) and rules.get("kind", "grocery") in ("grocery", "food") \
            and rules.get("kind", "grocery") != want:
        errors.append(f"{site} needs {want} fee rules")
    else:
        errors.extend(fees.validate(rules))
    if errors:
        raise InvalidRules(errors)
    return conn.execute(
        "INSERT INTO fee_rules (site, rules, verified, source, updated_by) VALUES (%s,%s,%s,'admin',%s) "
        "ON CONFLICT (site) DO UPDATE SET rules=excluded.rules, verified=excluded.verified, "
        "version=fee_rules.version + 1, source='admin', updated_at=now(), updated_by=excluded.updated_by "
        "RETURNING version", (site, Jsonb(rules), bool(verified), str(admin_user_id))).fetchone()[0]


# --- results and finalising ---------------------------------------------------------------------------------------

def record_site_result(conn, request_id: str, site: str, status: str, items=(), meta: dict | None = None,
                       error: str | None = None) -> bool:
    """Store one app's outcome while the request is still searching (a late result after finalising is dropped:
    returns False). NotFound when the request does not exist."""
    rid = _uuid(request_id)
    if rid is None or conn.execute("SELECT 1 FROM cart_requests WHERE id=%s", (rid,)).fetchone() is None:
        raise NotFound()
    return conn.execute(
        "INSERT INTO cart_results (request_id, user_id, site, status, items, meta, error) "
        "SELECT id, user_id, %s, %s, %s, %s, %s FROM cart_requests WHERE id=%s AND status='searching' "
        "ON CONFLICT (request_id, site) DO UPDATE SET status=excluded.status, items=excluded.items, "
        "meta=excluded.meta, error=excluded.error, priced_at=now(), subtotal=NULL, fees=NULL, total=NULL, "
        "fees_estimated=NULL",
        (site, status, Jsonb(list(items or [])), Jsonb(meta or {}), error, rid)).rowcount == 1


def _summarize(conn, req: dict, results: list[dict], s: Settings) -> dict:
    request = {"id": req["id"], "mode": req["mode"], "items": req["items"], "restaurant": req["restaurant"],
               "choices": req["choices"] or {}, "area": req["area"]}
    return cart_rank.build_summary(request, results, fee_rules(conn), s.split_min_saving_inr)


def _write_totals(conn, request_id: str, summary: dict) -> None:
    for card in summary["cards"]:
        if card["status"] != "ok":
            continue
        conn.execute("UPDATE cart_results SET subtotal=%s, fees=%s, total=%s, fees_estimated=%s "
                     "WHERE request_id=%s AND site=%s",
                     (card["subtotal"], Jsonb(card["fees"]) if card["fees"] is not None else None, card["total"],
                      card["fees_estimated"], request_id, card["site"]))


def _refund(conn, req: dict, s: Settings) -> None:
    """Give back the check this request used (on the IST day it was used)."""
    conn.execute("UPDATE usage SET cart_checks = cart_checks - 1 WHERE user_id=%s AND day=%s AND cart_checks > 0",
                 (req["user_id"], usage.today(req["created_at"], s.app_tz)))


def _fill_missing(conn, req: dict, have: set[str], sites, error: str) -> None:
    for site in sites:
        if site in req["sites"] and site not in have:
            conn.execute("INSERT INTO cart_results (request_id, user_id, site, status, error) "
                         "VALUES (%s,%s,%s,'failed',%s) ON CONFLICT (request_id, site) DO NOTHING",
                         (req["id"], req["user_id"], site, error))
            have.add(site)


def _finalize_locked(conn, request_id: str, s: Settings, missing_error: str | None = None) -> bool:
    req = _load(conn, request_id, lock=True)
    if req is None or req["status"] != "searching":
        return False
    have = {r["site"] for r in _results(conn, req["id"])}
    lost = [r[0] for r in conn.execute(
        "SELECT DISTINCT site FROM jobs WHERE type='search' AND status='failed' AND payload->>'request_id' = %s",
        (req["id"],))]
    _fill_missing(conn, req, have, lost, "worker_lost")
    if missing_error:
        _fill_missing(conn, req, have, req["sites"], missing_error)
    if not set(req["sites"]) <= have:
        return False
    try:
        summary = _summarize(conn, req, _results(conn, req["id"]), s)
    except Exception as e:  # never leave the user spinning: the check fails and is given back
        log.error("cart check %s: summary failed (%s)", req["id"], type(e).__name__)
        summary = None
    if summary is not None:
        _write_totals(conn, req["id"], summary)
    status = summary["status"] if summary is not None else "failed"
    error = None if status == "done" else (missing_error or ("all_failed" if summary is not None else "summary_failed"))
    conn.execute("UPDATE cart_requests SET status=%s, summary=%s, error=%s, priced_at=now() WHERE id=%s",
                 (status, Jsonb(summary) if summary is not None else None, error, req["id"]))
    if status == "failed":
        _refund(conn, req, s)
    log.info("cart check %s: %s", req["id"], status)
    return True


def finalize_if_complete(conn, request_id: str, s: Settings) -> bool:
    """Once every app has a result (a failed search job without one counts as failed / worker_lost), build the
    summary, write each app's fees and totals and close the request. Runs under the request's row lock, so two
    callers finalise it once. A check where nothing could be priced is refunded. True when this call closed it."""
    with conn.transaction():
        return _finalize_locked(conn, request_id, s)


def finalize_stale(conn, s: Settings, now: datetime | None = None) -> int:
    """Close checks still searching after cart_stale_s: apps without a result fail with "timeout"."""
    cutoff = usage.as_utc(now) - timedelta(seconds=s.cart_stale_s)
    ids = [str(r[0]) for r in conn.execute(
        "SELECT id FROM cart_requests WHERE status='searching' AND created_at < %s ORDER BY created_at", (cutoff,))]
    done = 0
    for rid in ids:
        with conn.transaction():
            done += _finalize_locked(conn, rid, s, missing_error="timeout")
    return done


# --- choices ------------------------------------------------------------------------------------------------------

def set_choice(conn, user_id: str, request_id: str, idx: int, site: str, choice_id, s: Settings) -> dict:
    """Answer a "Pick one" (or change a match) with one of that app's listed options, and return the recomputed
    summary. Never uses a check."""
    with conn.transaction():
        req = _load(conn, request_id, lock=True)
        if req is None or req["user_id"] != str(user_id):
            raise NotFound()
        if req["status"] == "searching":
            raise NotReady()
        if req["status"] != "done" or not isinstance(idx, int) or isinstance(idx, bool) \
                or not 0 <= idx < len(req["items"] or []):
            raise BadChoice()
        results = _results(conn, req["id"])
        result = next((r for r in results if r["site"] == site and r["status"] == "ok"), None)
        match = next((m for m in (result or {}).get("items") or [] if isinstance(m, dict) and m.get("idx") == idx),
                     None)
        options = [c for c in (match or {}).get("candidates") or [] if cart_rank._usable(c)]
        if not any(str(cart_rank._cand_id(c)) == str(choice_id) for c in options):
            raise BadChoice()
        choices = dict(req["choices"] or {})
        choices[str(idx)] = {**(choices.get(str(idx)) or {}), site: str(choice_id)}
        req["choices"] = choices
        summary = _summarize(conn, req, results, s)
        _write_totals(conn, req["id"], summary)
        conn.execute("UPDATE cart_requests SET choices=%s, summary=%s WHERE id=%s",
                     (Jsonb(choices), Jsonb(summary), req["id"]))
    return summary

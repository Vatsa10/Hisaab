"""Food compare store (Plan 5 T12): usual restaurants and dishes, per-user restaurant and menu caches, the capped
lookups, restaurant pairing and food checks. Only connected Swiggy / Zomato accounts are ever priced, the menu URL
always comes from the user's own search cache (never from the client), and nothing here logs names, queries or
coordinates."""
from datetime import datetime, timedelta

from psycopg.types.json import Jsonb

import db
from food_search import FOOD_SITES
from server import budget, cart, cart_rank, jobs, limits, match, restaurant_match, usage
from server.config import Settings


NOT_FOOD_PLATFORM = "coalesce(o.sub_platform, '') <> 'instamart'"
LIVE_ORDER = "coalesce(o.status, '') not in ('cancelled', 'refunded', 'failed')"
FOOD_ORDER = f"o.site in ('swiggy', 'zomato') and {NOT_FOOD_PLATFORM} and {LIVE_ORDER}"
PURGE_AFTER_S = 86400


class LookupBusy(Exception):
    """Another restaurant or menu lookup of this user's is still running for that app."""


# --- usual -------------------------------------------------------------------------------------------------------

def usual_restaurants(conn, user_id: str, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        f"select (array_agg(o.merchant order by o.placed_at desc))[1], array_agg(distinct o.site order by o.site), "
        f"count(*), max(o.placed_at) from orders o where o.user_id = %s and {FOOD_ORDER} "
        f"and o.merchant is not null and norm_name(o.merchant) <> '' group by norm_name(o.merchant) "
        f"order by count(*) desc, max(o.placed_at) desc, 1 limit %s", (user_id, limit)).fetchall()
    return [{"name": r[0], "sites": r[1], "orders": r[2], "last_order_at": r[3]} for r in rows]


def usual_dishes(conn, user_id: str, restaurant_name: str, limit: int = 15) -> list[dict]:
    rows = conn.execute(
        f"select (array_agg(i.name order by o.placed_at desc))[1] as text, "
        f"(array_agg(i.qty order by o.placed_at desc))[1], count(distinct (i.site, i.order_id)) as times "
        f"from order_items i join orders o on o.user_id = i.user_id and o.site = i.site and o.order_id = i.order_id "
        f"where o.user_id = %s and norm_name(o.merchant) = norm_name(%s) and {FOOD_ORDER} "
        f"and i.name is not null and norm_name(i.name) <> '' group by norm_name(i.name) "
        f"order by times desc, text limit %s", (user_id, restaurant_name, limit)).fetchall()
    return [{"text": r[0], "qty": max(1, round(r[1] or 1)), "times": r[2]} for r in rows]


# --- caches ------------------------------------------------------------------------------------------------------

def geo_key(lat: float, lon: float) -> str:
    return f"{lat:.2f},{lon:.2f}"


def _s(s: Settings | None) -> Settings:
    from server.config import settings
    return s or settings()


def get_restaurants(conn, user_id: str, site: str, geo_key: str, query: str, now: datetime | None = None,
                    s: Settings | None = None) -> list[dict] | None:
    """The user's cached results for this search, or None when absent or older than the TTL."""
    oldest = usage.as_utc(now) - timedelta(seconds=_s(s).menu_cache_ttl_s)
    row = conn.execute("select results from restaurant_cache where user_id = %s and site = %s and geo_key = %s "
                       "and query_norm = %s and fetched_at >= %s",
                       (user_id, site, geo_key, db.norm_name(query), oldest)).fetchone()
    return row[0] if row else None


def put_restaurants(conn, user_id: str, site: str, geo_key: str, query: str, results: list[dict],
                    now: datetime | None = None) -> None:
    conn.execute("insert into restaurant_cache (user_id, site, geo_key, query_norm, results, fetched_at) "
                 "values (%s,%s,%s,%s,%s,%s) on conflict (user_id, site, geo_key, query_norm) "
                 "do update set results = excluded.results, fetched_at = excluded.fetched_at",
                 (user_id, site, geo_key, db.norm_name(query), Jsonb(list(results)), usage.as_utc(now)))


def get_menu(conn, user_id: str, site: str, restaurant_id: str, now: datetime | None = None,
             s: Settings | None = None) -> dict | None:
    """The user's cached menu (with `fetched_at` added), or None when absent or older than the TTL."""
    oldest = usage.as_utc(now) - timedelta(seconds=_s(s).menu_cache_ttl_s)
    row = conn.execute("select menu, fetched_at from menu_cache where user_id = %s and site = %s "
                       "and restaurant_id = %s and fetched_at >= %s",
                       (user_id, site, str(restaurant_id), oldest)).fetchone()
    return {**row[0], "fetched_at": row[1].isoformat()} if row else None


def put_menu(conn, user_id: str, site: str, restaurant_id: str, menu: dict, now: datetime | None = None) -> None:
    menu = {k: v for k, v in menu.items() if k != "fetched_at"}
    conn.execute("insert into menu_cache (user_id, site, restaurant_id, menu, fetched_at) values (%s,%s,%s,%s,%s) "
                 "on conflict (user_id, site, restaurant_id) do update set menu = excluded.menu, "
                 "fetched_at = excluded.fetched_at",
                 (user_id, site, str(restaurant_id), Jsonb(menu), usage.as_utc(now)))


def purge_cache(conn, older_than_s: int = PURGE_AFTER_S, now: datetime | None = None) -> int:
    oldest = usage.as_utc(now) - timedelta(seconds=older_than_s)
    n = conn.execute("delete from restaurant_cache where fetched_at < %s", (oldest,)).rowcount
    return n + conn.execute("delete from menu_cache where fetched_at < %s", (oldest,)).rowcount


def _cached_listing(conn, user_id: str, site: str, restaurant_id: str) -> dict | None:
    """A restaurant row from this user's own search results; the only source of its id check and menu URL."""
    row = conn.execute("select r.value from restaurant_cache c, jsonb_array_elements(c.results) r "
                       "where c.user_id = %s and c.site = %s and r.value->>'restaurant_id' = %s "
                       "order by c.fetched_at desc limit 1", (user_id, site, str(restaurant_id))).fetchone()
    return row[0] if row else None


# --- lookups (R18) -----------------------------------------------------------------------------------------------

def lookups_left(conn, user_id: str, now: datetime | None, s: Settings) -> int:
    """Restaurant and menu lookups not attached to a check, started today (IST), against FOOD_LOOKUPS_PER_DAY."""
    start, end = usage.day_bounds(now, s.app_tz)
    used = conn.execute(
        "select count(*) from jobs where user_id = %s and type = 'search' and payload->>'op' in ('restaurants','menu') "
        "and payload->>'request_id' is null and created_at >= %s and created_at < %s",
        (user_id, start, end)).fetchone()[0]
    return max(0, s.food_lookups_per_day - used)


def _location(conn, user_id: str) -> tuple[float, float]:
    row = conn.execute("select lat, lon from profiles where user_id = %s", (user_id,)).fetchone()
    if row is None or row[0] is None or row[1] is None:
        raise cart.NoAddress()
    return float(row[0]), float(row[1])


def _access(conn, user_id: str, kind: str) -> dict[str, str]:
    return cart.site_access(conn, user_id, FOOD_SITES, {site: (kind,) for site in FOOD_SITES})


def _need_one(access: dict[str, str], sites) -> None:
    """Compares run only on connected apps: raise when none of `sites` can be priced."""
    if any(access[x] == "ok" for x in sites):
        return
    if any(access[x] == "unavailable" for x in sites):
        raise cart.Unavailable()
    raise cart.NoConnectedApp("food")


def _active(conn, user_id: str, site: str, op: str, key: str, value: str) -> str | None:
    """The id of this user's running or queued lookup job for the same thing, so polling never enqueues twice."""
    row = conn.execute("select id, payload from jobs where user_id = %s and site = %s and type = 'search' "
                       "and status in ('queued','running') and payload->>'op' = %s "
                       "and payload->>'request_id' is null", (user_id, site, op)).fetchone()
    if row and db.norm_name(str(row[1].get(key) or "")) == db.norm_name(value):
        return str(row[0])
    return None


def _admit(conn, user_id: str, count: int, now: datetime | None, s: Settings) -> None:
    """Refuse new lookups while the budget is paused, or when today's cap is spent."""
    if count <= 0:
        return
    if budget.is_paused(conn, now, s.app_tz):
        resumes = limits._first_of_next_month(now, s.app_tz)
        raise limits.BudgetPaused("cart", resumes, f"Cart checks are paused until {resumes.day} {resumes:%B}.")
    if lookups_left(conn, user_id, now, s) < count:
        raise limits.LimitExceeded(
            "food_lookup", s.food_lookups_per_day, limits.seconds_until_reset(now, s.app_tz),
            f"You can search restaurants {s.food_lookups_per_day} times a day. Try again tomorrow.")


def _enqueue_lookups(conn, user_id: str, wanted: list[tuple[str, dict, str, str]], now, s) -> list[str]:
    """wanted: (site, payload, key, value). Reuses a running job for the same thing; counts only new ones."""
    ids, fresh = [], []
    for site, payload, key, value in wanted:
        if (existing := _active(conn, user_id, site, payload["op"], key, value)) is not None:
            ids.append(existing)
        else:
            fresh.append((site, payload))
    _admit(conn, user_id, len(fresh), now, s)
    try:
        with conn.transaction():
            ids += [jobs.enqueue(conn, user_id, "search", site, payload) for site, payload in fresh]
    except jobs.Conflict:
        raise LookupBusy() from None
    return ids


def lookup_restaurants(conn, user_id: str, query: str, now: datetime | None, s: Settings) -> dict:
    lat, lon = _location(conn, user_id)
    gk = geo_key(lat, lon)
    access = _access(conn, user_id, "restaurants")
    ok = [x for x in FOOD_SITES if access[x] == "ok"]
    _need_one(access, FOOD_SITES)
    cached, missing = {}, []
    for site in ok:
        rows = get_restaurants(conn, user_id, site, gk, query, now, s)
        if rows is None:
            missing.append(site)
        else:
            cached[site] = rows
    if missing:
        ids = _enqueue_lookups(conn, user_id, [(x, {"op": "restaurants", "query": query}, "query", query)
                                               for x in missing], now, s)
        return {"status": "searching", "job_ids": ids, "access": access}
    by_site = {site: [x for r in rows if (x := restaurant_match.listing_from_raw(site, r))]
               for site, rows in cached.items()}
    return {"status": "ready", "results": restaurant_match.pair_results(by_site, lat, lon, access),
            "access": access}


def _snapshot(site: str, raw: dict, lat: float, lon: float) -> dict:
    x = restaurant_match.listing_from_raw(site, raw)
    return {"restaurant_id": x.restaurant_id, "name": x.name, "locality": x.locality, "lat": x.lat, "lon": x.lon,
            "distance_km": restaurant_match._distance(x, lat, lon)}


def _known_listings(conn, user_id: str, ids: dict[str, str]) -> dict[str, dict]:
    out = {}
    for site, rid in ids.items():
        raw = _cached_listing(conn, user_id, site, rid)
        if raw is None or restaurant_match.listing_from_raw(site, raw) is None:
            raise ValueError("Search for the restaurant again.")
        out[site] = raw
    return out


def _dish_rows(menu: dict) -> list[dict]:
    rows = []
    for raw in menu.get("dishes") or []:
        d = match.dish_from_raw(raw)
        if d is not None:
            rows.append({"dish_id": d.dish_id, "name": d.name, "variant": d.variant, "veg": d.veg,
                         "price": d.price, "price_label": cart_rank.inr(d.price), "in_stock": d.in_stock})
    return rows


def lookup_menus(conn, user_id: str, listings: dict[str, str], now: datetime | None, s: Settings) -> dict:
    """listings: {site: restaurant id}. The ids must come from the user's own restaurant search."""
    ids = {site: str(rid) for site, rid in listings.items() if site in FOOD_SITES and rid}
    if not ids:
        raise ValueError("Choose a restaurant first.")
    lat, lon = _location(conn, user_id)
    access = _access(conn, user_id, "menu")
    _need_one(access, list(ids))
    known = _known_listings(conn, user_id, ids)
    ok = [x for x in FOOD_SITES if x in ids and access[x] == "ok"]
    menus, missing = {}, []
    for site in ok:
        menu = get_menu(conn, user_id, site, ids[site], now, s)
        if menu is None:
            missing.append(site)
        else:
            menus[site] = menu
    if missing:
        wanted = [(x, {"op": "menu", "restaurant_id": ids[x], "url": known[x].get("url")}, "restaurant_id", ids[x])
                  for x in missing]
        return {"status": "searching", "job_ids": _enqueue_lookups(conn, user_id, wanted, now, s),
                "access": access}
    snaps = {site: _snapshot(site, raw, lat, lon) for site, raw in known.items()}
    first = next(iter(snaps.values()))
    primary = next(site for site in FOOD_SITES if site in menus)
    return {"status": "ready", "access": access,
            "restaurant": {"name": first["name"], "locality": first["locality"], "listings": snaps},
            "menu": _dish_rows(menus[primary]),
            "usual_dishes": usual_dishes(conn, user_id, first["name"])}


# --- pricing -----------------------------------------------------------------------------------------------------

def price_menu(site: str, menu: dict, items: list[dict]) -> tuple[str, list[dict], dict]:
    """(status, matched items, meta) for one app's menu. A closed restaurant is priced as "closed"; a menu with no
    readable dish is "failed". Observed packaging is the dishes' own, only when every priced dish has it."""
    dishes = [d for raw in menu.get("dishes") or [] if (d := match.dish_from_raw(raw)) is not None]
    is_open = menu.get("is_open") if isinstance(menu.get("is_open"), bool) else None
    fee_info = dict(menu.get("fee_info") or {})
    meta = {"fee_info": fee_info, "is_open": is_open, "menu_fetched_at": menu.get("fetched_at")}
    if is_open is False:
        return "closed", [], meta
    if not dishes:
        return "failed", [], meta
    matched = [match.match_dish(match.parse_dish_query(i, it["text"], it.get("qty") or 1, it.get("variant"),
                                                       it.get("veg")), dishes)
               for i, it in enumerate(items)]
    priced = [m["product"] for m in matched if m["status"] == "matched"]
    if priced and all(p.get("packaging") is not None for p in priced):
        fee_info.setdefault("packaging", round(sum(p["packaging"] * (p.get("packs") or 1) for p in priced), 2))
    return "ok", matched, meta


def record_menu(conn, request_id: str, site: str, menu: dict, items: list[dict], distance_km: float | None) -> None:
    """Price one app's menu for a check and store its result row (the worker uses this for menu jobs too)."""
    status, matched, meta = price_menu(site, menu, items)
    meta["distance_km"] = distance_km
    cart.record_site_result(conn, request_id, site, status, matched, meta)


# --- checks ------------------------------------------------------------------------------------------------------

def start_food_check(conn, user_id: str, restaurant: dict, items: list[dict], now: datetime | None,
                     s: Settings) -> dict:
    """Start a food compare. Raises cart.NoConnectedApp / NoAddress / CheckRunning / Unavailable, limits errors and
    ValueError (422) for a bad restaurant or dish list. Counts as one cart check; refused ones count none."""
    access = _access(conn, user_id, "menu")
    _need_one(access, FOOD_SITES)
    lat, lon = _location(conn, user_id)
    ids = {site: str(restaurant[f"{site}_id"]) for site in FOOD_SITES if restaurant.get(f"{site}_id")}
    if not ids:
        raise ValueError("Choose a restaurant first.")
    if not items or len(items) > s.food_max_items:
        raise ValueError(f"Pick between 1 and {s.food_max_items} dishes.")
    known = _known_listings(conn, user_id, ids)
    if len(ids) == 2 and not restaurant.get("confirmed"):
        a, b = (restaurant_match.listing_from_raw(site, known[site]) for site in FOOD_SITES)
        if not match._is_sure(restaurant_match.score(a, b)):
            raise ValueError("Confirm it's the same restaurant first.")
    snaps = {site: _snapshot(site, raw, lat, lon) for site, raw in known.items()}
    first = next(iter(snaps.values()))
    declined = [x for x in restaurant.get("declined") or [] if x in FOOD_SITES]
    snapshot = {"name": first["name"], "locality": first["locality"], "listings": snaps,
                "confirmed": bool(restaurant.get("confirmed")), "declined": declined}
    access = {site: access[site] if site in ids else "not_listed" for site in FOOD_SITES}

    with conn.transaction():
        out = cart.create_check(conn, user_id, "food", items, list(FOOD_SITES), access, now, s, restaurant=snapshot)
        rid = out["request_id"]
        for site in out["enqueue"]:
            menu = get_menu(conn, user_id, site, ids[site], now, s)
            if menu is not None:
                record_menu(conn, rid, site, menu, items, snaps[site]["distance_km"])
                continue
            payload = {"op": "menu", "restaurant_id": ids[site], "url": known[site].get("url"), "request_id": rid}
            try:
                jobs.enqueue(conn, user_id, "search", site, payload)
            except jobs.Conflict:
                raise cart.CheckRunning() from None
    cart.finalize_if_complete(conn, rid, s)
    return {"request_id": rid, "sites": out["sites"], "cart_checks_left": out["cart_checks_left"]}

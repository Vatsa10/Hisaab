"""Food search replay: restaurant search and menu fetch on the user's own logged-in session, read-only.

Two recipe kinds, both learned by an admin (food_learn) and only replayed here:
- "restaurants": a search request that binds the query text and the user's lat/lon, mapping a restaurant list.
- "menu": a menu request bound by the restaurant id (restaurant_param) and/or a restaurant page
  (page_url_template "...{restaurant_id}..."), mapping dishes that may sit in nested categories ("a.*.items.*").

Each call opens the caller's (the user's) Chrome profile with the read-only guard (never login mode, never a
click), checks the session is still logged in, then tries the fast path (the request replayed from inside the
page) and falls back to opening the page and reading the site's own captured response. Errors match the grocery
search: SearchDrift when both paths fail, Unserviceable when the user's location cannot be served, and
browser.SessionExpired for a logged-out session. Logs carry the site and counts only.
"""
import inspect
import json
import logging
import re
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import agent
import browser
import recipes
import sync

log = logging.getLogger("food_search")

FOOD_SITES = ("swiggy", "zomato")
FOOD_PAGES = {"swiggy": "https://www.swiggy.com/search?query={q}",
              "zomato": "https://www.zomato.com/{city}/delivery?q={q}"}
RESTAURANT_PROBE = "pizza"
FEE_KEYS = ("delivery", "packaging", "platform", "small_order", "surge", "member", "distance_km")
CAPTURE_WAIT_MS = 8000
CAPTURE_POLL_MS = 500


# Same classes and meaning as the grocery search module's (Plan 5 Task 4, built in parallel). Once both are on
# main these should become imports from search so the worker catches one set of classes.
class SearchError(Exception):
    pass


class SearchDrift(SearchError):
    """The recipe no longer works: the fast path and the page capture both failed."""


class Unserviceable(SearchError):
    """The app cannot serve the user's delivery location."""


# --- path language: "*" fans out ---------------------------------------------

def collect(data, path) -> list:
    """Every value at a dotted path, where "*" fans out over a list (or an id-keyed map). Missing branches are
    skipped, so "data.cards.*.items.*" yields every item of every category that has items. recipes.get_path
    stays the single-value form."""
    if path is None or path == "":
        return [] if data is None else [data]
    if isinstance(path, str) and path.startswith("="):
        return [path[1:]]
    path = re.sub(r"\[(-?\d+|\*)\]", r".\1", str(path))
    nodes = [data]
    for part in path.split("."):
        nxt = []
        for n in nodes:
            if part == "*":
                nxt.extend(n if isinstance(n, list) else n.values() if isinstance(n, dict) else ())
            elif isinstance(n, list) and part.lstrip("-").isdigit():
                i = int(part)
                if -len(n) <= i < len(n):
                    nxt.append(n[i])
            elif isinstance(n, dict) and part in n:
                nxt.append(n[part])
        nodes = [x for x in nxt if x is not None]
    return nodes


def _rows(data, path) -> list[dict]:
    """The row objects at a collection path: a path to a list yields its elements, a "*" path its matches."""
    out = []
    for r in collect(data, path):
        out.extend(r if isinstance(r, list) else [r])
    return [r for r in out if isinstance(r, dict)]


def _first(data, path):
    if path is None or path == "":
        return None
    if "*" in str(path):
        vals = collect(data, path)
        return vals[0] if vals else None
    return recipes.get_path(data, path)


# --- value normalisation -----------------------------------------------------

_OOS = {"out_of_stock", "sold_out", "unavailable", "oos", "false", "0"}


def to_stock(v) -> bool:
    """Same rules as the grocery search's to_stock: unknown means in stock."""
    if v is None:
        return True
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    s = str(v).strip().lower()
    return not (s in _OOS or "out of stock" in s or "sold out" in s)


def to_veg(v) -> bool | None:
    """Veg flag: True veg, False non-veg (egg counts as non-veg), None unknown."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    s = re.sub(r"[^a-z]", "", str(v).lower())
    if s in ("veg", "vegetarian", "pureveg", "true", "yes"):
        return True
    if s.startswith("non") or s in ("egg", "eggetarian", "false", "no"):
        return False
    return None


def to_open(v) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    s = str(v).strip().lower()
    if s in ("open", "opened", "true", "1", "yes", "online", "available", "open_now"):
        return True
    if "close" in s or s in ("false", "0", "no", "offline", "unavailable"):
        return False
    return None


def _text(v) -> str | None:
    if v is None or isinstance(v, (dict, list)):
        return None
    s = str(v).strip()
    return s or None


def _id(v) -> str | None:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return _text(v)


# --- extraction --------------------------------------------------------------

def extract_restaurants(data, field_map: dict) -> list[dict]:
    """Rows in server/restaurant_match.listing_from_raw's shape. Rows without an id or a name are dropped."""
    fm = field_map or {}
    out = []
    for row in _rows(data, fm.get("restaurants_path")):
        rid, name = _id(recipes.get_path(row, fm.get("restaurant_id"))), _text(recipes.get_path(row, fm.get("name")))
        if not rid or not name:
            continue
        out.append({
            "restaurant_id": rid, "name": name,
            "locality": _text(recipes.get_path(row, fm.get("locality"))),
            "lat": recipes.to_number(recipes.get_path(row, fm.get("lat"))),
            "lon": recipes.to_number(recipes.get_path(row, fm.get("lon"))),
            "distance_km": recipes.to_number(recipes.get_path(row, fm.get("distance_km"))),
            "is_open": to_open(recipes.get_path(row, fm.get("is_open"))),
            "url": _text(recipes.get_path(row, fm.get("url"))),
        })
    return out


def extract_menu(data, field_map: dict) -> dict:
    """{"is_open", "dishes"}; restaurant_open is a path from the response root, dish fields are relative to one
    dish. Rows without an id, a name or a price > 0 are dropped."""
    fm = field_map or {}
    div = float(fm.get("price_divisor") or 1)
    dishes = []
    for row in _rows(data, fm.get("dishes_path")):
        did, name = _id(recipes.get_path(row, fm.get("dish_id"))), _text(recipes.get_path(row, fm.get("name")))
        price = recipes.to_number(recipes.get_path(row, fm.get("price")))
        if not did or not name or price is None or price <= 0:
            continue
        packaging = recipes.to_number(recipes.get_path(row, fm.get("packaging")))
        dishes.append({
            "dish_id": did, "name": name,
            "variant": _text(recipes.get_path(row, fm.get("variant"))),
            "price": round(price / div, 2),
            "veg": to_veg(recipes.get_path(row, fm.get("veg"))),
            "in_stock": to_stock(recipes.get_path(row, fm.get("in_stock"))),
            "packaging": round(packaging / div, 2) if packaging is not None else None,
        })
    return {"is_open": to_open(_first(data, fm.get("restaurant_open"))), "dishes": dishes}


def extract_fee_info(data, fee_map: dict | None) -> dict:
    """Only the fees whose path resolves. Money is divided by fee_map.divisor; distance_km is not; member is a
    bool or the membership's name."""
    if not fee_map:
        return {}
    div = float(fee_map.get("divisor") or 1)
    out = {}
    for key in FEE_KEYS:
        v = _first(data, fee_map.get(key))
        if v is None:
            continue
        if key == "member":
            if isinstance(v, bool):
                out[key] = v
            elif isinstance(v, (int, float)):
                out[key] = v > 0
            elif isinstance(v, str) and v.strip():
                out[key] = v.strip()
            continue
        n = recipes.to_number(v)
        if n is not None:
            out[key] = n if key == "distance_km" else round(n / div, 2)
    return out


# --- requests ----------------------------------------------------------------

def _set_input(req: dict, where: str, name: str, value) -> None:
    """Bind one value into the query string, a header or a dotted JSON body path. A body value keeps the type
    the learned body had there (a string stays a string)."""
    if where == "query":
        parts = urlsplit(req["url"])
        q = dict(parse_qsl(parts.query)) | {name: str(value)}
        req["url"] = urlunsplit(parts._replace(query=urlencode(q)))
    elif where == "header":
        req["headers"] = {**(req.get("headers") or {}), name: str(value)}
    elif where == "body":
        body = req["body"] if isinstance(req["body"], dict) else json.loads(req["body"] or "{}")
        old = recipes.get_path(body, name)
        if isinstance(old, str):
            value = str(value)
        elif isinstance(old, int) and not isinstance(old, bool) and re.fullmatch(r"\d+", str(value)):
            value = int(value)
        recipes.set_path(body, name, value)
        req["body"] = body
    else:
        raise ValueError(f"unknown input location {where!r}")


def _param_where(req: dict) -> str:
    return "query" if req["method"] == "GET" or req["body"] is None else "body"


def _needs_location(recipe: dict) -> bool:
    return bool(recipe.get("location_inputs"))


def build_request(recipe: dict, location: dict, param: str | None = None, value=None) -> dict:
    """The recipe's request with `value` bound at `param` (query string for GET, body path for POST) and the
    user's lat/lon bound at every location input."""
    req = recipes.first_request(recipe)
    if param:
        _set_input(req, _param_where(req), param, value)
    for inp in recipe.get("location_inputs") or []:
        v = (location or {}).get(inp["value"])
        if v is None:
            raise Unserviceable("no delivery location saved")
        _set_input(req, inp["in"], inp["name"], v)
    return req


def food_page_url(site: str, query: str, location: dict | None, template: str | None = None) -> str | None:
    """A search page URL ({q}, and {city} from the location); None when the template needs a city we lack."""
    template = template or FOOD_PAGES[site]
    city = browser.city_slug(location if location is not None else {}) if "{city}" in template else None
    if "{city}" in template and not city:
        return None
    return template.replace("{city}", city or "").replace("{q}", quote(query))


def _open(site: str, recipe: dict, profile_dir, location: dict, proxy_server, block_media: bool):
    """browser.open_site on the caller's profile with the read-only guard: never login_mode. block_media is
    passed when the browser layer supports it (Plan 4 R12)."""
    kw = {"allowlist": list(recipe.get("read_post_allowlist") or []), "profile_dir": profile_dir,
          "location": location, "proxy_server": proxy_server}
    params = inspect.signature(browser.open_site).parameters
    if "block_media" in params or any(p.kind is p.VAR_KEYWORD for p in params.values()):
        kw["block_media"] = block_media
    return browser.open_site(site, **kw)


def _goto(page, url: str) -> None:
    browser.goto(page, url)
    sync.ensure_logged_in(page)


def _wait_capture(page, capture, recipe: dict, mark: int) -> list[dict]:
    """Successful responses of the recipe's endpoint seen since `mark`, waiting up to CAPTURE_WAIT_MS."""
    waited = 0
    while True:
        hits = agent.captured_for(recipe, capture.items[mark:])
        if hits or waited >= CAPTURE_WAIT_MS:
            return hits
        page.wait_for_timeout(CAPTURE_POLL_MS)
        waited += CAPTURE_POLL_MS


def _page_capture(page, capture, recipe: dict, url: str | None) -> list[dict]:
    if not url:
        return []
    mark = len(capture.items)
    _goto(page, url)
    return _wait_capture(page, capture, recipe, mark)


def _fast(page, req: dict):
    """The fast path's response, or None when it failed for any reason other than a logout."""
    try:
        return browser.fetch_in_page(page, req)
    except browser.SessionExpired:
        raise
    except Exception as e:
        log.info("fast path failed: %s", type(e).__name__)
        return None


def _absolute(url: str | None, base: str) -> str | None:
    return urljoin(base, url) if url else None


# --- replay ------------------------------------------------------------------

def _has_rows_path(data, path) -> bool:
    """The restaurant list's path exists (an empty list is a real "no results", not drift)."""
    if not path:
        return isinstance(data, list)
    return bool(collect(data, re.sub(r"\.\*$", "", str(path))))


def search_restaurants(site: str, query: str, location: dict, recipe: dict, profile_dir, proxy_server=None,
                       block_media=True, max_results=20) -> dict:
    """{"restaurants": [listing rows, url absolute], "fee_info": {...}} for one query at the user's location."""
    if site not in FOOD_SITES:
        raise ValueError(f"not a food site: {site}")
    if _needs_location(recipe) and ((location or {}).get("lat") is None or (location or {}).get("lon") is None):
        raise Unserviceable("no delivery location saved")
    fm = recipe["field_map"]
    with _open(site, recipe, profile_dir, location, proxy_server, block_media) as (_ctx, page, _guard, capture):
        home = browser.home_url(site, location)
        _goto(page, home)
        data = _fast(page, build_request(recipe, location, recipe.get("query_param"), query))
        fast = data is not None and _has_rows_path(data, fm.get("restaurants_path"))
        if not fast:
            page_url = food_page_url(site, query, location, recipe.get("search_page_url"))
            hits = [h for h in _page_capture(page, capture, recipe, page_url)
                    if _has_rows_path(h["json"], fm.get("restaurants_path"))]
            if not hits:
                raise SearchDrift(f"{site} restaurant search: fast path and page capture both failed")
            data = hits[-1]["json"]
        base = recipe.get("search_page_url") or home
        rows = extract_restaurants(data, fm)[:max_results]
        for r in rows:
            r["url"] = _absolute(r["url"], base)
        fee_info = extract_fee_info(data, recipe.get("fee_map"))
    log.info("%s restaurant search: %d restaurants (fast path %s)", site, len(rows), fast)
    return {"restaurants": rows, "fee_info": fee_info}


def menu_page_url(recipe: dict, restaurant_id: str, url: str | None = None) -> str | None:
    if url:
        return url
    template = recipe.get("page_url_template")
    return template.replace("{restaurant_id}", quote(str(restaurant_id), safe="")) if template else None


def fetch_menu(site: str, restaurant_id: str, location: dict, recipe: dict, profile_dir, proxy_server=None,
               url=None, block_media=True) -> dict:
    """{"is_open", "dishes", "fee_info"} for one restaurant. `url` (the listing's page) wins over the template."""
    if site not in FOOD_SITES:
        raise ValueError(f"not a food site: {site}")
    if _needs_location(recipe) and ((location or {}).get("lat") is None or (location or {}).get("lon") is None):
        raise Unserviceable("no delivery location saved")
    fm = recipe["field_map"]
    with _open(site, recipe, profile_dir, location, proxy_server, block_media) as (_ctx, page, _guard, capture):
        _goto(page, browser.home_url(site, location))
        menu, data, fast = None, None, False
        if recipe.get("restaurant_param"):
            data = _fast(page, build_request(recipe, location, recipe["restaurant_param"], str(restaurant_id)))
            if data is not None:
                menu = extract_menu(data, fm)
                fast = bool(menu["dishes"])
        if not fast:
            menu = None
            for h in reversed(_page_capture(page, capture, recipe, menu_page_url(recipe, restaurant_id, url))):
                m = extract_menu(h["json"], fm)
                if m["dishes"]:
                    menu, data = m, h["json"]
                    break
            if menu is None:
                raise SearchDrift(f"{site} menu: fast path and page capture both failed")
        fee_info = extract_fee_info(data, recipe.get("fee_map"))
    log.info("%s menu: %d dishes (fast path %s)", site, len(menu["dishes"]), fast)
    return {"is_open": menu["is_open"], "dishes": menu["dishes"], "fee_info": fee_info}

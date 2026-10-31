"""Grocery product search by replaying an admin-learned `search` recipe on the user's own logged-in profile.

Read-only: the site is opened with the normal read-only guard (never login mode, never a cart policy), only the
recipe's admin-approved read POSTs are allowed, nothing is clicked. Logs carry the site and counts only."""
import copy
import json
import logging
import random
import re
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import browser
import recipes
import sync
from server import timing

log = logging.getLogger("search")

SEARCH_SITES = ("zepto", "blinkit", "instamart")
SEARCH_PAGES = {"zepto": "https://www.zepto.com/search?query={q}",
                "blinkit": "https://blinkit.com/s/?q={q}",
                "instamart": "https://www.swiggy.com/instamart/search?custom_back=true&query={q}"}
CAPTURE_WAIT_MS, CAPTURE_POLLS = 500, 16  # the search page's own response must arrive within 8 s
OUT_OF_STOCK = {"out_of_stock", "sold_out", "unavailable", "oos", "false", "0"}
FEE_KEYS = ("delivery", "handling", "small_cart", "surge")


class SearchError(Exception):
    pass


class SearchDrift(SearchError):
    """The recipe no longer works: the app answered 2xx, but the body is unparseable or has no products_path."""


class Unserviceable(SearchError):
    """The app has no store for the user's location."""


class SearchTransient(SearchError):
    """A passing failure (timeout, 429, 5xx, a failed navigation or store lookup). It fails only this result and
    never marks the recipe unhealthy."""


# --- extraction --------------------------------------------------------------

def to_stock(v) -> bool:
    if v is None:
        return True
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v > 0
    s = str(v).strip().lower()
    return not (s in OUT_OF_STOCK or "out of stock" in s or "sold out" in s)


def rows_at(data, path):
    """Like recipes.get_path, but a "*" segment flattens a list (or a map's values): "data.widgets.*.items".
    Returns the list found, or None when the path does not lead to a list."""
    if not path:
        return data if isinstance(data, list) else None
    parts = str(path).split(".")
    if "*" not in parts:
        found = recipes.get_path(data, path)
        return list(found.values()) if isinstance(found, dict) and found and all(
            isinstance(v, dict) for v in found.values()) else found if isinstance(found, list) else None
    i = parts.index("*")
    head = recipes.get_path(data, ".".join(parts[:i])) if i else data
    if isinstance(head, dict):
        head = list(head.values())
    if not isinstance(head, list):
        return None
    rest, out, hit = ".".join(parts[i + 1:]), [], False
    for el in head:
        sub = rows_at(el, rest) if rest else [el]
        if sub is not None:
            hit = True
            out += sub
    return out if hit else None


def _text(v) -> str | None:
    if v is None or isinstance(v, (dict, list)):
        return None
    s = str(v).strip()
    return s or None


def extract_products(data, field_map: dict) -> list[dict]:
    rows = rows_at(data, field_map.get("products_path")) or []
    div = float(field_map.get("price_divisor") or 1)
    out = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        pid, name = _text(recipes.get_path(raw, field_map.get("product_id"))), \
            _text(recipes.get_path(raw, field_map.get("name")))
        price = recipes.to_number(recipes.get_path(raw, field_map.get("price")))
        if not pid or not name or price is None or price <= 0:
            continue
        mrp = recipes.to_number(recipes.get_path(raw, field_map.get("mrp")))
        out.append({"product_id": pid, "name": name,
                    "brand": _text(recipes.get_path(raw, field_map.get("brand"))),
                    "pack_size": _text(recipes.get_path(raw, field_map.get("pack_size"))),
                    "price": price / div, "mrp": mrp / div if mrp is not None else None,
                    "in_stock": to_stock(recipes.get_path(raw, field_map.get("in_stock")))})
    return out


def member_value(v):
    """A membership flag as the app returns it: a bool, or the pass/plan name. Anything else is unknown (None)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def extract_fee_info(data, fee_map: dict | None) -> dict:
    """Observed fees (rupees) and membership; only the keys whose path resolves."""
    if not fee_map:
        return {}
    div = float(fee_map.get("divisor") or 1)
    out = {}
    for k in FEE_KEYS:
        n = recipes.to_number(recipes.get_path(data, fee_map.get(k)))
        if n is not None:
            out[k] = n / div
    if (m := member_value(recipes.get_path(data, fee_map.get("member")))) is not None:
        out["member"] = m
    return out


# --- requests ----------------------------------------------------------------

def _set_body_path(body, path: str, value) -> None:
    parts = path.split(".")
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if isinstance(body, list) and part.lstrip("-").isdigit():
            if last:
                body[int(part)] = value
            else:
                body = body[int(part)]
        else:
            if last:
                body[part] = value
            else:
                body = body.setdefault(part, {})


def set_input(req: dict, where: str, name: str, value) -> None:
    """Bind one value into a request in place: a query parameter, a header or a dotted JSON-body path."""
    if where == "query":
        parts = urlsplit(req["url"])
        q = dict(parse_qsl(parts.query)) | {name: str(value)}
        req["url"] = urlunsplit(parts._replace(query=urlencode(q)))
    elif where == "header":
        req["headers"] = dict(req.get("headers") or {}) | {name: str(value)}
    elif where == "body":
        body = req.get("body")
        body = json.loads(body) if isinstance(body, str) else (body if body is not None else {})
        _set_body_path(body, name, value)
        req["body"] = body
    else:
        raise SearchError(f"unknown input location {where!r}")


def bind_location(req: dict, inputs: list[dict], location: dict | None) -> None:
    for inp in inputs or []:
        v = (location or {}).get(inp["value"])
        if v is None:
            raise SearchError("a saved location (lat/lon) is needed to search")
        set_input(req, inp["in"], inp["name"], float(v) if inp["in"] == "body" else str(v))


def build_request(recipe: dict, text: str, location: dict, store_id: str | None = None) -> dict:
    req = recipes.apply_param(recipes.first_request(recipe), recipe["query_param"], text)  # deep copy
    req["headers"] = dict(req.get("headers") or {})
    bind_location(req, recipe.get("location_inputs") or [], location)
    if store_id is not None:
        for inp in (recipe.get("store") or {}).get("inputs") or []:
            set_input(req, inp["in"], inp["name"], store_id)
    return req


def _lookup_store(page, recipe: dict, location: dict) -> tuple[str | None, object]:
    store = recipe.get("store")
    if not store:
        return None, None
    lk = store["lookup"]
    req = {"method": (lk.get("method") or "GET").upper(), "url": lk["url"], "headers": {},
           "body": copy.deepcopy(lk.get("body"))}
    bind_location(req, store.get("location_inputs") or [], location)
    try:
        data = browser.fetch_in_page(page, req)
    except (browser.SessionExpired, browser.SiteThrottled):
        raise
    except Exception as e:
        raise SearchTransient(f"store lookup failed ({type(e).__name__})") from e
    sid = _text(recipes.get_path(data, store.get("store_id_path")))
    return sid, data


def resolve_store(page, recipe: dict, location: dict) -> str | None:
    """The store id serving the user's location (None when the recipe binds lat/lon directly or no store serves it)."""
    return _lookup_store(page, recipe, location)[0]


# --- the search run ----------------------------------------------------------

def _pause() -> None:
    time.sleep(random.uniform(0.8, 1.6))


def search_page_url(recipe: dict, text: str) -> str:
    return recipe["search_page_url"].replace("{q}", quote(text))


def endpoint_responses(recipe: dict, items: list[dict]) -> list[dict]:
    """Captured responses (any status) of the recipe's endpoint: same host, path and method (and the GraphQL
    operationName when the recipe's body names one). Any status, so callers also see 401/403 and 429/5xx."""
    want = urlsplit(recipe["endpoint"])
    method = (recipe.get("method") or "GET").upper()
    body = recipe.get("body")
    op = body.get("operationName") if isinstance(body, dict) else None
    out = []
    for c in items:
        got = urlsplit(c["url"])
        if (got.netloc, got.path) == (want.netloc, want.path) and c["method"].upper() == method \
                and (not op or op in (c.get("request_body") or "")):
            out.append(c)
    return out


# --- failure evidence: drift (the recipe is wrong for everyone) vs transient (this run only) -------------------
# Fast path (the in-page replay) outcomes: "ok" (2xx JSON), "shape" (the caller found the 2xx JSON lacks its path),
# "unparseable" (2xx, not JSON: may be a captive portal or proxy page), "gone" (410), "not_found" (404),
# "transient" (timeout, transport error, 429, 5xx), "failed" (any other HTTP error), "none" (not tried).
# Page fallback outcomes: "hits" (2xx calls of the endpoint), "shape" (the caller found they lack its path),
# "no_call" (the page loaded but made no 2xx call to the endpoint within 8 s), "transient" (navigation failed or
# the endpoint answered 429/5xx), "no_page" (no page to open).

_HTTP_STATUS = re.compile(r"^HTTP (\d{3})\b")


def _transient_status(status: int) -> bool:
    return status == 429 or status >= 500


def fast_attempt(page, req: dict) -> tuple[str, object]:
    """The in-page replay: ("ok", data) or (outcome, None). A logout (401/403, login redirect) propagates."""
    try:
        return "ok", browser.fetch_in_page(page, req)
    except (browser.SessionExpired, browser.SiteThrottled):
        raise
    except ValueError:  # json.JSONDecodeError: a 2xx body that is not JSON
        log.info("fast path: unparseable 2xx response")
        return "unparseable", None
    except Exception as e:
        m = _HTTP_STATUS.match(str(e)) if isinstance(e, RuntimeError) else None
        log.info("fast path failed: %s", f"HTTP {m[1]}" if m else type(e).__name__)
        if not m:
            return "transient", None
        status = int(m[1])
        if status == 410:
            return "gone", None
        if status == 404:
            return "not_found", None
        return ("transient" if _transient_status(status) else "failed"), None


def page_attempt(page, capture, recipe: dict, url: str | None) -> tuple[list[dict], str]:
    """Open the app's own page and wait up to 8 s for its call to the recipe's endpoint.
    Returns (the 2xx responses, outcome); an endpoint 401/403 or a login redirect raises browser.SessionExpired."""
    if not url:
        return [], "no_page"
    mark = len(capture.items)
    try:
        browser.goto(page, url)
    except (browser.SessionExpired, browser.SiteThrottled):
        raise
    except Exception as e:
        log.info("page fallback: navigation failed (%s)", type(e).__name__)
        return [], "transient"
    sync.ensure_logged_in(page)
    for i in range(CAPTURE_POLLS + 1):
        seen = endpoint_responses(recipe, capture.items[mark:])
        if denied := [c for c in seen if c["status"] in (401, 403)]:
            raise browser.SessionExpired(f"HTTP {denied[0]['status']}")
        if ok := [c for c in seen if 200 <= c["status"] < 300]:
            return ok, "hits"
        if i < CAPTURE_POLLS:
            page.wait_for_timeout(CAPTURE_WAIT_MS)
    return [], "transient" if any(_transient_status(c["status"]) for c in seen) else "no_call"


def is_drift(fast: str, page: str) -> bool:
    """Drift needs evidence that the recipe itself is wrong:
    - a 2xx JSON missing the recipe's path (from the replay or from the page's own call), or a 410;
    - a 404 from the replay while the page loads but makes no 2xx call to the endpoint within 8 s.
    A 2xx that is not JSON is drift only when the page then returns a 2xx missing the path (the "shape" rule);
    on its own it may be a captive portal or proxy page. Everything else is transient."""
    return fast in ("shape", "gone") or page == "shape" or (fast == "not_found" and page == "no_call")


def fail(site: str, what: str, fast: str, page: str):
    if is_drift(fast, page):
        raise SearchDrift(f"{site} {what}: the response no longer matches the recipe")
    raise SearchTransient(f"{site} {what}: no usable response (retry later)")


def search_once(page, capture, recipe: dict, text: str, location: dict, store_id: str | None = None):
    """One query: the in-page API replay, else the search page and the endpoint's captured response.
    Returns the body whose products_path leads to a list; else raises SearchDrift / SearchTransient (see is_drift).
    401/403 and login redirects raise browser.SessionExpired."""
    site, path = recipe.get("site"), recipe["field_map"].get("products_path")
    fast, data = fast_attempt(page, build_request(recipe, text, location, store_id))
    if fast == "ok":
        if rows_at(data, path) is not None:
            return data
        fast = "shape"
    hits, outcome = page_attempt(page, capture, recipe, search_page_url(recipe, text))
    good = [h["json"] for h in hits if rows_at(h.get("json"), path) is not None]
    if good:
        return good[-1]
    fail(site, "search", fast, "shape" if hits else outcome)


def search_products(site: str, queries, location: dict, recipe: dict, profile_dir, proxy_server: str | None = None,
                    block_media: bool = True, max_results: int = 20) -> dict:
    """Search each query on the user's own profile.

    Returns {"products": {query: [...]}, "errors": {query: "drift"|"transient"}, "fee_info": {...}}; every query is
    in exactly one of products / errors. A failure on the first query raises SearchDrift / SearchTransient (the
    whole site fails); a later one is only that item's error. browser.SessionExpired always propagates."""
    queries = list(queries)
    fee_map = recipe.get("fee_map") or None
    products: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    fee_info: dict = {}
    with browser.open_site(site, allowlist=list(recipe.get("read_post_allowlist") or []), profile_dir=profile_dir,
                           location=location, proxy_server=proxy_server, block_media=block_media) as (
            _, page, _guard, capture):
        try:
            with timing.Phase("session_check"):
                if not browser.on_origin(page, recipe["home_url"]):  # a page already there skips the load
                    browser.goto(page, recipe["home_url"])
        except (browser.SessionExpired, browser.SiteThrottled):
            raise
        except Exception as e:
            raise SearchTransient(f"{site} home page did not load ({type(e).__name__})") from e
        sync.ensure_logged_in(page)
        store_id, store_data = _lookup_store(page, recipe, location)
        if recipe.get("store") and not store_id:
            raise Unserviceable(f"{site} has no store for this location")
        if fee_map and fee_map.get("source") == "store":
            fee_info = extract_fee_info(store_data, fee_map)
        path = recipe["field_map"].get("products_path")
        prefetched: dict[str, object] = {}
        for i, q in enumerate(queries):
            if i == 1:
                # The first query went alone (a failure there fails the site); the rest are plain in-page API
                # calls, at most browser.MAX_PARALLEL_FETCH at a time. Any that does not give rows falls back to
                # search_once below.
                with timing.Phase("fetch"):
                    rest = queries[1:]
                    got = browser.fetch_many_in_page(
                        page, [build_request(recipe, r, location, store_id) for r in rest])
                for r, d in zip(rest, got):
                    if isinstance(d, (browser.SessionExpired, browser.SiteThrottled)):
                        raise d
                    if not isinstance(d, Exception) and rows_at(d, path) is not None:
                        prefetched[r] = d
            try:
                if q in prefetched:
                    data = prefetched[q]
                else:
                    if i and not prefetched:
                        _pause()
                    with timing.Phase("fetch"):
                        data = search_once(page, capture, recipe, q, location, store_id)
            except (SearchDrift, SearchTransient) as e:
                if i == 0:
                    raise
                errors[q] = "drift" if isinstance(e, SearchDrift) else "transient"
                continue
            with timing.Phase("parse"):
                products[q] = extract_products(data, recipe["field_map"])[:max_results]
                if fee_map and fee_map.get("source", "search") == "search":
                    fee_info = extract_fee_info(data, fee_map) | fee_info  # first observation wins per key
    log.info("%s search: %d queries, %d products, %d failed", site, len(queries),
             sum(len(v) for v in products.values()), len(errors))
    return {"products": products, "errors": errors, "fee_info": fee_info}

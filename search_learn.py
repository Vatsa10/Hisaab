"""Supervised learning of a grocery `search` recipe on the admin's own logged-in profile.

The admin's restored profile is opened read-only, the site's own search page is probed, and the product search
call is turned into a recipe that binds the query text and the user's location (lat/lon, or a store id looked up
from them). New read POSTs need the admin's approval first. Field and fee maps come from an injected LLM callable
(`llm(prompt) -> dict | JSON str`, from llm.py); the recipe is validated by replaying a second probe. Nothing is saved
here (sanitising and storing are recipe_admin.apply_learned's job)."""
import json
import logging
import re
from collections.abc import Callable, Iterable
from urllib.parse import parse_qsl, quote, unquote_plus, urlsplit, urlunsplit

import agent
import browser
import search
import sync
from guard import BLOCKED_URL, CHALLENGE_POST, redact_url
from recipes import get_path, to_number
from sync import RecipeError

log = logging.getLogger("search_learn")

SEARCH_PROBES = ("milk", "bread")
PRODUCTISH_KEY = re.compile(r"product|price|mrp|name|brand|pack|unit|quantity|weight|inventory|stock|sku|variant|"
                            r"discount|offer|image", re.I)
MIN_PRODUCTS = 3
FIELD_TRIES = 3
NEAR_DEG = 0.01  # a captured coordinate this close to the saved lat/lon is the location input
DROP_HEADERS = ("sec-", ":", "content-length", "host", "accept-encoding", "user-agent", "referer", "origin", "cookie")
LLM = Callable[[str], dict | str]
FEE_KEYS = search.FEE_KEYS


# --- captured-request helpers ------------------------------------------------

def _ok(c: dict) -> bool:
    return 200 <= c.get("status", 0) < 300 and isinstance(c.get("json"), (dict, list))


def json_body(c: dict):
    try:
        body = json.loads(c.get("request_body") or "")
    except (TypeError, ValueError):
        return None
    return body if isinstance(body, (dict, list)) else None


def _leaves(obj, path: str = "", depth: int = 0):
    """(dotted path, scalar) for every scalar inside a JSON value."""
    if depth > 8:
        return
    children = obj.items() if isinstance(obj, dict) else enumerate(obj[:50]) if isinstance(obj, list) else None
    if children is None:
        yield path, obj
        return
    for k, v in children:
        yield from _leaves(v, f"{path}.{k}" if path else str(k), depth + 1)


def request_inputs(c: dict) -> list[tuple[str, str, object]]:
    """Every value the request carried: (in, name, value) for query parameters, headers and JSON-body leaves."""
    out = [("query", k, v) for k, v in parse_qsl(urlsplit(c["url"]).query)]
    out += [("header", k, v) for k, v in (c.get("request_headers") or {}).items()]
    body = json_body(c)
    if body is not None:
        out += [("body", p, v) for p, v in _leaves(body)]
    return out


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", v):
        return float(v)
    return None


def _coord(v, lat: float, lon: float) -> str | None:
    n = _num(v)
    if n is None:
        return None
    if abs(n - lat) <= NEAR_DEG:
        return "lat"
    if abs(n - lon) <= NEAR_DEG:
        return "lon"
    return None


def _scalar_text(v) -> str | None:
    if v is None or isinstance(v, (bool, dict, list)):
        return None
    s = str(v).strip()
    return s or None


def _endpoint(url: str) -> str:
    return urlunsplit(urlsplit(url)._replace(query="", fragment=""))


def post_entry(url: str, why: str = "product search (read-only)") -> dict:
    p = urlsplit(url)
    return {"url_regex": f"^https://{re.escape(p.netloc)}{re.escape(p.path)}$", "why": why}


# --- finding the search call -------------------------------------------------

def _product_score(data, depth: int = 0) -> int:
    """Best product-likeness of any list of objects in the body (keys of the item and of its child objects)."""
    if depth > 8:
        return 0
    best = 0
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data[:3]):
        keys = set()
        for it in data[:3]:
            keys |= set(it)
            keys |= {k for v in it.values() if isinstance(v, dict) for k in v}
        best = sum(bool(PRODUCTISH_KEY.search(k)) for k in keys)
    children = data.values() if isinstance(data, dict) else data[:5] if isinstance(data, list) else ()
    for v in children:
        best = max(best, _product_score(v, depth + 1))
    return best


def _carries(c: dict, probe: str) -> bool:
    p = probe.lower()
    return p in unquote_plus(c["url"]).lower() or p in (c.get("request_body") or "").lower()


def candidates(captured: list[dict], probe: str) -> list[dict]:
    """Successful JSON responses to requests that carried the probe and hold product-like lists, best first."""
    out = []
    for c in captured:
        if not _ok(c) or not _carries(c, probe) or BLOCKED_URL.search(urlsplit(c["url"]).path):
            continue
        s = _product_score(c["json"])
        if s >= 3:
            out.append((s + min(c.get("size", 0) // 5000, 4), c))
    return [c for _, c in sorted(out, key=lambda x: -x[0])]


def find_query_param(c: dict, probe: str) -> str | None:
    """Where the search text goes: a dotted JSON-body path for a POST with a JSON body, else a query parameter."""
    p = probe.strip().lower()
    body = json_body(c)
    if c["method"].upper() == "POST" and body is not None:
        return next((path for path, v in _leaves(body) if isinstance(v, str) and v.strip().lower() == p), None)
    return next((k for k, v in parse_qsl(urlsplit(c["url"]).query) if v.strip().lower() == p), None)


def find_location_inputs(c: dict, lat: float, lon: float) -> list[dict]:
    """Query parameters, headers and body paths carrying the saved lat/lon."""
    return [{"in": where, "name": name, "value": which} for where, name, v in request_inputs(c)
            if (which := _coord(v, lat, lon))]


def _find_value_path(data, text: str) -> str | None:
    """Shortest dotted path to a scalar equal to text."""
    level = [("", data)]
    for _ in range(9):
        nxt = []
        for path, v in level:
            if isinstance(v, dict):
                nxt += [(f"{path}.{k}" if path else str(k), x) for k, x in v.items()]
            elif isinstance(v, list):
                nxt += [(f"{path}.{i}" if path else str(i), x) for i, x in enumerate(v[:50])]
            elif path and _scalar_text(v) == text:
                return path
        level = nxt
    return None


def find_store_binding(captured: list[dict], c: dict, lat: float, lon: float) -> dict | None:
    """A store id the search request carries that an earlier location-bound response returned (the store lookup)."""
    lookups = [o for o in captured if o is not c and _ok(o) and find_location_inputs(o, lat, lon)]
    if not lookups:
        return None
    mine = request_inputs(c)
    for _, _, v in mine:
        s = _scalar_text(v)
        if not s or len(s) < 3 or s.lower() in ("true", "false", "null", "none") or _coord(v, lat, lon):
            continue
        for o in lookups:
            path = _find_value_path(o["json"], s)
            if path:
                return {"lookup": {"method": o["method"].upper(), "url": o["url"], "body": json_body(o)},
                        "location_inputs": find_location_inputs(o, lat, lon), "store_id_path": path,
                        "inputs": [{"in": w, "name": n} for w, n, x in mine if _scalar_text(x) == s]}
    return None


# --- LLM maps ----------------------------------------------------------------

def ask_json(llm: LLM, prompt: str) -> dict:
    r = llm(prompt)
    if isinstance(r, str):
        r = json.loads(re.sub(r"^\s*```(?:json)?|```\s*$", "", r.strip()))
    if not isinstance(r, dict):
        raise ValueError("the model did not return a JSON object")
    return r


def _sample(c: dict | None, limit: int = 12000) -> str:
    return json.dumps(c["json"], ensure_ascii=False)[:limit] if c else "none"


FIELD_MAP_PROMPT = """FIELD MAP TASK
This JSON is {site}'s product search response for the text {probe!r}, from {endpoint}.
Return a JSON object {{"field_map": {{...}}}} where field_map has exactly these keys:
 "products_path": str (dotted path to the product list; use "*" to flatten nested lists, e.g. "data.widgets.*.items"),
 "product_id": str, "name": str, "brand": str|null, "pack_size": str|null (e.g. "500 ml"),
 "price": str (the selling price the user pays), "mrp": str|null, "in_stock": str|null (a stock flag or inventory count),
 "price_divisor": 1|100 (100 when prices are in paise)
All paths except products_path are relative to ONE product row. Banners and ads without a price are skipped.
Reply with JSON only.

RESPONSE:
{sample}"""

FEE_MAP_PROMPT = """FEE MAP TASK
Below are {site}'s product search response and its store lookup response (for the user's location). Find the
delivery-related fees and the user's membership, if the responses show them. Return a JSON object
{{"fee_map": {{"source": "search"|"store", "delivery": str|null, "handling": str|null, "small_cart": str|null,
"surge": str|null, "member": str|null, "divisor": 1|100}}}} or {{"fee_map": null}} when no fee is shown.
Each value is a dotted path from the root of the chosen source response. "member" points at a boolean
membership flag or the pass/plan name. divisor 100 when fees are in paise. Reply with JSON only.

SEARCH RESPONSE:
{search}

STORE RESPONSE:
{store}"""

FIELD_KEYS = ("products_path", "product_id", "name", "brand", "pack_size", "price", "mrp", "in_stock")


def _diagnose(data, fm: dict) -> str:
    rows = search.rows_at(data, fm.get("products_path"))
    if rows is None:
        return f"products_path {fm.get('products_path')!r} does not lead to a list"
    first = next((r for r in rows if isinstance(r, dict) and get_path(r, fm.get("price")) is not None),
                 rows[0] if rows else {})
    vals = "; ".join(f"{k}: {fm.get(k)!r} -> {str(get_path(first, fm.get(k)))[:60]!r}"
                     for k in ("product_id", "name", "price") if isinstance(first, dict))
    return f"{len(rows)} rows at products_path; first priced row: {vals}"


def _field_map(site: str, c: dict, probe: str, llm: LLM) -> dict:
    data = c["json"]
    base = FIELD_MAP_PROMPT.format(site=site, probe=probe, endpoint=redact_url(c["url"]), sample=_sample(c))
    prompt, n = base, 0
    for attempt in range(FIELD_TRIES):
        fm, n = None, 0
        # err (with sample values from the response) goes back to the model only; it is never raised or logged.
        try:
            raw = ask_json(llm, prompt)
            fm = raw.get("field_map", raw)
            if not isinstance(fm, dict):
                raise ValueError("field_map is not an object")
            fm = {k: fm.get(k) for k in FIELD_KEYS} | {"price_divisor": 100 if fm.get("price_divisor") == 100 else 1}
            n = len(search.extract_products(data, fm))
            err = None if n >= MIN_PRODUCTS else (
                f"field_map extracted {n} products (need at least {MIN_PRODUCTS} with product_id, name and "
                f"price > 0): {_diagnose(data, fm)}")
        except Exception as e:
            err = f"invalid field_map: {type(e).__name__}: {e}"
        if not err:
            return fm
        log.warning("%s search field_map attempt %d failed (%d products)", site, attempt + 1, n)
        prompt = (f"{base}\n\nYour previous answer: {json.dumps(fm, ensure_ascii=False)[:2000]}\n"
                  f"Validation failed: {err}\nReturn a corrected JSON object.")
    raise RecipeError(f"could not map the {site} search response after {FIELD_TRIES} tries "
                      f"({n} products extracted, need {MIN_PRODUCTS})")


def find_fee_map(captured: list[dict], lat: float, lon: float, llm: LLM | None = None) -> dict | None:
    """LLM-proposed fee_map over the search response (captured[0]) and the location-bound store response; each
    path is kept only if it resolves to a number (a bool or a name for `member`). None when nothing resolves."""
    if llm is None or not captured:
        return None
    search_c = captured[0]
    store_c = next((o for o in captured[1:] if _ok(o) and find_location_inputs(o, lat, lon)), None)
    try:
        raw = ask_json(llm, FEE_MAP_PROMPT.format(site=urlsplit(search_c["url"]).netloc, search=_sample(search_c),
                                                  store=_sample(store_c, 6000)))
    except Exception as e:
        log.warning("fee_map proposal failed (%s)", type(e).__name__)
        return None
    fm = raw.get("fee_map", raw) if isinstance(raw, dict) else None
    if not isinstance(fm, dict):
        return None
    source = fm.get("source") or "search"
    if source not in ("search", "store") or (source == "store" and store_c is None):
        return None
    data = (store_c if source == "store" else search_c)["json"]
    out = {"source": source}
    for k in FEE_KEYS:
        p = fm.get(k)
        if isinstance(p, str) and p and not p.startswith("=") and to_number(get_path(data, p)) is not None:
            out[k] = p
    p = fm.get("member")
    if isinstance(p, str) and p and not p.startswith("=") and search.member_value(get_path(data, p)) is not None:
        out["member"] = p
    if len(out) == 1:
        return None
    out["divisor"] = 100 if fm.get("divisor") == 100 else 1
    return out


# --- checks ------------------------------------------------------------------

def _covered(url: str, allowlist: list[dict]) -> bool:
    return any(re.search(e["url_regex"], url) for e in allowlist)


def check_search_recipe(recipe: dict) -> str | None:
    for k in ("site", "home_url", "search_page_url", "endpoint", "method", "query_param", "field_map"):
        if not recipe.get(k):
            return f"missing {k}"
    if "{q}" not in recipe["search_page_url"]:
        return "search_page_url has no {q}"
    method = recipe["method"].upper()
    if method not in ("GET", "POST"):
        return f"method {method} is not allowed"
    fm = recipe["field_map"]
    for k in ("products_path", "product_id", "name", "price"):
        if not fm.get(k):
            return f"field_map.{k} missing"
    if fm.get("price_divisor", 1) not in (1, 100):
        return "field_map.price_divisor must be 1 or 100"
    store = recipe.get("store") or None
    inputs = list(recipe.get("location_inputs") or [])
    if store:
        if not (store.get("lookup") or {}).get("url") or not store.get("store_id_path") or not store.get("inputs"):
            return "store binding needs lookup.url, store_id_path and inputs"
        inputs += store.get("location_inputs") or []
    if not inputs:
        return "no location binding: the request must carry the user's lat/lon or a store id looked up from them"
    for inp in inputs:
        if inp.get("in") not in ("query", "header", "body") or inp.get("value") not in ("lat", "lon") \
                or not inp.get("name"):
            return f"bad location input {inp}"
    urls = [recipe["endpoint"]] + ([store["lookup"]["url"]] if store else [])
    for u in urls:
        if BLOCKED_URL.search(urlsplit(u).path):
            return f"endpoint path matches the blocklist: {urlsplit(u).path}"
    allow = recipe.get("read_post_allowlist") or []
    host = urlsplit(recipe["endpoint"]).netloc
    for e in allow:
        try:
            rx = re.compile(e["url_regex"])
        except (re.error, KeyError, TypeError):
            return f"bad allowlist entry {e}"
        if rx.search(f"https://{host}/") or rx.search("https://example.com/x") or rx.search(f"https://{host}/x"):
            return f"allowlist regex too broad: {e['url_regex']}"
    if method == "POST" and not _covered(recipe["endpoint"], allow):
        return "POST endpoint is not covered by read_post_allowlist"
    if store and (store["lookup"].get("method") or "GET").upper() == "POST" and not _covered(store["lookup"]["url"],
                                                                                             allow):
        return "POST store lookup is not covered by read_post_allowlist"
    fee = recipe.get("fee_map")
    if fee and (fee.get("source") not in ("search", "store") or (fee.get("source") == "store" and not store)):
        return "fee_map.source must be 'search', or 'store' with a store binding"
    return None


# --- learning ----------------------------------------------------------------

def _allow(guard, entries: list[dict], approved: set[str], approve) -> None:
    """New entries need the admin's approval before the guard lets them through."""
    new = [e for e in entries if e["url_regex"] not in approved]
    if new:
        if approve is None or not approve(new):
            raise RecipeError("POST allowlist rejected")
        approved.update(e["url_regex"] for e in new)
    for e in entries:
        if e not in guard.allowlist:
            guard.allowlist.append(e)


def _blocked_search_posts(guard, probe: str) -> list[dict]:
    out = []
    for b in getattr(guard, "blocked", []):
        if b.get("method", "").upper() != "POST" or probe.lower() not in (b.get("body") or "").lower():
            continue
        if BLOCKED_URL.search(urlsplit(b["url"]).path) or CHALLENGE_POST.search(b["url"]):
            continue
        if (e := post_entry(b["url"])) not in out:
            out.append(e)
    return out


def _probe(page, guard, capture, site: str, probe: str) -> list[dict]:
    capture.clear()
    if hasattr(guard, "blocked"):
        guard.blocked.clear()
    browser.goto(page, search.SEARCH_PAGES[site].replace("{q}", quote(probe)))
    sync.ensure_logged_in(page)
    agent.settle(page)
    for _ in range(search.CAPTURE_POLLS):
        if candidates(capture.items, probe) or _blocked_search_posts(guard, probe):
            break
        page.wait_for_timeout(search.CAPTURE_WAIT_MS)
    return list(capture.items)


def _recipe_from(site: str, home: str, c: dict, items: list[dict], lat: float, lon: float, probe: str) -> dict:
    method = c["method"].upper()
    qp = find_query_param(c, probe)
    if not qp:
        raise RecipeError("could not find where the search text goes in the search request")
    loc = find_location_inputs(c, lat, lon)
    store = None if loc else find_store_binding(items, c, lat, lon)
    bound = loc + (store["inputs"] if store else [])
    bound_query = {i["name"] for i in bound if i["in"] == "query"}
    bound_headers = {i["name"].lower() for i in bound if i["in"] == "header"}
    body = json_body(c) if method == "POST" else None
    if body is None:
        bound_query.add(qp)
    return {"kind": "search", "site": site, "home_url": home, "search_page_url": search.SEARCH_PAGES[site],
            "endpoint": _endpoint(c["url"]), "method": method,
            "query": {k: v for k, v in parse_qsl(urlsplit(c["url"]).query) if k not in bound_query},
            "body": body,
            "headers": {k: v for k, v in (c.get("request_headers") or {}).items()
                        if not k.lower().startswith(DROP_HEADERS) and k.lower() not in bound_headers},
            "query_param": qp, "location_inputs": loc, "store": store,
            "field_map": None, "fee_map": None, "read_post_allowlist": []}


def _validate_replay(page, capture, recipe: dict, location: dict, probe: str) -> int:
    try:
        store_id = search.resolve_store(page, recipe, location)
        if recipe.get("store") and not store_id:
            raise RecipeError("replay: the store lookup returned no store id")
        data = search.search_once(page, capture, recipe, probe, location, store_id)
    except search.SearchError as e:
        raise RecipeError(f"replay failed: {e}") from e
    n = len(search.extract_products(data, recipe["field_map"])) if data is not None else 0
    if n < MIN_PRODUCTS:
        raise RecipeError(f"replay with the second probe found {n} products (need at least {MIN_PRODUCTS})")
    return n


def learn_search(site: str, location: dict, profile_dir, proxy_server: str | None = None, approve=None,
                 preapproved: Iterable[str] = (), llm: LLM | None = None) -> dict:
    """Learn and validate a search recipe on the admin's restored, logged-in profile; returns it unsaved."""
    if site not in search.SEARCH_SITES:
        raise ValueError(f"{site} has no grocery search")
    lat, lon = (location or {}).get("lat"), (location or {}).get("lon")
    if lat is None or lon is None:
        raise RecipeError("learning a search recipe needs a location (lat/lon)")
    if llm is None:
        raise RecipeError("learning a search recipe needs an LLM callable (llm.py)")
    lat, lon = float(lat), float(lon)
    approved = set(preapproved)
    probe, check_probe = SEARCH_PROBES
    with browser.open_site(site, allowlist=[], profile_dir=profile_dir, location=location,
                           proxy_server=proxy_server) as (_, page, guard, capture):
        home = browser.home_url(site, location)
        browser.goto(page, home)
        sync.ensure_logged_in(page)
        if agent.shows_login_button(agent.snapshot(page)["elements"]):
            raise browser.SessionExpired("the site shows a Login button")
        items = _probe(page, guard, capture, site, probe)
        if not candidates(items, probe) and (entries := _blocked_search_posts(guard, probe)):
            _allow(guard, entries, approved, approve)
            items = _probe(page, guard, capture, site, probe)  # the site's own POST now goes through
        found = candidates(items, probe)
        if not found:
            raise RecipeError(f"no product search response found on {site}")
        c = found[0]
        recipe = _recipe_from(site, home, c, items, lat, lon, probe)
        allow = []
        if recipe["method"] == "POST":
            allow.append(post_entry(recipe["endpoint"]))
        if recipe["store"] and recipe["store"]["lookup"]["method"] == "POST":
            allow.append(post_entry(recipe["store"]["lookup"]["url"], "store lookup (read-only)"))
        _allow(guard, allow, approved, approve)
        recipe["read_post_allowlist"] = allow
        recipe["field_map"] = _field_map(site, c, probe, llm)
        recipe["fee_map"] = find_fee_map([c] + [o for o in items if o is not c], lat, lon, llm=llm)
        if err := check_search_recipe(recipe):
            raise RecipeError(f"search recipe rejected: {err}")
        n = _validate_replay(page, capture, recipe, location, check_probe)
    log.info("%s search recipe learned (%s %s); replay found %d products", site, recipe["method"],
             redact_url(recipe["endpoint"]), n)
    return recipe

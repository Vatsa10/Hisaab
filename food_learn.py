"""Supervised learning of the food recipes (kinds "restaurants" and "menu") on an admin's own logged-in profile.

learn_food opens the admin's restored profile read-only, probes the restaurant search page, has the LLM map the
captured restaurant list, opens the first open restaurant's page, has the LLM map the captured menu, then closes
the browser and validates both recipes by replaying them through food_search on the same profile with a second
restaurant. A read POST the guard blocked is allowlisted only after `approve` returns True (or when it was
preapproved). Nothing is saved here: sanitising and storing is apply_learned's job.

The LLM is an injectable callable `llm(messages) -> dict | str`: `messages` is a Messages-API style list whose last
entry is a user turn, the reply is a JSON object (or its text). The default is the engine's llm.py (Claude Haiku).
"""
import copy
import importlib
import json
import logging
import re
from collections.abc import Callable, Iterable
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

import agent
import browser
import food_search
import guard as guard_mod
import recipes
import sync
from food_search import FOOD_PAGES, FOOD_SITES, RESTAURANT_PROBE, SearchDrift, SearchError, SearchTransient, Unserviceable  # noqa: F401

log = logging.getLogger("food_learn")

MIN_RESTAURANTS = 3
MIN_DISHES = 5
LLM_TRIES = 3
SETTLE_MS = 3000
COORD_TOLERANCE = 0.005
RESTAURANTISH_KEY = re.compile(r"restaurant|name|locality|area|lat|lng|lon|distance|rating|cuisine|open|outlet|"
                               r"res_?id|sla|eta|delivery", re.I)
DISHISH_KEY = re.compile(r"dish|item|name|price|veg|variant|stock|packag|categor|menu|addon|serv", re.I)
FIELD_KEYS = {"restaurants": ("restaurants_path", "restaurant_id", "name", "locality", "lat", "lon", "distance_km",
                              "is_open", "url"),
              "menu": ("dishes_path", "dish_id", "name", "variant", "price", "veg", "in_stock", "packaging",
                       "restaurant_open", "price_divisor")}
REQUIRED_FIELDS = {"restaurants": ("restaurant_id", "name"), "menu": ("dish_id", "name", "price")}
_DROP_HEADER = re.compile(r"^(sec-|content-length$|host$|accept-encoding$|user-agent$|referer$|origin$|"
                          r"connection$|:)", re.I)

Llm = Callable[[list[dict]], dict | str]


# --- recipe checks -----------------------------------------------------------

def _check_common(recipe: dict) -> str | None:
    for k in ("endpoint", "method", "field_map"):
        if not recipe.get(k):
            return f"missing {k}"
    if recipe["method"].upper() not in ("GET", "POST"):
        return f"method {recipe['method']!r} is not GET or POST"
    if guard_mod.BLOCKED_URL.search(urlsplit(recipe["endpoint"]).path):
        return "endpoint path matches the blocklist"
    host = urlsplit(recipe["endpoint"]).netloc
    for e in recipe.get("read_post_allowlist") or []:
        try:
            rx = re.compile(e["url_regex"])
        except (re.error, KeyError, TypeError):
            return f"bad allowlist entry {e}"
        if rx.search(f"https://{host}/") or rx.search(f"https://{host}/x") or rx.search("https://example.com/x"):
            return f"allowlist regex too broad: {e['url_regex']}"
    if recipe["method"].upper() == "POST" and not any(
            re.search(e["url_regex"], recipe["endpoint"]) for e in recipe.get("read_post_allowlist") or []):
        return "POST endpoint is not covered by read_post_allowlist"
    for inp in recipe.get("location_inputs") or []:
        if inp.get("in") not in ("query", "header", "body") or inp.get("value") not in ("lat", "lon") \
                or not inp.get("name"):
            return f"bad location input {inp}"
    return None


def check_food_recipe(recipe: dict) -> str | None:
    """None when the recipe is usable, else what is wrong. restaurants must bind the user's lat and lon (prices
    belong to the saved address, whatever the session last browsed); menu must be bound by a restaurant_param
    or a page_url_template."""
    kind = recipe.get("kind")
    if kind not in ("restaurants", "menu"):
        return f"unknown kind {kind!r}: expected restaurants or menu"
    if err := _check_common(recipe):
        return err
    fm = recipe["field_map"]
    for k in REQUIRED_FIELDS[kind]:
        if not fm.get(k):
            return f"field_map.{k} missing"
    if kind == "restaurants":
        if not recipe.get("query_param"):
            return "restaurants recipe needs a query_param for the search text"
        bound = {i["value"] for i in recipe.get("location_inputs") or []}
        if not {"lat", "lon"} <= bound:
            return "restaurants recipe must bind the location (lat and lon)"
        return None
    template = recipe.get("page_url_template")
    if not recipe.get("restaurant_param") and not template:
        return "menu recipe needs a restaurant_param or a page_url_template"
    if template:
        if "{restaurant_id}" not in template:
            return "page_url_template must contain {restaurant_id}"
        if guard_mod.BLOCKED_URL.search(urlsplit(template).path):
            return "page_url_template matches the blocklist"
        site = recipe.get("site")
        if urlsplit(template).scheme != "https" or (site in browser.SITES and not agent.site_allows_host(
                site, template.replace("{restaurant_id}", "1"))):
            return "page_url_template is not on the site's own host"
    return None


# --- request binding discovery (duplicated from search_learn, Plan 5 Task 4; merge later) --------------------

def _walk(obj, path: str = ""):
    """(dotted path, leaf value) pairs of a JSON value."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}.{i}" if path else str(i))
    else:
        yield path, obj


def _body_json(captured: dict):
    body = captured.get("request_body")
    if not body:
        return None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def _inputs(captured: dict):
    """("query" | "header" | "body", name, value) for every scalar the captured request carried."""
    for k, v in parse_qsl(urlsplit(captured["url"]).query, keep_blank_values=True):
        yield "query", k, v
    for k, v in (captured.get("request_headers") or {}).items():
        yield "header", k, v
    body = _body_json(captured)
    if isinstance(body, (dict, list)):
        for p, v in _walk(body):
            yield "body", p, v


def find_query_param(captured: dict, probe: str) -> str | None:
    """Where the search text went: a query param (GET) or a dotted body path (POST)."""
    want = probe.lower()
    for where, name, v in _inputs(captured):
        if where != "header" and isinstance(v, str) and want in v.lower():
            return name
    return None


def find_value_param(captured: dict, value: str) -> str | None:
    """The query param or body path whose value is exactly `value` (a restaurant id)."""
    for where, name, v in _inputs(captured):
        if where != "header" and not isinstance(v, bool) and v is not None and str(v) == str(value):
            return name
    return None


def _coord(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d{1,3}\.\d+\s*", v):
        return float(v)
    return None


def find_location_inputs(captured: dict, lat: float, lon: float) -> list[dict]:
    """Every query param, header or body path carrying the location's lat or lon (lat entries first)."""
    found = []
    for where, name, v in _inputs(captured):
        n = _coord(v)
        if n is None:
            continue
        if abs(n - lat) <= COORD_TOLERANCE:
            found.append({"in": where, "name": name, "value": "lat"})
        elif abs(n - lon) <= COORD_TOLERANCE:
            found.append({"in": where, "name": name, "value": "lon"})
    return sorted(found, key=lambda i: i["value"])


# --- POST approval -----------------------------------------------------------

def _preapproved_entries(preapproved: Iterable) -> list[dict]:
    out = []
    for p in preapproved or ():
        e = p if isinstance(p, dict) else {"url_regex": str(p), "why": "read-only (preapproved)"}
        out.append({"url_regex": e["url_regex"], "why": e.get("why") or "read-only (preapproved)"})
    return out


def _approve_blocked(page, guard, needle: str, why: str, reload_url: str, approve, preapproved: set[str]) -> None:
    """A POST the guard blocked that carries `needle` becomes a narrow allowlist entry, but only once it is
    preapproved or `approve(entries)` returns True; then the page is reloaded so the site makes the call again."""
    new = []
    for b in guard.blocked:
        if b.get("method") != "POST" or b.get("reason") not in (None, "POST not allowlisted"):
            continue
        body = b.get("body") or ""
        if guard_mod.MUTATION.search(body) or needle.lower() not in (body + b["url"]).lower():
            continue
        p = urlsplit(b["url"])
        entry = {"url_regex": f"^https://{re.escape(p.netloc)}{re.escape(p.path)}$", "why": why}
        if any(e["url_regex"] == entry["url_regex"] for e in [*guard.allowlist, *new]):
            continue
        new.append(entry)
    ask = [dict(e) for e in new if e["url_regex"] not in preapproved]
    # Task 4's contract: approve(list_of_entries) -> bool, once, and only an exact True allows them
    if ask and not (approve is not None and approve(ask) is True):
        raise sync.RecipeError("POST allowlist rejected")
    if new:
        guard.allowlist.extend(new)
        log.info("allowlisted %d read POST(s) after approval; reloading", len(new))
        _visit(page, reload_url)


# --- capture analysis --------------------------------------------------------

def _row_keys(item: dict) -> set[str]:
    keys = set(item)
    for v in item.values():
        if isinstance(v, dict):
            keys |= set(v)
    return keys


def _collections(data, key_re, path: str = "", out: list | None = None, depth: int = 0) -> list:
    """(score, path, count, first item) for every list of objects (or id-keyed map) whose keys look right."""
    out = [] if out is None else out
    if depth > 10:
        return out
    items = None
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data[:3]):
        items = data
    elif isinstance(data, dict) and len(data) >= 2 and all(isinstance(v, dict) for v in list(data.values())[:3]):
        items = list(data.values())
    if items:
        keys = set().union(*(_row_keys(i) for i in items[:3]))
        score = sum(bool(key_re.search(k)) for k in keys)
        if score >= 3:
            out.append((score, path or "(root)", len(items), items[0]))
    children = data.items() if isinstance(data, dict) else enumerate(data[:6]) if isinstance(data, list) else ()
    for k, v in children:
        _collections(v, key_re, f"{path}.{k}" if path else str(k), out, depth + 1)
    return out


def candidates(items: list[dict], key_re, needle: str | None = None) -> list[dict]:
    """Successful captured JSON responses holding a collection with matching keys, best first."""
    scored = []
    for c in items:
        if not 200 <= c.get("status", 0) < 300:
            continue
        cols = _collections(c["json"], key_re)
        if not cols:
            continue
        s = max(sc for sc, *_ in cols) + sum(n for _, _, n, _ in cols[:5]) // 5
        if needle and needle.lower() in (c["url"] + (c.get("request_body") or "")).lower():
            s += 10
        scored.append((s, c))
    return [c for _, c in sorted(scored, key=lambda x: -x[0])]


def _describe(c: dict, key_re) -> str:
    cols = sorted(_collections(c["json"], key_re), key=lambda x: (-x[0], -x[2]))[:5]
    shapes = "\n".join(f"  collection at {p!r} ({n} items); first item: {json.dumps(first, ensure_ascii=False)[:2500]}"
                       for _, p, n, first in cols)
    return (f"#{c['id']} {c['method']} {c['url'][:300]}\n"
            f"request_body: {(c.get('request_body') or '')[:1000]}\n{shapes}\n"
            f"response start: {c['sample'][:1500]}")


FEE_RULES = """fee_map: null, or paths from the response ROOT (not from one row) to fees the app itself returns:
 {"delivery": str|null, "packaging": str|null, "platform": str|null, "small_order": str|null, "surge": str|null,
  "member": str|null (true/false, or the membership's name, when the user is a member), "distance_km": str|null,
  "divisor": 1|100 (100 when money is in paise)}. Leave out fees the response does not have."""

RESTAURANTS_PROMPT = """kind: restaurants
These JSON API calls were captured while {site} showed its restaurant search results for "{probe}" to the
logged-in user. Pick the ONE call that returns the list of restaurants and map it. Reply with a JSON object:
{{"candidate": int (the #number of the call),
 "field_map": {{"restaurants_path": str (dotted path from the response root to the restaurant list; "*" fans out
                over lists, e.g. "data.cards.*.restaurants"), "restaurant_id": str, "name": str, "locality": str|null,
                "lat": str|null, "lon": str|null, "distance_km": str|null, "is_open": str|null,
                "url": str|null (the restaurant page's link)}},
 "fee_map": object|null}}
Paths other than restaurants_path are relative to ONE restaurant object. A literal value is written "=value".
{fee_rules}
Reply with JSON only.

CANDIDATES:
{samples}"""

MENU_PROMPT = """kind: menu
These JSON API calls were captured while {site} showed the menu of restaurant id {restaurant_id} to the logged-in
user. Pick the ONE call that returns the menu's dishes and map it. Reply with a JSON object:
{{"candidate": int (the #number of the call),
 "field_map": {{"dishes_path": str (dotted path from the response root to the dishes; menus nest dishes in
                categories, so use "*" to fan out, e.g. "data.cards.*.items.*"), "dish_id": str, "name": str,
                "variant": str|null (half/full/size), "price": str, "veg": str|null, "in_stock": str|null,
                "packaging": str|null (per-dish packaging charge), "restaurant_open": str|null (path from the
                response ROOT to the restaurant's open flag), "price_divisor": 1|100}},
 "fee_map": object|null}}
Dish paths are relative to ONE dish object. A literal value is written "=value".
{fee_rules}
Reply with JSON only.

CANDIDATES:
{samples}"""


def _default_llm(messages: list[dict]) -> dict:
    """The engine's llm.py (Claude Haiku). Imported on first use so a missing key fails only a learn."""
    try:
        mod = importlib.import_module("llm")
    except ImportError as e:
        raise sync.RecipeError("no LLM available: llm.py is not installed") from e
    resp = mod.create("food_learn", effort="medium", max_tokens=8000, messages=messages)
    return mod.json_reply(resp)


def _parse(reply) -> dict:
    if isinstance(reply, dict):
        return reply
    obj = json.loads(reply)
    if not isinstance(obj, dict):
        raise ValueError("the reply is not a JSON object")
    return obj


def _ask(llm: Llm, prompt: str, cands: list[dict], kind: str, validate) -> tuple[dict, dict, dict | None]:
    """Up to LLM_TRIES rounds: the reply picks a candidate and a field_map; validate(candidate, field_map) returns
    an error string that goes back to the model, or None."""
    by_id = {c["id"]: c for c in cands}
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(LLM_TRIES):
        reply = llm(messages)
        try:
            obj = _parse(reply)
            cand = by_id.get(obj.get("candidate"))
            if cand is None:
                raise ValueError(f"candidate {obj.get('candidate')!r} is not one of {sorted(by_id)}")
            fm = {k: obj["field_map"].get(k) for k in FIELD_KEYS[kind]} if isinstance(obj.get("field_map"), dict) \
                else None
            err = "field_map missing" if fm is None else validate(cand, fm)
        except Exception as e:  # any bad reply goes back to the model, not up the stack
            obj, cand, fm, err = {}, None, None, f"invalid reply: {type(e).__name__}: {e}"
        if err is None:
            return cand, fm, obj.get("fee_map") if isinstance(obj.get("fee_map"), dict) else None
        log.warning("%s field map attempt %d failed", kind, attempt + 1)
        messages = [*messages, {"role": "assistant", "content": reply if isinstance(reply, str) else json.dumps(reply)},
                    {"role": "user", "content": f"Validation failed: {err}\nReturn a corrected JSON object."}]
    raise sync.RecipeError(f"could not map the {kind} response")


def keep_fee_map(fee_map: dict | None, data) -> dict | None:
    """The LLM's fee_map, keeping only paths that resolve to a number (a bool or name for member)."""
    if not isinstance(fee_map, dict):
        return None
    out = {}
    for key in food_search.FEE_KEYS:
        path = fee_map.get(key)
        if not isinstance(path, str) or not path:
            continue
        v = food_search._first(data, path)
        if key == "member":
            ok = isinstance(v, (bool, int, float)) or (isinstance(v, str) and v.strip())
        else:
            ok = recipes.to_number(v) is not None and not (isinstance(v, str) and not re.search(r"\d", v))
        if ok:
            out[key] = path
    if not out:
        return None
    out["divisor"] = 100 if fee_map.get("divisor") in (100, "100") else 1
    return out


def _request_parts(c: dict) -> dict:
    parts = urlsplit(c["url"])
    body = _body_json(c)
    return {"endpoint": urlunsplit(parts._replace(query="", fragment="")),
            "method": c["method"].upper(),
            "query": dict(parse_qsl(parts.query, keep_blank_values=True)),
            "body": body if c["method"].upper() == "POST" else None,
            "headers": {k: v for k, v in browser.scrub_headers(c.get("request_headers") or {}).items()
                        if not _DROP_HEADER.search(k)}}


def _covering(allowlist: list[dict], endpoint: str, method: str) -> list[dict]:
    if method != "POST":
        return []
    return [{"url_regex": e["url_regex"], "why": e.get("why", "")} for e in allowlist
            if re.search(e["url_regex"], endpoint)]


def page_template(url: str, restaurant_id: str) -> str | None:
    """The restaurant page URL with its id as a whole path segment or query value replaced by {restaurant_id};
    None when the id is only part of a name slug (that URL does not generalise to other restaurants)."""
    rx = re.compile(rf"(?<=[/=]){re.escape(str(restaurant_id))}(?=$|[/?&#])")
    return rx.sub("{restaurant_id}", url, count=1) if rx.search(url) else None


def _visit(page, url: str) -> None:
    browser.goto(page, url)
    sync.ensure_logged_in(page)
    page.wait_for_timeout(SETTLE_MS)


# --- learning ----------------------------------------------------------------

def _learn_restaurants(site, location, page, guard, capture, approve, preapproved, llm) -> tuple[dict, list[dict]]:
    probe_url = food_search.food_page_url(site, RESTAURANT_PROBE, location)
    mark = len(capture.items)
    _visit(page, probe_url)
    _approve_blocked(page, guard, RESTAURANT_PROBE, "restaurant search (read-only)", probe_url, approve,
                     preapproved)
    cands = candidates(capture.items[mark:], RESTAURANTISH_KEY, RESTAURANT_PROBE)[:6]
    if not cands:
        raise sync.RecipeError(f"{site}: the search page returned no restaurant-like response")

    def validate(c, fm):
        rows = food_search.extract_restaurants(c["json"], fm)
        if len(rows) >= MIN_RESTAURANTS:
            return None
        found = food_search._rows(c["json"], fm.get("restaurants_path"))
        first = json.dumps(found[0], ensure_ascii=False)[:1500] if found else "(none)"
        return (f"the field_map extracted {len(rows)} restaurants with an id and a name (need at least "
                f"{MIN_RESTAURANTS}); restaurants_path {fm.get('restaurants_path')!r} reaches {len(found)} rows; "
                f"first row: {first}")

    prompt = RESTAURANTS_PROMPT.format(site=site, probe=RESTAURANT_PROBE, fee_rules=FEE_RULES,
                                       samples="\n\n".join(_describe(c, RESTAURANTISH_KEY) for c in cands))
    cand, fm, fee_map = _ask(llm, prompt, cands, "restaurants", validate)
    req = _request_parts(cand)
    recipe = {"kind": "restaurants", "site": site, "search_page_url": FOOD_PAGES[site], **req,
              "query_param": find_query_param(cand, RESTAURANT_PROBE),
              "location_inputs": find_location_inputs(cand, location["lat"], location["lon"]),
              "field_map": fm, "fee_map": keep_fee_map(fee_map, cand["json"]),
              "read_post_allowlist": _covering(guard.allowlist, req["endpoint"], req["method"])}
    if err := check_food_recipe(recipe):
        raise sync.RecipeError(f"restaurants recipe rejected: {err}")
    rows = food_search.extract_restaurants(cand["json"], fm)
    for r in rows:
        r["url"] = urljoin(probe_url, r["url"]) if r["url"] else None
    return recipe, rows


def _learn_menu(site, location, page, guard, capture, approve, preapproved, llm, first: dict) -> dict:
    rid, url = first["restaurant_id"], first["url"]
    mark = len(capture.items)
    _visit(page, url)
    _approve_blocked(page, guard, rid, "restaurant menu (read-only)", url, approve, preapproved)
    cands = candidates(capture.items[mark:], DISHISH_KEY, rid)[:6]
    if not cands:
        raise sync.RecipeError(f"{site}: the restaurant page returned no menu-like response")

    def validate(c, fm):
        menu = food_search.extract_menu(c["json"], fm)
        if len(menu["dishes"]) >= MIN_DISHES:
            return None
        found = food_search._rows(c["json"], fm.get("dishes_path"))
        first_row = json.dumps(found[0], ensure_ascii=False)[:1500] if found else "(none)"
        return (f"the field_map extracted {len(menu['dishes'])} dishes with an id, a name and a price > 0 (need at "
                f"least {MIN_DISHES}); dishes_path {fm.get('dishes_path')!r} reaches {len(found)} rows (use '*' to "
                f"fan out over categories); first row: {first_row}")

    prompt = MENU_PROMPT.format(site=site, restaurant_id=rid, fee_rules=FEE_RULES,
                                samples="\n\n".join(_describe(c, DISHISH_KEY) for c in cands))
    cand, fm, fee_map = _ask(llm, prompt, cands, "menu", validate)
    req = _request_parts(cand)
    recipe = {"kind": "menu", "site": site, "page_url_template": page_template(url, rid), **req,
              "restaurant_param": find_value_param(cand, rid),
              "location_inputs": find_location_inputs(cand, location["lat"], location["lon"]),
              "field_map": fm, "fee_map": keep_fee_map(fee_map, cand["json"]),
              "read_post_allowlist": _covering(guard.allowlist, req["endpoint"], req["method"])}
    if err := check_food_recipe(recipe):
        raise sync.RecipeError(f"menu recipe rejected: {err}")
    return recipe


def _replay(site, location, profile_dir, proxy_server, rest: dict, menu: dict, second: dict) -> None:
    """Both recipes must work through the user-side replay on the same profile, the menu with a second
    restaurant."""
    try:
        found = food_search.search_restaurants(site, RESTAURANT_PROBE, location, rest, profile_dir, proxy_server)
        if len(found["restaurants"]) < MIN_RESTAURANTS:
            raise sync.RecipeError(f"restaurants replay found {len(found['restaurants'])} restaurants")
        got = food_search.fetch_menu(site, second["restaurant_id"], location, menu, profile_dir, proxy_server,
                                     url=second["url"])
        if len(got["dishes"]) < MIN_DISHES:
            raise sync.RecipeError(f"menu replay with a second restaurant found {len(got['dishes'])} dishes")
    except SearchError as e:
        raise sync.RecipeError(f"replay failed: {e}") from e


def learn_food(site: str, location: dict, profile_dir, proxy_server=None, approve=None, preapproved=(),
               llm: Llm | None = None) -> dict:
    """{"restaurants": recipe, "menu": recipe}, learned on the admin's logged-in profile and validated by replay.
    Raises browser.SessionExpired when the profile is logged out, sync.RecipeError on any other failure."""
    if site not in FOOD_SITES:
        raise ValueError(f"not a food site: {site}")
    if (location or {}).get("lat") is None or (location or {}).get("lon") is None:
        raise sync.RecipeError("learning needs a delivery location with lat and lon")
    if food_search.food_page_url(site, RESTAURANT_PROBE, location) is None:
        raise sync.RecipeError(f"{site} search needs a city: give the location as 'area, city, state'")
    llm = llm or _default_llm
    allowed = _preapproved_entries(preapproved)
    pre = {e["url_regex"] for e in allowed}
    kw = {"allowlist": copy.deepcopy(allowed), "profile_dir": profile_dir, "location": location,
          "proxy_server": proxy_server}
    with browser.open_site(site, **kw) as (_ctx, page, guard, capture):
        _visit(page, browser.home_url(site, location))
        if agent.shows_login_button(agent.snapshot(page)["elements"]):
            raise browser.SessionExpired("the admin profile still shows a Login button")
        rest, rows = _learn_restaurants(site, location, page, guard, capture, approve, pre, llm)
        openable = [r for r in rows if r["is_open"] is not False and r["url"]]
        if len(openable) < 2:
            raise sync.RecipeError(f"{site}: need two open restaurants with a page link, found {len(openable)}")
        menu = _learn_menu(site, location, page, guard, capture, approve, pre, llm, openable[0])
    _replay(site, location, profile_dir, proxy_server, rest, menu, openable[1])
    log.info("%s food recipes learned and replayed (restaurants %s, menu %s)", site, rest["method"], menu["method"])
    return {"restaurants": rest, "menu": menu}

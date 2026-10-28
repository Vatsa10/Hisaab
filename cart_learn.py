"""Supervised cart-recipe learning (Plan 6 T4): admin only, on the admin's logged-in profile, under the CartGuard.

The learner sets the delivery location, adds the probe product once, raises it to 2 with "+", and records the
requests the app sent. Every POST endpoint the guard blocked is shown to the admin through `approve(entries)`
before it is allowed. Clicks go only to "Add" / "+" (and the location picker), filtered by
`agent.cart_click_refusal`: the learner never checks out, pays, or lowers or deletes anything. The recipe is
returned unsaved; publishing it is the admin's separate Approve (it always lands `pending`).

LLM calls go through an injected `llm(prompt) -> dict | JSON str`; the default is llm.py (Claude Haiku 5.5),
which meters every call through its usage hooks.
"""
import json
import logging
import re
from collections.abc import Callable, Iterable
from urllib.parse import parse_qsl, quote, urlsplit

import agent
import browser
import llm as llm_mod
from cart_guard import MUTATION, CartPolicy, matches, operation_name
from cart_recipe import check_cart_recipe, extract_cart
from guard import redact_url
from sync import RecipeError

log = logging.getLogger("cart_learn")

CART_PROBE = "milk"
ADD_BUTTON = re.compile(r"^\s*(add|add to cart|\+)\s*$", re.I)
PLUS_BUTTON = re.compile(r"^\s*\+\s*$")
LOCATION_STEPS = 15
FIELD_TRIES = 3
CURRENT_NEAR_DEG = 0.001
DROP_HEADERS = ("sec-", ":", "content-length", "host", "accept-encoding", "user-agent", "referer", "origin", "cookie")
LLM = Callable[[str], dict | str]


def _no_prompt(text: str, kind: str = "question") -> str:
    """Unattended: nobody answers the location agent, so a question ends the run (note "ask_timeout")."""
    raise TimeoutError("cart learning runs unattended")


# --- Plan 5 edges (imported lazily: search recipes are learned before any cart recipe) ---------------

def _products(items: list[dict], search_recipe: dict) -> list[tuple[str, str]]:
    """(product_id, name) from the captured search responses, via the search recipe's field map."""
    import search
    out = []
    for c in items:
        if isinstance(c.get("json"), (dict, list)):
            for p in search.extract_products(c["json"], search_recipe["field_map"]):
                if (p["product_id"], p["name"]) not in out:
                    out.append((str(p["product_id"]), p["name"]))
    return out


def _store_id(page, search_recipe: dict, location: dict):
    import search
    try:
        return search.resolve_store(page, search_recipe, location)
    except Exception as e:
        log.warning("store lookup failed while learning a cart recipe (%s)", type(e).__name__)
        return None


def _location_inputs(c: dict, lat: float, lon: float) -> list[dict]:
    import search_learn
    return search_learn.find_location_inputs(c, lat, lon)


# --- the location agent ---------------------------------------------------------------------------

CART_SYSTEM = """You operate a real browser that is logged in to the user's own {site} account.
Goal: set the delivery location. Open the location / address picker (usually at the top of the page) and choose
"Use current location" / "Detect my location", or the user's existing saved address that matches the goal.
Rules: never add, edit, save or delete an address; never add to cart, check out, pay or log out. There is no typing
tool. Call finish(success=true) as soon as the location is set.
{popups}"""


class CartLearnAgent(agent.Discovery):
    """Sets the app's delivery location under the cart click policy, in at most 15 steps."""
    SYSTEM = CART_SYSTEM
    MAX_STEPS = LOCATION_STEPS
    GOAL = ("Open the delivery-location picker and choose 'Use current location' / 'Detect my location', or the saved "
            "address that matches {query}. Never add, edit, save or delete an address. Finish as soon as the "
            "location is set.")
    KINDS = agent.COMMON_KINDS | {
        "done": "The delivery location is set: the header shows the chosen location and no picker is open.",
        "logged_out": "The site shows the user is logged out: a Login/Sign in button instead of an account menu.",
    }
    DONE_CHECK = "Is a delivery location set (shown in the page header) with no location picker open?"

    def __init__(self, site, page, guard, capture, prompt=None, home=None, runs_dir=None, query: str = ""):
        super().__init__(site, page, guard, capture, prompt=prompt, home=home, runs_dir=runs_dir)
        self.GOAL = type(self).GOAL.format(query=repr(query or "the current location"))
        self.steps = 0

    def refuse_click(self, el: dict) -> str | None:
        return agent.cart_click_refusal(el)

    def observe(self) -> None:
        self.steps += 1
        if self.steps > self.MAX_STEPS:
            self.done = {"success": False, "note": "step limit", "url": self.page.url}
            return
        super().observe()

    def premature(self) -> str | None:
        return None

    def auto_load_more(self) -> bool:
        return False

    def progress(self) -> dict:
        return {"steps": self.steps}


# --- helpers --------------------------------------------------------------------------------------

def find_value_path(data, predicate: Callable[[object], bool], path: str = "") -> str | None:
    """Shortest dotted path to a scalar for which predicate holds (breadth first)."""
    level = [(path, data)]
    for _ in range(10):
        nxt = []
        for p, v in level:
            if isinstance(v, dict):
                nxt += [(f"{p}.{k}" if p else str(k), x) for k, x in v.items()]
            elif isinstance(v, list):
                nxt += [(f"{p}.{i}" if p else str(i), x) for i, x in enumerate(v[:50])]
            elif p:
                try:
                    if predicate(v):
                        return p
                except Exception:
                    continue
        if not nxt:
            break
        level = nxt
    return None


def _entry(url: str, why: str, op: str, body: str | None = None) -> dict:
    u = urlsplit(url)
    e = {"url_regex": f"^https://{re.escape(u.netloc)}{re.escape(u.path)}$", "why": why, "op": op}
    if body and MUTATION.search(body) and (name := operation_name(body)):
        e["operation"] = name
    return e


def propose(blocked: list[dict], needles: Iterable[str], op: str, why: str) -> list[dict]:
    """Allowlist entries for the blocked POSTs whose URL or body carries a needle, anchored and deduplicated."""
    needles = [str(n) for n in needles if str(n)]
    out = []
    for b in blocked:
        if (b.get("method") or "").upper() != "POST":
            continue
        body = b.get("body") or ""
        if not any(n in b["url"] or n in body for n in needles):
            continue
        e = _entry(b["url"], why, op, body)
        if all(o["url_regex"] != e["url_regex"] for o in out):
            out.append(e)
    return out


def _num(v) -> float | None:
    """A JSON number or an all-digits string; never a product code like "P1"."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", v):
        return float(v)
    return None


def qty_mode_from(first, second) -> str:
    """Add then "+": the second request carrying 2 means absolute quantities, 1 means increments (R4)."""
    a, b = _num(first), _num(second)
    if a == 1 and b == 2:
        return "set"
    if a == 1 and b == 1:
        return "increment"
    raise RecipeError("couldn't tell how the cart counts quantities")


def _ask(guard, op: str, entries: list[dict], approve, rejected: str) -> None:
    """New entries go to the admin; only approved ones reach the guard."""
    have = {e.get("url_regex") for e in getattr(guard.policy, op)}
    new = [e for e in entries if e["url_regex"] not in have]
    if not new:
        return
    if not approve(new):
        raise RecipeError(rejected)
    try:
        guard.allow(op, new)
    except ValueError as e:
        raise RecipeError(f"{op} permission can't be used: {e}") from e


def _json_body(c: dict):
    try:
        body = json.loads(c.get("request_body") or "")
    except (TypeError, ValueError):
        return None
    return body if isinstance(body, (dict, list)) else None


def _headers(c: dict) -> dict:
    return {k: v for k, v in (c.get("request_headers") or {}).items() if not k.lower().startswith(DROP_HEADERS)}


def _spec(c: dict) -> dict:
    return {"method": c["method"].upper(), "url": c["url"], "headers": _headers(c), "body": _json_body(c)}


def _ok(c: dict) -> bool:
    return 200 <= c.get("status", 0) < 300 and isinstance(c.get("json"), (dict, list))


def _allowed(url: str, entries) -> bool:
    return any(matches(e.get("url_regex"), url) for e in entries)


def _value_path(c: dict, predicate) -> str | None:
    """Where a request carries a value: a JSON-body path, else "query:<name>"."""
    body = _json_body(c)
    if body is not None and (p := find_value_path(body, predicate)):
        return p
    for k, v in parse_qsl(urlsplit(c["url"]).query):
        try:
            if predicate(v):
                return f"query:{k}"
        except Exception:
            continue
    return None


def _value_at(c: dict, path: str):
    if path.startswith("query:"):
        return dict(parse_qsl(urlsplit(c["url"]).query)).get(path.removeprefix("query:"))
    from recipes import get_path
    return get_path(_json_body(c), path)


def _same(a) -> Callable[[object], bool]:
    return lambda v: v is not None and not isinstance(v, (bool, dict, list)) and str(v) == str(a)


def _near(target: float) -> Callable[[object], bool]:
    def pred(v):
        n = _num(v)
        return n is not None and abs(n - target) <= CURRENT_NEAR_DEG
    return pred


def _clickable(page, pattern: re.Pattern) -> dict | None:
    """The first element matching pattern that the cart click policy allows."""
    for el in agent.snapshot(page)["elements"]:
        text = (el.get("text") or el.get("label") or "")
        if pattern.match(text) and agent.cart_click_refusal(el) is None:
            return el
    return None


def _click(page, pattern: re.Pattern) -> bool:
    el = _clickable(page, pattern)
    if el is None:
        return False
    agent.click_ref(page, el["ref"])
    agent.settle(page)
    return True


# --- location -------------------------------------------------------------------------------------

def _run_location_agent(site, page, guard, capture, home, runs_dir, query) -> dict:
    res = CartLearnAgent(site, page, guard, capture, prompt=_no_prompt, home=home, runs_dir=runs_dir,
                         query=query).run()
    if (res or {}).get("note") == "ask_timeout":
        raise RecipeError("the location agent needed help; cart learning runs unattended")
    return res or {}


def _saved_address_select(c: dict, items: list[dict], lat: float, lon: float) -> dict | None:
    """address_id mode: the select body holds an id from a captured saved-address list."""
    body = _json_body(c)
    for o in reversed(items):
        if o is c or not _ok(o) or o["method"].upper() != "GET":
            continue
        for list_path, rows in _lists(o["json"]):
            for row in rows:
                for key, v in row.items():
                    if not re.search(r"id$", str(key), re.I) or isinstance(v, (bool, dict, list)) or v in (None, ""):
                        continue
                    if body is not None and (p := find_value_path(body, _same(v))):
                        return {"mode": "address_id", **_spec(c), "address_id_path": p,
                                "saved": {"method": "GET", "url": o["url"], "headers": _headers(o),
                                          "list_path": list_path, "id": key,
                                          "lat": find_value_path(row, _near_deg(lat)),
                                          "lon": find_value_path(row, _near_deg(lon)),
                                          "label": next((k for k in row if re.search(r"label|tag|name|title", str(k),
                                                                                     re.I)), None)}}
    return None


def _near_deg(target: float, deg: float = 0.01):
    def pred(v):
        n = _num(v)
        return n is not None and abs(n - target) <= deg
    return pred


def _lists(data, path: str = "", depth: int = 0):
    """(path, rows) for every list of objects in a JSON value."""
    if depth > 6:
        return
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
        if rows:
            yield path, rows
        return
    if isinstance(data, dict):
        for k, v in data.items():
            yield from _lists(v, f"{path}.{k}" if path else str(k), depth + 1)


def _learn_location(site, page, guard, capture, home, runs_dir, location, store_id, approve) -> dict:
    lat, lon = float(location["lat"]), float(location["lon"])
    query = location.get("label") or location.get("address") or ""
    capture.clear()
    guard.blocked.clear()
    _run_location_agent(site, page, guard, capture, home, runs_dir, query)
    needles = [f"{lat:.3f}", f"{lat:.4f}", f"{lon:.3f}", f"{lon:.4f}"]
    entries = propose(guard.blocked, needles, "location", "select the delivery location (existing only)")
    if entries:
        _ask(guard, "location", entries, approve, "location permission rejected")
        _run_location_agent(site, page, guard, capture, home, runs_dir, query)

    select = None
    posts = [c for c in capture.items if c["method"].upper() == "POST" and _allowed(c["url"], guard.policy.location)]
    if posts:
        c = posts[-1]
        inputs = _location_inputs(c, lat, lon)
        lat_in = next((i for i in inputs if i["value"] == "lat"), None)
        lon_in = next((i for i in inputs if i["value"] == "lon"), None)
        if lat_in and lon_in:
            select = {"mode": "latlon", **_spec(c), "lat_input": lat_in, "lon_input": lon_in}
        else:
            select = _saved_address_select(c, capture.items, lat, lon)

    current = None
    for c in reversed(capture.items):
        if not _ok(c):
            continue
        if not (c["method"].upper() == "GET" or _allowed(c["url"], guard.policy.read)):
            continue
        sid = find_value_path(c["json"], _same(store_id)) if store_id is not None else None
        la, lo = find_value_path(c["json"], _near(lat)), find_value_path(c["json"], _near(lon))
        if sid or (la and lo):
            current = {**_spec(c), "store_id_path": sid, "lat_path": la if la and lo else None,
                       "lon_path": lo if la and lo else None, "area_path": None}
            break
    if current is None:
        raise RecipeError("cart recipe can't read the app's location")
    return {"current": current, "select": select}


# --- add ------------------------------------------------------------------------------------------

def _add_requests(items: list[dict], guard, ids: list[str]) -> list[dict]:
    return [c for c in items if c["method"].upper() == "POST" and _allowed(c["url"], guard.policy.cart)
            and any(i in c["url"] or i in (c.get("request_body") or "") for i in ids)]


def _learn_add(page, guard, capture, ids: list[str], store_id, approve) -> tuple[dict, str]:
    guard.blocked.clear()
    if not _click(page, ADD_BUTTON):
        raise RecipeError("no Add button found")
    entries = propose(guard.blocked, ids, "cart", "add the planned product to the cart")
    if not entries and not _add_requests(capture.items, guard, ids):
        raise RecipeError("the Add button sent no cart request")
    _ask(guard, "cart", entries, approve, "cart permission rejected")
    start = len(capture.items)
    if entries:  # the first click was blocked: add again, now allowed
        if not _click(page, ADD_BUTTON):
            raise RecipeError("no Add button found")
    else:
        start = 0
    if not _click(page, PLUS_BUTTON):
        raise RecipeError("no + button found after adding")
    adds = _add_requests(capture.items[start:], guard, ids)
    if len(adds) < 2:
        raise RecipeError("couldn't record adding the probe product twice")
    first, second = adds[0], adds[1]
    pid = next(i for i in ids if _value_path(first, _same(i)))
    pid_path = _value_path(first, _same(pid))
    sid_path = _value_path(first, _same(store_id)) if store_id is not None else None
    qty_path = _value_path(first, lambda v: _num(v) == 1 and str(v) not in (str(pid), str(store_id)))
    if qty_path is None or qty_path in (pid_path, sid_path):
        raise RecipeError("couldn't find the quantity in the add request")
    mode = qty_mode_from(_value_at(first, qty_path), _value_at(second, qty_path))
    add = {**_spec(first), "product_id_path": pid_path, "qty_path": qty_path, "store_id_path": sid_path,
           "qty_mode": mode}
    return add, pid


# --- read -----------------------------------------------------------------------------------------

def _holds_line(data, pid: str, qty: int) -> bool:
    """Some object carries the product id and, within it, the quantity."""
    stack = [data]
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            if find_value_path(v, _same(pid)) is not None and any(
                    _num(x) == qty for x in v.values()
                    if not isinstance(x, (dict, list))):
                return True
            stack += list(v.values())
        elif isinstance(v, list):
            stack += v[:100]
    return False


READ_PROMPT = """CART FIELD MAP TASK
This JSON is {site}'s cart response, after adding the product {pid!r} twice (quantity 2), from {endpoint}.
Return a JSON object {{"field_map": {{...}}}} where field_map has exactly these keys:
 "lines_path": str (dotted path from the root to the list of cart lines),
 "product_id": str, "name": str, "qty": str, "price": str|null (paths relative to ONE line),
 "subtotal": str, "total": str|null (paths from the root),
 "fees": {{"delivery"?: str, "handling"?: str, "small_cart"?: str, "surge"?: str, "other"?: str}} (paths from the root),
 "out_of_stock": str|null (path relative to one line)
Reply with JSON only.

RESPONSE:
{sample}"""

_FM_KEYS = ("lines_path", "product_id", "name", "qty", "price", "subtotal", "total", "fees", "out_of_stock")


def _ask_json(llm: LLM, prompt: str) -> dict:
    r = llm(prompt)
    if isinstance(r, str):
        r = json.loads(re.sub(r"^\s*```(?:json)?|```\s*$", "", r.strip()))
    if not isinstance(r, dict):
        raise ValueError("the model did not return a JSON object")
    return r


def _read_field_map(site: str, c: dict, pid: str, llm: LLM) -> tuple[dict, str | None]:
    base = READ_PROMPT.format(site=site, pid=pid, endpoint=redact_url(c["url"]),
                              sample=json.dumps(c["json"], ensure_ascii=False)[:12000])
    prompt, err, fm = base, "", None
    for attempt in range(FIELD_TRIES):
        fm = None
        try:
            raw = _ask_json(llm, prompt)
            fm = raw.get("field_map", raw)
            if not isinstance(fm, dict):
                raise ValueError("field_map is not an object")
            fm = {k: fm.get(k) for k in _FM_KEYS}
            fm["fees"] = fm["fees"] if isinstance(fm["fees"], dict) else {}
            cart = extract_cart(c["json"], fm)
            line = next((ln for ln in cart["lines"] if ln["product_id"] == str(pid)), None)
            if line is None:
                err = f"no line with product_id {pid!r} ({len(cart['lines'])} lines extracted)"
            elif line["qty"] != 2:
                err = f"the probe line's qty is {line['qty']}, expected 2"
            elif not (cart["subtotal"] or 0) > 0:
                err = f"subtotal {cart['subtotal']!r} is not above 0"
            else:
                return fm, line.get("name")
        except Exception as e:
            err = f"invalid field_map: {type(e).__name__}: {e}"
        log.warning("%s cart field_map attempt %d failed", site, attempt + 1)
        prompt = (f"{base}\n\nYour previous answer: {json.dumps(fm, ensure_ascii=False)[:2000]}\n"
                  f"Validation failed: {err}\nReturn a corrected JSON object.")
    raise RecipeError(f"could not map the {site} cart response: {err}")


def _learn_read(page, guard, capture, pid: str, approve, home: str) -> dict:
    def latest():
        return next((c for c in reversed(capture.items) if _ok(c) and _holds_line(c["json"], pid, 2)), None)
    c = latest()
    if c is None:
        reads = [b for b in guard.blocked if not _allowed(b["url"], guard.policy.cart)]
        entries = propose(reads, ["cart"], "read", "read the cart (read-only)")
        if not entries:
            raise RecipeError("couldn't find the cart response")
        _ask(guard, "read", entries, approve, "cart read permission rejected")
        browser.goto(page, home)
        agent.settle(page)
        if (c := latest()) is None:
            raise RecipeError("couldn't find the cart response")
    if c["method"].upper() == "POST":
        _ask(guard, "read", [_entry(c["url"], "read the cart (read-only)", "read", c.get("request_body"))], approve,
             "cart read permission rejected")
    return c


def _default_llm(prompt: str):
    resp = llm_mod.create("cart_field_map", effort="low", max_tokens=4000,
                          messages=[{"role": "user", "content": prompt}])
    return llm_mod.json_reply(resp)


# --- learning -------------------------------------------------------------------------------------

def learn_cart(site: str, profile_dir, location: dict, search_recipe: dict, approve, preapproved=(),
               proxy_server: str | None = None, llm: LLM | None = None, runs_dir=None) -> dict:
    """Learn a cart recipe on the admin's restored, logged-in profile. Returns {"recipe", "probe_item"}; never
    saves. Every new POST endpoint is approved through approve(entries) first."""
    if (location or {}).get("lat") is None or (location or {}).get("lon") is None:
        raise RecipeError("learning a cart recipe needs a location (lat/lon)")
    if not search_recipe or not search_recipe.get("field_map"):
        raise RecipeError("learn the site's search recipe first")
    llm = llm or _default_llm
    pre = {op: tuple(dict(e) for e in preapproved if e.get("op") == op) for op in ("cart", "location", "read")}
    search_read = tuple(dict(e) for e in search_recipe.get("read_post_allowlist") or ())
    policy = CartPolicy(cart=pre["cart"], location=pre["location"], read=pre["read"] + search_read)
    home = search_recipe.get("home_url") or browser.home_url(site, location)
    with browser.open_site(site, profile_dir=profile_dir, location=location, proxy_server=proxy_server,
                           cart_policy=policy) as (_, page, guard, capture):
        browser.goto(page, home)
        agent.settle(page)
        store_id = _store_id(page, search_recipe, location)
        loc = _learn_location(site, page, guard, capture, home, runs_dir, location, store_id, approve)

        capture.clear()
        guard.blocked.clear()
        browser.goto(page, search_recipe["search_page_url"].replace("{q}", quote(CART_PROBE)))
        agent.settle(page)
        products = _products(capture.items, search_recipe)
        if not products:
            raise RecipeError(f"no products found for {CART_PROBE!r}")
        names = dict(products)
        add, pid = _learn_add(page, guard, capture, list(names), store_id, approve)
        c = _learn_read(page, guard, capture, pid, approve, home)
        field_map, line_name = _read_field_map(site, c, pid, llm)
        search_regexes = {e.get("url_regex") for e in search_read}
        recipe = {"kind": "cart", "site": site, "home_url": home, "add": add,
                  "read": {**_spec(c), "field_map": field_map}, "location": loc,
                  "cart_allowlist": [dict(e) for e in guard.policy.cart],
                  "location_allowlist": [dict(e) for e in guard.policy.location],
                  "read_post_allowlist": [dict(e) for e in guard.policy.read if e.get("url_regex") not in search_regexes]}
    if err := check_cart_recipe(recipe, site):
        raise RecipeError(f"cart recipe rejected: {err}")
    log.info("%s cart recipe learned (qty_mode %s)", site, add["qty_mode"])
    return {"recipe": recipe, "probe_item": line_name or names.get(pid) or CART_PROBE}

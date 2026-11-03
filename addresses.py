"""Saved delivery addresses, read live from an app's own saved-address list.

Strictly read-only: one GET of the list the app already shows the user, replayed from inside the page (the
fast path) under the normal read-only guard. Addresses are never created, edited, selected or deleted.

The `addresses` recipe (endpoint + field_map) is learned by an admin only; a user's job just replays it. Nothing
here knows any site's endpoint. Address text and coordinates are never logged: logs carry counts and the site.
"""
import json
import logging
import math
import re
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import agent
import browser
import recipes
from browser import SITES, fetch_in_page, home_url, open_site
from guard import BLOCKED_URL, redact_url
from sync import RecipeError, goto

log = logging.getLogger("addresses")

FIELDS = ("address_id", "label", "line", "area", "city", "lat", "lon", "is_default")
TEXT_MAX = 300
LABELS = {"home": "Home", "work": "Work", "office": "Work", "other": "Other", "others": "Other"}
_TRUE = {"true", "1", "yes", "y", "default"}


def db_site(site: str) -> str:
    """The account a site's addresses belong to (Instamart -> swiggy)."""
    return SITES.get(site, {}).get("db_site", site)


def distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(h)))


def norm_line(text) -> str:
    """An address line reduced for comparison: lower case, letters and digits, single spaces."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(text or "").lower()).split())


def _text(v) -> str | None:
    if v is None or isinstance(v, (dict, list)):
        return None
    s = " ".join(str(v).split())
    return s[:TEXT_MAX] or None


def _coord(v, limit: float) -> float | None:
    n = recipes.to_number(v)
    return n if n is not None and math.isfinite(n) and abs(n) <= limit else None


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v or "").strip().lower() in _TRUE


def normalise(data, field_map: dict) -> list[dict]:
    """Apply an `addresses` field_map to one response. Entries without coordinates are kept only when an area
    or city still places them; entries without an id are dropped; an id seen twice is kept once."""
    rows = recipes.get_path(data, field_map.get("list_path")) if field_map.get("list_path") else data
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list):
        return []
    out, seen = [], set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        get = lambda key: recipes.get_path(raw, field_map.get(key))  # noqa: E731
        address_id = _text(get("address_id"))
        if not address_id or address_id in seen:
            continue
        lat, lon = _coord(get("lat"), 90), _coord(get("lon"), 180)
        if lat is None or lon is None or (lat == 0 and lon == 0):
            lat = lon = None
        area, city = _text(get("area")), _text(get("city"))
        if lat is None and not (area or city):
            continue
        label = _text(get("label"))
        seen.add(address_id)
        out.append({"address_id": address_id,
                    "label": LABELS.get(label.lower(), label) if label else None,
                    "line": _text(get("line")), "area": area, "city": city,
                    "lat": lat, "lon": lon, "is_default": _bool(get("is_default"))})
    return out


def fetch_saved(site: str, page, recipe: dict) -> list[dict]:
    """GET the user's saved addresses on site from inside page (its cookies apply) and normalise them.
    Only a GET recipe is ever replayed."""
    if str(recipe.get("method") or "GET").upper() != "GET":
        raise ValueError("an addresses recipe must be a GET (read-only)")
    req = recipes.first_request(recipe)
    req["body"] = None
    rows = normalise(fetch_in_page(page, req), recipe.get("field_map") or {})
    log.info("%s: %d saved addresses", site, len(rows))
    return rows


def fetch_for_site(site: str, recipe: dict, profile_dir=None, location: dict | None = None,
                   proxy_server: str | None = None) -> list[dict]:
    """Open the site's home page in the user's profile under the read-only guard (no read POSTs at all) and
    fetch_saved there. The page itself is never an address page: the guard blocks those navigations."""
    with open_site(site, allowlist=[], profile_dir=profile_dir, location=location,
                   proxy_server=proxy_server) as (_, page, _guard, _capture):
        goto(page, home_url(site, location))
        return fetch_saved(site, page, recipe)


# --- learning the `addresses` recipe (admin only) ------------------------------------------------------------
# The admin's restored, logged-in profile is opened read-only (no POST is ever allowed; media is never blocked), the
# account page that lists the saved addresses is opened (code clicks an "Addresses" link; Jev, then Claude, decide
# anything else), and the captured JSON list becomes a GET recipe whose field_map an LLM writes and normalise()
# validates on that same response. Nothing is saved here (recipe_admin.apply_learned does that).

LLM = Callable[[str], dict | str]
ADDRESS_LINK = re.compile(r"^\s*(manage |saved |my |your )?(delivery )?addresses\s*$", re.I)
# Words that change an address book: never clicked while learning (on top of agent's write words).
ADDRESS_CHANGE = re.compile(r"\b(add(?!ress)\w*|new|edit\w*|delete\w*|remove\w*|save|set as default|make default|"
                            r"use this|deliver here|change\w*|update\w*|select\w*)\b|\+", re.I)
LAT_KEY = re.compile(r"^(lat|latitude)$", re.I)
LON_KEY = re.compile(r"^(lng|lon|long|longitude)$", re.I)
TEXT_KEY = re.compile(r"address|line|locality|landmark|area|street|building|flat|house", re.I)
ADDRESSISH_KEY = re.compile(r"address|line|locality|landmark|area|city|street|building|flat|house|pin|zip|label|"
                            r"annotation|tag|type|default|lat|lng|lon", re.I)
PIN_BONUS = 6
FIELD_TRIES = 3
DROP_HEADERS = ("sec-", ":", "content-length", "host", "accept-encoding", "user-agent", "referer", "origin", "cookie")
_WRITE_PATH = re.compile(r"cart|checkout|pay|place-?order|cancel", re.I)


def address_click_refusal(el: dict) -> str | None:
    """The address learner's click policy: agent's read-only policy, except that a link to the saved-address list
    may say "address", plus a refusal of every word that adds, edits, deletes, selects or saves an address."""
    blob = " ".join(str(el.get(k) or "") for k in ("text", "label"))
    if ADDRESS_CHANGE.search(blob):
        return f"text would change an address: {blob[:60]!r}"
    if agent.CLICK_BLOCK.search(re.sub(r"address\w*", " ", blob, flags=re.I)):
        return f"text looks like a write action: {blob[:60]!r}"
    href = el.get("href") or ""
    if href and BLOCKED_URL.search(re.sub(r"address\w*", " ", href, flags=re.I)):
        return "link points to a blocked URL"
    if el.get("type") == "submit" or (el.get("in_form") and el.get("tag") in ("button", "input")
                                      and el.get("type") != "button"):
        return "would submit a form"
    return None


def _keys(item: dict) -> set[str]:
    return set(item) | {k for v in item.values() if isinstance(v, dict) for k in v}


def address_lists(data, path: str = "", out: list | None = None, depth: int = 0) -> list:
    """Address-like lists anywhere in a JSON body (lists of objects, or id->object maps): (score, path, count).
    A list counts only when its records carry address text; a lat/lon key pair adds PIN_BONUS."""
    out = [] if out is None else out
    if depth > 8:
        return out
    items = None
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data[:3]):
        items = data
    elif (isinstance(data, dict) and len(data) >= 1 and all(isinstance(v, dict) for v in list(data.values())[:3])
          and not any(ADDRESSISH_KEY.search(str(k)) for k in data)):
        items = list(data.values())
    if items:
        keys = set().union(*(_keys(i) for i in items[:3]))
        if any(TEXT_KEY.search(k) for k in keys):
            pin = any(LAT_KEY.match(k) for k in keys) and any(LON_KEY.match(k) for k in keys)
            score = sum(bool(ADDRESSISH_KEY.search(k)) for k in keys) + (PIN_BONUS if pin else 0)
            out.append((score, path or "(root)", len(items), pin))
    children = data.items() if isinstance(data, dict) else enumerate(data[:3]) if isinstance(data, list) else ()
    for k, v in children:
        address_lists(v, f"{path}.{k}" if path else str(k), out, depth + 1)
    return out


def _has_pins(data) -> bool:
    return any(pin for *_, pin in address_lists(data))


def best_address_response(captured: list[dict]) -> dict | None:
    """The captured successful GET JSON response holding the best address-record list. A list needs address text
    plus a pin pair or several address fields (an order list with one delivery_address string does not count)."""
    best, best_score = None, 0
    for c in captured:
        if not (200 <= c.get("status", 0) < 300) or str(c.get("method") or "GET").upper() != "GET":
            continue
        if not isinstance(c.get("json"), (dict, list)):
            continue
        sc = max((s for s, *_ in address_lists(c["json"])), default=0)
        if sc >= 4 and sc > best_score:
            best, best_score = c, sc
    return best


class AddressDiscovery(agent.Discovery):
    """agent.Discovery aimed at the account page that lists the saved delivery addresses. DONE as soon as a
    captured JSON response holds a list of address records; code clicks an obvious "Addresses" link first."""
    SYSTEM = """You operate a real browser that is logged in to the user's own {site} account.
Goal: open the page that lists the user's SAVED DELIVERY ADDRESSES (account menu, profile, "Addresses" or
"Manage addresses"). Only open and look at that list.
Rules: you are strictly read-only. Never add, edit, delete, select, save or set as default an address; never click
"Deliver here", "Use this address" or "Change"; never add to cart, pay, cancel, or log out. There is no typing tool.
The address list usually lives under an account/profile menu (often an icon top-right).
If the site shows you are logged out (a Login/Sign in button instead of an account menu), call
finish(success=false, note="LOGGED_OUT").
{popups}"""
    KINDS = agent.COMMON_KINDS | {
        "done": "The list of the user's saved delivery addresses is shown on the page.",
        "logged_out": "The site shows the user is logged out: a Login/Sign in button instead of an account menu.",
    }
    GOAL = ("Open this site's page that lists the user's saved delivery addresses. Never add, edit, delete or "
            "select an address.")
    DONE_CHECK = "Does this page show a list of the user's own saved delivery addresses?"
    LINK_CLICK_CAP = 2

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.GOAL = type(self).GOAL  # not the order-history target of SITES[site]["goal"]
        self.link_clicks = 0

    def refuse_click(self, el: dict) -> str | None:
        return address_click_refusal(el)

    def address_responses(self) -> int:
        items = self.capture.items if self.capture is not None else []
        return sum(best_address_response([c]) is not None for c in items)

    def premature(self) -> str | None:
        if self.address_responses() >= 1 or self.refusals >= 2:
            return None
        self.refusals += 1
        return "no saved-address list has loaded yet; open the account menu and its Addresses page"

    def progress(self) -> dict:
        return {"address_api_responses_seen": self.address_responses()}

    def fast_decision(self):
        """Code, not a model: finish once an address list has arrived; else click a visible "Addresses" link."""
        if self.address_responses() >= 1:
            return "finish", {"success": True, "note": "done"}, "code"
        if self.link_clicks < self.LINK_CLICK_CAP:
            el = next((e for e in self.snap["elements"]
                       if ADDRESS_LINK.match(e.get("text") or e.get("label") or "") and not self.refuse_click(e)),
                      None)
            if el:
                self.link_clicks += 1
                return "click", {"element_ref": el["ref"]}, "code"
        return None

    def auto_load_more(self) -> bool:
        return False  # an address list has nothing to page through

    def _new_run_log(self) -> Path:
        return (self.runs_dir or agent.RUNS) / f"{self.site}-addresses-{datetime.now():%Y%m%d-%H%M%S}.jsonl"


FIELD_MAP_PROMPT = """FIELD MAP TASK
This JSON is {site}'s list of the user's saved delivery addresses, from {endpoint}.
Return a JSON object {{"field_map": {{...}}}} where field_map has exactly these keys:
 "list_path": str (dotted path to the address list; "" when the root is the list),
 "address_id": str, "label": str|null (Home / Work / a name), "line": str (the address text),
 "area": str|null (locality / area), "city": str|null, "lat": str|null, "lon": str|null, "is_default": str|null
All paths except list_path are relative to ONE address record (dotted for nested objects).
Reply with JSON only.

SHAPE: {hint}

RESPONSE:
{sample}"""
FIELD_KEYS = ("list_path", "address_id", "label", "line", "area", "city", "lat", "lon", "is_default")


def _ask_json(llm: LLM, prompt: str) -> dict:
    r = llm(prompt)
    if isinstance(r, str):
        r = json.loads(re.sub(r"^\s*```(?:json)?|```\s*$", "", r.strip()))
    if not isinstance(r, dict):
        raise ValueError("the model did not return a JSON object")
    return r


def _map_error(data, fm: dict) -> str | None:
    rows = normalise(data, fm)
    if not any(r.get("line") for r in rows):
        return f"field_map extracted {len(rows)} addresses, none with text (need an id and a line)"
    if _has_pins(data) and not any(r.get("lat") is not None for r in rows):
        return "the response carries coordinates but no address got a lat/lon"
    return None


def address_field_map(site: str, c: dict, llm: LLM) -> dict:
    """The LLM's field_map for a captured address list, accepted only when normalise() finds at least one address
    with text (and a pin when the response has coordinates). Retried with the failure; never logs the response."""
    data = c["json"]
    hint = "; ".join(f"address list at {p!r} ({n} records)"
                     for _, p, n, _pin in sorted(address_lists(data), key=lambda x: -x[0])[:3])
    base = FIELD_MAP_PROMPT.format(site=site, endpoint=redact_url(c["url"]), hint=hint or "unknown",
                                   sample=json.dumps(data, ensure_ascii=False)[:12000])
    prompt, fm = base, None
    for attempt in range(FIELD_TRIES):
        try:
            raw = _ask_json(llm, prompt)
            fm = raw.get("field_map", raw)
            if not isinstance(fm, dict):
                raise ValueError("field_map is not an object")
            fm = {k: fm.get(k) for k in FIELD_KEYS}
            err = _map_error(data, fm)
        except Exception as e:
            err = f"invalid field_map: {type(e).__name__}: {e}"
        if not err:
            return fm
        # err goes back to the model only; it is never raised or logged (it may quote an address).
        log.warning("%s addresses field_map attempt %d failed", site, attempt + 1)
        prompt = (f"{base}\n\nYour previous answer: {json.dumps(fm, ensure_ascii=False)[:2000]}\n"
                  f"Validation failed: {err}\nReturn a corrected JSON object.")
    raise RecipeError(f"could not map the {site} saved-address response after {FIELD_TRIES} tries")


def check_addresses_recipe(recipe: dict) -> str | None:
    """Why a learned addresses recipe must not be used, or None. It is one read-only GET and nothing else."""
    for k in ("endpoint", "method", "field_map"):
        if not recipe.get(k):
            return f"missing {k}"
    if str(recipe["method"]).upper() != "GET":
        return f"an addresses recipe must be a GET (read-only), not {recipe['method']}"
    if recipe.get("body") not in (None, "", {}):
        return "an addresses recipe carries no body"
    if recipe.get("read_post_allowlist"):
        return "an addresses recipe allows no POST"
    parts = urlsplit(recipe["endpoint"])
    if parts.scheme != "https":
        return "endpoint must be https"
    if _WRITE_PATH.search(parts.path):
        return "endpoint path matches the blocklist"
    fm = recipe["field_map"]
    if not isinstance(fm, dict) or not fm.get("address_id") or not fm.get("line"):
        return "field_map needs address_id and line"
    return None


def _recipe_from(site: str, c: dict) -> dict:
    parts = urlsplit(c["url"])
    return {"kind": "addresses", "site": site, "home_url": SITES[site]["home"],
            "endpoint": urlunsplit(parts._replace(query="", fragment="")), "method": "GET",
            "query": dict(parse_qsl(parts.query)), "body": None,
            "headers": {k: v for k, v in (c.get("request_headers") or {}).items()
                        if not k.lower().startswith(DROP_HEADERS)},
            "field_map": None, "read_post_allowlist": []}


def learn_addresses(site: str, profile_dir, location: dict | None = None, proxy_server: str | None = None,
                    approve=None, preapproved: Iterable[str] = (), llm: LLM | None = None,
                    start_url: str | None = None, runs_dir: Path | None = None) -> dict:
    """Learn and validate the site's `addresses` recipe on the admin's restored, logged-in profile; returns it
    unsaved. The browser stays read-only throughout: no POST is ever allowed (so approve is never asked, and
    preapproved, accepted for the learn interface, is unused) and media is not blocked. start_url: the admin's own
    orders page, whose account menu links to the addresses; default the home page."""
    if llm is None:
        raise RecipeError("learning an addresses recipe needs an LLM callable (llm.py)")
    del approve, preapproved
    with browser.open_site(site, allowlist=[], profile_dir=profile_dir, location=location,
                           proxy_server=proxy_server) as (_, page, guard, capture):
        start = start_url or home_url(site, location)
        goto(page, start)
        agent.settle(page)
        if agent.shows_login_button(agent.snapshot(page)["elements"]):
            raise browser.SessionExpired("the site shows a Login button")
        d = AddressDiscovery(site, page, guard, capture, home=start, runs_dir=runs_dir)
        result = d.run()
        if not result["success"] and "LOGGED_OUT" in result["note"]:
            raise browser.SessionExpired("not logged in")
        found = best_address_response(list(capture.items))
        if found is None:
            raise RecipeError(f"no saved-address list found on {site}")
        recipe = _recipe_from(site, found)
        recipe["field_map"] = address_field_map(site, found, llm)
    if err := check_addresses_recipe(recipe):
        raise RecipeError(f"addresses recipe rejected: {err}")
    log.info("%s addresses recipe learned (GET %s)", site, redact_url(recipe["endpoint"]))
    return recipe

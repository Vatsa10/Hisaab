"""Saved delivery addresses, read live from an app's own saved-address list.

Strictly read-only: one GET of the list the app already shows the user, replayed from inside the page (the
fast path) under the normal read-only guard. Addresses are never created, edited, selected or deleted.

The `addresses` recipe (endpoint + field_map) is learned by an admin only; a user's job just replays it. Nothing
here knows any site's endpoint. Address text and coordinates are never logged: logs carry counts and the site.
"""
import logging
import math
import re

import recipes
from browser import SITES, fetch_in_page, home_url, open_site
from sync import goto

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

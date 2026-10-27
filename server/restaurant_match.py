"""Pair one restaurant's Swiggy and Zomato listings (Plan 5 R13). Pure: no database, no network.

Pairs are only made when the names and the places agree. Same-name branches more than 3 km apart are never paired,
and a doubtful pair is shown as "Check this is the same restaurant" instead of being priced as one.
"""
import math
from dataclasses import asdict, dataclass

from server import match

BUSINESS_WORDS = frozenset({"restaurant", "restaurants", "pvt", "ltd"})
MAX_PAIR_KM = 3.0
MAX_RESULTS = 10
APP_NAMES = {"swiggy": "Swiggy", "zomato": "Zomato", "zepto": "Zepto", "blinkit": "Blinkit",
             "instamart": "Instamart"}
PRICEABLE = frozenset({"ok", "connected", "syncing", "priceable"})


@dataclass(frozen=True)
class Listing:
    site: str
    restaurant_id: str
    name: str
    locality: str | None
    lat: float | None
    lon: float | None
    distance_km: float | None
    is_open: bool | None
    url: str | None


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _text(v) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s or None


def listing_from_raw(site: str, d: dict) -> Listing | None:
    """A listing from a `food_search.extract_restaurants` row; None without an id or a name."""
    rid, name = _text(d.get("restaurant_id")), _text(d.get("name"))
    if rid is None or name is None:
        return None
    lat, lon = _num(d.get("lat")), _num(d.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat = lon = None
    is_open = d.get("is_open")
    return Listing(site, rid, name, _text(d.get("locality")), lat, lon, _num(d.get("distance_km")),
                   is_open if isinstance(is_open, bool) else None, _text(d.get("url")))


def name_tokens(name: str | None) -> frozenset[str]:
    return match.tokens(name) - BUSINESS_WORDS


def haversine_km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _has_coords(x: Listing) -> bool:
    return x.lat is not None and x.lon is not None


def score(a: Listing, b: Listing) -> float:
    """0.6 × name Jaccard + 0.4 × place agreement; listings more than 3 km apart score 0."""
    if _has_coords(a) and _has_coords(b):
        km = haversine_km(a.lat, a.lon, b.lat, b.lon)
        if km > MAX_PAIR_KM:
            return 0.0
        place = 1.0 if km <= 0.25 else 0.7 if km <= 1 else 0.3 if km <= 2 else 0.0
    else:
        la, lb = match.tokens(a.locality), match.tokens(b.locality)
        place = (0.7 if _jaccard(la, lb) >= 0.5 else 0.0) if la and lb else 0.5
    return 0.6 * _jaccard(name_tokens(a.name), name_tokens(b.name)) + 0.4 * place


def _app(site: str) -> str:
    return APP_NAMES.get(site, site.title())


def _distance(x: Listing, user_lat, user_lon) -> float | None:
    if x.distance_km is not None:
        return x.distance_km
    if _has_coords(x) and user_lat is not None and user_lon is not None:
        return round(haversine_km(user_lat, user_lon, x.lat, x.lon), 2)
    return None


def _single_note(site: str, sites: list[str], access: dict[str, str]) -> str:
    for other in sites:
        if other == site:
            continue
        state = access.get(other)
        if state == "needs_login":
            return f"Log in to {_app(other)} again to compare"
        if state not in PRICEABLE:
            return f"Connect {_app(other)} to compare"
    return f"Only on {_app(site)}"


def pair_results(by_site: dict[str, list[Listing]], user_lat, user_lon, access: dict[str, str]) -> list[dict]:
    """Pair listings across apps (greedy by score, each listing used once); nearest first, at most 10."""
    sites = list(by_site) + [s for s in access if s not in by_site]
    pool = [(site, x) for site in by_site for x in by_site[site]]
    edges = []
    for i, (sa, a) in enumerate(pool):
        for j in range(i + 1, len(pool)):
            sb, b = pool[j]
            if sa != sb and (s := score(a, b)) >= match.NEAR:
                edges.append((s, i, j))
    edges.sort(key=lambda e: (-e[0], e[1], e[2]))

    used: set[int] = set()
    groups: list[tuple[list[int], float | None]] = []
    for s, i, j in edges:
        if i not in used and j not in used:
            used.update((i, j))
            groups.append(([i, j], s))
    groups += [([i], None) for i in range(len(pool)) if i not in used]

    out = []
    for members, s in groups:
        listed = sorted((pool[i] for i in members), key=lambda t: sites.index(t[0]))
        first = listed[0][1]
        distances = [d for _, x in listed if (d := _distance(x, user_lat, user_lon)) is not None]
        if s is None:
            confidence, note = "single", _single_note(listed[0][0], sites, access)
        elif s >= match.SURE:
            confidence, note = "sure", None
        else:
            confidence, note = "ask", "Check this is the same restaurant"
        by = {site: x for site, x in listed}
        out.append({
            "key": "|".join(f"{site}:{x.restaurant_id}" for site, x in listed),
            "name": first.name,
            "locality": next((x.locality for _, x in listed if x.locality), None),
            "distance_km": min(distances) if distances else None,
            "listings": {site: asdict(by[site]) if site in by else None for site in sites},
            "confidence": confidence,
            "note": note,
        })
    out.sort(key=lambda r: (r["distance_km"] is None, r["distance_km"] or 0.0, r["name"].casefold(), r["key"]))
    return out[:MAX_RESULTS]

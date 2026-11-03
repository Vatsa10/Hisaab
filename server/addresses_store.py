"""Saved delivery addresses per user (table app_addresses): storage and the
per-app address a compare or cart fill uses. Nothing here logs an address or a coordinate."""
from psycopg.rows import dict_row

import addresses
import recipes
from addresses import db_site
from server import address_match, store

SITE_ORDER = ("zepto", "blinkit", "swiggy", "zomato")  # the Accounts screen's order
SAME_PLACE_M = 150.0  # the worker flags a refreshed address whose pin moved further than this
COLUMNS = ("address_id", "label", "line", "area", "city", "lat", "lon", "is_default")


def recipe(conn, site: str, user_id: str | None = None) -> dict | None:
    """The admin-learned `addresses` recipe for a site's account, or None (then nothing is fetched). A recipe the
    admin (or the error budget) paused is not used either. A templated one (Zomato's /users/<slug>) is bound to
    user_id's own account page; when that page is unknown there is nothing to fetch yet (None)."""
    row = conn.execute("SELECT recipe, paused FROM recipes WHERE site=%s AND kind='addresses'",
                       (db_site(site),)).fetchone()
    if not row or row[1]:
        return None
    if user_id is None:
        return row[0]
    page = conn.execute("SELECT orders_page_url FROM connections WHERE user_id=%s AND site=%s",
                        (user_id, db_site(site))).fetchone()
    try:
        return recipes.bind(row[0], page[0] if page else None)
    except recipes.TemplateError:
        return None


def replace(conn, user_id: str, site: str, rows: list[dict]) -> int:
    """Replace the user's saved addresses for one site (Instamart -> swiggy) in a single transaction: either the
    whole new list is stored or the old one stays. Returns how many rows were stored."""
    account = db_site(site)
    with conn.transaction():
        conn.execute("DELETE FROM app_addresses WHERE user_id=%s AND site=%s", (user_id, account))
        if rows:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO app_addresses (user_id, site, address_id, label, line, area, city, lat, lon, "
                    "is_default, fetched_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())",
                    [(user_id, account, r.get("address_id"), r.get("label"), r.get("line"), r.get("area"),
                      r.get("city"), r.get("lat"), r.get("lon"), bool(r.get("is_default"))) for r in rows])
    return len(rows)


def _rows(conn, user_id: str, site: str | None = None) -> list[dict]:
    q = "SELECT site, " + ", ".join(COLUMNS) + " FROM app_addresses WHERE user_id=%s"
    args: list = [user_id]
    if site:
        q += " AND site=%s"
        args.append(site)
    with conn.cursor(row_factory=dict_row) as cur:
        rows = cur.execute(q, args).fetchall()
    rank = {s: i for i, s in enumerate(SITE_ORDER)}
    return sorted(rows, key=lambda r: (rank.get(r["site"], len(rank)), not r["is_default"], r["address_id"]))


def _pin(r: dict) -> tuple[float, float] | None:
    return (r["lat"], r["lon"]) if r.get("lat") is not None and r.get("lon") is not None else None


def _chosen(conn, user_id: str) -> dict | None:
    row = conn.execute("SELECT chosen FROM profiles WHERE user_id=%s", (user_id,)).fetchone()
    return row[0] if row and isinstance(row[0], dict) else None


def get(conn, user_id: str, site: str, address_id: str) -> dict | None:
    """One of the user's saved addresses, or None."""
    return next((r for r in _rows(conn, user_id, db_site(site)) if r["address_id"] == address_id), None)


def saved_for(conn, user_id: str, site: str, near: tuple[float, float] | None = None) -> dict | None:
    """The app's own saved address to price (or fill) at for the user's chosen place, or None when this app has
    no saved address there (then the app is not priced: "Add this address in <App> to compare here").

    - The choice came from this app: that same address_id.
    - Otherwise: this app's best-scoring saved address against the chosen one (address_match.score: same threshold
      and vetoes as the picker), with the chosen pin replaced by near (default: the profile's saved lat/lon).
    Instamart uses the swiggy account's addresses."""
    account = db_site(site)
    rows = _rows(conn, user_id, account)
    if not rows:
        return None
    chosen = _chosen(conn, user_id)
    if chosen and chosen.get("site") == account:
        hit = next((r for r in rows if r["address_id"] == chosen.get("address_id")), None)
        if hit:
            return hit
    picked = get(conn, user_id, chosen.get("site") or "", chosen.get("address_id") or "") if chosen else None
    profile = store.get_profile(conn, user_id) or {}
    if near is None and profile.get("lat") is not None and profile.get("lon") is not None:
        near = (profile["lat"], profile["lon"])
    # A typed address (no chosen app address) is matched by its text and pin like any other pair.
    ref = dict(picked) if picked else {"line": profile.get("address"), "lat": None, "lon": None}
    if near is not None:
        ref["lat"], ref["lon"] = near
    if not ref.get("line") and near is not None:
        # a bare pin (no address text to score): the app's nearest saved pin within SAME_PLACE_M
        close = [(addresses.distance_m(near, pin), r) for r in rows if (pin := _pin(r))]
        close = [(d, r) for d, r in close if d <= SAME_PLACE_M]
        return min(close, key=lambda x: x[0])[1] if close else None
    scored = [(address_match.score(ref, r), r) for r in rows]
    scored = [(sc, r) for sc, r in scored if sc >= address_match.THRESHOLD]
    return max(scored, key=lambda x: x[0])[1] if scored else None

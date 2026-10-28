"""Saved delivery addresses per user (table app_addresses): storage and the
per-app address a compare or cart fill uses. Nothing here logs an address or a coordinate."""
from psycopg.rows import dict_row

from addresses import db_site, distance_m, norm_line
from server import store

SAME_PLACE_M = 150.0  # two saved addresses this close (or with the same line) are one place
SITE_ORDER = ("zepto", "blinkit", "swiggy", "zomato")  # the Accounts screen's order
COLUMNS = ("address_id", "label", "line", "area", "city", "lat", "lon", "is_default")


def recipe(conn, site: str) -> dict | None:
    """The admin-learned `addresses` recipe for a site's account, or None (then nothing is fetched)."""
    return store.get_recipe(conn, db_site(site), kind="addresses")


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
    - Otherwise: this app's saved address with the same line as the chosen one, else the nearest within
      SAME_PLACE_M of near (default: the profile's saved lat/lon).
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
    if chosen:
        picked = get(conn, user_id, chosen.get("site") or "", chosen.get("address_id") or "")
        line = norm_line(picked.get("line")) if picked else ""
        hit = next((r for r in rows if line and norm_line(r["line"]) == line), None)
        if hit:
            return hit
    if near is None:
        p = store.get_profile(conn, user_id) or {}
        near = (p["lat"], p["lon"]) if p.get("lat") is not None and p.get("lon") is not None else None
    if near is None:
        return None
    close = [(distance_m(near, pin), r) for r in rows if (pin := _pin(r))]
    close = [(d, r) for d, r in close if d <= SAME_PLACE_M]
    return min(close, key=lambda x: x[0])[1] if close else None

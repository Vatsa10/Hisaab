"""Fake delivery-app backends for local end-to-end runs (WORKER_FAKE=1): no browser, no network.

Nothing here logs or stores the phone, the OTP or any answer."""
import os
import random
import time
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import browser
import sync
from server import match

FAKE_OTP = "123456"
MONTHS = 6
ORDER_COUNT = 28
PAGE_SIZE = 8  # orders per fake "page", so progress arrives in a few batches like a real paginated fetch
CANCELLED_EVERY = 11
RESTAURANTS = {
    "zomato": ["Behrouz Biryani", "Faasos", "Burger Singh", "Domino's Pizza", "Chaayos"],
    "swiggy": ["Truffles", "Meghana Foods", "Subway", "Theobroma", "Leon Grill"],
}
DISHES = ["Veg Cheese Frankie", "Chicken Biryani", "Paneer Tikka Roll", "Margherita Pizza", "Cold Coffee",
          "Veg Grilled Sandwich", "Butter Naan", "Dal Makhani", "Brownie", "Masala Dosa"]
GROCERIES = ["Amul Taza Milk 500ml", "Farm Eggs 6 pcs", "Banana Robusta 1kg", "Onion 1kg", "Tomato 500g",
             "Britannia Brown Bread", "Amul Butter 100g", "Maggi Noodles 4 pack", "Coriander Bunch",
             "Aashirvaad Atta 5kg", "Curd 400g", "Potato 1kg"]


def login(site, phone, prompt, **_) -> bool:
    """Ask for the OTP (after a city question when the phone ends in 0000); only FAKE_OTP is accepted."""
    if str(phone or "").endswith("0000"):
        prompt("Which city do you order from?", "question")
    return prompt("Enter the OTP sent to your phone", "otp") == FAKE_OTP


def _post_entry(site: str, path: str, why: str, approve, preapproved) -> list[dict]:
    """The POST-allowlist entry for one fake endpoint: asked of approve() unless preapproved already holds it."""
    entry = {"url_regex": f"^https://fake\\.{site}\\.test{path}$", "why": why}
    if entry["url_regex"] not in set(preapproved) and not approve([entry]):
        raise sync.RecipeError("POST allowlist rejected")
    return [entry]


def _learn_search(site, approve, preapproved) -> dict:
    post = site == "instamart"
    allow = _post_entry(site, "/api/search", "fake product search", approve, preapproved) if post else []
    return {"home_url": f"https://fake.{site}.test/", "method": "POST" if post else "GET",
            "endpoint": f"https://fake.{site}.test/api/search", "query_param": "q", "location_inputs": [],
            "store": None, "read_post_allowlist": allow, "fee_map": None,
            "field_map": {"products_path": "products", "product_id": "product_id", "name": "name", "brand": "brand",
                          "pack_size": "pack_size", "price": "price", "mrp": "mrp", "in_stock": "in_stock"}}


def _learn_food(site, approve, preapproved) -> dict:
    post = site == "zomato"
    allow = _post_entry(site, "/api/search", "fake restaurant search", approve, preapproved) if post else []
    home = f"https://fake.{site}.test/"
    restaurants = {"home_url": home, "method": "POST" if post else "GET",
                   "endpoint": f"https://fake.{site}.test/api/search" if post
                   else f"https://fake.{site}.test/api/restaurants",
                   "query_param": "q", "location_inputs": [], "read_post_allowlist": allow, "fee_map": None,
                   "field_map": {"restaurants_path": "restaurants", "restaurant_id": "restaurant_id", "name": "name",
                                 "locality": "locality", "lat": "lat", "lon": "lon"}}
    menu = {"home_url": home, "method": "GET", "endpoint": f"https://fake.{site}.test/api/menu",
            "restaurant_param": "restaurant_id", "location_inputs": [], "read_post_allowlist": [], "fee_map": None,
            "field_map": {"dishes_path": "dishes", "dish_id": "dish_id", "name": "name", "price": "price",
                          "veg": "veg", "in_stock": "in_stock", "packaging": "packaging"}}
    return {"restaurants": restaurants, "menu": menu}


def learn(site, approve, preapproved=(), kind="orders", **_) -> dict:
    """A deterministic recipe for the fake site. kind "orders" (default): the orders recipe; "search": the grocery
    search recipe (GET; Instamart POSTs); "food": {"restaurants", "menu"} (Swiggy GET; Zomato's restaurant search
    POSTs). Each POST-allowlist entry goes to approve([entry]) unless preapproved already holds its regex; a
    refusal is sync.RecipeError("POST allowlist rejected")."""
    if kind == "search":
        return _learn_search(site, approve, preapproved)
    if kind == "food":
        return _learn_food(site, approve, preapproved)
    entry ={"url_regex": f"^https://fake\\.{site}\\.test/api/orders$", "why": "fake order list"}
    if entry["url_regex"] not in set(preapproved) and not approve([entry]):
        raise sync.RecipeError("POST allowlist rejected")
    return {"orders_page_url": f"https://fake.{site}.test/orders", "endpoint": f"https://fake.{site}.test/api/orders",
            "method": "POST", "fast_path_ok": True, "read_post_allowlist": [entry]}


def _order(rng, site, n, placed, grocery, instamart):
    if grocery:
        names = rng.sample(GROCERIES, rng.randint(2, 6))
        merchant = "Instamart" if instamart else site.capitalize()
        total = float(rng.randint(180, 1400))
    else:
        names = rng.sample(DISHES, rng.randint(1, 3))
        merchant = rng.choice(RESTAURANTS[site])
        total = float(rng.randint(150, 900))
    items = [{"name": nm, "qty": float(rng.randint(1, 3)), "price": None} for nm in names]  # unpriced, as live
    oid = f"FAKE-{site}-{n:04d}"
    return {"order_id": oid, "placed_at": placed.strftime("%Y-%m-%dT%H:%M:%S"), "total_amount": total,
            "currency": "INR", "status": "cancelled" if n % CANCELLED_EVERY == 0 else "delivered",
            "merchant": merchant, "sub_platform": "instamart" if instamart else None, "items": items,
            "raw": {"id": oid, "fake": True}}


def page_delay_s() -> float:
    """Pause between fake pages (FAKE_PAGE_DELAY_S, default 0), so a local run can watch the count climb."""
    try:
        return max(0.0, float(os.getenv("FAKE_PAGE_DELAY_S", "0") or 0))
    except ValueError:
        return 0.0


def fetch_orders(site, since=None, on_progress=None, **_) -> list[dict]:
    """Deterministic synthetic orders for the last 6 months (seeded by site). Swiggy returns food orders plus
    Instamart grocery rows (sub_platform='instamart'); the 'instamart' site returns only the latter.
    on_progress(count, amount) is called after each "page" of PAGE_SIZE orders, as the real fetch does."""
    out = _all_orders(site)
    if since:
        out = [o for o in out if o["placed_at"] >= str(since)[:19]]
    delay = page_delay_s()
    for end in range(PAGE_SIZE, len(out) + PAGE_SIZE, PAGE_SIZE):
        if delay and end > PAGE_SIZE:
            time.sleep(delay)
        sync.report_progress(on_progress, out[:end])
    return out


def _all_orders(site) -> list[dict]:
    rng = random.Random(f"fake-orders-{site}")
    now = datetime.now()
    span_days = MONTHS * 30
    out = []
    for n in range(1, ORDER_COUNT + 1):
        day = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=(n - 1) * span_days // ORDER_COUNT + rng.randint(0, 3))
        placed = day + timedelta(hours=rng.randint(9, 22), minutes=rng.randint(0, 59))
        if placed > now:
            placed -= timedelta(days=1)
        if site == "instamart":
            out.append(_order(rng, site, n, placed, True, True))
        elif site in ("zepto", "blinkit"):
            out.append(_order(rng, site, n, placed, True, False))
        else:
            out.append(_order(rng, site, n, placed, site == "swiggy" and n % 3 == 0, site == "swiggy" and n % 3 == 0))
    return out


# Saved delivery addresses (synthetic, Bengaluru). Home is the same place on every app (each app has its own id
# and a pin a few metres off, as real apps do); the second address differs per app.
_FAKE_HOME = {"label": "Home", "line": "Flat 12, Lakeview Residency, 27th Main Road", "area": "HSR Layout",
              "city": "Bengaluru", "lat": 12.9116, "lon": 77.6474}
_FAKE_WORK = {"label": "Work", "line": "Tower B, 4th Floor, Prestige Tech Park", "area": "Koramangala",
              "city": "Bengaluru", "lat": 12.9352, "lon": 77.6245}
_FAKE_SECOND = {
    "zepto": _FAKE_WORK,
    "blinkit": _FAKE_WORK | {"lat": 12.9353},
    "swiggy": _FAKE_WORK | {"lat": 12.9351},
    "zomato": {"label": "Other", "line": "House 4, 2nd Cross, Defence Colony", "area": "Indiranagar",
               "city": "Bengaluru", "lat": 12.9719, "lon": 77.6412},
}
_FAKE_NUDGE = {"zepto": 0.0, "blinkit": 0.0002, "swiggy": -0.0002, "zomato": 0.0003}  # degrees lat, under 40 m


def saved_addresses(site) -> list[dict]:
    """Two saved addresses per app, already in addresses.normalise() form (Instamart: Swiggy's)."""
    account = "swiggy" if site == "instamart" else site
    home = _FAKE_HOME | {"address_id": f"fake-{account}-home", "is_default": True,
                         "lat": round(_FAKE_HOME["lat"] + _FAKE_NUDGE.get(account, 0.0), 6)}
    second = _FAKE_SECOND.get(account, _FAKE_WORK) | {"address_id": f"fake-{account}-2", "is_default": False}
    return [home, second]


# --- compare fakes (Plan 5): grocery search, restaurant search and menus ------------------------------------------

def logged_out_sites() -> frozenset[str]:
    """Sites whose fake session has "expired" (env FAKE_LOGGED_OUT_SITES, comma-separated; local runs only)."""
    return frozenset(x.strip() for x in (os.getenv("FAKE_LOGGED_OUT_SITES") or "").split(",") if x.strip())


def __getattr__(name):  # fake_sites.LOGGED_OUT_SITES always reflects the environment as it is now
    if name == "LOGGED_OUT_SITES":
        return logged_out_sites()
    raise AttributeError(name)


def _check_session(site: str) -> None:
    if site in logged_out_sites():
        raise browser.SessionExpired("fake session expired")


# (product name as an app lists it, brand, base price)
FAKE_CATALOG = [
    ("Amul Taza Milk 500ml", "Amul", 29), ("Farm Eggs 6 pcs", None, 54), ("Banana Robusta 1kg", None, 60),
    ("Onion 1kg", None, 40), ("Tomato 500g", None, 30), ("Britannia Brown Bread (400 g)", "Britannia", 50),
    ("Amul Butter 100g", "Amul", 58), ("Maggi Noodles 4 pack", "Maggi", 56), ("Coriander Bunch", None, 10),
    ("Aashirvaad Atta 5kg", "Aashirvaad", 260), ("Curd 400g", None, 35), ("Potato 1kg", None, 35),
    ("Amul Taaza Toned Milk 1 L", "Amul", 60), ("Milk Bikis Biscuits 200 g", "Britannia", 30),
    ("Lay's Classic Salted Chips 52 g", "Lay's", 20), ("Lay's Magic Masala Chips 52 g", "Lay's", 20),
]
SITE_PRICE_FACTOR = {"zepto": Decimal("0.92"), "blinkit": Decimal("1.00"), "instamart": Decimal("1.06")}
OUT_OF_STOCK = {"instamart": {"Lay's Classic Salted Chips 52 g"}}
NOT_STOCKED = {"blinkit": {"Coriander Bunch"}}


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


def _catalog(site: str) -> list[dict]:
    factor = SITE_PRICE_FACTOR[site]
    rows = []
    for name, brand, base in FAKE_CATALOG:
        if name in NOT_STOCKED.get(site, ()):
            continue
        price = int((Decimal(base) * factor).quantize(Decimal(1), ROUND_HALF_UP))
        rows.append({"product_id": f"{site}-{_slug(name)}", "name": name, "brand": brand, "pack_size": None,
                     "price": price, "mrp": round(price * 1.1, 2), "in_stock": name not in OUT_OF_STOCK.get(site, ())})
    return rows


def search(site, queries, **_) -> dict:
    """{"products": {query: [rows]}, "fee_info": {}}: the catalog entries that share a word with the query."""
    _check_session(site)
    rows = _catalog(site)
    return {"products": {q: [r for r in rows if match.tokens(q) & match.tokens(r["name"])] for q in queries},
            "errors": {}, "fee_info": {}}


# (id slug, name, locality, lat, lon, is_open)
_FAKE_FOOD = {
    "swiggy": [("meghana", "Meghana Foods", "Gorwa", 22.3310, 73.1510, True),
               ("truffles", "Truffles", "Gorwa", 22.3290, 73.1490, True),
               ("behrouz", "Behrouz Biryani", "Gorwa", 22.3320, 73.1480, True),
               ("burger-singh", "Burger Singh", "Gorwa", 22.3305, 73.1505, True),
               ("theobroma", "Theobroma", "Gorwa", 22.3295, 73.1495, True)],
    "zomato": [("meghana", "Meghana Foods", "Gorwa", 22.3310, 73.1510, True),
               ("truffles-cafe", "Truffles Cafe", "Gorwa", 22.3290, 73.1490, True),
               ("behrouz", "Behrouz Biryani", "Gorwa", 22.3320, 73.1480, True),
               ("behrouz-alkapuri", "Behrouz Biryani", "Alkapuri", 22.3100, 73.1800, True),
               ("burger-singh", "Burger Singh", "Gorwa", 22.3305, 73.1505, False)],
}


def _restaurant_row(site: str, r: tuple) -> dict:
    slug, name, locality, lat, lon, is_open = r
    rid = f"{site}-{slug}"
    return {"restaurant_id": rid, "name": name, "locality": locality, "lat": lat, "lon": lon, "distance_km": None,
            "is_open": is_open, "url": f"https://fake.{site}.test/restaurant/{rid}"}


def search_restaurants(site, query, **_) -> dict:
    """{"restaurants": [rows], "fee_info": {}}: the listings sharing any name word with the query."""
    _check_session(site)
    words = match.tokens(query)
    return {"restaurants": [_restaurant_row(site, r) for r in _FAKE_FOOD[site] if words & match.tokens(r[1])],
            "fee_info": {}}


# (name, price, packaging, veg); Zomato's listing carries no packaging field
_MEGHANA = {
    "swiggy": [("Chicken Biryani (Half)", 220, 15, False), ("Chicken Biryani (Full)", 380, 20, False),
               ("Paneer Biryani", 260, 10, True), ("Veg Biryani", 200, 10, True), ("Chicken 65", 240, 10, False)],
    "zomato": [("Chicken Biryani - Half", 235, None, False), ("Chicken Biryani - Full", 395, None, False),
               ("Paneer Biryani", 250, None, True), ("Veg Biryani", 205, None, True),
               ("Chicken 65", 245, None, False)],
}


def fetch_menu(site, restaurant_id, **_) -> dict:
    """{"is_open", "dishes", "fee_info"} for a fake restaurant id."""
    _check_session(site)
    listing = next((r for r in _FAKE_FOOD[site] if f"{site}-{r[0]}" == str(restaurant_id)), None)
    if listing is None:
        return {"is_open": False, "dishes": [], "fee_info": {}}
    if listing[1] == "Meghana Foods":
        rows = _MEGHANA[site]
    else:
        rng = random.Random(f"fake-menu-{site}-{listing[0]}")
        rows = [(f"{listing[1]} {d}", rng.randint(120, 380), None, bool(rng.getrandbits(1)))
                for d in rng.sample(DISHES, 3)]
    dishes = [{"dish_id": f"{restaurant_id}-d{n}", "name": name, "variant": None, "price": float(price),
               "veg": veg, "in_stock": True, "packaging": float(pack) if pack is not None else None}
              for n, (name, price, pack, veg) in enumerate(rows, 1)]
    return {"is_open": listing[5], "dishes": dishes, "fee_info": {}}

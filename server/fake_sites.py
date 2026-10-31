"""Fake delivery-app backends for local end-to-end runs (WORKER_FAKE=1): no browser, no network.

Nothing here logs or stores the phone, the OTP or any answer."""
import json
import os
import random
import time
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import browser
import cart_fill
import sync
from server import fees, match

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
    if kind == "cart":
        return _learn_cart(site, approve, preapproved)
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


def fetch_orders(site, since=None, on_progress=None, orders_page_url=None, **_) -> list[dict]:
    """Deterministic synthetic orders for the last 6 months (seeded by site). Swiggy returns food orders plus
    Instamart grocery rows (sub_platform='instamart'); the 'instamart' site returns only the latter.
    on_progress(count, amount) is called after each "page" of PAGE_SIZE orders, as the real fetch does."""
    if str(orders_page_url or "").endswith("/expired"):  # lets the e2e suite exercise the logged-out path
        raise browser.SessionExpired("fake")
    if str(orders_page_url or "").endswith("/throttle"):  # ... and the rate-limited one
        raise browser.SiteThrottled("rate_limited", 60)
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


# --- cart fill fakes (Plan 6): learn a cart recipe, fill a per-user fake cart -------------------------------------

FILL_OUT_OF_STOCK = {"zepto": {"Farm Eggs 6 pcs"}, "blinkit": set(), "instamart": set()}
FAKE_MAX_QTY = 5
REPRICE_FACTOR = Decimal("1.03")
FAKE_FEES = {  # the Plan 5 grocery seeds (supabase/migrations/0008)
    "zepto": {"delivery": [{"min_subtotal": 0, "fee": 30}, {"min_subtotal": 99, "fee": 0}], "handling": 15,
              "small_cart": {"below": 99, "fee": 25}, "surge": {"possible": True}},
    "blinkit": {"delivery": [{"min_subtotal": 0, "fee": 25}, {"min_subtotal": 199, "fee": 0}], "handling": 9,
                "small_cart": {"below": 99, "fee": 20}, "surge": {"possible": True}},
    "instamart": {"delivery": [{"min_subtotal": 0, "fee": 35}, {"min_subtotal": 199, "fee": 0}], "handling": 10,
                  "small_cart": {"below": 99, "fee": 15}, "surge": {"possible": True}},
}
FILL_PROBE_ITEM = "Amul Taaza Toned Milk 1 L"


def _pack_price(site: str, base: int, reprice: bool) -> int:
    v = Decimal(base) * SITE_PRICE_FACTOR[site] * (REPRICE_FACTOR if reprice else 1)
    return int(v.quantize(Decimal(1), ROUND_HALF_UP))


def _fill_catalog(site: str, reprice: bool) -> dict[str, tuple[str, int]]:
    return {f"{site}-{_slug(n)}": (n, _pack_price(site, base, reprice)) for n, _b, base in FAKE_CATALOG}


def _learn_cart(site, approve, preapproved) -> dict:
    """A valid fake cart recipe. The add and the location select go to approve() unless preapproved holds their
    regexes; a refusal is sync.RecipeError("cart permission rejected")."""
    host = f"https://fake.{site}.test"
    add = {"url_regex": f"^https://fake\\.{site}\\.test/api/cart/add$", "why": "add to cart", "op": "cart",
           "qty_path": "qty"}
    loc = {"url_regex": f"^https://fake\\.{site}\\.test/api/location/select$",
           "why": "pick a saved or current location", "op": "location"}
    known = {e if isinstance(e, str) else (e or {}).get("url_regex") for e in preapproved}
    ask = [e for e in (add, loc) if e["url_regex"] not in known]
    if ask and not approve([dict(e) for e in ask]):
        raise sync.RecipeError("cart permission rejected")
    recipe = {
        "kind": "cart", "site": site, "home_url": f"{host}/",
        "add": {"method": "POST", "url": f"{host}/api/cart/add", "headers": {},
                "body": {"product_id": "seed", "qty": 1}, "product_id_path": "product_id", "qty_path": "qty",
                "store_id_path": None, "qty_mode": "set"},
        "read": {"method": "GET", "url": f"{host}/api/cart", "headers": {}, "body": None,
                 "field_map": {"lines_path": "lines", "product_id": "product_id", "name": "name", "qty": "qty",
                               "price": "price", "subtotal": "subtotal", "total": "total",
                               "fees": {"handling": "fees.handling", "delivery": "fees.delivery"},
                               "out_of_stock": None}},
        "location": {"current": {"method": "GET", "url": f"{host}/api/location", "headers": {}, "body": None,
                                 "store_id_path": None, "lat_path": "lat", "lon_path": "lon", "area_path": "area"},
                     "select": {"mode": "latlon", "method": "POST", "url": f"{host}/api/location/select",
                                "headers": {}, "body": {"lat": 0, "lon": 0}, "lat_input": "lat",
                                "lon_input": "lon"}},
        "cart_allowlist": [add], "location_allowlist": [loc], "read_post_allowlist": [],
    }
    return {"recipe": recipe, "probe_item": FILL_PROBE_ITEM}


def _state_path(state_dir, user_id, site) -> Path:
    return Path(state_dir) / f"{user_id}-{site}.json"


def _load_state(path: Path, site: str, priced: dict) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"cart": {f"{site}-amul-butter-100g": {"name": "Amul Butter 100g", "qty": 1}},
            "location": {"lat": priced.get("lat"), "lon": priced.get("lon"), "area": priced.get("area")},
            "locked": False}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _cart_view(site: str, state: dict, prices: dict) -> dict:
    subtotal = sum(prices.get(pid, (None, 0))[1] * line["qty"] for pid, line in state["cart"].items())
    fee = fees.compute(FAKE_FEES[site], subtotal, True, {"member": False})
    return {"subtotal": subtotal, "total": subtotal + fee["total"],
            "fees": {k: fee[k] for k in ("delivery", "handling", "small_cart", "surge") if fee.get(k)},
            "line_count": len(state["cart"])}


def fill_cart(site, plan, priced, recipe, search_recipe, *, ask, on_item, on_stage, user_id, state_dir,
              confirm_location=None, max_location_m=500, **_) -> dict:
    """The fake add-only fill on a per-user cart kept in state_dir/<user_id>-<site>.json; the same outcome as
    cart_fill.fill_cart."""
    if not state_dir:
        raise RuntimeError("FAKE_STATE_DIR is not set")
    path = _state_path(state_dir, user_id, site)
    target = confirm_location or priced
    state = _load_state(path, site, priced)
    prices = _fill_catalog(site, confirm_location is not None)
    before = {pid: line["qty"] for pid, line in state["cart"].items()}

    on_stage("checking_location")
    loc = state["location"]
    app = {"lat": loc["lat"], "lon": loc["lon"], "store_id": None, "area": loc.get("area")}
    matched = cart_fill.location_matches(app, target, None, max_location_m)
    if not matched and confirm_location is not None:
        return cart_fill.outcome("failed", [], None, app_location=app, error="location_changed")
    selected = False
    if not matched and not state["locked"]:
        on_stage("selecting_location")
        state["location"] = {"lat": target["lat"], "lon": target["lon"], "area": target.get("area")}
        app = {"lat": target["lat"], "lon": target["lon"], "store_id": None, "area": target.get("area")}
        matched = selected = True
        _save_state(path, state)
    if not matched:
        on_stage("repricing")
        again = _fill_catalog(site, True)
        out = []
        for item in (i for i in plan if i.get("action") == "add"):
            name, price = again.get(item.get("product_id"), (None, None))
            packs = item.get("packs") or 1
            old = item.get("price")
            out.append({"idx": item.get("idx"), "name": item.get("name"), "product_id": item.get("product_id"),
                        "old_price": round(packs * old, 2) if old is not None else None,
                        "new_price": packs * price if price is not None else None,
                        "in_stock": name not in FILL_OUT_OF_STOCK[site] if name else False})
        return cart_fill.outcome("needs_confirmation", [], None, app_location=app, repriced=out)

    on_stage("adding")
    results, attempted = [], []
    for item in plan:
        res = cart_fill._base(item)
        action = item.get("action")
        if action == "skip":
            res["status"] = "skipped"
            res["skip_reason"] = item.get("reason") if item.get("reason") in cart_fill.SKIP_REASONS else None
        elif action == "ask":
            answer = ask(item.get("question"), item.get("choices") or [])
            choice = None if answer in (None, "skip") else next(
                (c for c in item.get("choices") or [] if c.get("id") is not None and str(c["id"]) == str(answer)),
                None)
            if choice is None:
                res["status"] = "skipped"
                res["skip_reason"] = "no_answer" if answer is None else "you_skipped"
            else:
                res.update(product_id=choice.get("product_id"), name=choice.get("name"), packs=choice.get("packs"))
                action = "add"
        elif action != "add":
            res["status"] = "not_added"
        if res["status"] is None:
            pid, packs = str(res["product_id"]), res["packs"]
            name = (prices.get(pid) or (res["name"], 0))[0]
            res["product_id"] = pid
            if name in FILL_OUT_OF_STOCK[site]:
                res["status"] = "out_of_stock"
            else:
                line = state["cart"].setdefault(pid, {"name": name, "qty": 0})
                line["qty"] = min(FAKE_MAX_QTY, line["qty"] + packs)
            attempted.append(len(results))
        results.append(res)
        on_item(dict(res))
    _save_state(path, state)

    after = {pid: line["qty"] for pid, line in state["cart"].items()}
    checked, existing_changed = cart_fill.verify(before, after, [results[i] for i in attempted])
    for pos, item in zip(attempted, checked):
        results[pos] = item
    cart = _cart_view(site, state, prices)
    cart["existing_count"] = sum(1 for pid in state["cart"] if before.get(pid, 0) > 0)
    counts = {s: sum(1 for i in results if i["status"] == s) for s in ("added", "partial")}
    tried = [i for i in results if not (i["status"] == "skipped" and i["skip_reason"] in cart_fill.USER_SKIPS)]
    if not counts["added"] and not counts["partial"]:
        status, error = "failed", "nothing_added"
    elif all(i["status"] == "added" for i in tried):
        status, error = "done", None
    else:
        status, error = "partial", None
    return cart_fill.outcome(status, results, cart, app_location=app, location_selected=selected, error=error,
                             existing_changed=existing_changed)

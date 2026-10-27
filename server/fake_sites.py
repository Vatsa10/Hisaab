"""Fake delivery-app backends for local end-to-end runs (WORKER_FAKE=1): no browser, no network.

Nothing here logs or stores the phone, the OTP or any answer."""
import os
import random
import time
from datetime import datetime, timedelta

import sync

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

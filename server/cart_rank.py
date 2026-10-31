"""Ranked cart summaries (Plan 5 R5, R7): every number and every user-facing string the PWA shows for a cart check.

`build_summary` turns a request and its per-site results into the one summary shape the API, the worker and the
PWA all use. The PWA renders these strings and does no price math. Pure: no database, no I/O.

Result items are `match.match_item` / `match.match_dish` dicts:
`{idx, status, confidence, product, packs, line_total, swap_from, candidates}`.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from itertools import combinations

from server import fees

SITE_ORDER = ("zepto", "blinkit", "instamart", "swiggy", "zomato")
APP_NAMES = {"zepto": "Zepto", "blinkit": "Blinkit", "instamart": "Instamart", "swiggy": "Swiggy", "zomato": "Zomato"}
NON_OK_ORDER = ("closed", "not_listed", "needs_login", "not_connected", "unserviceable", "failed", "unavailable")
PRICED = ("matched", "swapped", "chosen")
UNRESOLVED = ("out_of_stock", "not_found", "pick")

PRICE_NOTE = "Your prices, with your memberships and your store's stock."
OFFERS_NOTE = "Offers in the app may lower this."
NO_TOTAL = "—"
ACCOUNTS = "/accounts"

# Used only when a site has no fee_rules row: no fees, always "estimated" (unverified).
_FALLBACK_RULES = {
    "grocery": {"delivery": [{"min_subtotal": 0, "fee": 0}], "handling": 0},
    "food": {"kind": "food", "delivery_by_km": [{"max_km": None, "fee": 0}], "packaging_per_item": 0, "platform": 0,
             "gst": {"food_pct": 5, "fees_pct": 18}},
}


# --- labels ------------------------------------------------------------------------------------------------------

def inr(x: float) -> str:
    """Rupees with Indian digit grouping: 15 → "₹15", 123456 → "₹1,23,456", 49.5 → "₹49.50"."""
    paise_total = int(round(abs(float(x)) * 100))
    sign = "-" if x < 0 and paise_total else ""
    rupees, paise = divmod(paise_total, 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{sign}₹{digits}" + (f".{paise:02d}" if paise else "")


_LATLON = re.compile(r"^\s*-?\d{1,3}(?:\.\d+)?\s*,\s*-?\d{1,3}(?:\.\d+)?\s*$")
_PINCODE = re.compile(r"\b\d{3}\s?\d{3}\b")


def area_label(address: str | None) -> str:
    """A short, non-identifying area for the header: "Gorwa, Vadodara". Never the street or the pincode."""
    if not isinstance(address, str) or not address.strip() or _LATLON.match(address):
        return "your address"
    parts = [" ".join(_PINCODE.sub(" ", p).split()).strip(" -") for p in address.split(",")]
    parts = [p for p in parts if p]
    while parts and parts[-1].lower() == "india":
        parts.pop()
    if not parts:
        return "your address"
    if len(parts) >= 3:
        return f"{parts[-3]}, {parts[-2]}"
    return parts[0]


def _app(site: str) -> str:
    return APP_NAMES.get(site, site.capitalize())


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _money(x: float) -> float:
    return round(float(x) + 0.0, 2)


def _site_rank(site: str) -> int:
    return SITE_ORDER.index(site) if site in SITE_ORDER else len(SITE_ORDER)


def _qty(item: dict) -> int:
    q = item.get("qty")
    return q if isinstance(q, int) and not isinstance(q, bool) and q > 0 else 1


def _row_label(item: dict) -> str:
    q = _qty(item)
    return f"{item.get('text', '')}{f' ×{q}' if q > 1 else ''}"


# --- cells -------------------------------------------------------------------------------------------------------

def _cand_id(c: dict):
    return c.get("product_id", c.get("dish_id"))


def _cand_total(c: dict) -> float | None:
    if _is_num(c.get("line_total")):
        return float(c["line_total"])
    if _is_num(c.get("price")):
        return float(c["price"]) * (c.get("packs") or 1)
    return None


def _usable(c) -> bool:
    return isinstance(c, dict) and c.get("in_stock", True) is not False and _cand_total(c) is not None


def _cell(match: dict | None, qty: int) -> dict:
    if not isinstance(match, dict):
        return {"status": "not_found", "name": None, "line_total": None, "packs": None, "swap_from": None,
                "candidates": []}
    status = match.get("status")
    line_total = match.get("line_total")
    if status in PRICED and not _is_num(line_total):
        status = "not_found"                            # a price we can't read is never shown as priced
    if status not in PRICED and status not in UNRESOLVED:
        status = "not_found"
    product = match.get("product") if isinstance(match.get("product"), dict) else {}
    priced = status in PRICED
    return {"status": status, "name": product.get("name") if priced else None,
            "line_total": float(line_total) if priced else None,
            "packs": (match.get("packs") or qty) if priced else None,
            "swap_from": match.get("swap_from") if status == "swapped" else None,
            "candidates": [c for c in (match.get("candidates") or []) if isinstance(c, dict)]}


def _cells(request: dict, site: str, matches: list) -> dict[int, dict]:
    """One cell per request item, with the user's choices applied."""
    by_idx = {m["idx"]: m for m in matches or () if isinstance(m, dict) and isinstance(m.get("idx"), int)}
    choices = request.get("choices") or {}
    out = {}
    for idx, item in enumerate(request.get("items") or []):
        cell = _cell(by_idx.get(idx), _qty(item))
        picked = choices.get(str(idx)) or choices.get(idx) or {}
        chosen_id = picked.get(site) if isinstance(picked, dict) else None
        if chosen_id is not None:
            cand = next((c for c in cell["candidates"] if _usable(c) and str(_cand_id(c)) == str(chosen_id)), None)
            if cand is not None:
                cell = {"status": "chosen", "name": cand.get("name"), "line_total": _cand_total(cand),
                        "packs": cand.get("packs") or _qty(item), "swap_from": None, "candidates": cell["candidates"]}
        out[idx] = cell
    return out


# --- cards -------------------------------------------------------------------------------------------------------

def _note_and_action(site: str, status: str, declined: list, error: str | None = None) -> tuple[str, dict | None]:
    app = _app(site)
    if status == "unserviceable" and error == "address_changed":
        return f"Your saved address in {app} changed. Pick it again.", None
    if status == "unserviceable":
        return f"{app} doesn't deliver to this address.", None
    if status == "unavailable":
        return f"{app} prices are unavailable right now.", None
    if status == "not_connected":
        note = "Connect Swiggy to compare Instamart." if site == "instamart" else f"Connect {app} to compare."
        return note, {"label": "Connect", "href": ACCOUNTS}
    if status == "needs_login":
        return f"{app} logged you out. Log in again to compare.", {"label": "Log in again", "href": ACCOUNTS}
    if status == "closed":
        return f"Closed on {app} right now.", None
    if status == "not_listed":
        if site in declined:
            return f"Couldn't confirm this restaurant on {app}.", None
        return f"Not on {app}.", None
    return f"Couldn't price {app} right now.", None


def _empty_card(site: str, status: str) -> dict:
    return {"site": site, "name": _app(site), "rank": 0, "status": status, "cheapest": False, "subtotal": None,
            "fees": None, "fees_estimated": False, "total": None, "total_label": NO_TOTAL, "delta_label": None,
            "fee_line": None, "member_note": None, "fee_hint": None, "stock_line": None, "swaps": [], "to_pick": 0,
            "note": None, "action": None}


def _fee_fn(mode: str, rules: dict, verified: bool, meta: dict):
    observed = meta.get("fee_info") if isinstance(meta.get("fee_info"), dict) else None
    if mode == "food":
        distance = meta.get("distance_km")

        def food(subtotal: float, units: int) -> dict:
            return fees.compute_food(rules, subtotal, units, distance, verified, observed)
        return food

    def grocery(subtotal: float, units: int) -> dict:
        return fees.compute(rules, subtotal, verified, observed)
    return grocery


def _fee_line(mode: str, subtotal: float, f: dict) -> str:
    if mode == "food":
        parts = [f"Items {inr(subtotal)}", f"delivery {inr(f['delivery'])}", f"packaging {inr(f['packaging'])}",
                 f"platform {inr(f['platform'])}", f"GST {inr(f['gst'])}"]
        extra = (("small-order", "small_order"), ("surge", "surge"))
    else:
        parts = [f"Items {inr(subtotal)}", f"delivery {inr(f['delivery'])}", f"handling {inr(f['handling'])}"]
        extra = (("small-cart", "small_cart"), ("surge", "surge"))
    parts += [f"{label} {inr(f[key])}" for label, key in extra if f[key] > 0]
    return " + ".join(parts)


def _stock_line(mode: str, n: int, cells: dict[int, dict]) -> str:
    count = {s: sum(1 for c in cells.values() if c["status"] == s) for s in UNRESOLVED}
    if mode == "food":
        missing = count["out_of_stock"] + count["not_found"]
        if not missing and not count["pick"]:
            return f"all {n} available"
        parts = [f"{missing} not available"] if missing else []
    else:
        if not any(count.values()):
            return f"all {n} in stock"
        parts = [f"{count['out_of_stock']} out of stock"] if count["out_of_stock"] else []
        if count["not_found"]:
            parts.append(f"{count['not_found']} not found")
    if count["pick"]:
        parts.append(f"{count['pick']} to pick")
    return " · ".join(parts)


def _swap_from_name(swap_from, fallback: str) -> str:
    if isinstance(swap_from, dict) and swap_from.get("name"):
        return str(swap_from["name"])
    if isinstance(swap_from, str) and swap_from.strip():
        return swap_from
    return fallback


def _free_delivery_hint(rules: dict, subtotal: float, delivery: float) -> str | None:
    if delivery <= 0:
        return None
    free = [t["min_subtotal"] for t in rules.get("delivery") or [] if t.get("fee") == 0 and t["min_subtotal"] > subtotal]
    return f"Free delivery on {inr(min(free))} or more" if free else None


class _Entry:
    """A card plus what ranking and the split need."""

    def __init__(self, card: dict, cells: dict[int, dict] | None = None, fee_fn=None, fee_total: float = 0.0):
        self.card = card
        self.cells = cells or {}
        self.fee_fn = fee_fn
        self.fee_total = fee_total

    @property
    def ok(self) -> bool:
        return self.card["status"] == "ok"

    @property
    def priced(self) -> list[int]:
        return [i for i, c in self.cells.items() if c["status"] in PRICED]

    @property
    def unresolved(self) -> int:
        return len(self.cells) - len(self.priced)


def _ok_entry(request: dict, result: dict, mode: str, fee_rules: dict) -> _Entry:
    site = result["site"]
    items = request.get("items") or []
    cells = _cells(request, site, result.get("items") or [])
    card = _empty_card(site, "ok")
    meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
    rules, verified = fee_rules.get(site) or (_FALLBACK_RULES[mode], False)
    fee_fn = _fee_fn(mode, rules, bool(verified), meta)
    entry = _Entry(card, cells, fee_fn)

    priced = [cells[i] for i in entry.priced]
    card["stock_line"] = _stock_line(mode, len(items), cells)
    card["to_pick"] = sum(1 for c in cells.values() if c["status"] == "pick")
    card["swaps"] = [f"{_swap_from_name(c['swap_from'], items[i].get('text', ''))} swapped for {c['name']}"
                     for i, c in cells.items() if c["status"] == "swapped"]
    subtotal = _money(sum(c["line_total"] for c in priced))
    card["subtotal"] = subtotal
    if not priced:
        return entry
    units = sum(int(c["packs"] or 1) for c in priced)
    f = fee_fn(subtotal, units)
    components = fees.FOOD_COMPONENTS + ("gst",) if mode == "food" else fees.GROCERY_COMPONENTS
    card["fees"] = {**{k: f[k] for k in components}, "total": f["total"]}
    card["fees_estimated"] = bool(f["estimated"])
    card["total"] = _money(subtotal + f["total"])
    card["total_label"] = inr(card["total"])
    card["fee_line"] = _fee_line(mode, subtotal, f)
    card["member_note"] = f"{f['member']} applied" if f["member"] else None
    if mode == "grocery":
        card["fee_hint"] = _free_delivery_hint(rules, subtotal, f["delivery"])
    entry.fee_total = f["total"]
    return entry


# --- split -------------------------------------------------------------------------------------------------------

def _split(best: _Entry, ok: list[_Entry], split_min_saving: float) -> dict:
    """Best two-app split covering the best app's priced items, each item at the cheaper of the two."""
    wanted = best.priced
    found = None
    for a, b in combinations([e for e in ok if e.priced], 2):
        sides: dict[int, list[float]] = {0: [], 1: []}
        covered = True
        for idx in wanted:
            la, lb = (e.cells[idx]["line_total"] if e.cells[idx]["status"] in PRICED else None for e in (a, b))
            if la is None and lb is None:
                covered = False
                break
            if lb is None or (la is not None and la <= lb):     # ties stay on the better-ranked app
                sides[0].append(la)
            else:
                sides[1].append(lb)
        if not covered or not sides[0] or not sides[1]:
            continue
        sub_a, sub_b = _money(sum(sides[0])), _money(sum(sides[1]))
        item_saving = _money(best.card["subtotal"] - sub_a - sub_b)
        if item_saving <= 0:
            continue
        split_fees = a.fee_fn(sub_a, len(sides[0]))["total"] + b.fee_fn(sub_b, len(sides[1]))["total"]
        saving = _money(best.card["total"] - (sub_a + sub_b + split_fees))
        extra_fees = _money(split_fees - best.fee_total)
        if found is None or saving > found[0]:
            found = (saving, item_saving, extra_fees, a.card["site"], b.card["site"])
    if found is None:
        return {"worth_it": False, "sites": [], "saving": 0,
                "line": f"No item is cheaper on another app, so one order on {_app(best.card['site'])} is best."}
    saving, item_saving, extra_fees, site_a, site_b = found
    if saving >= split_min_saving:
        line = f"Splitting between {_app(site_a)} and {_app(site_b)} saves {inr(saving)} even after the second order's fees."
    elif extra_fees > 0:
        line = f"Splitting across apps saves {inr(item_saving)} on items but adds {inr(extra_fees)} in fees. Not worth it."
    else:
        line = f"Splitting across apps saves only {inr(saving)}. Not worth a second order."
    return {"worth_it": saving >= split_min_saving, "sites": [site_a, site_b], "saving": saving, "line": line}


# --- summary -----------------------------------------------------------------------------------------------------

def _as_datetime(v) -> datetime | None:
    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, str) and v:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _oldest(values: list) -> str | None:
    dated = [(dt, v) for v in values if (dt := _as_datetime(v)) is not None]
    if not dated:
        return None
    v = min(dated, key=lambda p: p[0])[1]
    return v.isoformat() if isinstance(v, datetime) else v


def _priced_at(mode: str, results: list[dict]) -> str | None:
    ok = [r for r in results if r.get("status") == "ok"]
    if mode == "food":
        used = [((r.get("meta") or {}).get("menu_fetched_at") or r.get("priced_at")) for r in ok]
    else:
        used = [r.get("priced_at") for r in ok]
    return _oldest(used) or _oldest([r.get("priced_at") for r in results])


def _price_label(mode: str, cell: dict) -> str:
    if cell["status"] in PRICED:
        return inr(cell["line_total"])
    if cell["status"] == "pick":
        return "Pick"
    if cell["status"] == "out_of_stock":
        return "Not available" if mode == "food" else "Out of stock"
    return "n/a"


def build_summary(request: dict, results: list[dict], fee_rules: dict[str, tuple[dict, bool]],
                  split_min_saving: float) -> dict:
    """The summary the API stores in `cart_requests.summary`, the worker writes and the PWA renders."""
    mode = request.get("mode") or "grocery"
    items = request.get("items") or []
    restaurant = request.get("restaurant") if isinstance(request.get("restaurant"), dict) else None
    declined = list((restaurant or {}).get("declined") or [])

    ok_entries: list[_Entry] = []
    other: list[_Entry] = []
    for r in results:
        status = r.get("status")
        if status == "ok":
            ok_entries.append(_ok_entry(request, r, mode, fee_rules))
            continue
        status = status if status in NON_OK_ORDER else "failed"
        card = _empty_card(r["site"], status)
        card["note"], card["action"] = _note_and_action(r["site"], status, declined, r.get("error"))
        other.append(_Entry(card))

    ok_entries.sort(key=lambda e: (e.unresolved, e.card["total"] if e.card["total"] is not None else math.inf,
                                   _site_rank(e.card["site"])))
    other.sort(key=lambda e: (NON_OK_ORDER.index(e.card["status"]), _site_rank(e.card["site"])))
    entries = ok_entries + other
    for rank, e in enumerate(entries, 1):
        e.card["rank"] = rank

    best = ok_entries[0] if ok_entries and ok_entries[0].card["total"] is not None else None
    if best is not None:
        best.card["cheapest"] = True
        for e in ok_entries[1:]:
            if e.card["total"] is None or e.unresolved != best.unresolved:
                continue
            diff = _money(e.card["total"] - best.card["total"])
            e.card["delta_label"] = f"+{inr(diff)}" if diff > 0 else "Same total"

    columns = [e.card["site"] for e in ok_entries]
    rows = []
    for idx, item in enumerate(items):
        priced = [e.cells[idx]["line_total"] for e in ok_entries if e.cells[idx]["status"] in PRICED]
        low = min(priced) if priced else None
        cells = {}
        for e in ok_entries:
            c = e.cells[idx]
            cells[e.card["site"]] = {"status": c["status"], "name": c["name"], "price_label": _price_label(mode, c),
                                     "cheapest": c["status"] in PRICED and abs(c["line_total"] - low) < 0.005}
        rows.append({"idx": idx, "label": _row_label(item), "cells": cells})

    picks = []
    for e in ok_entries:
        for idx, c in e.cells.items():
            if c["status"] != "pick":
                continue
            picks.append({"idx": idx, "site": e.card["site"], "label": _row_label(items[idx]), "candidates": [
                {"product_id": _cand_id(k), "name": k.get("name"), "size_label": k.get("size_label"),
                 "price_label": inr(_cand_total(k)), "packs": k.get("packs") or _qty(items[idx])}
                for k in c["candidates"] if _usable(k)]})

    split = None
    if mode == "grocery" and best is not None and len(ok_entries) >= 2:
        split = _split(best, ok_entries, split_min_saving)

    if mode == "food":
        title = (restaurant or {}).get("name") or "Your order"
        count_label = _plural(len(items), "dish", "dishes")
    else:
        title = "Cheapest cart"
        count_label = _plural(len(items), "item", "items")

    return {
        "request_id": request.get("id"),
        "mode": mode,
        "status": "done" if ok_entries else "failed",
        "title": title,
        "count_label": count_label,
        "area": request.get("area"),
        "priced_at": _priced_at(mode, results),
        "price_note": PRICE_NOTE,
        "offers_note": OFFERS_NOTE if mode == "food" else None,
        "restaurant": {"name": restaurant.get("name"), "locality": restaurant.get("locality")}
        if mode == "food" and restaurant else None,
        "best_site": best.card["site"] if best is not None else None,
        "cards": [e.card for e in entries],
        "table": {"columns": columns, "rows": rows, "totals": {e.card["site"]: e.card["total_label"] for e in ok_entries}},
        "picks": picks,
        "split": split,
    }

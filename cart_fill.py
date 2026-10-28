"""Add-only cart fill (Plan 6 T3): the only engine code that writes to a delivery app.

A fill replays the approved cart recipe's requests inside the user's logged-in page, behind the CartGuard:
  - it only adds planned products, or raises their quantity (set mode sends what is there plus the packs);
  - it never removes, empties or lowers a line, and never checks out, pays or edits an address;
  - it never clicks: every write is a recorded request, never a UI action;
  - it reads the cart before, after every add and at the end, and stops adding as soon as any line went down;
  - it stops adding as soon as the guard saw a write answered with a redirect (CartGuard.redirected_writes),
    and refuses to start with a guard that doesn't keep that record;
  - an item it is unsure of was planned as a question, and is asked live; it never guesses.
Logs carry the site and counts only: no product names, ids, coordinates, store ids or answers.
"""
import logging
import math
import random
import re
import time
from collections.abc import Callable

import browser
from cart_recipe import (_request, add_value, build_add_request, build_current_request, build_location_request,
                         build_read_request, extract_cart, policy_for)
from recipes import get_path, to_number

log = logging.getLogger("cart_fill")

OUT_OF_STOCK_TEXT = re.compile(r"out of stock|sold out|unavailable|not available|no stock", re.I)
SKIP_REASONS = ("you_skipped", "no_answer", "out_of_stock", "not_found")
USER_SKIPS = ("you_skipped", "no_answer")
# Fields of a 2xx add response that carry the app's own message ("Item is out of stock").
_MESSAGE_KEY = re.compile(r"message|error|reason|status|title|description|toast", re.I)


class CartError(Exception):
    pass


class CartDrift(CartError):
    """The cart recipe no longer works (the read or the first add failed): pause this site's filling."""


# --- pure helpers --------------------------------------------------------------------------------

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def location_matches(app, target, expected_store, max_m) -> bool:
    """Same store when both sides have one; else within max_m when both have coordinates; else no match."""
    if not app or not target:
        return False
    store = app.get("store_id")
    if store is not None and expected_store is not None:
        return str(store) == str(expected_store)
    coords = (app.get("lat"), app.get("lon"), target.get("lat"), target.get("lon"))
    if any(v is None for v in coords):
        return False
    return haversine_m(*coords) <= max_m


def classify_add(text) -> str:
    return "out_of_stock" if text and OUT_OF_STOCK_TEXT.search(str(text)) else "ok"


def _response_text(resp) -> str:
    """The app's message strings in a 2xx add response (top level and one level down), not its whole body."""
    out = []
    for obj in (resp, *(v for v in resp.values() if isinstance(v, dict))) if isinstance(resp, dict) else ():
        out += [v for k, v in obj.items() if isinstance(v, str) and _MESSAGE_KEY.search(str(k))]
    return " ".join(out)


def verify(before: dict[str, int], after: dict[str, int], sent: list[dict]) -> tuple[list[dict], bool]:
    """Each sent item's added_qty from after - before (shared out in plan order when a product repeats; an
    out_of_stock item keeps its status and takes no share), its status from that, and whether any line that was
    already in the cart went down."""
    remaining = {pid: max(0, after.get(pid, 0) - before.get(pid, 0)) for pid in {i["product_id"] for i in sent}}
    items = []
    for item in sent:
        item = dict(item)
        pid, packs = item["product_id"], item["packs"]
        if item.get("status") == "out_of_stock":   # the app refused it: it takes no share of the diff
            item["added_qty"] = 0
            items.append(item)
            continue
        got = min(remaining[pid], packs)
        remaining[pid] -= got
        item["added_qty"] = got
        item["status"] = "added" if got >= packs else "partial" if got > 0 else "not_added"
        items.append(item)
    changed = any(q > 0 and after.get(pid, 0) < q for pid, q in before.items())
    return items, changed


def outcome(status, items, cart, app_location=None, location_selected=False, repriced=None, error=None,
            existing_changed=False) -> dict:
    return {"status": status, "error": error, "existing_changed": existing_changed, "items": items, "cart": cart,
            "app_location": app_location, "location_selected": location_selected, "repriced": repriced}


# --- reads ---------------------------------------------------------------------------------------

def _read_cart(page, recipe: dict) -> dict:
    try:
        return extract_cart(browser.fetch_in_page(page, build_read_request(recipe)), recipe["read"]["field_map"])
    except browser.SessionExpired:
        raise
    except Exception as e:
        raise CartDrift("cart read failed") from e


def _quantities(cart: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in cart["lines"]:
        out[line["product_id"]] = out.get(line["product_id"], 0) + line["qty"]
    return out


def _read_location(page, recipe: dict) -> dict:
    spec = recipe["location"]["current"]
    try:
        data = browser.fetch_in_page(page, build_current_request(recipe))
    except browser.SessionExpired:
        raise
    except Exception as e:
        raise CartDrift("location read failed") from e
    store = get_path(data, spec.get("store_id_path"))
    area = get_path(data, spec.get("area_path"))
    return {"lat": to_number(get_path(data, spec.get("lat_path"))),
            "lon": to_number(get_path(data, spec.get("lon_path"))),
            "store_id": None if store is None or str(store) == "" else str(store),
            "area": None if area is None else str(area)}


def _redirected(guard) -> list:
    return list(guard.redirected_writes)


# --- location ------------------------------------------------------------------------------------

def _select_location(page, recipe: dict, target: dict, max_m) -> bool:
    """Select the target through the recorded request: its lat/lon, or the nearest saved address within max_m.
    Never creates or edits an address. True when a select request was sent."""
    select = (recipe.get("location") or {}).get("select")
    lat, lon = target.get("lat"), target.get("lon")
    if not select or lat is None or lon is None:
        return False
    try:
        if select.get("mode") == "latlon":
            req = build_location_request(recipe, lat=lat, lon=lon)
        elif select.get("mode") == "address_id":
            saved = select["saved"]
            rows = get_path(browser.fetch_in_page(page, _request(saved)), saved.get("list_path"))
            best = None
            for row in rows if isinstance(rows, list) else []:
                aid = get_path(row, saved.get("id"))
                alat, alon = to_number(get_path(row, saved.get("lat"))), to_number(get_path(row, saved.get("lon")))
                if aid is None or alat is None or alon is None:
                    continue
                d = haversine_m(lat, lon, alat, alon)
                if d <= max_m and (best is None or d < best[0]):
                    best = (d, aid)
            if best is None:
                return False
            req = build_location_request(recipe, address_id=best[1])
        else:
            return False
        browser.fetch_in_page(page, req)
        return True
    except browser.SessionExpired:
        raise
    except Exception as e:
        log.warning("location select failed (%s)", type(e).__name__)
        return False


def _reprice(page, plan: list[dict], search_recipe: dict | None, app: dict) -> list[dict]:
    """Each planned add, priced again at the app's own location. Nothing is added."""
    if not search_recipe:
        return []
    import search  # Plan 5; lazy so the fill imports without it
    out = []
    for n, item in enumerate(i for i in plan if i.get("action") == "add"):
        if n:
            time.sleep(random.uniform(0.8, 1.6))
        pid, name, packs, price = item.get("product_id"), item.get("name") or "", item.get("packs") or 1, item.get("price")
        try:
            req = search.build_request(search_recipe, name, app, store_id=app.get("store_id"))
            products = search.extract_products(browser.fetch_in_page(page, req), search_recipe["field_map"]) or []
        except browser.SessionExpired:
            raise
        except Exception as e:
            log.warning("re-price search failed (%s)", type(e).__name__)
            products = []
        hit = (next((p for p in products if pid is not None and str(p.get("product_id")) == str(pid)), None)
               or next((p for p in products if str(p.get("name") or "").casefold() == name.casefold()), None))
        new = to_number(hit.get("price")) if hit else None
        out.append({"idx": item.get("idx"), "name": item.get("name"), "product_id": pid,
                    "old_price": round(packs * price, 2) if price is not None else None,
                    "new_price": round(packs * new, 2) if new is not None else None,
                    "in_stock": bool(hit.get("in_stock", True)) if hit else False})
    return out


# --- the fill ------------------------------------------------------------------------------------

def _base(item: dict) -> dict:
    return {"idx": item.get("idx"), "label": item.get("label"), "name": item.get("name"),
            "product_id": item.get("product_id"), "packs": item.get("packs"), "status": None, "added_qty": 0,
            "skip_reason": None}


def _valid_add(pid, packs) -> bool:
    return (pid is not None and str(pid) != "" and isinstance(packs, int) and not isinstance(packs, bool)
            and packs >= 1)


def fill_cart(site, plan, priced, recipe, search_recipe, profile_dir, ask: Callable[[str, list[dict]], str | None],
              on_item: Callable[[dict], None], on_stage=lambda s: None, proxy_server=None, confirm_location=None,
              max_location_m=500) -> dict:
    """Fill the cart with the plan's items (add-only) and return the fill outcome."""
    target = confirm_location or priced
    location = {"query": target.get("area"), "lat": target.get("lat"), "lon": target.get("lon")}
    with browser.open_site(site, profile_dir=profile_dir, location=location, proxy_server=proxy_server,
                           block_media=True, cart_policy=policy_for(recipe, search_recipe)) as (_ctx, page, guard, _c):
        browser.goto(page, recipe["home_url"])
        return _fill(site, page, guard, plan, target, recipe, search_recipe, ask, on_item, on_stage,
                     confirm_location, max_location_m)


def _fill(site, page, guard, plan, target, recipe, search_recipe, ask, on_item, on_stage, confirm_location,
          max_m) -> dict:
    if not isinstance(getattr(guard, "redirected_writes", None), list):
        # Fail closed: without the record of redirected writes, a fill can't tell a write went astray.
        raise CartError("the cart guard can't report redirected writes")
    before = _quantities(_read_cart(page, recipe))

    on_stage("checking_location")
    app = _read_location(page, recipe)
    expected_store = None
    if search_recipe and search_recipe.get("store"):
        import search  # Plan 5
        expected_store = search.resolve_store(page, search_recipe, target)
    matched = location_matches(app, target, expected_store, max_m)
    if not matched and confirm_location is not None:
        log.info("cart fill on %s: the app's location changed again", site)
        return outcome("failed", [], None, app_location=app, error="location_changed")
    selected = False
    if not matched:
        on_stage("selecting_location")
        if _select_location(page, recipe, target, max_m):
            if _redirected(guard):
                log.warning("cart fill on %s stopped: a write was redirected", site)
                return outcome("failed", [], None, app_location=app, error="redirected_write")
            app = _read_location(page, recipe)
            matched = selected = location_matches(app, target, expected_store, max_m)
    if not matched:
        on_stage("repricing")
        repriced = _reprice(page, plan, search_recipe, app)
        log.info("cart fill on %s: location mismatch, %d items re-priced, nothing added", site, len(repriced))
        return outcome("needs_confirmation", [], None, app_location=app, repriced=repriced)

    on_stage("adding")
    mode, store_id = recipe["add"]["qty_mode"], app.get("store_id")
    results: list[dict] = []
    attempted: list[int] = []          # positions in results whose add request went out
    sent_so_far: dict[str, int] = {}
    floor = dict(before)               # the highest quantity seen per product: no line may go below it
    any_ok, requests, abort = False, 0, None

    for item in plan:
        res = _base(item)
        action = item.get("action")
        if abort:
            res["status"] = "not_added"
        elif action == "skip":
            res["status"] = "skipped"
            res["skip_reason"] = item.get("reason") if item.get("reason") in SKIP_REASONS else None
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

        if res["status"] is None and action == "add":
            pid, packs = res["product_id"], res["packs"]
            if not _valid_add(pid, packs):
                res["status"] = "not_added"
            elif _redirected(guard):
                abort, res["status"] = "redirected_write", "not_added"
            else:
                pid = str(pid)
                if requests:
                    time.sleep(random.uniform(0.8, 1.6))
                base = max(before.get(pid, 0) + sent_so_far.get(pid, 0), floor.get(pid, 0))
                req = build_add_request(recipe, pid, add_value(mode, base, packs), store_id)
                requests += 1
                try:
                    verdict = classify_add(_response_text(browser.fetch_in_page(page, req)))
                except browser.SessionExpired:
                    raise
                except Exception as e:
                    verdict = "out_of_stock" if classify_add(str(e)) == "out_of_stock" else "error"
                if verdict == "error" and not any_ok:
                    raise CartDrift("add failed")
                attempted.append(len(results))
                if verdict == "ok":
                    any_ok = True
                    sent_so_far[pid] = sent_so_far.get(pid, 0) + packs
                    res["status"], res["added_qty"] = "added", packs
                else:
                    res["status"] = "out_of_stock" if verdict == "out_of_stock" else "not_added"
                res["product_id"] = pid
                abort = _check(page, recipe, guard, floor, before)

        results.append(res)
        on_item(dict(res))

    try:
        after_cart = _read_cart(page, recipe)
    except CartDrift:
        if not requests:
            raise                   # nothing went out: the recipe's read is broken (drift)
        # Adds went out: report what each add request answered, unverified, and tell the user to check the app.
        log.warning("cart fill on %s: cart read failed after %d add requests", site, requests)
        return outcome("failed", results, None, app_location=app, location_selected=selected,
                       error="cart_read_failed", existing_changed=abort == "existing_changed")
    after = _quantities(after_cart)
    checked, existing_changed = verify(before, after, [results[i] for i in attempted])
    for pos, item in zip(attempted, checked):
        results[pos] = item
    if _redirected(guard) and not abort:
        abort = "redirected_write"
    if abort == "existing_changed":
        existing_changed = True
    cart = {"subtotal": after_cart["subtotal"], "total": after_cart["total"], "fees": after_cart["fees"],
            "line_count": len(after_cart["lines"]),
            "existing_count": sum(1 for line in after_cart["lines"] if before.get(line["product_id"], 0) > 0)}

    counts = {s: sum(1 for i in results if i["status"] == s)
              for s in ("added", "partial", "out_of_stock", "not_added", "skipped")}
    if abort or existing_changed:
        status, error = "failed", abort or "existing_changed"
        log.warning("cart fill on %s stopped (%s)", site, error)
    else:
        # User skips (you_skipped, no_answer) don't spoil "done"; plan-time skips (out_of_stock, not_found) do.
        tried = [i for i in results if not (i["status"] == "skipped" and i["skip_reason"] in USER_SKIPS)]
        if not counts["added"] and not counts["partial"]:
            status, error = "failed", "nothing_added"
        elif all(i["status"] == "added" for i in tried):
            status, error = "done", None
        else:
            status, error = "partial", None
    log.info("cart fill on %s: %s; %d added, %d partial, %d out of stock, %d not added, %d skipped, %d requests",
             site, status, counts["added"], counts["partial"], counts["out_of_stock"], counts["not_added"],
             counts["skipped"], requests)
    return outcome(status, results, cart, app_location=app, location_selected=selected, error=error,
                   existing_changed=existing_changed)


def _check(page, recipe, guard, floor: dict[str, int], before: dict[str, int]) -> str | None:
    """After an add: a reason to stop adding (a redirected write, a failed read, a line that went down), or None.
    Raises the floor to what the cart now holds."""
    if _redirected(guard):
        return "redirected_write"
    try:
        now = _quantities(_read_cart(page, recipe))
    except CartDrift:
        return "cart_read_failed"
    if any(q > 0 and now.get(pid, 0) < q for pid, q in floor.items()):
        return "existing_changed" if any(q > 0 and now.get(p, 0) < q for p, q in before.items()) else "cart_changed"
    for pid, q in now.items():
        floor[pid] = max(floor.get(pid, 0), q)
    return None

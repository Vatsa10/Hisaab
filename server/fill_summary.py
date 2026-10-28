"""The result of a cart fill as the PWA shows it (Plan 6 R10): every user-facing line, built here, rendered as is.

`build(fill, area, open_url)` takes a fill row (or the row merged with the engine's outcome, which adds
`existing_changed`) and returns the summary stored in `cart_fills.summary`. Pure: no database, no I/O.

Fill items are the engine's outcome items:
`{idx, label, name, product_id, packs, status: added|partial|out_of_stock|not_added|skipped|pending, added_qty,
skip_reason: you_skipped|no_answer|out_of_stock|not_found|None}`.
"""
from __future__ import annotations

from server.cart_rank import inr
from server.config import SITE_NAMES

USER_SKIPS = ("you_skipped", "no_answer")
SKIP_NOTES = {"you_skipped": "you skipped it", "no_answer": "no answer",
              "out_of_stock": "out of stock when we checked", "not_found": "not found"}
# The engine stopped adding with adds possibly already in the app's cart (cart_fill.fill_cart): the store ends the
# fill as interrupted (slot kept, never refunded), and the user is told to check the app.
CART_UNKNOWN = ("redirected_write", "existing_changed", "cart_changed", "cart_read_failed")
CHECK_APP = "Some items may already be in your cart. Check your {app} cart before filling again."


def app_name(site: str) -> str:
    return SITE_NAMES.get(site, str(site).capitalize())


def conn_name(site: str) -> str:
    """The app whose login a fill uses: Instamart fills use the Swiggy connection."""
    return "Swiggy" if site == "instamart" else app_name(site)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _num(v, default=0) -> float:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def _name(i: dict) -> str:
    return str(i.get("name") or i.get("label") or "")


def _label(i: dict) -> str:
    return str(i.get("label") or i.get("name") or "")


def _added_row(i: dict) -> str:
    qty = int(_num(i.get("added_qty")) or _num(i.get("packs"), 1) or 1)
    return _name(i) + (f" ×{qty}" if qty > 1 else "")


def _skip_row(i: dict) -> str:
    note = SKIP_NOTES.get(i.get("skip_reason"))
    return f"{_label(i)} ({note})" if note else _label(i)


def _sections(items: list[dict]) -> list[dict]:
    spec = [("added", "Added", lambda i: i.get("status") == "added", _added_row),
            ("partial", "Only some added", lambda i: i.get("status") == "partial",
             lambda i: f"{_name(i)}: {int(_num(i.get('added_qty')))} of {int(_num(i.get('packs'), 1))}"),
            ("out_of_stock", "Out of stock now", lambda i: i.get("status") == "out_of_stock", _name),
            ("not_added", "Not added", lambda i: i.get("status") == "not_added", _name),
            ("skipped", "Skipped", lambda i: i.get("status") == "skipped", _skip_row)]
    out = []
    for key, heading, keep, row in spec:
        rows = [row(i) for i in items if keep(i)]
        if rows:
            out.append({"key": key, "heading": heading, "rows": rows})
    return out


def _error_line(status: str, error: str | None, a: int, app: str, conn: str) -> str | None:
    if status not in ("failed", "interrupted"):
        return None
    check = CHECK_APP.format(app=app)
    stop_lines = {
        "redirected_write": f"{app} answered one of our adds in a way we didn't expect, so we stopped. {check}",
        # warning_line already says what changed.
        "existing_changed": f"We stopped adding. {check}",
        "cart_changed": f"Your {app} cart changed while we were adding, so we stopped. {check}",
        "cart_read_failed": f"We couldn't read your {app} cart after adding, so we stopped. {check}",
    }
    if error in CART_UNKNOWN:
        return stop_lines[error]
    if status == "interrupted" or error == "worker_lost":
        return f"We lost track of this fill. Check your {app} cart before filling again."
    n = "Nothing was added." if a == 0 else "Some items were added before it stopped."
    lines = {
        "needs_login": f"{conn} logged you out. Reconnect it, then try again. {n}",
        "recipe_drift": f"Filling {app} carts stopped working. We've told the team. {n}",
        "location_changed": f"{app}'s location changed again. Nothing was added.",
        "nothing_added": f"{app} didn't accept any of the items. Nothing was added.",
    }
    return lines.get(error, f"Something went wrong. {n}")


def _reprice(repriced: list | None, app: str) -> tuple[dict, bool]:
    rows, old_sum, new_sum, priced = [], 0.0, 0.0, 0
    for r in repriced or []:
        if not isinstance(r, dict):
            continue
        old = _num(r.get("old_price"), None)
        new = _num(r.get("new_price"), None)
        available = new is not None and r.get("in_stock", True) is not False
        rows.append({"label": str(r.get("name") or ""), "old": inr(old) if old is not None else "—",
                     "new": inr(new) if available else "Not available"})
        if available and old is not None:
            old_sum, new_sum, priced = old_sum + old, new_sum + new, priced + 1
    if not priced:
        return {"rows": rows, "total_line": f"We couldn't get {app}'s prices there."}, False
    return {"rows": rows, "total_line": f"Items there: {inr(new_sum)} instead of {inr(old_sum)}"}, True


def build(fill: dict, area: str, open_url: str | None) -> dict:
    site = fill.get("site") or ""
    app, conn = app_name(site), conn_name(site)
    status = fill.get("status") or "failed"
    error = fill.get("error")
    items = [i for i in fill.get("items") or [] if isinstance(i, dict)]
    a = sum(1 for i in items if i.get("status") in ("added", "partial"))
    t = sum(1 for i in items if not (i.get("status") == "skipped" and i.get("skip_reason") in USER_SKIPS))
    unknown = status in ("failed", "interrupted") and error in CART_UNKNOWN

    if status == "done":
        title, tone = f"Added {_plural(a, 'item', 'items')} to your {app} cart", "success"
    elif status == "partial":
        title, tone = f"Added {a} of {_plural(t, 'item', 'items')} to your {app} cart", "warning"
    elif status == "interrupted" or unknown:
        title, tone = f"Your {app} cart may be half filled", "warning"
    elif status == "needs_confirmation":
        title, tone = f"{app} is set to a different location", "warning"
    else:
        title, tone = f"Couldn't fill your {app} cart", "error"

    cart = fill.get("cart") if isinstance(fill.get("cart"), dict) else None
    cart_line = existing_line = None
    if cart and _num(cart.get("total"), None) is not None and _num(cart.get("subtotal"), None) is not None:
        total, subtotal = float(cart["total"]), float(cart["subtotal"])
        cart_line = f"{app} cart total {inr(total)} · items {inr(subtotal)} + fees {inr(total - subtotal)}"
    k = int(_num((cart or {}).get("existing_count")))
    if k == 1:
        existing_line = "Includes 1 item that was already in your cart."
    elif k > 1:
        existing_line = f"Includes {k} items that were already in your cart."

    existing_changed = bool(fill.get("existing_changed")) or error == "existing_changed"
    warning_line = (f"Something already in your {app} cart changed while we were adding. Please check your cart."
                    if existing_changed else None)

    reprice, confirm_label, location_line = None, None, None
    if status == "needs_confirmation":
        reprice, priced = _reprice(fill.get("repriced"), app)
        confirm_label = "Fill at these prices" if priced else None
        app_loc = fill.get("app_location") if isinstance(fill.get("app_location"), dict) else {}
        location_line = f"{app} delivers to {app_loc.get('area') or 'another area'} right now, not {area}."
    elif fill.get("location_selected"):
        location_line = f"We set {app}'s delivery location to {area}."

    show_open = open_url and (a > 0 or status == "interrupted" or unknown)
    return {
        "status": status,
        "title": title,
        "tone": tone,
        "sections": _sections(items),
        "cart_line": cart_line,
        "existing_line": existing_line,
        "warning_line": warning_line,
        "location_line": location_line,
        "reprice": reprice,
        "error_line": _error_line(status, error, a, app, conn),
        "open": {"label": f"Open {app} to pay", "url": open_url} if show_open else None,
        "pay_note": f"We never place the order or pay. Review your cart in {app} and pay there.",
        "confirm_label": confirm_label,
        "counted": fill.get("counted", True) is not False,
    }

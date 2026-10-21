"""Recipe persistence, field_map extraction and pagination. No site knowledge lives here."""
import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

RECIPES = Path(__file__).parent / "recipes"

# --- persistence -------------------------------------------------------------

def recipe_path(site: str) -> Path:
    return RECIPES / f"{site}.json"


def hint_path(site: str) -> Path:
    return RECIPES / f"{site}.hint.txt"


def load_seed(site: str) -> dict | None:
    """Optional known endpoint + pagination for a site; the field_map is still learned and validated."""
    p = RECIPES / f"{site}.seed.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def load(site: str) -> dict | None:
    p = recipe_path(site)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save(site: str, recipe: dict) -> None:
    RECIPES.mkdir(exist_ok=True)
    recipe_path(site).write_text(json.dumps(recipe, indent=2, ensure_ascii=False), encoding="utf-8")


# --- path language -----------------------------------------------------------
# Dotted paths into JSON: "data.orders", "items.0.name". A value starting with "="
# is a literal ("=INR"). Empty/None path -> None.

def get_path(obj, path):
    if path is None or path == "":
        return None
    if isinstance(path, str) and path.startswith("="):
        return path[1:]
    path = re.sub(r"\[(-?\d+)\]", r".\1", str(path))  # models also write "orders[-1].id"
    for part in path.split("."):
        if isinstance(obj, list) and part.lstrip("-").isdigit():
            idx = int(part)
            obj = obj[idx] if -len(obj) <= idx < len(obj) else None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
        if obj is None:
            return None
    return obj


def set_path(obj: dict, path: str, value) -> None:
    parts = path.split(".")
    for part in parts[:-1]:
        obj = obj.setdefault(part, {})
    obj[parts[-1]] = value


# --- normalization -----------------------------------------------------------

def to_number(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^\d.\-]", "", str(v))
    try:
        return float(s) if s not in ("", "-", ".") else None
    except ValueError:
        return None


_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d %b %Y, %I:%M %p", "%d %b %Y %I:%M %p",
               "%b %d, %Y, %I:%M %p", "%a, %b %d, %Y, %I:%M %p", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M",
               "%Y-%m-%d", "%d %b %Y", "%b %d, %Y", "%d %b, %I:%M %p", "%d %b %I:%M %p",
               "%B %d, %Y at %I:%M %p", "%B %d, %Y, %I:%M %p", "%B %d, %Y", "%d %B %Y, %I:%M %p", "%d %B %Y",
               "%b %d, %Y at %I:%M %p", "%d %b %Y at %I:%M %p")


def normalize_ts(v, fmt: str | None = None) -> str | None:
    """Return ISO-8601 string or None. Accepts epoch s/ms, ISO, or common formats."""
    if v is None or v == "":
        return None
    n = to_number(v) if not isinstance(v, str) or re.fullmatch(r"\s*\d{9,14}(\.\d+)?\s*", v) else None
    if n is not None:
        if n > 1e12:
            n /= 1000
        return datetime.fromtimestamp(n, tz=timezone.utc).astimezone().isoformat()
    s = str(v).strip()
    if fmt:
        try:
            return _parse(s, fmt).isoformat()
        except ValueError:  # LLM-written formats are sometimes prose ("ISO 8601"): fall back to auto-detect
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt.astimezone() if dt.tzinfo else dt).isoformat()
    except ValueError:
        pass
    for f in _TS_FORMATS:
        try:
            return _parse(s, f).isoformat()
        except ValueError:
            continue
    return None


def _parse(s: str, fmt: str, now: datetime | None = None) -> datetime:
    """strptime that copes with apps showing recent orders without a year ("18 Jul, 11:56 pm"):
    the most recent such date that is not in the future."""
    if "%Y" in fmt or "%y" in fmt:
        return datetime.strptime(s, fmt)
    dt = datetime.strptime(f"{s} 2000", f"{fmt} %Y")  # explicit leap year: no ambiguity warning, 29 Feb parses
    now = now or datetime.now()
    for year in range(now.year, now.year - 8, -1):
        try:
            candidate = dt.replace(year=year)
        except ValueError:  # 29 Feb in a non-leap year
            continue
        if candidate <= now:
            return candidate
    return dt.replace(year=now.year)


def normalize_status(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if "refund" in s:
        return "refunded"
    if "cancel" in s:
        return "cancelled"
    if "fail" in s:
        return "failed"
    if "deliver" in s or s in ("completed", "complete"):
        return "delivered"
    return s


# --- extraction --------------------------------------------------------------

def extract_orders(data, field_map: dict) -> list[dict]:
    """Apply a recipe field_map to one response body. Returns normalized orders."""
    rows = get_path(data, field_map.get("orders_path")) if field_map.get("orders_path") else data
    if isinstance(rows, dict):  # some APIs key orders by id
        rows = list(rows.values())
    if not isinstance(rows, list):
        return []
    div = float(field_map.get("amount_divisor") or 1)
    item_div = float(field_map.get("item_price_divisor") or div)
    imap = field_map.get("items") or {}
    out = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        total = to_number(get_path(raw, field_map.get("total_amount")))
        items = []
        raw_items, item_map = get_path(raw, field_map.get("items_path")) or [], imap
        if isinstance(raw_items, str):  # "2 x Veg Cheese Frankie, 1 x Veg Grilled Sandwich" (Zomato)
            raw_items = [{"name": m[2], "qty": m[1]} for part in raw_items.split(",")
                         if (m := re.match(r"\s*(\d+)\s*[x×]\s*(.+?)\s*$", part))]
            item_map = {"name": "name", "qty": "qty"}
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            name = get_path(it, item_map.get("name"))
            if not name:
                continue
            qty = to_number(get_path(it, item_map.get("qty"))) or 1.0
            price = to_number(get_path(it, item_map.get("price")))
            if price is not None:
                price /= item_div
                if field_map.get("price_is_unit"):
                    price *= qty
            items.append({"name": str(name).strip(), "qty": qty, "price": price})
        oid = get_path(raw, field_map.get("order_id"))
        out.append({
            "order_id": str(oid) if oid is not None else None,
            "placed_at": normalize_ts(get_path(raw, field_map.get("placed_at")), field_map.get("placed_at_format")),
            "total_amount": total / div if total is not None else None,
            "currency": get_path(raw, field_map.get("currency")) or "INR",
            "status": normalize_status(get_path(raw, field_map.get("status"))),
            "merchant": get_path(raw, field_map.get("store_or_restaurant")),
            "sub_platform": get_path(raw, field_map.get("sub_platform")),
            "items": items,
            "raw": raw,
        })
    return out


def diagnose(data, field_map: dict) -> str:
    """What each required field extracts from the first order - tells the model exactly what to fix."""
    rows = get_path(data, field_map.get("orders_path")) if field_map.get("orders_path") else data
    if isinstance(rows, dict):
        rows = list(rows.values())
    if not isinstance(rows, list) or not rows:
        return f"orders_path {field_map.get('orders_path')!r} does not point to a list/map of orders"
    first = rows[0] if isinstance(rows[0], dict) else {}
    out = [f"{len(rows)} rows at orders_path"]
    for key in ("order_id", "placed_at", "total_amount"):
        raw = get_path(first, field_map.get(key))
        parsed = (normalize_ts(raw, field_map.get("placed_at_format")) if key == "placed_at"
                  else to_number(raw) if key == "total_amount" else raw)
        out.append(f"{key}: path {field_map.get(key)!r} -> raw {str(raw)[:80]!r} -> parsed {parsed!r}")
    return "; ".join(out)


def valid_orders(orders: list[dict]) -> list[dict]:
    return [o for o in orders if o["order_id"] and o["placed_at"] and o["total_amount"] is not None]


# --- requests & pagination ---------------------------------------------------

def first_request(recipe: dict) -> dict:
    """Request dict {method, url, headers, body(str|None)} for page 1."""
    query = dict(recipe.get("query") or {})
    parts = urlsplit(recipe["endpoint"])
    merged = dict(parse_qsl(parts.query)) | {k: str(v) for k, v in query.items()}
    url = urlunsplit(parts._replace(query=urlencode(merged)))
    body = recipe.get("body")
    return {"method": recipe.get("method", "GET").upper(), "url": url,
            "headers": recipe.get("headers") or {},
            "body": copy.deepcopy(body) if body is not None else None}


def apply_param(req: dict, param: str, value) -> dict:
    """Set pagination param in the query string (GET) or JSON body (dotted path)."""
    req = copy.deepcopy(req)
    if req["method"] == "GET" or req["body"] is None:
        parts = urlsplit(req["url"])
        q = dict(parse_qsl(parts.query)) | {param: str(value)}
        req["url"] = urlunsplit(parts._replace(query=urlencode(q)))
    else:
        body = req["body"] if isinstance(req["body"], dict) else json.loads(req["body"])
        set_path(body, param, value)
        req["body"] = body
    return req


def current_param(req: dict, param: str):
    if req["method"] == "GET" or req["body"] is None:
        return dict(parse_qsl(urlsplit(req["url"]).query)).get(param)
    body = req["body"] if isinstance(req["body"], dict) else json.loads(req["body"])
    return get_path(body, param)


def next_request(req: dict, pagination: dict, data, n_orders: int) -> dict | None:
    """Next page request, or None when there are no more pages."""
    if n_orders == 0 or not pagination:
        return None
    kind, param = pagination.get("type"), pagination.get("param")
    if kind == "cursor":
        cursor = get_path(data, pagination.get("next_path"))
        if cursor in (None, "", 0, False) or str(cursor) == str(current_param(req, param)):
            return None
        return apply_param(req, param, cursor)
    cur = to_number(current_param(req, param))
    if kind == "page":
        return apply_param(req, param, int(cur if cur is not None else pagination.get("start", 1)) + 1)
    if kind == "offset":
        return apply_param(req, param, int(cur or 0) + n_orders)
    return None

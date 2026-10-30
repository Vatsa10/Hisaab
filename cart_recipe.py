"""Cart recipe format (recipes.kind='cart'): request builders, cart extraction, guard policy and the recipe check.

`*_path` values are dotted JSON-body paths (list indices allowed, e.g. "items.0.qty") or "query:<name>".

{kind: "cart", site, home_url,
 add: {method: "POST", url, headers, body, product_id_path, qty_path, store_id_path|null, qty_mode: "set"|"increment"},
 read: {method, url, headers, body|null, field_map: {lines_path, product_id, name, qty, price, subtotal, total,
        fees: {delivery?, handling?, small_cart?, surge?, other?}, out_of_stock|null}},
 location: {current: {method, url, headers, body|null, store_id_path|null, lat_path|null, lon_path|null, area_path|null},
            select: null | {mode: "latlon", method, url, headers, body, lat_input, lon_input}
                         | {mode: "address_id", method, url, headers, body, address_id_path,
                            saved: {method, url, headers, list_path, id, lat, lon, label}}},
 cart_allowlist: [{url_regex, why, op: "cart", qty_path, operation?}], location_allowlist: [{..., op: "location"}],
 read_post_allowlist: [{url_regex, why}]}
"""
import copy
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cart_guard import (CartPolicy, broad_error, decide_cart, forbidden_category, matches, narrow,  # noqa: F401
                        regex_host, site_error)
from recipes import get_path, to_number

QTY_MODES = ("set", "increment")
_WHOLE_CART_KEY = re.compile(r"product|item|sku|variant", re.I)


# --- quantities ----------------------------------------------------------------------------------

def add_value(qty_mode: str, before_qty: int, packs: int) -> int:
    """The quantity to send so the line grows by `packs`: never below before + 1 in set mode (add-only)."""
    if qty_mode not in QTY_MODES:
        raise ValueError(f"unknown qty_mode {qty_mode!r}")
    if packs < 1:
        raise ValueError("packs must be at least 1")
    if before_qty < 0:
        raise ValueError("existing quantity can't be negative")
    return before_qty + packs if qty_mode == "set" else packs


# --- request builders ----------------------------------------------------------------------------

def _coerce(old, new):
    """Keep the recorded value's type: a site that sent "1" gets "3", one that sent 1 gets 3."""
    if isinstance(old, str) and not isinstance(new, str):
        return str(new)
    if isinstance(old, int) and not isinstance(old, bool) and isinstance(new, str) and re.fullmatch(r"-?\d+", new):
        return int(new)
    return new


def _set_body(body, path: str, value):
    parts = re.sub(r"\[(-?\d+)\]", r".\1", path).split(".")
    obj = body
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if isinstance(obj, list):
            idx = int(part)
            if last:
                obj[idx] = _coerce(obj[idx], value)
            else:
                obj = obj[idx]
        elif isinstance(obj, dict):
            if last:
                obj[part] = _coerce(obj.get(part), value)
            else:
                obj = obj.setdefault(part, {})
        else:
            raise ValueError(f"can't set {path!r} in the request body")


def _set_query(url: str, name: str, value) -> str:
    p = urlsplit(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q[name] = str(value)
    return urlunsplit(p._replace(query=urlencode(q)))


def _request(spec: dict) -> dict:
    body = copy.deepcopy(spec.get("body"))
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            pass
    return {"method": (spec.get("method") or "GET").upper(), "url": spec["url"],
            "headers": dict(spec.get("headers") or {}), "body": body}


def _apply(req: dict, where, value) -> None:
    """Set a value at a `*_path` ("query:<name>" or a dotted body path) or a Plan 5 input {in, name}."""
    if isinstance(where, dict):
        kind, name = where.get("in"), where["name"]
        if kind == "query":
            req["url"] = _set_query(req["url"], name, value)
        elif kind == "header":
            req["headers"][name] = str(value)
        elif kind == "body":
            _apply(req, name, value)
        else:
            raise ValueError(f"unknown input location {kind!r}")
    elif where.startswith("query:"):
        req["url"] = _set_query(req["url"], where.removeprefix("query:"), value)
    else:
        if req["body"] is None:
            req["body"] = {}
        if not isinstance(req["body"], (dict, list)):
            raise ValueError("request body is not JSON")
        _set_body(req["body"], where, value)


def build_add_request(recipe: dict, product_id, value: int, store_id) -> dict:
    add = recipe["add"]
    req = _request(add)
    _apply(req, add["product_id_path"], product_id)
    _apply(req, add["qty_path"], value)
    if add.get("store_id_path") and store_id is not None:
        _apply(req, add["store_id_path"], store_id)
    return req


def build_read_request(recipe: dict) -> dict:
    return _request(recipe["read"])


def build_current_request(recipe: dict) -> dict:
    return _request(recipe["location"]["current"])


def build_location_request(recipe: dict, *, lat=None, lon=None, address_id=None) -> dict:
    select = (recipe.get("location") or {}).get("select")
    if not select:
        raise ValueError("this cart recipe can't select a location")
    req = _request(select)
    if select.get("mode") == "latlon":
        if lat is None or lon is None:
            raise ValueError("latlon select needs lat and lon")
        _apply(req, select["lat_input"], lat)
        _apply(req, select["lon_input"], lon)
    elif select.get("mode") == "address_id":
        if address_id is None:
            raise ValueError("address_id select needs an address id")
        _apply(req, select["address_id_path"], address_id)
    else:
        raise ValueError(f"unknown location select mode {select.get('mode')!r}")
    return req


# --- cart extraction -----------------------------------------------------------------------------

def _flag(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0", "no", "n", "none", "null")
    return bool(v)


def extract_cart(data, field_map: dict) -> dict:
    """{"lines": [{product_id, name, qty, price, out_of_stock}], "subtotal", "total", "fees": {name: float}}.
    Lines without a product id or with qty <= 0 are dropped."""
    rows = get_path(data, field_map.get("lines_path"))
    lines = []
    oos = field_map.get("out_of_stock")
    for row in rows if isinstance(rows, list) else []:
        pid = get_path(row, field_map.get("product_id"))
        qty = to_number(get_path(row, field_map.get("qty")))
        if pid is None or str(pid) == "" or qty is None or int(qty) <= 0:
            continue
        lines.append({"product_id": str(pid), "name": get_path(row, field_map.get("name")), "qty": int(qty),
                      "price": to_number(get_path(row, field_map.get("price"))),
                      "out_of_stock": _flag(get_path(row, oos)) if oos else False})
    fees = {}
    for name, path in (field_map.get("fees") or {}).items():
        v = to_number(get_path(data, path)) if path else None
        if v is not None:
            fees[name] = v
    return {"lines": lines, "subtotal": to_number(get_path(data, field_map.get("subtotal"))),
            "total": to_number(get_path(data, field_map.get("total"))), "fees": fees}


# --- guard policy --------------------------------------------------------------------------------

def policy_for(recipe: dict, search_recipe: dict | None = None) -> CartPolicy:
    """The cart guard's policy: the recipe's cart and location entries; read entries = its own plus the search
    recipe's read POSTs (a fill re-prices through search). When the recipe declares a binary add body
    (add.body_encoding == "binary"), the cart entries matching the add URL exactly are marked binary. Cart
    entries keep their own qty_path (a recipe must give every one; add.qty_path fills a missing one), so the guard
    requires a quantity from 1 to 99 there."""
    def entries(lst):
        return tuple(dict(e) for e in lst or ())
    cart = entries(recipe.get("cart_allowlist"))
    add = recipe.get("add") or {}
    if add.get("qty_path"):
        cart = tuple(e if e.get("qty_path") else e | {"qty_path": add["qty_path"]} for e in cart)
    if add.get("body_encoding") == "binary" and add.get("url"):
        cart = tuple(e | {"binary": True} if matches(e.get("url_regex"), add["url"]) else e for e in cart)
    return CartPolicy(cart=cart,
                      location=entries(recipe.get("location_allowlist")),
                      read=entries(recipe.get("read_post_allowlist"))
                      + entries((search_recipe or {}).get("read_post_allowlist")))


def _whole_cart(body) -> bool:
    """A list holding >= 2 objects that each name a product/item/sku/variant: the request sends the whole cart."""
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            return False
    if isinstance(body, dict):
        return any(_whole_cart(v) for v in body.values())
    if isinstance(body, list):
        products = [o for o in body if isinstance(o, dict) and any(_WHOLE_CART_KEY.search(str(k)) for k in o)]
        return len(products) >= 2 or any(_whole_cart(v) for v in body)
    return False


def _body_text(body) -> str | None:
    if body is None:
        return None
    return body if isinstance(body, str) else json.dumps(body)


def _known_urls(recipe: dict) -> list[str]:
    """Every URL the recipe itself requests: the probes for host confinement."""
    loc = recipe.get("location") or {}
    select = loc.get("select") or {}
    specs = [recipe.get("add"), recipe.get("read"), loc.get("current"), select, select.get("saved")]
    return [u for u in [recipe.get("home_url"), *((sp or {}).get("url") for sp in specs)] if u]


_REQUIRED = ("kind", "site", "home_url", "add", "read", "location", "cart_allowlist", "location_allowlist",
             "read_post_allowlist")
_REQUIRED_ADD = ("method", "url", "body", "product_id_path", "qty_path", "qty_mode")
_REQUIRED_READ = ("method", "url", "field_map")


def check_cart_recipe(recipe: dict, site: str) -> str | None:
    """The first reason this cart recipe can't be used, or None."""
    for key in _REQUIRED:
        if recipe.get(key) is None:
            return f"missing {key}"
    add, read = recipe["add"], recipe["read"]
    for key in _REQUIRED_ADD:
        if add.get(key) is None:
            return f"missing add.{key}"
    for key in _REQUIRED_READ:
        if read.get(key) is None:
            return f"missing read.{key}"

    add_body = _body_text(add["body"])
    if cat := forbidden_category(add["url"], add_body, False):
        return f"add endpoint is {cat}"
    if _whole_cart(add["body"]):
        return "add request carries the whole cart"
    if add["qty_mode"] not in QTY_MODES:
        return "unknown qty_mode"
    ok, reason = decide_cart(add["method"], add["url"], False, add_body, CartPolicy(cart=policy_for(recipe).cart))
    if reason.startswith("forbidden: "):  # e.g. the recorded quantity at qty_path isn't a number >= 1
        return f"add endpoint is {reason.removeprefix('forbidden: ')}"
    if (ok, reason) != (True, "cart add"):
        return "add endpoint is not on the cart allowlist"

    entries = [*recipe["cart_allowlist"], *recipe["location_allowlist"], *recipe["read_post_allowlist"]]
    hosts = {h for h in (urlsplit(recipe["home_url"]).hostname, urlsplit(add["url"]).hostname) if h}
    for e in entries:
        if err := broad_error(e.get("url_regex") or "", hosts):
            return err
    samples = _known_urls(recipe)
    for e in entries:
        if err := site_error(e.get("url_regex") or "", site, samples):
            return err

    current = recipe["location"].get("current")
    if not current or not (current.get("store_id_path") or (current.get("lat_path") and current.get("lon_path"))):
        return "cart recipe can't read the app's location"

    select = recipe["location"].get("select")
    if select:
        ok = decide_cart(select.get("method") or "POST", select.get("url") or "", False, _body_text(select.get("body")),
                         CartPolicy(location=tuple(recipe["location_allowlist"])))
        if ok not in ((True, "location select"), (True, "safe method")):
            return "location select is not on the location allowlist"
    # Every cart write names where its quantity sits, so the guard can hold it to 1-99 (add-only).
    if any(not (isinstance(e.get("qty_path"), str) and e["qty_path"].strip()) for e in recipe["cart_allowlist"]):
        return "cart allowlist entry has no qty_path"
    return None


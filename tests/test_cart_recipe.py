"""Cart recipe format (Plan 6 T2): request builders, cart extraction, policy and the recipe check."""
import copy
import json

import pytest

import cart_guard
import cart_recipe
from cart_guard import CartPolicy, decide_cart

ADD_ENTRY = {"url_regex": r"^https://www\.zepto\.com/api/v1/cart/add$", "why": "add to cart", "op": "cart",
             "qty_path": "item.quantity"}
LOC_ENTRY = {"url_regex": r"^https://www\.zepto\.com/api/v1/location/select$", "why": "pick location", "op": "location"}
READ_ENTRY = {"url_regex": r"^https://www\.zepto\.com/api/v1/cart/view$", "why": "read cart"}

RECIPE = {
    "kind": "cart", "site": "zepto", "home_url": "https://www.zepto.com/",
    "add": {"method": "POST", "url": "https://www.zepto.com/api/v1/cart/add", "headers": {"x-app": "web"},
            "body": {"item": {"product_id": "seed", "quantity": 1}, "store": {"id": "S0"}},
            "product_id_path": "item.product_id", "qty_path": "item.quantity", "store_id_path": "store.id",
            "qty_mode": "set"},
    "read": {"method": "POST", "url": "https://www.zepto.com/api/v1/cart/view", "headers": {}, "body": {"v": 1},
             "field_map": {"lines_path": "cart.items", "product_id": "pid", "name": "name", "qty": "count",
                           "price": "price", "subtotal": "cart.bill.item_total", "total": "cart.bill.to_pay",
                           "fees": {"handling": "cart.bill.handling", "delivery": "cart.bill.delivery"},
                           "out_of_stock": "oos"}},
    "location": {"current": {"method": "GET", "url": "https://www.zepto.com/api/v1/location/current", "headers": {},
                             "body": None, "store_id_path": "store.id", "lat_path": "lat", "lon_path": "lon",
                             "area_path": "area"},
                 "select": {"mode": "latlon", "method": "POST", "url": "https://www.zepto.com/api/v1/location/select",
                            "headers": {}, "body": {"latitude": 0, "longitude": 0},
                            "lat_input": "latitude", "lon_input": "longitude"}},
    "cart_allowlist": [ADD_ENTRY], "location_allowlist": [LOC_ENTRY], "read_post_allowlist": [READ_ENTRY],
}


def recipe(**changes) -> dict:
    r = copy.deepcopy(RECIPE)
    for path, value in changes.items():
        obj, *keys, last = [r, *path.split("__")]
        for k in keys:
            obj = obj[k]
        if value is _DEL:
            del obj[last]
        else:
            obj[last] = value
    return r


_DEL = object()


@pytest.fixture(autouse=True)
def _no_fake(monkeypatch):
    monkeypatch.delenv("WORKER_FAKE", raising=False)


# --- add_value -------------------------------------------------------------------------------

def test_add_value_set_mode_adds_on_top():
    assert cart_recipe.add_value("set", 2, 1) == 3


def test_add_value_increment_and_bad_input():
    assert cart_recipe.add_value("increment", 2, 1) == 1
    assert cart_recipe.add_value("increment", 0, 3) == 3
    assert cart_recipe.add_value("set", 0, 3) == 3
    for packs in (0, -1):
        with pytest.raises(ValueError):
            cart_recipe.add_value("set", 2, packs)
        with pytest.raises(ValueError):
            cart_recipe.add_value("increment", 2, packs)
    with pytest.raises(ValueError):
        cart_recipe.add_value("replace", 2, 1)
    with pytest.raises(ValueError):
        cart_recipe.add_value("set", -1, 1)       # a negative existing quantity is a broken cart read


# --- request builders ------------------------------------------------------------------------

def test_build_add_request_body_and_query_paths():
    req = cart_recipe.build_add_request(RECIPE, "p42", 3, "S9")
    assert req == {"method": "POST", "url": "https://www.zepto.com/api/v1/cart/add", "headers": {"x-app": "web"},
                   "body": {"item": {"product_id": "p42", "quantity": 3}, "store": {"id": "S9"}}}
    assert RECIPE["add"]["body"]["item"]["product_id"] == "seed"       # the recipe is not mutated

    q = recipe(add__url="https://www.zepto.com/api/v1/cart/add?pid=seed&qty=1&v=2", add__product_id_path="query:pid",
               add__qty_path="query:qty", add__store_id_path="query:store")
    req = cart_recipe.build_add_request(q, "p 42", 2, "S9")
    assert req["url"] == "https://www.zepto.com/api/v1/cart/add?pid=p+42&qty=2&v=2&store=S9"

    # list indices in a path, string body, and a null store id path
    s = recipe(add__body=json.dumps({"items": [{"id": 0, "qty": "1"}]}), add__product_id_path="items.0.id",
               add__qty_path="items.0.qty", add__store_id_path=None)
    req = cart_recipe.build_add_request(s, "77", 4, "S9")
    assert req["body"] == {"items": [{"id": 77, "qty": "4"}]}    # the recorded value types are kept

    # no store id known: the recorded one is kept
    assert cart_recipe.build_add_request(RECIPE, "p1", 1, None)["body"]["store"] == {"id": "S0"}


def test_build_read_and_current_requests():
    assert cart_recipe.build_read_request(RECIPE) == {"method": "POST", "url": "https://www.zepto.com/api/v1/cart/view",
                                                      "headers": {}, "body": {"v": 1}}
    assert cart_recipe.build_current_request(RECIPE) == {
        "method": "GET", "url": "https://www.zepto.com/api/v1/location/current", "headers": {}, "body": None}


def test_build_location_request_latlon_and_address_id():
    req = cart_recipe.build_location_request(RECIPE, lat=22.3101, lon=73.1702)
    assert req["body"] == {"latitude": 22.3101, "longitude": 73.1702} and req["method"] == "POST"
    with pytest.raises(ValueError):
        cart_recipe.build_location_request(RECIPE, address_id="a1")

    a = recipe(location__select={"mode": "address_id", "method": "POST",
                                 "url": "https://www.zepto.com/api/v1/location/select", "headers": {},
                                 "body": {"address": {"id": None}}, "address_id_path": "address.id",
                                 "saved": {"method": "GET", "url": "https://www.zepto.com/api/v1/addresses",
                                           "headers": {}, "list_path": "data", "id": "id", "lat": "lat",
                                           "lon": "lng", "label": "tag"}})
    assert cart_recipe.build_location_request(a, address_id="a1")["body"] == {"address": {"id": "a1"}}
    with pytest.raises(ValueError):
        cart_recipe.build_location_request(a, lat=1.0, lon=2.0)

    # Plan 5 style location inputs {in, name}
    i = recipe(location__select__lat_input={"in": "query", "name": "lat"},
               location__select__lon_input={"in": "body", "name": "geo.lng"})
    req = cart_recipe.build_location_request(i, lat=22.3, lon=73.2)
    assert req["url"].endswith("/location/select?lat=22.3") and req["body"]["geo"] == {"lng": 73.2}

    with pytest.raises(ValueError):
        cart_recipe.build_location_request(recipe(location__select=None), lat=1.0, lon=2.0)


# --- extract_cart ----------------------------------------------------------------------------

CART = {"cart": {"items": [
    {"pid": "p1", "name": "Amul Taaza Milk 500 ml", "count": 2, "price": "₹29", "oos": False},
    {"pid": 7, "name": "Bread", "count": "1", "price": 45.5, "oos": "true"},
    {"pid": "p3", "name": "Gone", "count": 0, "price": 10},
    {"name": "No id", "count": 1, "price": 5},
    {"pid": "p4", "name": "Negative", "count": -1, "price": 5}],
    "bill": {"item_total": "412.00", "to_pay": 427, "handling": 15, "delivery": None}}}


def test_extract_cart_maps_lines_and_fees():
    out = cart_recipe.extract_cart(CART, RECIPE["read"]["field_map"])
    assert out == {"lines": [{"product_id": "p1", "name": "Amul Taaza Milk 500 ml", "qty": 2, "price": 29.0,
                              "out_of_stock": False},
                             {"product_id": "7", "name": "Bread", "qty": 1, "price": 45.5, "out_of_stock": True}],
                   "subtotal": 412.0, "total": 427.0, "fees": {"handling": 15.0}}


def test_extract_cart_empty_and_missing():
    fm = RECIPE["read"]["field_map"] | {"out_of_stock": None, "fees": {}}
    assert cart_recipe.extract_cart({}, fm) == {"lines": [], "subtotal": None, "total": None, "fees": {}}
    assert cart_recipe.extract_cart({"cart": {"items": [{"pid": "a", "count": 1, "oos": True}]}}, fm)["lines"] == \
        [{"product_id": "a", "name": None, "qty": 1, "price": None, "out_of_stock": False}]


# --- policy_for / narrow ---------------------------------------------------------------------

def test_policy_for_merges_search_read_posts():
    search = {"kind": "search", "read_post_allowlist": [{"url_regex": r"^https://www\.zepto\.com/api/v1/search$",
                                                         "why": "search"}]}
    p = cart_recipe.policy_for(RECIPE, search)
    assert isinstance(p, CartPolicy)
    assert p.cart == (ADD_ENTRY | {"qty_path": "item.quantity"},) and p.location == (LOC_ENTRY,)
    assert p.read == (READ_ENTRY, search["read_post_allowlist"][0])
    assert cart_recipe.policy_for(RECIPE).read == (READ_ENTRY,)
    assert decide_cart("POST", "https://www.zepto.com/api/v1/search", False, "{}", p) == (True, "read POST")
    assert decide_cart("POST", "https://www.zepto.com/api/v1/cart/add", False, '{"item":{"quantity":1}}', p) == \
        (True, "cart add")
    # an add without a quantity at qty_path is refused
    assert decide_cart("POST", "https://www.zepto.com/api/v1/cart/add", False, "{}", p) == \
        (False, "forbidden: cart_removal")
    # entries are copies: changing the policy's dicts can't change the recipe
    p.cart[0]["url_regex"] = ".*"
    assert RECIPE["cart_allowlist"][0]["url_regex"] == ADD_ENTRY["url_regex"] != ".*"


@pytest.mark.parametrize("regex,ok", [
    (r"^https://www\.zepto\.com/api/v1/cart/add$", True),
    (r"^https://www\.zepto\.com/api/v1/cart/(add|update)$", True),
    (r"^https://www\.zepto\.com/api/v1/cart/add", False),          # no $
    (r"https://www\.zepto\.com/api/v1/cart/add$", False),          # no ^https://
    (r"^http://www\.zepto\.com/api/v1/cart/add$", False),
    (r"^https://www\.zepto\.com/api/.*$", False),
    (r"^https://www\.zepto\.com/api/.+$", False),
    (r"^https://www\.zepto\.com/[^?]*$", False),                    # matches https://host/x
    (r"^https://www\.zepto\.com/\w*$", False),
    (r"^https://www\.zepto\.com/$", False),                        # matches https://host/
    (r"^https://www\.zepto\.com/(", False),                        # invalid regex
])
def test_narrow(regex, ok):
    assert cart_recipe.narrow(regex, "www.zepto.com") is ok


# --- check_cart_recipe -----------------------------------------------------------------------

def test_valid_recipe_passes():
    assert cart_recipe.check_cart_recipe(RECIPE, "zepto") is None
    assert cart_recipe.check_cart_recipe(recipe(location__select=None, location_allowlist=[]), "zepto") is None


@pytest.mark.parametrize("key", ["kind", "site", "home_url", "add", "read", "location", "cart_allowlist",
                                 "location_allowlist", "read_post_allowlist"])
def test_check_reports_missing_keys(key):
    assert cart_recipe.check_cart_recipe(recipe(**{key: _DEL}), "zepto") == f"missing {key}"


@pytest.mark.parametrize("key", ["method", "url", "body", "product_id_path", "qty_path", "qty_mode"])
def test_check_reports_missing_add_keys(key):
    assert cart_recipe.check_cart_recipe(recipe(**{f"add__{key}": _DEL}), "zepto") == f"missing add.{key}"


@pytest.mark.parametrize("key", ["method", "url", "field_map"])
def test_check_reports_missing_read_keys(key):
    assert cart_recipe.check_cart_recipe(recipe(**{f"read__{key}": _DEL}), "zepto") == f"missing read.{key}"


def test_check_rejects_whole_cart_add():
    whole = {"items": [{"product_id": "p1", "quantity": 1}, {"product_id": "p2", "quantity": 3}]}
    r = recipe(add__body=whole, add__product_id_path="items.0.product_id", add__qty_path="items.0.quantity")
    assert cart_recipe.check_cart_recipe(r, "zepto") == "add request carries the whole cart"
    for body in ([{"sku": "a"}, {"sku": "b"}], {"variables": {"cart": [{"itemId": 1}, {"variantId": 2}]}},
                 json.dumps({"lines": [{"Product": 1}, {"product": 2}]})):
        assert cart_recipe.check_cart_recipe(recipe(add__body=body), "zepto") == "add request carries the whole cart"
    # one product in a list is fine
    one = recipe(add__body={"items": [{"product_id": "p1", "quantity": 1}]},
                 add__product_id_path="items.0.product_id", add__qty_path="items.0.quantity",
                 cart_allowlist=[ADD_ENTRY | {"qty_path": "items.0.quantity"}])
    assert cart_recipe.check_cart_recipe(one, "zepto") is None


def test_check_rejects_forbidden_add_and_broad_regex():
    bad = recipe(add__url="https://www.zepto.com/api/checkout/add",
                 cart_allowlist=[{"url_regex": r"^https://www\.zepto\.com/api/checkout/add$", "why": "x", "op": "cart"}])
    assert cart_recipe.check_cart_recipe(bad, "zepto") == "add endpoint is checkout"
    assert cart_recipe.check_cart_recipe(recipe(add__url="https://www.zepto.com/api/cart/remove-item"), "zepto") == \
        "add endpoint is cart_removal"
    assert cart_recipe.check_cart_recipe(recipe(add__body={"operationName": "ApplyCoupon", "product_id": "x"}),
                                         "zepto") == "add endpoint is coupon"
    assert cart_recipe.check_cart_recipe(recipe(add__body={"item": {"product_id": "x", "quantity": 0}}), "zepto") == \
        "add endpoint is cart_removal"

    assert cart_recipe.check_cart_recipe(recipe(add__qty_mode="replace"), "zepto") == "unknown qty_mode"
    assert cart_recipe.check_cart_recipe(recipe(cart_allowlist=[]), "zepto") == \
        "add endpoint is not on the cart allowlist"
    # a location or read entry doesn't stand in for a cart entry
    assert cart_recipe.check_cart_recipe(recipe(cart_allowlist=[], location_allowlist=[LOC_ENTRY, ADD_ENTRY | {"op": "location"}],
                                                read_post_allowlist=[READ_ENTRY, ADD_ENTRY]), "zepto") == \
        "add endpoint is not on the cart allowlist"
    broad = r"^https://www\.zepto\.com/api/.*$"
    assert cart_recipe.check_cart_recipe(recipe(cart_allowlist=[ADD_ENTRY, {"url_regex": broad, "why": "x",
                                                                           "op": "cart"}]), "zepto") == \
        f"allowlist regex is too broad: {broad}"
    assert cart_recipe.check_cart_recipe(recipe(read_post_allowlist=[{"url_regex": r"^https://www\.zepto\.com/\w+$",
                                                                      "why": "x"}]), "zepto") == \
        r"allowlist regex is too broad: ^https://www\.zepto\.com/\w+$"
    assert cart_recipe.check_cart_recipe(recipe(cart_allowlist=[{"url_regex": r"https://www\.zepto\.com/api/v1/cart/add",
                                                                 "why": "x", "op": "cart"}]), "zepto") == \
        r"allowlist regex is too broad: https://www\.zepto\.com/api/v1/cart/add"


def test_check_mutation_add_needs_operation_on_entry():
    gql = {"operationName": "AddToCart", "query": "mutation AddToCart($id: ID!) { add(id: $id) }",
           "variables": {"id": "seed", "qty": 1}}
    r = recipe(add__body=gql, add__product_id_path="variables.id", add__qty_path="variables.qty")
    assert cart_recipe.check_cart_recipe(r, "zepto") == "add endpoint is not on the cart allowlist"
    r["cart_allowlist"] = [ADD_ENTRY | {"operation": "AddToCart", "qty_path": "variables.qty"}]
    assert cart_recipe.check_cart_recipe(r, "zepto") is None


def test_check_requires_location_read():
    msg = "cart recipe can't read the app's location"
    assert cart_recipe.check_cart_recipe(recipe(location__current=_DEL), "zepto") == msg
    assert cart_recipe.check_cart_recipe(recipe(location__current=None), "zepto") == msg
    assert cart_recipe.check_cart_recipe(recipe(location__current__store_id_path=None,
                                                location__current__lat_path=None), "zepto") == msg
    assert cart_recipe.check_cart_recipe(recipe(location__current__store_id_path=None,
                                                location__current__lon_path=None), "zepto") == msg
    # either a store id or both lat and lon is enough
    assert cart_recipe.check_cart_recipe(recipe(location__current__store_id_path=None), "zepto") is None
    assert cart_recipe.check_cart_recipe(recipe(location__current__lat_path=None,
                                                location__current__lon_path=None), "zepto") is None


def test_check_location_select_on_location_allowlist():
    msg = "location select is not on the location allowlist"
    assert cart_recipe.check_cart_recipe(recipe(location_allowlist=[]), "zepto") == msg
    # a cart entry does not stand in for a location entry
    assert cart_recipe.check_cart_recipe(recipe(location_allowlist=[],
                                                cart_allowlist=[ADD_ENTRY, LOC_ENTRY | {"op": "cart"}]), "zepto") == msg
    # selecting an address by writing one is forbidden even when allowlisted
    w = recipe(location__select__url="https://www.zepto.com/api/v1/update-address",
               location_allowlist=[{"url_regex": r"^https://www\.zepto\.com/api/v1/update-address$", "why": "x",
                                    "op": "location"}])
    assert cart_recipe.check_cart_recipe(w, "zepto") == msg


def test_check_rejects_other_site_host():
    other = {"url_regex": r"^https://api\.blinkit\.com/v1/cart/add$", "why": "x", "op": "cart"}
    assert cart_recipe.check_cart_recipe(recipe(cart_allowlist=[ADD_ENTRY, other]), "zepto") == \
        "allowlist entry for another site"
    evil = {"url_regex": r"^https://www\.zepto\.com\.evil\.io/cart/add$", "why": "x"}
    assert cart_recipe.check_cart_recipe(recipe(read_post_allowlist=[evil]), "zepto") == \
        "allowlist entry for another site"
    wild = {"url_regex": r"^https://[a-z]+\.zepto\.com/v1/cart/add$", "why": "x", "op": "location"}
    assert cart_recipe.check_cart_recipe(recipe(location_allowlist=[LOC_ENTRY, wild]), "zepto") == \
        "allowlist entry for another site"
    # a subdomain of the site is the site
    sub = {"url_regex": r"^https://api\.zepto\.com/v1/cart/view$", "why": "x"}
    assert cart_recipe.check_cart_recipe(recipe(read_post_allowlist=[READ_ENTRY, sub]), "zepto") is None


def test_fake_host_only_with_worker_fake(monkeypatch):
    fake = recipe(home_url="https://fake.zepto.test/", add__url="https://fake.zepto.test/api/cart/add",
                  cart_allowlist=[{"url_regex": r"^https://fake\.zepto\.test/api/cart/add$", "why": "x", "op": "cart",
                                   "qty_path": "item.quantity"}],
                  location__select=None, location_allowlist=[], read_post_allowlist=[], read__method="GET")
    assert cart_recipe.check_cart_recipe(fake, "zepto") == "allowlist entry for another site"
    monkeypatch.setenv("WORKER_FAKE", "1")
    assert cart_recipe.check_cart_recipe(fake, "zepto") is None
    # still only this site's fake host
    assert cart_recipe.check_cart_recipe(fake, "blinkit") == "allowlist entry for another site"


# --- review fix round 1 ------------------------------------------------------------------------

@pytest.mark.parametrize("regex", [
    r"^https://www\.zepto\.com/api/v1/cart/add$|^https://evil\.com/a/b$",
    r"^https://www\.zepto\.com/api/v1/cart/add$|/api/[a-z/]+$",
    r"^https://www\.zepto\.com/api/v1/cart/(add|view)$|x",
    r"^https://www\.zepto\.com/api/v1/cart/add\$",                    # an escaped $ is not an anchor
    r"^https://www\.zepto\.com/api/v1/cart/[^?]{1,}$",
    r"^https://www\.zepto\.com/api/v1/cart/[^?]{0,60}$",
    r"^https://www\.zepto\.com/api/v1/cart/\S+$",
    r"^https://www\.zepto\.com/api/v1/cart/[\s\S]+$",
    r"^https://www\.zepto\.com/api/v1/(?:[a-z]+/?)+$",
    r"^https://www\.zepto\.com/[a-z]+/[a-z]+$",
    r"^https://www\.zepto\.com/[a-z0-9]+$",
    r"^https://www\.zepto\.com/[\w/?=&]+$",
])
def test_narrow_rejects_alternation_catch_alls_and_random_paths(regex):
    assert cart_recipe.narrow(regex, "www.zepto.com") is False
    r = recipe(cart_allowlist=[ADD_ENTRY, {"url_regex": regex, "why": "x", "op": "cart"}])
    assert cart_recipe.check_cart_recipe(r, "zepto") == f"allowlist regex is too broad: {regex}"


@pytest.mark.parametrize("regex", [
    r"^https://www\.zepto\.com/api/v1/cart/add$",
    r"^https://www\.zepto\.com/api/v1/cart/(add|update)$",            # a group's alternation stays narrow
    r"^https://www\.zepto\.com/api/v1/cart/[\w-]{1,40}$",             # one id segment
    r"^https://www\.zepto\.com/api/v1/cart/[^/?]{1,30}$",             # a short bounded wildcard in one segment
    r"^https://www\.zepto\.com/api/v1/search\?q=[\w%+-]{1,40}$",
])
def test_narrow_keeps_specific_regexes(regex):
    assert cart_recipe.narrow(regex, "www.zepto.com") is True


def test_check_probes_known_urls_on_a_foreign_host():
    """A regex that matches the add URL must not also match the same path on another host."""
    sneaky = r"^https://www\.zepto\.com/api/v1/cart/add$"
    assert cart_recipe.check_cart_recipe(recipe(cart_allowlist=[{"url_regex": sneaky, "why": "x", "op": "cart",
                                                                 "qty_path": "item.quantity"}]),
                                         "zepto") is None
    # hosts written as a group or class are never literal, so they are another site
    for regex in (r"^https://(?:www\.zepto\.com|evil\.com)/api/v1/cart/add$",
                  r"^https://www\.zepto\.co[m]/api/v1/cart/add$"):
        r = recipe(cart_allowlist=[ADD_ENTRY, {"url_regex": regex, "why": "x", "op": "cart"}])
        assert cart_recipe.check_cart_recipe(r, "zepto") == "allowlist entry for another site"


@pytest.mark.parametrize("regex", [
    r"^https://www\.zepto\.com/api/v1/cart/(?:[^?]{0,40}){0,40}$",
    r"^https://www\.zepto\.com/api/v1/cart/(?:(?:[^?]{0,10}){0,5}){0,5}$",
    r"^https://www\.zepto\.com/api/v1/cart/(?:\S{0,20}/){0,3}$",
])
def test_narrow_multiplies_nested_repeat_bounds(regex):
    assert cart_recipe.narrow(regex, "www.zepto.com") is False


def test_narrow_keeps_small_nested_bounds():
    assert cart_recipe.narrow(r"^https://www\.zepto\.com/api/v1/cart/(?:[^/?]{0,10}){0,4}$", "www.zepto.com") is True


@pytest.mark.parametrize("regex", [r"^https://www.zepto.com/api/v1/cart/add$", r"^https://www\.zepto.com/api/v1/cart/add$",
                                   r"^https://api.zepto\.com/v1/cart/view$"])
def test_unescaped_dot_in_host_is_another_site(regex):
    assert cart_guard.regex_host(regex) is None
    r = recipe(read_post_allowlist=[READ_ENTRY, {"url_regex": regex, "why": "x"}])
    assert cart_recipe.check_cart_recipe(r, "zepto") == "allowlist entry for another site"
    g = cart_guard.CartGuard(CartPolicy(), site="zepto")
    with pytest.raises(ValueError, match="another site"):
        g.allow("read", [{"url_regex": regex, "why": "x"}])


def test_policy_for_carries_qty_path_and_check_uses_it():
    p = cart_recipe.policy_for(RECIPE)
    assert all(e["qty_path"] == "item.quantity" for e in p.cart)
    url = "https://www.zepto.com/api/v1/cart/add"
    assert decide_cart("POST", url, False, '{"item":{"product_id":"p","quantity":2}}', p) == (True, "cart add")
    assert decide_cart("POST", url, False, '{"item":{"product_id":"p","quantity":null}}', p) == \
        (False, "forbidden: cart_removal")
    # the recorded add template itself must carry a quantity of at least 1
    assert cart_recipe.check_cart_recipe(recipe(add__body={"item": {"product_id": "x", "quantity": None},
                                                           "store": {"id": "S0"}}), "zepto") == \
        "add endpoint is cart_removal"


def test_policy_for_marks_the_exact_add_entry_binary_when_declared():
    plain = cart_recipe.policy_for(RECIPE)
    assert not any(e.get("binary") for e in plain.cart)
    other = {"url_regex": r"^https://www\.zepto\.com/api/v1/cart/bulk$", "why": "x", "op": "cart"}
    b = recipe(add__body_encoding="binary", cart_allowlist=[ADD_ENTRY, other])
    p = cart_recipe.policy_for(b)
    assert [e.get("binary", False) for e in p.cart] == [True, False]
    assert decide_cart("POST", "https://www.zepto.com/api/v1/cart/add", False, None, p, binary=True) == \
        (True, "cart add")
    assert decide_cart("POST", "https://www.zepto.com/api/v1/cart/bulk", False, None, p, binary=True) == \
        (False, "binary body")


# --- review fix round 3 ------------------------------------------------------------------------

@pytest.mark.parametrize("regex", [
    r"^https://\S\S\S\.zepto\.com/api/v1/cart/add$",
    r"^https://\w\w\w\.zepto\.com/api/v1/cart/add$",
    r"^https://www\.zepto\.co\w/api/v1/cart/add$",
    r"^https://www\.zepto\.com\S/api/v1/cart/add$",
    r"^https://www\.zep\x74o\.com/api/v1/cart/add$",
    r"^https://www\.zepto\.com\\/api/v1/cart/add$",
])
def test_escape_classes_in_the_host_are_another_site(regex):
    assert cart_guard.regex_host(regex) is None
    r = recipe(read_post_allowlist=[READ_ENTRY, {"url_regex": regex, "why": "x"}])
    assert cart_recipe.check_cart_recipe(r, "zepto") == "allowlist entry for another site"
    with pytest.raises(ValueError, match="another site"):
        cart_guard.CartGuard(CartPolicy(), site="zepto").allow("read", [{"url_regex": regex, "why": "x"}])


def test_escape_class_probes_match_foreign_hosts():
    """The two bypasses as found: both regexes do match a host that isn't the site's."""
    assert cart_guard.matches(r"^https://\S\S\S\.zepto\.com/a$", "https://a/b.zepto.com/a")   # host "a"
    assert cart_guard.matches(r"^https://\w\w\w\.zepto\.com/a$", "https://pay.zepto.com/a")
    for regex in (r"^https://\S\S\S\.zepto\.com/a$", r"^https://\w\w\w\.zepto\.com/a$"):
        assert cart_recipe.check_cart_recipe(recipe(read_post_allowlist=[{"url_regex": regex, "why": "x"}]),
                                             "zepto") == "allowlist entry for another site"


@pytest.mark.parametrize("host_regex", [r"www\.zepto\.com", r"api-v2\.zepto\.com", r"api\-v2\.zepto\.com"])
def test_dot_and_dash_escapes_are_the_only_ones_allowed(host_regex):
    assert cart_guard.regex_host(f"^https://{host_regex}/api/x$") is not None


@pytest.mark.parametrize("regex", [
    r"^https://www\.zepto\.com/api/v1/cart/[^?]{0,30}x[^?]{0,30}$",            # 60 in total
    r"^https://www\.zepto\.com/api/v1/[^?]{0,20}/[^?]{0,20}/[^?]{0,20}$",
])
def test_narrow_sums_concatenated_wildcards(regex):
    assert cart_recipe.narrow(regex, "www.zepto.com") is False


def test_narrow_keeps_a_total_within_the_cap():
    assert cart_recipe.narrow(r"^https://www\.zepto\.com/api/v1/cart/[^/?]{0,20}x[^/?]{0,20}$", "www.zepto.com")


# --- review fix round 4: structural wildcard width -----------------------------------------------

@pytest.mark.parametrize("regex", [
    r"^https://www\.zepto\.com/api/(?:[^?][^?/][^?]){0,40}$",               # 3-char item: 60 slashes fit
    r"^https://www\.zepto\.com/api/(?:ab[^?]){0,40}$",
    r"^https://www\.zepto\.com/api/(?:a[^?]){0,21}$",                       # 42
    r"^https://www\.zepto\.com/api/(?:a/){0,21}$",                          # a literal slash inside a repeat
    r"^https://www\.zepto\.com/api/(?:(?:ab[^?]){0,5}x){0,5}$",             # nested
    r"^https://www\.zepto\.com/api/(?:(?:ab[^?]){0,3}(?:cd[^?]){0,3}){0,3}$",
    r"^https://www\.zepto\.com/api/(?:(?:a|[^?])bc){0,15}$",                # alternation inside a repeat
    r"^https://www\.zepto\.com/api/(?:abc|[^?]{0,41})$",                    # alternation takes the max
    r"^https://www\.zepto\.com/api/(?:x(?:[^/?]{2}|\S){0,7}){0,7}$",
    r"^https://www\.zepto\.com/api/(?:[a-z]{3}\?){0,14}$",
    r"^https://www\.zepto\.com/api/(?:[.-0]ab){0,14}$",                     # a range that includes '/'
    r"^https://www\.zepto\.com/api/(?:[^a]bc){0,14}$",
    r"^https://www\.zepto\.com/api/(?:\W..){0,14}$",
    r"^https://www\.zepto\.com/api/(?:ab[^?]){0,10}x(?:ab[^?]){0,10}$",     # 60 summed
    r"^https://www\.zepto\.com/api/(x)(?:\1[^?]){0,30}$",                   # backreferences are refused
])
def test_narrow_counts_the_full_width_of_crossing_repeats(regex):
    assert cart_recipe.narrow(regex, "www.zepto.com") is False


def test_round_4_probe_really_spans_segments():
    path = "/a/" * 30
    assert len(path) == 90 and path.count("/") == 60
    assert cart_guard.matches(r"^https://www\.zepto\.com/api/(?:[^?][^?/][^?]){0,40}$",
                              "https://www.zepto.com/api/" + path)


@pytest.mark.parametrize("regex", [
    r"^https://www\.zepto\.com/api/(?:[^/?]{3}-){0,10}$",                   # a repeat confined to one segment
    r"^https://www\.zepto\.com/api/(?:ab[^/?]){0,13}$",                     # 39
    r"^https://www\.zepto\.com/api/(?:abc|[^/?]{0,40})$",
    r"^https://www\.zepto\.com/api/v1/cart/item/[\w-]{1,40}$",
])
def test_narrow_keeps_crossing_width_within_the_cap(regex):
    assert cart_recipe.narrow(regex, "www.zepto.com") is True


# --- review fix round 5 ------------------------------------------------------------------------

THREE_SEGMENTS = r"^https://www\.zepto\.com/api/[\w-]{1,20}/[\w-]{1,20}/[\w-]{1,20}$"


@pytest.mark.parametrize("path", ["/api/v1/orders/new", "/api/v2/order/initiate", "/api/v1/cart/confirm",
                                  "/api/v1/address/set-default"])
def test_round_5_three_segment_bypass_is_refused(path):
    """The reviewer's probe: the regex really matches each dangerous endpoint, and narrow refuses it."""
    assert cart_guard.matches(THREE_SEGMENTS, "https://www.zepto.com" + path)
    assert cart_recipe.narrow(THREE_SEGMENTS, "www.zepto.com") is False
    r = recipe(cart_allowlist=[ADD_ENTRY, {"url_regex": THREE_SEGMENTS, "why": "x", "op": "cart",
                                           "qty_path": "item.quantity"}])
    assert cart_recipe.check_cart_recipe(r, "zepto") == f"allowlist regex is too broad: {THREE_SEGMENTS}"
    with pytest.raises(ValueError, match="too broad"):
        cart_guard.CartGuard(CartPolicy(), site="zepto").allow("read", [{"url_regex": THREE_SEGMENTS, "why": "x"}])


@pytest.mark.parametrize("regex", [
    r"^https://www\.zepto\.com/api/[\w-]{1,20}/[\w-]{1,20}/x$",
    r"^https://www\.zepto\.com/api/[^/]{1,10}/[^/]{1,10}$",
    r"^https://www\.zepto\.com/api/(?:v1|v2)/cart/[\w-]{1,10}$",            # an alternation is non-literal too
    r"^https://www\.zepto\.com/api/v\d/cart/[\w-]{1,10}$",                 # so is a class
    r"^https://www\.zepto\.com/api/v1/cart/[\w-]{1,41}$",                  # the one segment is wider than 40
    r"^https://www\.zepto\.com/api/v1/cart/[\w-]+$",
    r"^https://www\.zepto\.com/api/v1/cart/[^?]{1,30}$",                   # can match '/': spans segments
    r"^https://www\.zepto\.com/api/v1/cart/(?:add|x/y)$",                  # a '/' inside a group
    r"^https://www\.zepto\.com/[\w-]{1,10}/v1/cart/[\w-]{1,10}$",          # two, apart
])
def test_round_5_at_most_one_non_literal_segment(regex):
    assert cart_recipe.narrow(regex, "www.zepto.com") is False


@pytest.mark.parametrize("regex", [
    r"^https://www\.zepto\.com/api/v1/cart/[\w-]{1,24}$",
    r"^https://www\.zepto\.com/api/v1/cart/add$",
    r"^https://www\.zepto\.com/api/v1/cart/(add|update)$",
    r"^https://www\.zepto\.com/api/v1/cart/(?:add)/x$",                    # a literal-only group is literal
    r"^https://www\.zepto\.com/api/v1/cart/[\w-]{1,10}/add$",              # the one segment may be in the middle
    r"^https://www\.zepto\.com/api/v1/search\?q=[\w%+-]{1,40}$",
])
def test_round_5_one_non_literal_segment_is_allowed(regex):
    assert cart_recipe.narrow(regex, "www.zepto.com") is True


def test_round_5_random_probes_start_at_the_literal_prefix():
    probes = cart_guard._random_paths(r"^https://www\.zepto\.com/api/v1/cart/[\w-]{1,24}$")
    deeper = [p for p in probes if p.startswith("/api/v1/cart/")]
    depths = sorted(p.removeprefix("/api/v1/cart/").count("/") + 1 for p in deeper)
    assert depths == [2, 3]
    assert any(p.startswith("/api/v1/") and not p.startswith("/api/v1/cart/") and p.count("/") == 3 for p in probes)
    # root probes are still there
    assert any(not p.startswith("/api/") for p in probes)
    # a regex that matches the prefix plus two more segments is caught by the probes on their own
    two = r"^https://www\.zepto\.com/api/v1/cart/[a-z]{9}/[a-z]{9}$"
    assert any(cart_guard.matches(two, "https://www.zepto.com" + p) for p in cart_guard._random_paths(two))


@pytest.mark.parametrize("entry", [{k: v for k, v in ADD_ENTRY.items() if k != "qty_path"},
                                   ADD_ENTRY | {"qty_path": ""}, ADD_ENTRY | {"qty_path": None}])
def test_round_5_cart_entries_must_carry_qty_path(entry):
    assert cart_recipe.check_cart_recipe(recipe(cart_allowlist=[entry]), "zepto") == \
        "cart allowlist entry has no qty_path"
    other = {"url_regex": r"^https://www\.zepto\.com/api/v1/cart/bulk$", "why": "x", "op": "cart"}
    assert cart_recipe.check_cart_recipe(recipe(cart_allowlist=[ADD_ENTRY, other]), "zepto") == \
        "cart allowlist entry has no qty_path"


def test_round_5_policy_keeps_each_entry_qty_path():
    bulk = {"url_regex": r"^https://www\.zepto\.com/api/v1/cart/bulk$", "why": "x", "op": "cart", "qty_path": "n"}
    p = cart_recipe.policy_for(recipe(cart_allowlist=[ADD_ENTRY, bulk]))
    assert [e["qty_path"] for e in p.cart] == ["item.quantity", "n"]
    assert cart_recipe.check_cart_recipe(recipe(cart_allowlist=[ADD_ENTRY, bulk]), "zepto") is None

"""Cart-mode guard (Plan 6 R1): forbidden categories beat every allowlist; the cart click policy."""
import contextlib
import logging
import re
from types import SimpleNamespace

import pytest

import agent
import browser
import cart_guard
import guard as read_guard
from cart_guard import (ADDRESS_PAGE, FORBIDDEN, REMOVE_BODY, CartGuard, CartPolicy, decide_cart,
                        forbidden_category, operation_name)

SHOP = "https://shop.test"
BROAD = CartPolicy(cart=({"url_regex": r"^https://shop\.test/.*$", "why": "x", "op": "cart"},))   # forbidden must still win
ADD = CartPolicy(cart=({"url_regex": r"^https://shop\.test/api/cart/add$", "why": "add", "op": "cart"},),
                 location=({"url_regex": r"^https://shop\.test/api/location/select$", "why": "loc", "op": "location"},),
                 read=({"url_regex": r"^https://shop\.test/api/cart/view$", "why": "read"},))
# Everything allowlisted at once, with every operation name: nothing here may unlock a forbidden request.
EVERYTHING = CartPolicy(
    cart=({"url_regex": r".*", "why": "x", "op": "cart"},),
    location=({"url_regex": r".*", "why": "x", "op": "location"},),
    read=({"url_regex": r".*", "why": "x"},))

FORBIDDEN_URLS = [("checkout", "/checkout"), ("checkout", "/api/v2/check-out/init"), ("payment", "/api/pay/init"),
    ("payment", "/razorpay/order"), ("payment", "/api/upi/collect"), ("order_placement", "/api/place-order"),
    ("order_placement", "/v1/confirm_order"), ("address_write", "/api/address/add"), ("address_write", "/api/update-address"),
    ("coupon", "/api/coupon/apply"), ("wallet", "/api/swiggy-money/use"), ("cart_removal", "/api/clear-cart"),
    ("cart_removal", "/api/remove_item")]

# Exhaustive table: at least one path per alternative of every FORBIDDEN pattern.
FORBIDDEN_TABLE = [
    ("checkout", "/checkout"), ("checkout", "/CheckOut/start"), ("checkout", "/api/check-out"),
    ("checkout", "/cart?next=checkout"),
    ("payment", "/payment/options"), ("payment", "/api/pay"), ("payment", "/pay/init"), ("payment", "/api/v1/pay?x=1"),
    ("payment", "/paynow"), ("payment", "/pay-now"), ("payment", "/razorpay/x"), ("payment", "/juspay/session"),
    ("payment", "/cashfree/order"), ("payment", "/upi/intent"), ("payment", "/api/UPI"),
    ("order_placement", "/place-order"), ("order_placement", "/placeorder"), ("order_placement", "/api/PlaceOrder"),
    ("order_placement", "/order/create"), ("order_placement", "/order/confirm"), ("order_placement", "/order/place"),
    ("order_placement", "/order/submit"), ("order_placement", "/createorder"), ("order_placement", "/create-order"),
    ("order_placement", "/create_order"), ("order_placement", "/confirmOrder"), ("order_placement", "/submit_order"),
    ("address_write", "/add-address"), ("address_write", "/createaddress"), ("address_write", "/new_address"),
    ("address_write", "/saveAddress"), ("address_write", "/update-address"), ("address_write", "/edit_address"),
    ("address_write", "/delete-address"), ("address_write", "/removeAddress"), ("address_write", "/add-user-address"),
    ("address_write", "/save_user_address"), ("address_write", "/addresses/add"), ("address_write", "/address/create"),
    ("address_write", "/address/new"), ("address_write", "/address/save"), ("address_write", "/addresses/update"),
    ("address_write", "/address/edit"), ("address_write", "/address/delete"), ("address_write", "/addresses/remove"),
    ("coupon", "/coupons"), ("coupon", "/api/promo/list"), ("coupon", "/promocode"), ("coupon", "/voucher/redeem"),
    ("coupon", "/apply-offer"), ("coupon", "/applyoffer"), ("coupon", "/offer/apply"), ("coupon", "/offers/apply"),
    ("wallet", "/wallet"), ("wallet", "/gift-card"), ("wallet", "/giftcard/balance"), ("wallet", "/swiggy_money"),
    ("wallet", "/money"), ("wallet", "/cashback/claim"), ("wallet", "/super-cash"), ("wallet", "/supercash/use"),
    ("cart_removal", "/remove-cart"), ("cart_removal", "/delete_item"), ("cart_removal", "/clearcart"),
    ("cart_removal", "/empty-cart"), ("cart_removal", "/reset_cart"), ("cart_removal", "/decrement-item"),
    ("cart_removal", "/remove-from-cart"), ("cart_removal", "/remove_from_items"), ("cart_removal", "/deleteproducts"),
    ("cart_removal", "/remove/item"), ("cart_removal", "/api/remove/products"),
    ("cart_removal", "/cart/remove"), ("cart_removal", "/cart/delete"), ("cart_removal", "/cart/clear"),
    ("cart_removal", "/cart/empty"), ("cart_removal", "/cart/reset"), ("cart_removal", "/cart/decrement"),
]

# The same intent sent as a GraphQL operation name on an allowlisted URL.
FORBIDDEN_OPERATIONS = [
    ("checkout", "StartCheckout"), ("payment", "InitPayment"), ("payment", "PayNow"), ("payment", "RazorpayOrder"),
    ("order_placement", "PlaceOrder"), ("order_placement", "createOrder"), ("order_placement", "ConfirmOrder"),
    ("address_write", "AddAddress"), ("address_write", "UpdateUserAddress"), ("address_write", "deleteAddress"),
    ("coupon", "ApplyCoupon"), ("coupon", "applyPromo"), ("coupon", "RedeemVoucher"),
    ("wallet", "UseWallet"), ("wallet", "ApplySuperCash"), ("wallet", "giftCardRedeem"),
    ("cart_removal", "RemoveFromCart"), ("cart_removal", "removeItem"), ("cart_removal", "ClearCart"),
    ("cart_removal", "EmptyCart"), ("cart_removal", "decrementItem"), ("cart_removal", "DeleteProducts"),
]

REMOVE_BODIES = [
    '{"product_id":"p1","quantity":0}', '{"product_id":"p1","qty":0}', '{"count":0}', '{"itemCount":0}',
    '{"item_count":0}', '{"quantity":"0"}', '{"qty":-1}', '{"quantity":-3}', '{"items":[{"id":"p1","qty":-1}]}',
    '{"items":[{"id":"p1","quantity":0}]}', '{"quantity" : 0 }', '{"a":[{"qty":0}]}',
    '{"action":"REMOVE_ITEM","id":"p1"}', '{"op":"delete"}', '{"operation":"clearCart"}', '{"type":"EMPTY_CART"}',
    '{"event":"decrement"}', '{"action":"reset"}', '{"operationName":"cartDecrement"}',
]

SAFE_BODIES = ['{"product_id":"p1","quantity":1}', '{"product_id":"p1","quantity":10}', '{"qty":"2"}',
               '{"count":20}', '{"action":"ADD_ITEM","id":"p1"}', '{"type":"increment"}']


# --- decide_cart: forbidden categories ---------------------------------------------------------

@pytest.mark.parametrize("category,path", FORBIDDEN_URLS)
@pytest.mark.parametrize("method,is_nav", [("GET", True), ("POST", False)])
def test_forbidden_categories_blocked_even_when_allowlisted(category, path, method, is_nav):
    assert decide_cart(method, SHOP + path, is_nav, "{}", BROAD) == (False, f"forbidden: {category}")


def test_forbidden_table_covers_every_category():
    assert set(FORBIDDEN) == {"checkout", "payment", "order_placement", "address_write", "coupon", "wallet",
                              "cart_removal"}
    assert {c for c, _ in FORBIDDEN_TABLE} == set(FORBIDDEN)
    assert {c for c, _ in FORBIDDEN_OPERATIONS} == set(FORBIDDEN)
    assert all(p.flags & re.I for p in FORBIDDEN.values())


@pytest.mark.parametrize("category,path", FORBIDDEN_TABLE)
@pytest.mark.parametrize("method,is_nav", [("GET", True), ("GET", False), ("HEAD", False), ("OPTIONS", False),
                                           ("POST", False), ("POST", True)])
@pytest.mark.parametrize("policy", [BROAD, ADD, EVERYTHING], ids=["broad", "add", "everything"])
def test_forbidden_table_wins_over_every_allowlist(category, path, method, is_nav, policy):
    assert forbidden_category(SHOP + path, None, is_nav) == category
    assert decide_cart(method, SHOP + path, is_nav, None, policy) == (False, f"forbidden: {category}")


@pytest.mark.parametrize("category,path", FORBIDDEN_TABLE)
def test_forbidden_in_query_string_too(category, path):
    url = f"{SHOP}/api/cart/add?next={path.lstrip('/').replace('?', '&')}"
    assert decide_cart("POST", url, False, "{}", EVERYTHING) == (False, f"forbidden: {category}")


@pytest.mark.parametrize("category,path", [("checkout", "/check%2Dout"), ("payment", "/%70ay/init")])
def test_percent_encoded_paths_are_decoded(category, path):
    assert decide_cart("POST", SHOP + path, False, "{}", EVERYTHING) == (False, f"forbidden: {category}")


def test_host_alone_is_not_searched():
    """Only the path and query are searched: a host like pay.example is not a payment request by itself."""
    assert forbidden_category("https://paytm-cdn.example/img/a.png", None, False) is None
    assert decide_cart("GET", "https://checkout.example/img/a.png", False, None, ADD) == (True, "safe method")


@pytest.mark.parametrize("category,name", FORBIDDEN_OPERATIONS)
@pytest.mark.parametrize("policy", [ADD, EVERYTHING], ids=["add", "everything"])
def test_forbidden_operation_names_blocked_on_allowlisted_add(category, name, policy):
    body = f'{{"operationName":"{name}","variables":{{}},"query":"mutation {name} {{ x }}"}}'
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, policy) == (False, f"forbidden: {category}")
    # an approved entry with that exact operation does not help either
    op = CartPolicy(cart=({"url_regex": r"^https://shop\.test/api/cart/add$", "why": "x", "op": "cart",
                           "operation": name},))
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, op) == (False, f"forbidden: {category}")


@pytest.mark.parametrize("category,name", FORBIDDEN_OPERATIONS)
def test_forbidden_mutation_name_in_query_text_without_operation_name(category, name):
    body = f'{{"query":"mutation {name}($id: ID!) {{ x(id: $id) }}","variables":{{"id":"p1"}}}}'
    assert decide_cart("POST", SHOP + "/graphql", False, body, EVERYTHING) == (False, f"forbidden: {category}")


def test_forbidden_operation_in_batched_graphql():
    body = '[{"operationName":"AddToCart","query":"mutation AddToCart { a }"},' \
           '{"operationName":"PlaceOrder","query":"mutation PlaceOrder { b }"}]'
    assert decide_cart("POST", SHOP + "/graphql", False, body, EVERYTHING) == (False, "forbidden: order_placement")


@pytest.mark.parametrize("body,category", [
    ('{"operationName":"PlaceOrder","query":"mutation PlaceOrder{…}"}', "order_placement"),
    ('{"product_id":"p1","quantity":0}', "cart_removal"), ('{"items":[{"id":"p1","qty":-1}]}', "cart_removal"),
    ('{"action":"REMOVE_ITEM","id":"p1"}', "cart_removal"), ('{"operationName":"ApplyCoupon"}', "coupon")])
def test_forbidden_bodies_blocked_on_allowlisted_add(body, category):
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, ADD) == (False, f"forbidden: {category}")


@pytest.mark.parametrize("body", REMOVE_BODIES)
@pytest.mark.parametrize("policy", [ADD, EVERYTHING], ids=["add", "everything"])
def test_remove_bodies_blocked_on_every_allowlist(body, policy):
    assert REMOVE_BODY.search(body)
    for url in (SHOP + "/api/cart/add", SHOP + "/api/location/select", SHOP + "/api/cart/view"):
        assert decide_cart("POST", url, False, body, policy) == (False, "forbidden: cart_removal")


@pytest.mark.parametrize("body", SAFE_BODIES)
def test_add_bodies_that_raise_quantity_pass(body):
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, ADD) == (True, "cart add")


def test_forbidden_beats_challenge_post():
    url = "https://b67f.token.awswaf.com/b67f/verify"
    assert decide_cart("POST", url, False, '{"quantity":0}', ADD) == (False, "forbidden: cart_removal")
    assert decide_cart("POST", url + "?next=/checkout", False, "{}", ADD) == (False, "forbidden: checkout")


# --- decide_cart: methods, address pages, allowlists -------------------------------------------

@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_non_post_writes_never_allowed(method):
    assert decide_cart(method, SHOP + "/api/cart/add", False, '{"product_id":"p1","quantity":1}', EVERYTHING) == \
        (False, f"{method} never allowed")
    assert decide_cart(method.lower(), SHOP + "/api/cart/add", False, None, ADD) == (False, f"{method} never allowed")


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "PROPFIND"])
def test_unknown_methods_never_allowed(method):
    assert decide_cart(method, SHOP + "/", True, None, EVERYTHING) == (False, f"{method} never allowed")


def test_address_page_navigation_blocked_but_address_list_xhr_allowed():
    assert ADDRESS_PAGE.search("/my-account/addresses")
    for path in ("/my-account/addresses", "/account/address", "/address?select=1"):
        assert decide_cart("GET", SHOP + path, True, None, ADD) == (False, "forbidden: address_write")
    assert decide_cart("GET", SHOP + "/api/user/addresses", False, None, ADD) == (True, "safe method")
    assert decide_cart("GET", SHOP + "/api/v1/address/list?lat=22.3", False, None, ADD) == (True, "safe method")
    # a POST that reads the saved addresses still needs a read entry
    assert decide_cart("POST", SHOP + "/api/user/addresses", False, "{}", ADD) == (False, "POST not on cart allowlist")


def test_allowed_requests():
    assert decide_cart("POST", SHOP + "/api/cart/add", False, '{"product_id":"p1","quantity":2}', ADD) == (True, "cart add")
    assert decide_cart("POST", SHOP + "/api/location/select", False, '{"lat":22.33}', ADD) == (True, "location select")
    assert decide_cart("POST", SHOP + "/api/cart/view", False, "{}", ADD) == (True, "read POST")
    assert decide_cart("GET", SHOP + "/v1/display/banners", True, None, ADD)[0]     # "display" is not "pay"
    assert decide_cart("GET", SHOP + "/v1/display/banners", True, None, ADD) == (True, "safe method")
    assert decide_cart("HEAD", SHOP + "/", False, None, ADD) == (True, "safe method")
    assert decide_cart("post", SHOP + "/api/cart/add", False, "{}", ADD) == (True, "cart add")


@pytest.mark.parametrize("path", ["/v1/display/banners", "/api/harmony/product", "/api/cart", "/api/cart/view",
                                  "/api/upipe/x", "/api/display-offers", "/api/store/address-free"])
def test_lookalike_paths_are_not_forbidden(path):
    assert forbidden_category(SHOP + path, None, False) is None


def test_unlisted_post_blocked():
    assert decide_cart("POST", SHOP + "/api/cart/add2", False, "{}", ADD) == (False, "POST not on cart allowlist")
    assert decide_cart("POST", SHOP + "/analytics/beacon", False, "{}", ADD) == (False, "POST not on cart allowlist")
    assert decide_cart("POST", SHOP + "/api/cart/add", False, "{}", CartPolicy()) == (False, "POST not on cart allowlist")
    # a read entry never stands in for a cart entry the other way round
    assert decide_cart("POST", SHOP + "/api/cart/view", False, "{}", CartPolicy(cart=ADD.location)) == \
        (False, "POST not on cart allowlist")


def test_mutation_needs_matching_operation():
    body = '{"operationName":"AddToCart","query":"mutation AddToCart($id: ID!) { add(id: $id) }"}'
    other = '{"operationName":"SetPincode","query":"mutation SetPincode { x }"}'
    url = SHOP + "/graphql"
    cart = {"url_regex": r"^https://shop\.test/graphql$", "why": "add", "op": "cart"}
    loc = {"url_regex": r"^https://shop\.test/graphql$", "why": "loc", "op": "location"}
    read = {"url_regex": r"^https://shop\.test/graphql$", "why": "read"}
    # no operation on the entry: a mutation never passes
    assert decide_cart("POST", url, False, body, CartPolicy(cart=(cart,))) == (False, "POST not on cart allowlist")
    assert decide_cart("POST", url, False, body, CartPolicy(cart=(cart | {"operation": "AddToCart"},))) == \
        (True, "cart add")
    assert decide_cart("POST", url, False, other, CartPolicy(cart=(cart | {"operation": "AddToCart"},))) == \
        (False, "POST not on cart allowlist")
    assert decide_cart("POST", url, False, other, CartPolicy(location=(loc | {"operation": "SetPincode"},))) == \
        (True, "location select")
    # a read entry never passes a mutation, whatever its operation
    assert decide_cart("POST", url, False, body, CartPolicy(read=(read | {"operation": "AddToCart"},))) == \
        (False, "POST not on cart allowlist")
    # a query (not a mutation) passes the read entry
    q = '{"operationName":"Cart","query":"query Cart { cart { id } }"}'
    assert decide_cart("POST", url, False, q, CartPolicy(read=(read,))) == (True, "read POST")


def test_mutation_without_operation_name_uses_the_query_name():
    body = '{"query":"mutation AddToCart { add }"}'
    assert operation_name(body) == "AddToCart"
    entry = {"url_regex": r"^https://shop\.test/graphql$", "why": "x", "op": "cart", "operation": "AddToCart"}
    assert decide_cart("POST", SHOP + "/graphql", False, body, CartPolicy(cart=(entry,))) == (True, "cart add")
    # an anonymous mutation has no name, so it never matches an entry
    anon = '{"query":"mutation { add }"}'
    assert operation_name(anon) is None
    assert decide_cart("POST", SHOP + "/graphql", False, anon, CartPolicy(cart=(entry,))) == \
        (False, "POST not on cart allowlist")


def test_batched_mutations_never_match_one_operation():
    body = '[{"operationName":"AddToCart","query":"mutation AddToCart { a }"},' \
           '{"operationName":"AddToCart","query":"mutation AddToCart { b }"}]'
    entry = {"url_regex": r"^https://shop\.test/graphql$", "why": "x", "op": "cart", "operation": "AddToCart"}
    assert operation_name(body) is None
    assert decide_cart("POST", SHOP + "/graphql", False, body, CartPolicy(cart=(entry,))) == \
        (False, "POST not on cart allowlist")


def test_operation_name():
    assert operation_name('{"operationName":"Cart","query":"query Cart { a }"}') == "Cart"
    assert operation_name('{"query":"query Cart { a }"}') == "Cart"
    assert operation_name("{}") is None
    assert operation_name(None) is None
    assert operation_name("not json") is None
    assert operation_name('{"operationName": 5}') is None


def test_challenge_post_allowed_and_lookalike_blocked():
    for url in ("https://b67f7794189c.f957f42c.ap-south-1.token.awswaf.com/b67f7794189c/5504ea1b6187/219ce1/verify",
                "https://www.example.com/cdn-cgi/challenge-platform/h/g/flow/ov1"):
        assert decide_cart("POST", url, False, "{}", ADD) == (True, "bot-challenge token")
    assert decide_cart("POST", "https://token.awswaf.com.evil.com/x", False, "{}", ADD) == \
        (False, "POST not on cart allowlist")
    assert decide_cart("POST", SHOP + "/api/x/token.awswaf.com", False, "{}", ADD) == \
        (False, "POST not on cart allowlist")


def test_read_guard_unchanged():
    """R1: the read-only guard keeps blocking every cart path; the cart guard is a separate code path."""
    assert read_guard.decide("POST", SHOP + "/api/cart/add", False, "{}", False, list(ADD.cart)) == \
        (False, "POST to blocked path")


# --- CartGuard ---------------------------------------------------------------------------------

class FakeRoute:
    def __init__(self):
        self.result = None

    def continue_(self):
        self.result = "continue"

    def abort(self, why=None):
        self.result = f"abort:{why}"


def fake_request(method, url, body=None, nav=False, resource_type="xhr", headers=None):
    return SimpleNamespace(method=method, url=url, post_data=body, resource_type=resource_type,
                           is_navigation_request=lambda: nav, headers=headers or {})


def test_cart_guard_records_blocked_without_logging_body(caplog):
    g = CartGuard(ADD)
    caplog.set_level(logging.DEBUG)
    secret = '{"product_id":"p1","quantity":0,"address":"12 Shanti Society"}'
    r = FakeRoute()
    g._handle(r, fake_request("POST", SHOP + "/api/cart/add?token=abc", secret))
    assert r.result == "abort:blockedbyclient"
    assert g.blocked == [{"method": "POST", "url": SHOP + "/api/cart/add?token=abc", "body": secret,
                          "reason": "forbidden: cart_removal"}]
    r = FakeRoute()
    g._handle(r, fake_request("POST", SHOP + "/api/other", "x" * 5000))
    assert r.result == "abort:blockedbyclient" and len(g.blocked[-1]["body"]) == 4000
    text = caplog.text
    assert "Shanti" not in text and "quantity" not in text and "token=abc" not in text and "xxxx" not in text
    assert "forbidden: cart_removal" in text and "/api/cart/add" in text
    # allowed requests continue and are not recorded
    r = FakeRoute()
    g._handle(r, fake_request("POST", SHOP + "/api/cart/add", '{"product_id":"p1","quantity":1}'))
    assert r.result == "continue" and len(g.blocked) == 2


def test_cart_guard_forbidden_navigation_logged_as_warning(caplog):
    g = CartGuard(EVERYTHING)
    caplog.set_level(logging.DEBUG, logger="cart_guard")
    g._handle(FakeRoute(), fake_request("GET", SHOP + "/checkout?session=s3cr3t", nav=True))
    rec = [r for r in caplog.records if r.name == "cart_guard"][-1]
    assert rec.levelno == logging.WARNING and "s3cr3t" not in caplog.text
    assert g.blocked[-1]["reason"] == "forbidden: checkout"


def _binary_request(method, url):
    class Req:
        resource_type, headers = "xhr", {}

        @property
        def post_data(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

        def is_navigation_request(self):
            return False

    req = Req()
    req.method, req.url = method, url
    return req


def test_cart_guard_binary_body_fails_closed():
    """An undecodable body can't be scanned, so a non-GET with one is blocked, even on the add endpoint."""
    g = CartGuard(EVERYTHING)
    for method, url in (("POST", SHOP + "/api/cart/add"), ("POST", SHOP + "/api/cart/view"),
                        ("OPTIONS", SHOP + "/api/cart/add"),
                        ("POST", "https://b67f.token.awswaf.com/b67f/verify")):
        r = FakeRoute()
        g._handle(r, _binary_request(method, url))
        assert r.result == "abort:blockedbyclient" and g.blocked[-1]["reason"] == "binary body"
    assert decide_cart("POST", SHOP + "/api/cart/add", False, None, ADD, binary=True) == (False, "binary body")


def test_binary_body_only_on_an_exact_add_endpoint_declared_binary():
    exact = {"url_regex": r"^https://shop\.test/api/cart/add$", "why": "add", "op": "cart", "binary": True}
    policy = CartPolicy(cart=(exact,))
    r = FakeRoute()
    CartGuard(policy)._handle(r, _binary_request("POST", SHOP + "/api/cart/add"))
    assert r.result == "continue"
    assert decide_cart("POST", SHOP + "/api/cart/add", False, None, policy, binary=True) == (True, "cart add")
    # a binary flag on a location or read entry, or a forbidden URL, never helps
    assert decide_cart("POST", SHOP + "/api/loc", False, None,
                       CartPolicy(location=(exact | {"url_regex": r"^https://shop\.test/api/loc$"},)),
                       binary=True) == (False, "binary body")
    bad = CartPolicy(cart=(exact | {"url_regex": r"^https://shop\.test/api/clear-cart$"},))
    assert decide_cart("POST", SHOP + "/api/clear-cart", False, None, bad, binary=True) == \
        (False, "forbidden: cart_removal")
    # GET with an undecodable body is still a GET
    assert decide_cart("GET", SHOP + "/api/x", False, None, ADD, binary=True) == (True, "safe method")


ZEPTO = "https://www.zepto.com"


def test_cart_guard_install_and_allow():
    routes, listeners = [], []
    ctx = SimpleNamespace(route=lambda pattern, handler: routes.append((pattern, handler)),
                          on=lambda event, handler: listeners.append((event, handler)))
    g = CartGuard(CartPolicy(), site="zepto")
    g.install(ctx)
    assert routes == [("**/*", g._handle)]
    assert listeners == [("response", g._on_response)]
    url = ZEPTO + "/api/cart/add"
    assert decide_cart("POST", url, False, "{}", g.policy)[0] is False
    g.allow("cart", [{"url_regex": r"^https://www\.zepto\.com/api/cart/add$", "why": "add", "op": "cart",
                      "qty_path": "qty"}])
    g.allow("location", [{"url_regex": r"^https://www\.zepto\.com/api/loc$", "why": "loc", "op": "location"}])
    g.allow("read", [{"url_regex": r"^https://www\.zepto\.com/api/view$", "why": "read"}])
    r = FakeRoute()
    g._handle(r, fake_request("POST", url, '{"qty":1}'))
    assert r.result == "continue"
    assert decide_cart("POST", ZEPTO + "/api/loc", False, "{}", g.policy) == (True, "location select")
    assert decide_cart("POST", ZEPTO + "/api/view", False, "{}", g.policy) == (True, "read POST")
    # allowing never unlocks a forbidden request, even with an entry for that exact URL
    g.allow("cart", [{"url_regex": r"^https://www\.zepto\.com/api/place-order$", "why": "x", "op": "cart",
                      "qty_path": "qty"}])
    r = FakeRoute()
    g._handle(r, fake_request("POST", ZEPTO + "/api/place-order", "{}"))
    assert r.result == "abort:blockedbyclient"
    with pytest.raises(ValueError):
        g.allow("checkout", [])


def test_cart_guard_media_blocking():
    g = CartGuard(ADD, block_media=True)
    for rt in ("image", "media", "font"):
        r = FakeRoute()
        g._handle(r, fake_request("GET", SHOP + "/a.png", resource_type=rt))
        assert r.result == "abort:blockedbyclient"
    assert g.media_blocked == 3 and g.blocked == []
    r = FakeRoute()
    g._handle(r, fake_request("GET", SHOP + "/app.js", resource_type="script"))
    assert r.result == "continue"
    off = CartGuard(ADD)
    r = FakeRoute()
    off._handle(r, fake_request("GET", SHOP + "/a.png", resource_type="image"))
    assert r.result == "continue" and off.media_blocked == 0


# --- browser.open_site wiring ------------------------------------------------------------------

def test_open_site_refuses_login_with_cart_policy(monkeypatch):
    monkeypatch.setattr(browser.subprocess, "Popen", lambda *a, **k: pytest.fail("Chrome started"))
    monkeypatch.setattr(browser, "find_chrome", lambda: pytest.fail("Chrome looked up"))
    with pytest.raises(ValueError, match="cart mode cannot log in"):
        with browser.open_site("zepto", login_mode=True, cart_policy=CartPolicy()):
            pass
    with pytest.raises(ValueError, match="cart mode cannot log in"):
        with browser.open_site("zepto", login_mode=True, cart_policy=ADD):
            pass


class _FakeCtx:
    def __init__(self, pages=()):
        self.routes, self.pages = [], list(pages)
        self.listeners, self.removed, self.cdp = [], [], []

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def on(self, event, handler):
        self.listeners.append((event, handler))

    def remove_listener(self, event, handler):
        self.removed.append((event, handler))

    def unroute_all(self, **k):
        pass

    def grant_permissions(self, *a):
        pass

    def set_geolocation(self, *a):
        pass

    def new_cdp_session(self, page):
        return SimpleNamespace(send=lambda m, p=None: self.cdp.append((page, m, p)))

    def new_page(self):
        page = f"page{len(self.pages)}"
        self.pages.append(page)
        for event, handler in list(self.listeners):
            if event == "page":
                handler(page)
        return page


def _fake_chrome(monkeypatch, pages=()):
    ctx = _FakeCtx(pages)
    sent = []
    cdp = SimpleNamespace(send=lambda m, p=None: sent.append((m, p)) or {"targetInfos": []})
    br = SimpleNamespace(contexts=[ctx], new_browser_cdp_session=lambda: cdp)

    @contextlib.contextmanager
    def fake_pw():
        yield SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda url: br))

    monkeypatch.setattr(browser, "find_chrome", lambda: "chrome")
    monkeypatch.setattr(browser.subprocess, "Popen",
                        lambda *a, **k: SimpleNamespace(poll=lambda: None, wait=lambda **k: 0, kill=lambda: None))
    monkeypatch.setattr(browser, "_wait_for_cdp", lambda port, proc: None)
    monkeypatch.setattr(browser, "sync_playwright", fake_pw)
    return ctx, sent


def test_open_site_installs_cart_guard(monkeypatch, tmp_path):
    ctx, sent = _fake_chrome(monkeypatch)
    with browser.open_site("zepto", profile_dir=tmp_path / "p", location={}, cart_policy=ADD,
                           block_media=True) as (_, page, g, capture):
        assert isinstance(g, CartGuard) and not isinstance(g, read_guard.Guard)
        assert g.policy is ADD and g.block_media is True and g.site == "zepto"
        assert ctx.routes == [("**/*", g._handle)]
        assert ("response", g._on_response) in ctx.listeners      # redirect hops are recorded
    assert ("Browser.setDownloadBehavior", {"behavior": "deny"}) in sent    # downloads stay denied
    assert ("response", g._on_response) in ctx.removed


def test_open_site_cart_mode_bypasses_service_workers(monkeypatch, tmp_path):
    """Service-worker fetches never reach context.route, so cart mode makes every page bypass them."""
    ctx, sent = _fake_chrome(monkeypatch, pages=["old"])
    with browser.open_site("zepto", profile_dir=tmp_path / "p", location={}, cart_policy=ADD) as (_, page, g, c):
        assert page == "old"
        later = ctx.new_page()                                   # a popup / new tab opened during the run
        for p in ("old", later):
            calls = [(m, a) for pg, m, a in ctx.cdp if pg == p]
            assert ("Network.setBypassServiceWorker", {"bypass": True}) in calls
    assert any(e == "page" for e, _ in ctx.removed)


def test_open_site_cart_mode_without_pages_bypasses_the_new_page(monkeypatch, tmp_path):
    ctx, sent = _fake_chrome(monkeypatch)
    with browser.open_site("zepto", profile_dir=tmp_path / "p", location={}, cart_policy=ADD) as (_, page, g, c):
        assert (page, "Network.setBypassServiceWorker", {"bypass": True}) in ctx.cdp


def test_open_site_default_still_installs_read_guard(monkeypatch, tmp_path):
    ctx, sent = _fake_chrome(monkeypatch, pages=["old"])
    allow = [{"url_regex": "^x$"}]
    with browser.open_site("zepto", allowlist=allow, profile_dir=tmp_path / "p", location={}) as (_, page, g, c):
        assert type(g) is read_guard.Guard and g.allowlist == allow and g.login_mode is False
        assert ctx.routes == [("**/*", g._handle)]
        ctx.new_page()
    with browser.open_site("zepto", login_mode=True, profile_dir=tmp_path / "p", location={}) as (_, page, g, c):
        assert type(g) is read_guard.Guard and g.login_mode is True
    assert ("Browser.setDownloadBehavior", {"behavior": "deny"}) in sent
    assert ctx.cdp == [] and not any(e == "page" for e, _ in ctx.listeners)   # no SW bypass outside cart mode


class _Stop(Exception):
    pass


def _record_open_site(monkeypatch, module):
    seen = []

    def fake(*a, **k):
        seen.append(k)
        raise _Stop

    monkeypatch.setattr(module, "open_site", fake, raising=False)
    if module is not browser:
        monkeypatch.setattr(browser, "open_site", fake)
    return seen


def test_sync_login_and_search_never_pass_cart_policy(monkeypatch):
    import sync
    seen = _record_open_site(monkeypatch, sync)
    monkeypatch.setattr(sync.recipes, "load", lambda site: {"orders_page_url": "https://www.zepto.com/o",
                                                             "read_post_allowlist": []})
    for call in (lambda: sync.fetch_orders("zepto", None, allow_learn=False),
                 lambda: sync.login("zepto", phone="9000000000", prompt=lambda t, k: ""),
                 lambda: sync.learn("zepto")):
        with pytest.raises(_Stop):
            call()
    assert len(seen) == 3 and all(k.get("cart_policy") is None for k in seen)


def test_search_never_passes_cart_policy(monkeypatch):
    search = pytest.importorskip("search")    # Plan 5's search.py; runs once it lands
    seen = _record_open_site(monkeypatch, search)
    recipe = {"kind": "search", "site": "zepto", "read_post_allowlist": [], "search_page_url": "https://x/?q={q}"}
    with pytest.raises(_Stop):
        search.search_products("zepto", ["milk"], {"lat": 22.3, "lon": 73.2}, recipe, None)
    assert seen and all(k.get("cart_policy") is None for k in seen)


# --- cart click policy -------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["Proceed to Checkout", "Pay ₹412", "Place Order", "Proceed", "Remove", "Clear cart", "−",
                                  "Add new address", "Edit address", "Apply coupon", "Use Zepto Cash wallet", "Delete"])
def test_cart_click_refusal_blocks(text):
    assert agent.cart_click_refusal({"text": text, "tag": "button"})


@pytest.mark.parametrize("text", ["check out", "Checkout", "Payment options", "Pay now", "place the order",
                                  "Confirm order", "Empty cart", "Log out", "Sign out", "Cancel", "Cancellation",
                                  "Reorder", "Buy again", "Save address", "Change address", "Update address",
                                  "New address", "Add address", "Apply", "-", "–", " − ", "Coupons"])
def test_cart_click_refusal_blocks_more(text):
    assert agent.cart_click_refusal({"text": text, "tag": "button"})


def test_cart_click_refusal_checks_label_and_href():
    assert agent.cart_click_refusal({"text": "", "label": "Remove item", "tag": "button"})
    # an icon-only minus button: its label says what it does
    for label in ("Decrease quantity", "decrement", "Reduce", "minus", "-"):
        assert agent.cart_click_refusal({"text": "", "label": label, "tag": "button"})
    assert agent.cart_click_refusal({"text": "Continue", "tag": "a", "href": "/checkout"})
    assert agent.cart_click_refusal({"text": "Go", "tag": "a", "href": "https://shop.test/api/pay/init"})
    assert agent.cart_click_refusal({"text": "Offers", "tag": "a", "href": "/offers/apply?id=1"})
    assert agent.cart_click_refusal({"text": "Home", "tag": "a", "href": "/"}) is None


@pytest.mark.parametrize("text", ["Add", "ADD", "+", "Use current location", "Detect my location", "Home · 12 Shanti Society, Gorwa"])
def test_cart_click_refusal_allows(text):
    assert agent.cart_click_refusal({"text": text, "tag": "button", "type": "submit"}) is None


def test_cart_click_refusal_allows_add_inside_a_form():
    assert agent.cart_click_refusal({"text": "Add to cart", "tag": "button", "in_form": True}) is None


# --- Discovery.refuse_click hook ---------------------------------------------------------------

ELEMENTS = [{"text": "Orders", "tag": "a", "href": "/account/orders"}, {"text": "Add", "tag": "button"},
            {"text": "Continue", "tag": "button", "type": "submit"}, {"text": "Logout", "tag": "button"},
            {"text": "Load more", "tag": "button", "in_form": True}, {"text": "x", "tag": "a", "href": "/cart"}]


def test_discovery_refuse_click_hook_default_unchanged(monkeypatch):
    d = agent.Discovery("zepto", None, None, None)
    la = agent.LoginAgent("zepto", None, None, None, "9000000000")
    for el in ELEMENTS:
        assert d.refuse_click(el) == agent.click_refusal(el, False)
        assert la.refuse_click(el) == agent.click_refusal(el, True)

    # both call sites go through the hook
    class Cart(agent.Discovery):
        def refuse_click(self, el):
            return "cart policy" if el.get("text") == "Load more" else None

    c = Cart("zepto", None, None, None)
    c.refs = {"r1": {"ref": "r1", "text": "Load more", "tag": "button"}}
    assert c.click("r1") == {"error": "refused: cart policy"}
    clicked = []
    monkeypatch.setattr(agent, "click_ref", lambda page, ref: clicked.append(ref) or "click")
    c.snap = {"elements": [{"ref": "r1", "text": "Load more", "tag": "button"}]}
    assert c.auto_load_more() is False and clicked == []
    d.snap = {"elements": [{"ref": "r1", "text": "Load more", "tag": "button", "type": "submit"}]}
    assert d.auto_load_more() is False and clicked == []      # default policy still refuses the submit button


# --- review fix round 1 ------------------------------------------------------------------------

GQL = CartPolicy(cart=({"url_regex": r"^https://shop\.test/graphql$", "why": "add", "op": "cart",
                        "operation": "AddToCart"},),
                 read=({"url_regex": r"^https://shop\.test/graphql$", "why": "read"},))


@pytest.mark.parametrize("url", [
    SHOP + "/api/cart/update?r=/cdn-cgi/challenge-platform/",
    SHOP + "/x/cdn-cgi/challenge-platform/h/g",
    SHOP + "/api/cart/update#/cdn-cgi/challenge-platform/",
    "https://token.awswaf.com.evil.com/x", "http://a.token.awswaf.com/verify",
    "https://a.token.awswaf.com@evil.com/verify", SHOP + "/api/x/token.awswaf.com/",
])
def test_challenge_rule_matches_host_or_path_start_only(url):
    assert decide_cart("POST", url, False, "{}", ADD) == (False, "POST not on cart allowlist")


@pytest.mark.parametrize("body,category", [
    ('{"query":"\\u006dutation RemoveItem{ x }"}', "cart_removal"),
    ('{"query":"mutation \\u0052emoveItem{ x }"}', "cart_removal"),
    ('{"\\u0071uantity":0}', "cart_removal"),
    ('{"qty":"\\u0030"}', "cart_removal"),
    ('{"operationName":"Place\\u004frder"}', "order_placement"),
    ('{"action":"\\u0072emove"}', "cart_removal"),
    ('{"variables":"{\\"qty\\":0}"}', "cart_removal"),                   # JSON inside a JSON string
    ('{"payload":"{\\"operationName\\":\\"ApplyCoupon\\"}"}', "coupon"),
])
def test_json_escapes_are_decoded_before_scanning(body, category):
    for url in (SHOP + "/api/cart/add", SHOP + "/graphql"):
        assert decide_cart("POST", url, False, body, ADD) == (False, f"forbidden: {category}")
        assert decide_cart("POST", url, False, body, GQL) == (False, f"forbidden: {category}")


def test_escaped_mutation_never_passes_a_read_entry():
    body = '{"query":"\\u006dutation AddToCart{ x }"}'
    read_only = CartPolicy(read=GQL.read)
    assert decide_cart("POST", SHOP + "/graphql", False, body, read_only) == (False, "POST not on cart allowlist")
    assert decide_cart("POST", SHOP + "/graphql", False, body, GQL) == (True, "cart add")


@pytest.mark.parametrize("body,category", [
    ("product_id=p1&quantity=0", "cart_removal"), ("qty=-1&id=p1", "cart_removal"),
    ("action=remove_item&id=p1", "cart_removal"), ("operationName=PlaceOrder", "order_placement"),
    ("query=mutation%20RemoveItem%7Bx%7D", "cart_removal"), ("variables=%7B%22qty%22%3A0%7D", "cart_removal"),
    ("op=applyCoupon", "coupon"),
])
def test_form_urlencoded_bodies_are_decoded_before_scanning(body, category):
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, ADD) == (False, f"forbidden: {category}")


def test_form_bodies_that_add_pass_and_form_mutations_need_an_operation():
    assert decide_cart("POST", SHOP + "/api/cart/add", False, "product_id=p1&quantity=1", ADD) == (True, "cart add")
    body = "operationName=AddToCart&query=mutation%20AddToCart%7Bx%7D"
    assert decide_cart("POST", SHOP + "/graphql", False, body, CartPolicy(read=GQL.read)) == \
        (False, "POST not on cart allowlist")
    assert decide_cart("POST", SHOP + "/graphql", False, body, GQL) == (True, "cart add")
    assert operation_name(body) == "AddToCart"


def test_allow_regexes_are_wrapped_so_alternation_cant_widen_them():
    loose = CartPolicy(cart=({"url_regex": r"^https://shop\.test/api/cart/add$|/api/[a-z/]+$", "why": "x",
                              "op": "cart"},))
    assert decide_cart("POST", SHOP + "/api/cart/add", False, "{}", loose) == (True, "cart add")
    assert decide_cart("POST", SHOP + "/api/other/thing", False, "{}", loose) == (False, "POST not on cart allowlist")
    unanchored = CartPolicy(read=({"url_regex": r"shop\.test/api/view", "why": "x"},))
    assert decide_cart("POST", SHOP + "/api/view", False, "{}", unanchored) == (False, "POST not on cart allowlist")
    broken = CartPolicy(read=({"url_regex": r"^https://shop\.test/(", "why": "x"},))
    assert decide_cart("POST", SHOP + "/(", False, "{}", broken) == (False, "POST not on cart allowlist")


@pytest.mark.parametrize("regex,error", [
    (r"^https://www\.zepto\.com/api/cart/add$|^https://evil\.com/a/b$", "too broad"),
    (r"^https://www\.zepto\.com/api/cart/add$|/api/[a-z/]+$", "too broad"),
    (r"^https://www\.zepto\.com/api/.*$", "too broad"),
    (r"^https://www\.zepto\.com/api/cart/[^?]{1,}$", "too broad"),
    (r"^https://www\.zepto\.com/[a-z]+/[a-z]+$", "too broad"),
    (r"^https://api\.blinkit\.com/v1/cart/add$", "another site"),
    (r"^https://(?:www\.zepto\.com|evil\.com)/api/cart/add$", "another site"),
    (r"^https://www\.zepto\.com\.evil\.io/api/cart/add$", "another site"),
])
def test_cart_guard_allow_validates_entries(regex, error):
    g = CartGuard(CartPolicy(), site="zepto")
    with pytest.raises(ValueError, match=error):
        g.allow("cart", [{"url_regex": regex, "why": "x", "op": "cart", "qty_path": "qty"}])
    assert g.policy == CartPolicy()


def test_cart_guard_allow_needs_a_site():
    with pytest.raises(ValueError, match="site"):
        CartGuard(CartPolicy()).allow("read", [{"url_regex": r"^https://www\.zepto\.com/api/view$", "why": "x"}])


def test_cart_guard_allow_probes_blocked_urls_on_a_foreign_host():
    g = CartGuard(CartPolicy(), site="zepto")
    g.blocked.append({"method": "POST", "url": ZEPTO + "/api/cart/add", "body": "", "reason": "x"})
    g.allow("cart", [{"url_regex": r"^https://www\.zepto\.com/api/cart/add$", "why": "x", "op": "cart",
                      "qty_path": "qty"}])
    assert decide_cart("POST", ZEPTO + "/api/cart/add", False, '{"qty":1}', g.policy) == (True, "cart add")


@pytest.mark.parametrize("text", ["Reset cart", "Replace cart", "Start afresh", "afresh", "Add to wishlist",
                                  "Wishlist", "Save for later", "Buy now", "Order now", "Login", "Log in", "Sign in",
                                  "Signin", "Redeem", "Zepto Cash", "Use cash", "Wallet", "×", "✕", "🗑", " 🗑️ "])
def test_cart_click_refusal_fix_round_words_and_glyphs(text):
    assert agent.cart_click_refusal({"text": text, "tag": "button"})
    assert agent.cart_click_refusal({"text": "", "label": text, "tag": "button"})


@pytest.mark.parametrize("category,path", [
    ("checkout", "/check%252Dout"), ("checkout", "/check%25252Dout"), ("payment", "/%2570ay/init"),
    ("cart_removal", "/api/remove%255Fitem"), ("order_placement", "/api/x?next=%252Fplace-order"),
])
def test_double_encoding_is_decoded(category, path):
    assert decide_cart("POST", SHOP + path, False, "{}", EVERYTHING) == (False, f"forbidden: {category}")


@pytest.mark.parametrize("url,category", [
    ("https://checkout.shop.test/", "checkout"), ("https://pay.shop.test/session", "payment"),
    ("https://wallet.shop.test/", "wallet"), ("https://coupons.shop.test/x", "coupon"),
])
def test_host_is_scanned_on_navigations_and_writes(url, category):
    assert decide_cart("GET", url, True, None, EVERYTHING) == (False, f"forbidden: {category}")
    assert decide_cart("POST", url, False, "{}", EVERYTHING) == (False, f"forbidden: {category}")
    # a background GET (an image on a CDN host) is not a navigation or a write
    assert decide_cart("GET", url, False, None, EVERYTHING) == (True, "safe method")


@pytest.mark.parametrize("header", ["X-HTTP-Method-Override", "x-method-override", "X-HTTP-Method"])
@pytest.mark.parametrize("override", ["DELETE", "put", "PATCH"])
def test_method_override_headers_are_the_effective_method(header, override):
    assert decide_cart("POST", SHOP + "/api/cart/add", False, '{"quantity":1}', EVERYTHING,
                       headers={header: override}) == (False, f"{override.upper()} never allowed")


def test_method_override_params_and_guard_headers():
    url = SHOP + "/api/cart/add"
    assert decide_cart("POST", url + "?_method=DELETE", False, "{}", EVERYTHING) == (False, "DELETE never allowed")
    assert decide_cart("POST", url, False, "_method=patch&qty=1", EVERYTHING) == (False, "PATCH never allowed")
    assert decide_cart("POST", url, False, '{"_method":"PUT","qty":1}', EVERYTHING) == (False, "PUT never allowed")
    # an override can't turn a POST into a safe method either
    assert decide_cart("POST", SHOP + "/api/other", False, "{}", ADD, headers={"X-HTTP-Method-Override": "GET"}) == \
        (False, "POST not on cart allowlist")
    r = FakeRoute()
    CartGuard(ADD)._handle(r, fake_request("POST", url, '{"quantity":1}', headers={"x-http-method-override": "DELETE"}))
    assert r.result == "abort:blockedbyclient"


@pytest.mark.parametrize("body", ['{"quantity":0.0}', '{"qty":0.00}', '{"newQuantity":0}', '{"itemQuantity":"0"}',
                                  '{"item_quantity":0}', '{"new_quantity":-2}', '{"quantity":-0.5}', '{"qty":-0}',
                                  '{"quantity":00}'])
def test_more_zero_quantity_shapes_are_removal(body):
    assert REMOVE_BODY.search(body)
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, ADD) == (False, "forbidden: cart_removal")


@pytest.mark.parametrize("body", ['{"newQuantity":2}', '{"itemQuantity":"3"}', '{"qty":10}'])
def test_positive_quantity_shapes_pass(body):
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, ADD) == (True, "cart add")


class FakeResponse:
    def __init__(self, method, url, status, location=None):
        self.status, self.url = status, url
        self.headers = {"location": location} if location else {}
        self.request = SimpleNamespace(method=method, url=url)


def test_redirected_writes_are_recorded(caplog):
    g = CartGuard(ADD)
    caplog.set_level(logging.DEBUG, logger="cart_guard")
    g._on_response(FakeResponse("POST", SHOP + "/api/cart/add?t=s3cr3t", 307, SHOP + "/checkout?t=s3cr3t"))
    g._on_response(FakeResponse("GET", SHOP + "/old", 302, SHOP + "/new"))
    g._on_response(FakeResponse("POST", SHOP + "/api/cart/add", 200))
    g._on_response(FakeResponse("POST", SHOP + "/api/cart/add", 304))
    assert g.redirected_writes == [{"method": "POST", "url": SHOP + "/api/cart/add?t=s3cr3t", "status": 307,
                                    "location": SHOP + "/checkout?t=s3cr3t"}]
    assert "s3cr3t" not in caplog.text and "/api/cart/add" in caplog.text
    g._on_response(SimpleNamespace())          # a response it can't read is ignored, not raised
    assert CartGuard(ADD).redirected_writes == []


# --- review fix round 2 ------------------------------------------------------------------------

@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "OPTIONS", "HEAD"])
@pytest.mark.parametrize("headers", [None, {}, {"content-type": "application/octet-stream"},
                                     {"Content-Length": "12"}, {"content-type": "text/plain", "content-length": "0"}])
def test_unreadable_none_body_on_a_non_get_fails_closed(method, headers):
    """Playwright gives post_data None for Blob/File bodies or ones Chrome omits: None is not proof of empty."""
    got = decide_cart(method, SHOP + "/api/cart/add", False, None, EVERYTHING, headers=headers)
    if method in ("PUT", "PATCH"):
        assert got == (False, f"{method} never allowed")
    elif method in ("OPTIONS", "HEAD") and not headers:
        assert got == (True, "safe method")          # nothing says these carry a body
    else:
        assert got == (False, "binary body")


def test_none_body_is_fine_when_confirmed_empty_or_a_get():
    empty = {"content-length": "0"}
    assert decide_cart("POST", SHOP + "/api/cart/view", False, None, ADD, headers=empty) == (True, "read POST")
    assert decide_cart("GET", SHOP + "/api/cart/view", False, None, ADD) == (True, "safe method")
    assert decide_cart("GET", SHOP + "/x", True, None, ADD, headers={"content-type": "text/html"}) == \
        (True, "safe method")


def test_cart_guard_treats_none_post_data_as_unreadable():
    r = FakeRoute()
    g = CartGuard(EVERYTHING)
    g._handle(r, fake_request("POST", SHOP + "/api/cart/add", None, headers={"content-type": "application/json"}))
    assert r.result == "abort:blockedbyclient" and g.blocked[-1]["reason"] == "binary body"
    r = FakeRoute()
    g._handle(r, fake_request("POST", SHOP + "/api/cart/add", None))
    assert r.result == "abort:blockedbyclient"


QTY = CartPolicy(cart=({"url_regex": r"^https://shop\.test/api/cart/add$", "why": "add", "op": "cart",
                        "qty_path": "item.qty"},
                       {"url_regex": r"^https://shop\.test/api/cart/q$", "why": "add", "op": "cart",
                        "qty_path": "query:qty"}))


@pytest.mark.parametrize("value", ['0', '"0 "', '" 0"', 'null', 'false', 'true', '"-1e0"', '-1e0', '0.5', '"1x"',
                                   '"abc"', '[]', '{}', '""', '-1'])
def test_cart_entry_qty_path_must_be_a_number_at_least_one(value):
    body = f'{{"item":{{"id":"p1","qty":{value}}}}}'
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, QTY) == (False, "forbidden: cart_removal")


def test_cart_entry_qty_path_missing_or_query():
    assert decide_cart("POST", SHOP + "/api/cart/add", False, '{"item":{"id":"p1"},"delta":-1}', QTY) == \
        (False, "forbidden: cart_removal")
    for good in ('{"item":{"id":"p1","qty":1}}', '{"item":{"id":"p1","qty":"3"}}', '{"item":{"qty":99}}'):
        assert decide_cart("POST", SHOP + "/api/cart/add", False, good, QTY) == (True, "cart add")
    assert decide_cart("POST", SHOP + "/api/cart/q?id=p1&qty=2", False, "{}", QTY) == (False, "POST not on cart allowlist")
    q = CartPolicy(cart=({"url_regex": r"^https://shop\.test/api/cart/q\?id=p1&qty=\d+$", "why": "x", "op": "cart",
                          "qty_path": "query:qty"},))
    assert decide_cart("POST", SHOP + "/api/cart/q?id=p1&qty=2", False, "{}", q) == (True, "cart add")
    assert decide_cart("POST", SHOP + "/api/cart/q?id=p1&qty=0", False, "{}", q) == (False, "forbidden: cart_removal")


@pytest.mark.parametrize("body", ['{"quantity":"0 "}', '{"qty":null}', '{"quantity":false}', '{"qty":"-1e0"}',
                                  '{"delta":-1}', '{"quantityDelta":"-2"}', '{"qty": " 0"}', '{"count":0e0}'])
def test_remove_body_regex_second_layer(body):
    assert REMOVE_BODY.search(body)
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, ADD) == (False, "forbidden: cart_removal")


@pytest.mark.parametrize("url", [
    SHOP + "/cdn-cgi/challenge-platform/../../api/cart/update",
    SHOP + "/cdn-cgi/challenge-platform/%2e%2e/%2e%2e/api/cart/update",
    SHOP + "/cdn-cgi/challenge-platform%2f..%2fapi/cart/update",
    SHOP + "/cdn-cgi%2Fchallenge-platform/x",
    SHOP + "/cdn-cgi/challenge-platform/./x/../../y",
    SHOP + "/cdn-cgi/challenge-platform\\..\\api",
])
def test_challenge_path_refuses_dot_segments_and_encoded_slashes(url):
    assert decide_cart("POST", url, False, "{}", ADD) in ((False, "POST not on cart allowlist"),
                                                          (False, "encoded path separator"), (False, "dot segment in path"))
    assert not cart_guard._challenge(url)


def test_challenge_path_is_checked_decoded():
    assert decide_cart("POST", SHOP + "/cdn-cgi/challenge-platform/h/g/flow", False, "{}", ADD) == \
        (True, "bot-challenge token")
    assert decide_cart("POST", SHOP + "/cdn-cgi/%63hallenge-platform/h/g", False, "{}", ADD) == \
        (True, "bot-challenge token")


@pytest.mark.parametrize("text", ["Add a tip", "Tip ₹20", "Tips", "Donate ₹1", "Donation", "Use Swiggy Money",
                                  "Cashback", "Get cashback"])
def test_cart_click_refusal_fix_round_2(text):
    assert agent.cart_click_refusal({"text": text, "tag": "button"})
    assert agent.cart_click_refusal({"text": "", "label": text, "tag": "button"})


# --- review fix round 3 ------------------------------------------------------------------------

@pytest.mark.parametrize("body", ['{"action":"decrease"}', '{"op":"subtractItem"}', '{"type":"MINUS"}',
                                  '{"qtyChange":-1}', '{"quantityChange":"-2"}', '{"quantity_change":-1}',
                                  '{"qty":[0]}', '{"quantity":[ 0 ]}', '{"operationName":"DecreaseQty"}'])
def test_round_3_removal_bodies(body):
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, ADD) == (False, "forbidden: cart_removal")


@pytest.mark.parametrize("body", ['{"qtyChange":1}', '{"quantityChange":"2"}'])
def test_round_3_positive_changes_pass(body):
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, ADD) == (True, "cart add")


@pytest.mark.parametrize("value,ok", [("1", True), ("99", True), ("2.0", False), ('"2.0"', False), ("100", False), ("1000", False), ("99.5", False),
                                      ('"99"', True), ('"100"', False), ('"١"', False), ('"1٠"', False),
                                      ('"１"', False)])
def test_qty_must_be_ascii_digits_and_at_most_99(value, ok):
    body = f'{{"item":{{"id":"p1","qty":{value}}}}}'
    expected = (True, "cart add") if ok else (False, "forbidden: cart_removal")
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, QTY) == expected


@pytest.mark.parametrize("url", [SHOP + "/cdn-cgi/challenge-platform/..;/api/cart/update",
                                 SHOP + "/cdn-cgi/challenge-platform/.;x/y",
                                 SHOP + "/cdn-cgi/challenge-platform/%2e%2e;/api"])
def test_challenge_path_refuses_semicolon_dot_segments(url):
    assert decide_cart("POST", url, False, "{}", ADD) in ((False, "POST not on cart allowlist"),
                                                          (False, "encoded path separator"), (False, "dot segment in path"))
    assert not cart_guard._challenge(url)


BIN_QTY = CartPolicy(cart=({"url_regex": r"^https://shop\.test/api/cart/add$", "why": "add", "op": "cart",
                            "binary": True, "qty_path": "item.qty"},))


def test_binary_entry_still_checks_qty_when_a_view_decodes():
    url = SHOP + "/api/cart/add"
    assert decide_cart("POST", url, False, '{"item":{"qty":0}}', BIN_QTY, binary=True) == \
        (False, "forbidden: cart_removal")
    assert decide_cart("POST", url, False, '{"item":{"qty":null}}', BIN_QTY, binary=True) == \
        (False, "forbidden: cart_removal")
    assert decide_cart("POST", url, False, '{"item":{"qty":2}}', BIN_QTY, binary=True) == (True, "cart add")
    # no decodable view: only the declared-binary entry passes
    assert decide_cart("POST", url, False, None, BIN_QTY, binary=True) == (True, "cart add")
    undeclared = CartPolicy(cart=({**BIN_QTY.cart[0], "binary": False},))
    assert decide_cart("POST", url, False, '{"item":{"qty":2}}', undeclared, binary=True) == (False, "binary body")
    # a query qty_path is always checkable
    q = CartPolicy(cart=({"url_regex": r"^https://shop\.test/api/cart/add\?qty=\d+$", "why": "x", "op": "cart",
                          "binary": True, "qty_path": "query:qty"},))
    assert decide_cart("POST", url + "?qty=0", False, None, q, binary=True) == (False, "forbidden: cart_removal")
    assert decide_cart("POST", url + "?qty=3", False, None, q, binary=True) == (True, "cart add")


def test_cart_guard_passes_a_latin1_view_of_binary_bodies():
    class Req:
        method, url, resource_type, headers = "POST", SHOP + "/api/cart/add", "xhr", {}
        post_data_buffer = b'{"item":{"qty":0}}\xff'

        @property
        def post_data(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")

        def is_navigation_request(self):
            return False

    r = FakeRoute()
    g = CartGuard(BIN_QTY)
    g._handle(r, Req())
    assert r.result == "abort:blockedbyclient" and g.blocked[-1]["reason"] == "forbidden: cart_removal"


@pytest.mark.parametrize("text", ["Use SuperCash", "Supercash", "Zepto Pass", "Join Swiggy One", "Subscribe",
                                  "Subscription", "Add gift wrap", "Giftwrap"])
def test_cart_click_refusal_fix_round_3(text):
    assert agent.cart_click_refusal({"text": text, "tag": "button"})


@pytest.mark.parametrize("text", ["Add", "Add Cashew Nuts 200g", "+"])
def test_cart_click_refusal_round_3_still_allows(text):
    assert agent.cart_click_refusal({"text": text, "tag": "button"}) is None


# --- review fix round 4 ------------------------------------------------------------------------

@pytest.mark.parametrize("body", ['{"quantity":"+0"}', '{"quantity":"0x0"}', '{"quantity":0.4}', '{"qty":[[0]]}',
                                  '{"qty":0.5}', '{"qty":1.5}', '{"qty":2.0}', '{"qty":"02"}', '{"qty":100}',
                                  '{"qty":"+1"}', '{"qty":[1]}', '{"qty":{"value":1}}', '{"qty":"1 "}',
                                  '{"newQty":true}', '{"count":"abc"}', '{"itemCount":1e1}', '{"qty":""}',
                                  '{"items":[{"id":"p1","quantity":"0x1"}]}', '{"variables":"{\\"qty\\":0.4}"}',
                                  'product_id=p1&quantity=0.4', 'qty=%2B0'])
def test_round_4_quantity_must_be_a_plain_integer_1_to_99(body):
    assert forbidden_category(SHOP + "/api/cart/add", body, False) == "cart_removal"
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, ADD) == (False, "forbidden: cart_removal")


@pytest.mark.parametrize("body", ['{"quantity":"+0"}', '{"quantity":"0x0"}', '{"quantity":0.4}', '{"qty":[[0]]}'])
def test_round_4_remove_body_itself_catches_the_reported_bodies(body):
    assert REMOVE_BODY.search(body)


@pytest.mark.parametrize("body", ['{"qty":1}', '{"qty":99}', '{"quantity":"7"}', '{"qty" : 3 }',
                                  '{"items":[{"id":"p1","qty":2},{"id":"p2","quantity":"5"}]}',
                                  'product_id=p1&quantity=2'])
def test_round_4_plain_quantities_pass(body):
    assert not REMOVE_BODY.search(body)
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, ADD) == (True, "cart add")


# --- review fix round 5 ------------------------------------------------------------------------

QTY_ADD = CartPolicy(cart=({"url_regex": r"^https://shop\.test/api/cart/add\?id=p1&qty=\d+$", "why": "add",
                            "op": "cart", "qty_path": "query:qty"},))


@pytest.mark.parametrize("query", ["id=p1&qty=0", "id=p1&quantity=-1", "id=p1&qty=0.4", "id=p1&action=removeItem",
                                   "id=p1&cartQuantity=0", "id=p1&items%5B0%5D%5Bquantity%5D=0"])
def test_round_5_query_string_is_scanned_on_writes(query):
    url = SHOP + "/api/cart/add?" + query
    assert forbidden_category(url, "{}", False, write=True) == "cart_removal"
    assert decide_cart("POST", url, False, "{}", QTY_ADD) == (False, "forbidden: cart_removal")
    assert decide_cart("POST", url, False, "{}", EVERYTHING) == (False, "forbidden: cart_removal")


def test_round_5_query_quantity_one_passes():
    assert decide_cart("POST", SHOP + "/api/cart/add?id=p1&qty=1", False, "{}", QTY_ADD) == (True, "cart add")


@pytest.mark.parametrize("is_nav", [True, False])
def test_round_5_gets_stay_safe(is_nav):
    """HTTP semantics: a GET doesn't change the cart, so its query isn't scanned for quantities."""
    assert decide_cart("GET", SHOP + "/api/cart/view?id=p1&qty=0", is_nav, None, ADD) == (True, "safe method")


def multipart(name, value, boundary="XyZ"):
    return (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="id"\r\n\r\np1\r\n--{boundary}--\r\n')


@pytest.mark.parametrize("body", [
    '{"cartQuantity":0}', '{"units":0}', '{"quantity ":0}', '{" qty":"0"}', 'quantity[]=0', 'items[0][quantity]=0',
    '{"items[0][quantity]":0}', '{"Qty":"0"}', '{"QUANTITY":-1}', '{"itemUnits":"+1"}', '{"lineItemCount":0.5}',
    '{"cart":{"productQuantity":[[0]]}}', '{"qty":2,"qty":0}', '{"variables":"{\\"cartQuantity\\":0}"}',
    multipart("quantity", "0"), multipart("items[0][qty]", "-1"), multipart("cartQuantity", "0.4"),
    multipart("units", "+0"),
])
def test_round_5_quantity_key_spellings(body):
    assert forbidden_category(SHOP + "/api/cart/add", body, False) == "cart_removal"
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, ADD) == (False, "forbidden: cart_removal")


@pytest.mark.parametrize("body", ['{"cartQuantity":2}', 'items[0][quantity]=3', '{"quantity ":"5"}',
                                  multipart("quantity", "1"), '{"countryCode":"IN","accountId":"a1","qty":1}'])
def test_round_5_plain_quantities_under_any_spelling_pass(body):
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, ADD) == (True, "cart add")


# --- final review fixes ------------------------------------------------------------------------

WILD = CartPolicy(cart=({"url_regex": r"^https://www\.zepto\.com/api/v1/cart/[^/?]{1,30}$", "why": "x", "op": "cart"},
                        {"url_regex": r"^https://www\.zepto\.com/api/v1/[^/?]{1,30}$", "why": "x", "op": "cart"},
                        {"url_regex": r"^https://www\.zepto\.com/api/v\d/[^/?]{1,10}/[^/?]{1,10}$", "why": "x",
                         "op": "cart"}))


@pytest.mark.parametrize("path", ["/api/v1/cart/..%2F..%2Forders", "/api/v1/cart/..%2f..%2forders",
                                  "/api/v1/cart/%2E%2E", "/api/v1/cart/a%5Cb", "/api/v1/cart/a%252Fb",
                                  "/api/v1/cart/a%25252eb", "/api/v1/cart/a\\b"])
def test_final_encoded_separators_refused_on_writes(path):
    assert decide_cart("POST", ZEPTO + path, False, '{"qty":1}', WILD) == (False, "encoded path separator")
    assert decide_cart("POST", ZEPTO + path, False, '{"qty":1}', EVERYTHING)[0] is False


def test_final_encoded_separator_example_from_the_review():
    url = ZEPTO + "/api/v1/cart/..%2F..%2Forders"
    assert cart_guard.matches(WILD.cart[0]["url_regex"], url)
    assert decide_cart("POST", url, False, '{"qty":1}', WILD) == (False, "encoded path separator")


def test_final_gets_with_encoded_separators_stay_safe():
    assert decide_cart("GET", ZEPTO + "/api/v1/cart/a%2Fb", False, None, WILD) == (True, "safe method")


@pytest.mark.parametrize("path", ["/api/v1/orders", "/api/v1/orders/new", "/api/v1/order", "/api/v2/order/initiate",
                                  "/api/v1/Orders", "/api/v1/ORDER", "/api/v1/newOrder", "/api/v1/order-history",
                                  "/api/v1/order_create", "/api/v1/orderId", "/api/v1/orders.json"])
def test_final_order_paths_refused_on_writes(path):
    assert decide_cart("POST", ZEPTO + path, False, '{"qty":1}', WILD) == (False, "forbidden: order")
    assert decide_cart("POST", ZEPTO + path, False, '{"qty":1}', EVERYTHING) == (False, "forbidden: order")


@pytest.mark.parametrize("path", ["/api/v1/reorder", "/api/v1/border", "/api/v1/recorder", "/api/v1/orderly"])
def test_final_order_word_boundaries(path):
    assert decide_cart("POST", ZEPTO + path, False, '{"qty":1}', WILD) == (True, "cart add")


@pytest.mark.parametrize("is_nav", [True, False])
def test_final_order_history_gets_stay_allowed(is_nav):
    assert decide_cart("GET", ZEPTO + "/api/v1/orders", is_nav, None, WILD) == (True, "safe method")


def test_final_allow_cart_needs_qty_path(monkeypatch):
    monkeypatch.delenv("WORKER_FAKE", raising=False)
    g = CartGuard(CartPolicy(), site="zepto")
    entry = {"url_regex": r"^https://www\.zepto\.com/api/v1/cart/add$", "why": "x", "op": "cart"}
    for bad in (entry, entry | {"qty_path": ""}, entry | {"qty_path": "  "}, entry | {"qty_path": None}):
        with pytest.raises(ValueError, match="qty_path"):
            g.allow("cart", [bad])
    assert g.policy.cart == ()
    # location and read entries don't need one
    g.allow("read", [{"url_regex": r"^https://www\.zepto\.com/api/v1/cart/view$", "why": "x"}])


def mp_preamble(value="0"):
    return ("This is a preamble.\r\n--fake\r\nnot a part\r\n--XyZ\r\nContent-Disposition: form-data; name=\"id\""
            "\r\n\r\np1\r\n--XyZ\r\nContent-Disposition: form-data; name=\"quantity\"\r\n\r\n" + value +
            "\r\n--XyZ--\r\n")


def mp_rfc2231(value="0"):
    return ("--XyZ\r\nContent-Disposition: form-data; name*=utf-8''quantity\r\n\r\n" + value +
            "\r\n--XyZ\r\nContent-Disposition: form-data; name=\"item.qty\"\r\n\r\n1\r\n--XyZ--\r\n")


@pytest.mark.parametrize("body", ['{"items":[]}', mp_preamble(), mp_rfc2231()])
def test_final_allowed_cart_entry_still_blocks_removal_shapes(body, monkeypatch):
    monkeypatch.delenv("WORKER_FAKE", raising=False)
    g = CartGuard(CartPolicy(), site="zepto")
    g.allow("cart", [{"url_regex": r"^https://www\.zepto\.com/api/v1/cart/add$", "why": "x", "op": "cart",
                      "qty_path": "item.qty"}])
    assert decide_cart("POST", ZEPTO + "/api/v1/cart/add", False, body, g.policy) == \
        (False, "forbidden: cart_removal")


@pytest.mark.parametrize("body", [mp_preamble("2"), mp_rfc2231("2")])
def test_final_multipart_parts_are_read_past_preambles_and_rfc2231_names(body):
    assert dict(cart_guard._multipart(body))["quantity"] == "2"


# --- last fix ----------------------------------------------------------------------------------

MP_QTY = CartPolicy(cart=({"url_regex": r"^https://shop\.test/api/cart/add$", "why": "add", "op": "cart",
                           "qty_path": "qty"},))


def mp(*parts):
    """parts: (header block, value) pairs."""
    out = "".join(f"--XyZ\r\n{head}\r\n\r\n{value}\r\n" for head, value in parts)
    return out + "--XyZ--\r\n"


GOOD_QTY = ('Content-Disposition: form-data; name="qty"', "1")
AMBIGUOUS_MULTIPART = [
    mp(('Content-Disposition: form-data;\r\n name="quantity"', "0"), GOOD_QTY),            # folded name
    mp(('Content-Disposition: form-data; name="x"; name="quantity"', "0"), GOOD_QTY),     # two names
    mp(('Content-Disposition : form-data; name="quantity"', "0"), GOOD_QTY),             # space before colon
    mp(('Content-Disposition: form-data;\r\n\tname="newQuantity"', "0"), GOOD_QTY),       # folded with a tab
    mp(('Content-Disposition: form-data; name="id"\r\n X-Extra: 1', "p1"), GOOD_QTY),    # any folded header
    mp(GOOD_QTY, ('Content-Disposition: form-data; name="qty"', "0")),               # repeat, other value
    mp(GOOD_QTY, ('Content-Disposition: form-data; name="qty"', "2")),
    mp(('Content-Disposition: form-data; name="x"; name*=utf-8\'\'quantity', "0"), GOOD_QTY),
]


@pytest.mark.parametrize("body", AMBIGUOUS_MULTIPART)
def test_last_ambiguous_multipart_fails_closed(body):
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, MP_QTY) == (False, "ambiguous multipart body")
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, EVERYTHING)[0] is False


def test_last_plain_multipart_still_passes():
    body = mp(('Content-Disposition: form-data; name="id"', "p1"), GOOD_QTY, GOOD_QTY)   # a same-value repeat is fine
    assert decide_cart("POST", SHOP + "/api/cart/add", False, body, MP_QTY) == (True, "cart add")
    # JSON that merely mentions the header is not multipart
    assert decide_cart("POST", SHOP + "/api/cart/add", False, '{"note":"Content-Disposition : x","qty":1}', ADD) == \
        (True, "cart add")


@pytest.mark.parametrize("path", ["/api/v1/cart/%u002e%u002e", "/api/v1/cart/%U2215", "/api/v1/cart/%c0%af",
                                  "/api/v1/cart/%C1%9C", "/api/v1/cart/%e0%80%af", "/api/v1/cart/%%32%66"])
def test_last_odd_encodings_refused_on_writes(path):
    assert decide_cart("POST", ZEPTO + path, False, '{"qty":1}', WILD) == (False, "encoded path separator")


@pytest.mark.parametrize("path", ["/api/v1/cart/..;/..;/orders", "/api/v1/cart/..;x/y", "/api/v1/cart/..",
                                  "/api/v1/cart/../x"])
def test_last_dot_dot_segments_refused_on_writes(path):
    assert decide_cart("POST", ZEPTO + path, False, '{"qty":1}', WILD) == (False, "dot segment in path")


def test_last_odd_paths_on_gets_stay_safe():
    for path in ("/api/v1/cart/..;/x", "/api/v1/cart/%c0%af"):
        assert decide_cart("GET", ZEPTO + path, False, None, WILD) == (True, "safe method")


@pytest.mark.parametrize("path", ["/api/v1/newORDER", "/api/v1/NEWOrder", "/api/v1/ordersv2", "/api/v1/ORDERS",
                                  "/api/v1/myOrders"])
def test_last_order_word_across_camel_case(path):
    assert decide_cart("POST", ZEPTO + path, False, '{"qty":1}', WILD) == (False, "forbidden: order")


@pytest.mark.parametrize("path", ["/api/v1/purchase", "/api/v1/purchases", "/api/v1/Purchase", "/api/v1/newPurchase",
                                  "/api/v1/NEWPurchase", "/api/v1/purchase-init", "/api/v1/PURCHASES"])
def test_last_purchase_paths_refused_on_writes(path):
    assert decide_cart("POST", ZEPTO + path, False, '{"qty":1}', WILD) == (False, "forbidden: purchase")
    assert decide_cart("GET", ZEPTO + path, False, None, WILD) == (True, "safe method")


@pytest.mark.parametrize("path", ["/api/v1/reorder", "/api/v1/border", "/api/v1/recorder", "/api/v1/orderly",
                                  "/api/v1/purchased", "/api/v1/repurchase"])
def test_last_word_boundaries_still_hold(path):
    assert decide_cart("POST", ZEPTO + path, False, '{"qty":1}', WILD) == (True, "cart add")

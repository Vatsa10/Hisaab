import pytest

from guard import HEAVY_RESOURCE_TYPES, Guard, decide

API = "https://api.example.com/v1/orders"
ALLOW = [{"url_regex": r"^https://api\.example\.com/graphql$"}]


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_allows_safe_methods(method):
    assert decide(method, API, False, None, False, [])[0]


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_blocks_writes_by_default(method):
    assert not decide(method, API, False, "{}", False, [])[0]


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_put_delete_blocked_even_in_login_mode(method):
    assert not decide(method, API, False, "{}", True, ALLOW)[0]


def test_login_mode_allows_post():
    assert decide("POST", "https://api.example.com/v1/otp/verify", False, "{}", True, [])[0]


def test_login_mode_still_blocks_post_to_cart():
    assert not decide("POST", "https://api.example.com/v1/cart/add", False, "{}", True, [])[0]


@pytest.mark.parametrize("url", [
    "https://site.com/cart", "https://site.com/checkout?x=1", "https://site.com/payment",
    "https://site.com/pay", "https://site.com/account/address", "https://site.com/place-order",
    "https://site.com/placeorder", "https://site.com/order/1/cancel", "https://site.com/CART",
])
def test_blocks_navigation_to_blocked_urls(url):
    assert not decide("GET", url, True, None, False, [])[0]


def test_allows_normal_navigation():
    assert decide("GET", "https://site.com/account/orders", True, None, False, [])[0]


def test_allows_allowlisted_read_post():
    body = '{"query": "query Orders($p:Int){ orders(page:$p){ id } }"}'
    assert decide("POST", "https://api.example.com/graphql", False, body, False, ALLOW)[0]


def test_blocks_allowlisted_url_with_mutation_body():
    body = '{"query": "mutation CancelOrder { cancel(id: 1) }"}'
    assert not decide("POST", "https://api.example.com/graphql", False, body, False, ALLOW)[0]


def test_blocks_post_not_on_allowlist():
    assert not decide("POST", "https://api.example.com/other", False, "{}", False, ALLOW)[0]


@pytest.mark.parametrize("url", [
    "https://b67f7794189c.f957f42c.ap-south-1.token.awswaf.com/b67f7794189c/5504ea1b6187/219ce1/verify",
    "https://www.example.com/cdn-cgi/challenge-platform/h/g/flow/ov1",
])
def test_allows_bot_challenge_posts(url):
    assert decide("POST", url, False, "{}", False, [])[0]


def test_challenge_lookalike_hosts_still_blocked():
    assert not decide("POST", "https://token.awswaf.com.evil.com/x", False, "{}", False, [])[0]
    assert not decide("POST", "https://www.swiggy.com/api/order/token.awswaf.com", False, "{}", False, [])[0]


# --- media blocking (user syncs only) ---------------------------------------------------------------------

class _Route:
    def __init__(self):
        self.done = None

    def continue_(self):
        self.done = "continue"

    def abort(self, reason=None):
        self.done = ("abort", reason)


class _Request:
    def __init__(self, resource_type, method="GET", url="https://cdn.example.com/a.png", nav=False, body=None):
        self.resource_type, self.method, self.url, self._nav, self.post_data = resource_type, method, url, nav, body

    def is_navigation_request(self):
        return self._nav


def _run(guard, req):
    route = _Route()
    guard._handle(route, req)
    return route.done


def test_heavy_resource_types():
    assert HEAVY_RESOURCE_TYPES == frozenset({"image", "media", "font"})


@pytest.mark.parametrize("kind", ["image", "media", "font"])
def test_media_blocked_in_sync_mode(kind, caplog):
    g = Guard(block_media=True)
    caplog.set_level("DEBUG", logger="guard")
    assert _run(g, _Request(kind)) == ("abort", "blockedbyclient")
    assert g.media_blocked == 1 and g.blocked == [] and caplog.text == ""


def test_media_not_blocked_in_login_mode():
    g = Guard(login_mode=True, block_media=True)
    assert _run(g, _Request("image")) == "continue"
    assert g.media_blocked == 0


def test_media_not_blocked_by_default():
    g = Guard()
    assert _run(g, _Request("font")) == "continue" and g.media_blocked == 0


def test_block_media_leaves_other_resources_to_decide():
    g = Guard(block_media=True)
    assert _run(g, _Request("script", url="https://site.com/app.js")) == "continue"
    assert _run(g, _Request("fetch", method="POST", url="https://site.com/api/x", body="{}")) == (
        "abort", "blockedbyclient")
    assert g.media_blocked == 0 and len(g.blocked) == 1


def test_leaving_login_mode_starts_blocking_media():
    g = Guard(login_mode=True, block_media=True)
    g.login_mode = False
    assert _run(g, _Request("image")) == ("abort", "blockedbyclient")

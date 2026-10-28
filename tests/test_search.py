"""Grocery search replay: field extraction, request binding and the read-only search run (browser edges faked)."""
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest

import browser
import search

FIX = Path(__file__).parent / "fixtures"
GET = json.loads((FIX / "search_get.json").read_text(encoding="utf-8"))
STORE = json.loads((FIX / "search_store.json").read_text(encoding="utf-8"))
LOC = {"query": "12 Test Society, Gorwa, Vadodara, Gujarat", "lat": 22.3301, "lon": 73.1502}

FIELD_MAP = {"products_path": "data.widgets.*.items", "product_id": "product.id", "name": "product.name",
             "brand": "product.brand", "pack_size": "product.unit", "price": "pricing.selling_price",
             "mrp": "pricing.mrp", "in_stock": "inventory", "price_divisor": 100}
ENDPOINT = "https://api.zepto.test/api/v3/search"
LOOKUP = "https://api.zepto.test/api/v1/store/serviceability"


def get_recipe(**over):
    r = {"kind": "search", "site": "zepto", "home_url": "https://www.zepto.com/",
         "search_page_url": "https://www.zepto.com/search?query={q}", "endpoint": ENDPOINT, "method": "GET",
         "query": {"page": "0"}, "body": None, "headers": {"x-app": "web"}, "query_param": "query",
         "location_inputs": [],
         "store": {"lookup": {"method": "GET", "url": LOOKUP, "body": None},
                   "location_inputs": [{"in": "query", "name": "latitude", "value": "lat"},
                                       {"in": "query", "name": "longitude", "value": "lon"}],
                   "store_id_path": "data.store.id", "inputs": [{"in": "header", "name": "storeid"}]},
         "field_map": dict(FIELD_MAP),
         "fee_map": {"source": "search", "delivery": "delivery_fee", "handling": "handling_fee",
                     "member": "user.pass_active", "divisor": 100},
         "read_post_allowlist": []}
    r.update(over)
    return r


def query_of(url):
    return dict(parse_qsl(urlsplit(url).query))


# --- pure helpers ------------------------------------------------------------

def test_extract_products_maps_fields_and_divisor():
    rows = search.extract_products(GET, FIELD_MAP)
    assert [r["product_id"] for r in rows] == ["p-101", "p-102", "p-103"]  # banner and the zero-price row dropped
    first = rows[0]
    assert first == {"product_id": "p-101", "name": "Amul Taaza Toned Milk", "brand": "Amul", "pack_size": "500 ml",
                     "price": 29.0, "mrp": 30.0, "in_stock": True}
    assert rows[1]["in_stock"] is False  # inventory 0


def test_extract_products_without_divisor_or_stock_path():
    fm = dict(FIELD_MAP, price_divisor=1, in_stock=None, mrp=None, brand=None)
    rows = search.extract_products(GET, fm)
    assert rows[0]["price"] == 2900.0 and rows[0]["in_stock"] is True and rows[0]["mrp"] is None
    assert rows[0]["brand"] is None
    assert search.extract_products({"data": {}}, FIELD_MAP) == []


def test_extract_fee_info_reads_member_and_delivery():
    fee_map = {"source": "search", "delivery": "delivery_fee", "handling": "handling_fee",
               "small_cart": "fees.small", "member": "user.pass_active", "divisor": 100}
    assert search.extract_fee_info(GET, fee_map) == {"delivery": 0.0, "member": True}
    assert search.extract_fee_info(GET, {"member": "user.pass_name"}) == {"member": "Zepto Pass"}
    assert search.extract_fee_info(STORE, {"source": "store", "handling": "data.fees.handling",
                                           "small_cart": "data.fees.small_cart", "divisor": 100}) == \
        {"handling": 4.0, "small_cart": 15.0}
    assert search.extract_fee_info(GET, None) == {}


def test_to_stock_values():
    assert [search.to_stock(v) for v in (None, 0, 3, "OUT_OF_STOCK", "Sold out", "available")] == \
        [True, False, True, False, False, True]
    assert [search.to_stock(v) for v in (True, False, "unavailable", "0", "Currently out of stock", "IN_STOCK")] == \
        [True, False, False, False, False, True]


def test_build_request_get_with_location_header():
    r = get_recipe(store=None, location_inputs=[{"in": "header", "name": "latitude", "value": "lat"},
                                                {"in": "header", "name": "longitude", "value": "lon"}])
    req = search.build_request(r, "amul butter", LOC)
    assert req["method"] == "GET" and req["url"].startswith(ENDPOINT)
    assert query_of(req["url"]) == {"page": "0", "query": "amul butter"}
    assert req["headers"] == {"x-app": "web", "latitude": "22.3301", "longitude": "73.1502"}
    assert r["headers"] == {"x-app": "web"}  # the recipe itself is not mutated


def test_build_request_post_body_path_and_store():
    r = get_recipe(method="POST", body={"variables": {"q": "", "page": 0}, "geo": {"lat": 0, "lng": 0}},
                   query={}, query_param="variables.q",
                   location_inputs=[{"in": "body", "name": "geo.lat", "value": "lat"},
                                    {"in": "body", "name": "geo.lng", "value": "lon"}],
                   store={"lookup": {"method": "GET", "url": LOOKUP, "body": None}, "location_inputs": [],
                          "store_id_path": "data.store.id",
                          "inputs": [{"in": "body", "name": "storeId"}, {"in": "query", "name": "sid"}]})
    req = search.build_request(r, "bread", LOC, store_id="store-77")
    assert req["method"] == "POST"
    assert req["body"] == {"variables": {"q": "bread", "page": 0}, "geo": {"lat": 22.3301, "lng": 73.1502},
                           "storeId": "store-77"}
    assert query_of(req["url"]) == {"sid": "store-77"}
    assert r["body"]["variables"]["q"] == ""  # recipe not mutated


def test_build_request_needs_a_location():
    with pytest.raises(search.SearchError):
        search.build_request(get_recipe(store=None, location_inputs=[{"in": "query", "name": "lat", "value": "lat"}]),
                             "milk", {})


# --- the search run ----------------------------------------------------------

def fake_env(monkeypatch, fetch=None, capture_on_goto=None, start_url="https://www.zepto.com/"):
    """Patch the browser edges search uses. fetch(req) answers fetch_in_page; capture_on_goto(url) returns the
    captured items the site's own page would produce on that navigation."""
    seen = {"open_site": [], "goto": [], "fetch": [], "pauses": 0}
    page = SimpleNamespace(url=start_url, wait_for_timeout=lambda ms: None)
    capture = SimpleNamespace(items=[])
    capture.clear = lambda: capture.items.clear()
    guard = SimpleNamespace(allowlist=[], blocked=[])

    @contextlib.contextmanager
    def fake_open(site, **k):
        seen["open_site"].append({"site": site, **k})
        guard.allowlist = list(k.get("allowlist") or [])
        yield None, page, guard, capture

    def fake_goto(p, url, *a, **k):
        seen["goto"].append(url)
        p.url = url
        if capture_on_goto:
            capture.items += capture_on_goto(url)

    def fake_fetch(p, req):
        seen["fetch"].append(req)
        if fetch is None:
            raise RuntimeError("HTTP 500: nope")
        return fetch(req)

    monkeypatch.setattr(browser, "open_site", fake_open)
    monkeypatch.setattr(browser, "goto", fake_goto)
    monkeypatch.setattr(browser, "fetch_in_page", fake_fetch)
    monkeypatch.setattr(search, "_pause", lambda: seen.__setitem__("pauses", seen["pauses"] + 1))
    return seen


def api(req):
    if req["url"].startswith(LOOKUP):
        return STORE
    if req["url"].startswith(ENDPOINT):
        return GET
    raise AssertionError(req["url"])


def test_search_products_fast_path(monkeypatch):
    seen = fake_env(monkeypatch, fetch=api)
    out = search.search_products("zepto", ["milk", "amul gold"], LOC, get_recipe(), Path("/u/profile"))
    assert list(out["products"]) == ["milk", "amul gold"]
    assert [p["product_id"] for p in out["products"]["milk"]] == ["p-101", "p-102", "p-103"]
    assert out["fee_info"] == {"delivery": 0.0, "member": True}
    lookup, s1, s2 = seen["fetch"]
    assert query_of(lookup["url"]) == {"latitude": "22.3301", "longitude": "73.1502"}
    assert s1["headers"]["storeid"] == "store-77" and query_of(s1["url"])["query"] == "milk"
    assert query_of(s2["url"])["query"] == "amul gold"
    assert seen["goto"] == ["https://www.zepto.com/"]  # home only: no search page navigation needed
    assert seen["pauses"] == 1  # between the two queries


def test_search_respects_max_results_and_store_fees(monkeypatch):
    fake_env(monkeypatch, fetch=api)
    r = get_recipe(fee_map={"source": "store", "handling": "data.fees.handling", "divisor": 100})
    out = search.search_products("zepto", ["milk"], LOC, r, Path("/u/profile"), max_results=2)
    assert len(out["products"]["milk"]) == 2
    assert out["fee_info"] == {"handling": 4.0}


def test_search_uses_given_profile_read_only_with_media_blocked(monkeypatch):
    seen = fake_env(monkeypatch, fetch=api)
    allow = [{"url_regex": r"^https://api\.zepto\.test/api/v3/search$", "why": "product search (read-only)"}]
    search.search_products("zepto", ["milk"], LOC, get_recipe(read_post_allowlist=allow), Path("/u/profile"),
                           proxy_server="http://127.0.0.1:9000")
    (call,) = seen["open_site"]
    assert call == {"site": "zepto", "allowlist": allow, "profile_dir": Path("/u/profile"), "location": LOC,
                    "proxy_server": "http://127.0.0.1:9000", "block_media": True}
    assert "login_mode" not in call and "cart_policy" not in call


def test_search_logged_out_raises_session_expired(monkeypatch):
    seen = fake_env(monkeypatch, fetch=api)

    def to_login(p, url, *a, **k):
        seen["goto"].append(url)
        p.url = "https://www.zepto.com/login?redirect=%2F"
    monkeypatch.setattr(browser, "goto", to_login)
    with pytest.raises(browser.SessionExpired):
        search.search_products("zepto", ["milk"], LOC, get_recipe(), Path("/u/profile"))
    assert seen["fetch"] == []  # nothing is searched on a logged-out session


def test_search_api_401_raises_session_expired(monkeypatch):
    def expired(req):
        raise browser.SessionExpired("HTTP 401")
    fake_env(monkeypatch, fetch=expired)
    with pytest.raises(browser.SessionExpired):
        search.search_products("zepto", ["milk"], LOC, get_recipe(store=None, location_inputs=[
            {"in": "query", "name": "lat", "value": "lat"}, {"in": "query", "name": "lon", "value": "lon"}]),
            Path("/u/profile"))


def page_capture(url):
    if "/search?query=" not in url:
        return []
    q = query_of(url)["query"]
    return [{"id": 0, "url": f"{ENDPOINT}?query={q}&page=0", "method": "GET", "status": 200,
             "request_headers": {}, "request_body": None, "sample": "", "size": 10, "json": GET}]


def test_search_falls_back_to_search_page_capture(monkeypatch):
    def api_down(req):
        if req["url"].startswith(LOOKUP):
            return STORE
        raise RuntimeError("HTTP 500: signed request needed")
    seen = fake_env(monkeypatch, fetch=api_down, capture_on_goto=page_capture)
    out = search.search_products("zepto", ["milk", "bread"], LOC, get_recipe(), Path("/u/profile"))
    assert [p["product_id"] for p in out["products"]["bread"]] == ["p-101", "p-102", "p-103"]
    assert seen["goto"][1:] == ["https://www.zepto.com/search?query=milk", "https://www.zepto.com/search?query=bread"]
    assert out["fee_info"] == {"delivery": 0.0, "member": True}


def test_search_drift_when_both_paths_fail(monkeypatch):
    def api_down(req):
        if req["url"].startswith(LOOKUP):
            return STORE
        return {"unexpected": "shape"}
    fake_env(monkeypatch, fetch=api_down, capture_on_goto=lambda url: [])
    with pytest.raises(search.SearchDrift):
        search.search_products("zepto", ["milk", "bread"], LOC, get_recipe(), Path("/u/profile"))


def test_unserviceable_when_store_lookup_empty(monkeypatch):
    seen = fake_env(monkeypatch, fetch=lambda req: {"data": {"serviceable": False, "store": None}})
    with pytest.raises(search.Unserviceable):
        search.search_products("zepto", ["milk"], LOC, get_recipe(), Path("/u/profile"))
    assert len(seen["fetch"]) == 1  # only the lookup


def test_resolve_store_without_store_binding_is_none(monkeypatch):
    seen = fake_env(monkeypatch, fetch=api)
    assert search.resolve_store(SimpleNamespace(), get_recipe(store=None), LOC) is None
    assert seen["fetch"] == []
    assert search.resolve_store(SimpleNamespace(), get_recipe(), LOC) == "store-77"


def test_search_sites_and_pages():
    assert search.SEARCH_SITES == ("zepto", "blinkit", "instamart")
    assert all("{q}" in search.SEARCH_PAGES[s] for s in search.SEARCH_SITES)
    assert issubclass(search.SearchDrift, search.SearchError) and issubclass(search.Unserviceable, search.SearchError)

import contextlib
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest

import browser
import food_search

FIX = Path(__file__).parent / "fixtures"
REST_DATA = json.loads((FIX / "restaurants_search.json").read_text(encoding="utf-8"))
MENU_DATA = json.loads((FIX / "menu_nested.json").read_text(encoding="utf-8"))
LOC = {"query": "Sunrise Society, Gorwa, Vadodara, Gujarat", "lat": 22.33, "lon": 73.15}

REST_RECIPE = {
    "kind": "restaurants", "site": "swiggy", "search_page_url": "https://www.swiggy.com/search?query={q}",
    "endpoint": "https://www.swiggy.com/dapi/restaurants/search", "method": "GET",
    "query": {"str": "x", "lat": "0", "lng": "0"}, "body": None, "headers": {"x-app": "web"},
    "query_param": "str",
    "location_inputs": [{"in": "query", "name": "lat", "value": "lat"}, {"in": "query", "name": "lng", "value": "lon"}],
    "field_map": {"restaurants_path": "data.restaurants", "restaurant_id": "info.id", "name": "info.name",
                  "locality": "info.locality", "lat": "info.lat", "lon": "info.lng",
                  "distance_km": "info.sla.lastMileTravel", "is_open": "info.isOpen", "url": "cta.link"},
    "fee_map": {"delivery": "fees.deliveryFee", "member": "fees.isMember", "surge": "fees.surge", "divisor": 100},
    "read_post_allowlist": [],
}
MENU_FM = {"dishes_path": "data.cards.*.items.*", "dish_id": "id", "name": "name", "variant": "variant",
           "price": "price", "veg": "isVeg", "in_stock": "inStock", "packaging": "packagingCharges",
           "restaurant_open": "data.restaurant.isOpen", "price_divisor": 100}
MENU_FEES = {"delivery": "data.fees.deliveryFee", "platform": "data.fees.platformFee",
             "small_order": "data.fees.smallOrderFee", "member": "data.fees.isGold",
             "distance_km": "data.fees.distanceKm", "divisor": 100}
MENU_PARAM_RECIPE = {
    "kind": "menu", "site": "swiggy", "page_url_template": None,
    "endpoint": "https://www.swiggy.com/dapi/menu/pl", "method": "GET",
    "query": {"restaurantId": "0", "lat": "0", "lng": "0"}, "body": None, "headers": {},
    "restaurant_param": "restaurantId",
    "location_inputs": [{"in": "query", "name": "lat", "value": "lat"}, {"in": "query", "name": "lng", "value": "lon"}],
    "field_map": MENU_FM, "fee_map": MENU_FEES, "read_post_allowlist": [],
}
MENU_TEMPLATE_RECIPE = {
    "kind": "menu", "site": "zomato",
    "page_url_template": "https://www.zomato.com/vadodara/restaurant/{restaurant_id}/order",
    "endpoint": "https://www.zomato.com/webroutes/getPage", "method": "GET", "query": {}, "body": None,
    "headers": {}, "restaurant_param": None, "location_inputs": [],
    "field_map": MENU_FM, "fee_map": None, "read_post_allowlist": [],
}


def captured(url, data, method="GET", status=200, body=None):
    return {"id": 0, "url": url, "method": method, "status": status, "request_headers": {},
            "request_body": body, "sample": json.dumps(data)[:2000], "size": 100, "json": data}


def fake_env(monkeypatch, fetch=None, on_goto=None, logged_out=False):
    """Fake only the browser edges: open_site, goto and fetch_in_page. Records what was asked."""
    seen = {"open_site": [], "goto": [], "fetch": []}
    page = SimpleNamespace(url="about:blank", wait_for_timeout=lambda ms: None)
    capture = SimpleNamespace(items=[])

    @contextlib.contextmanager
    def fake_open(site, **k):
        seen["open_site"].append({"site": site, **k})
        guard = SimpleNamespace(allowlist=list(k.get("allowlist") or []), blocked=[], login_mode=False)
        yield None, page, guard, capture

    def fake_goto(p, url, *a, **k):
        seen["goto"].append(url)
        p.url = "https://www.swiggy.com/auth/login" if logged_out else url
        if on_goto:
            capture.items.extend(on_goto(url))

    def fake_fetch(p, req):
        seen["fetch"].append(copy.deepcopy(req))
        if fetch is None:
            raise RuntimeError("HTTP 500: nope")
        return fetch(req)

    monkeypatch.setattr(browser, "open_site", fake_open)
    monkeypatch.setattr(browser, "goto", fake_goto)
    monkeypatch.setattr(browser, "fetch_in_page", fake_fetch)
    return seen


# --- pure extraction ---------------------------------------------------------

def test_collect_fans_out_with_star():
    dishes = food_search.collect(MENU_DATA, "data.cards.*.items.*")
    assert [d["id"] for d in dishes] == ["d1", "d2", "d3", "d4", "d5"]
    assert food_search.collect(MENU_DATA, "data.cards.0.title") == ["Biryani"]
    assert food_search.collect(MENU_DATA, "data.cards.*.title") == ["Biryani", "Starters", "Offers"]
    assert food_search.collect(MENU_DATA, "data.nope.*") == []
    assert food_search.collect({"a": {"x": {"n": 1}, "y": {"n": 2}}}, "a.*.n") == [1, 2]  # id-keyed maps too


def test_extract_menu_variants_veg_packaging_and_open():
    menu = food_search.extract_menu(MENU_DATA, MENU_FM)
    assert menu["is_open"] is True
    d = {x["dish_id"]: x for x in menu["dishes"]}
    assert list(d) == ["d1", "d2", "d3", "d4", "d5"]
    assert d["d1"] == {"dish_id": "d1", "name": "Chicken Biryani", "variant": "Half", "price": 189.0,
                       "veg": False, "in_stock": True, "packaging": 20.0}
    assert d["d2"]["variant"] == "Full" and d["d2"]["price"] == 329.0
    assert d["d3"]["variant"] is None and d["d3"]["veg"] is True and d["d3"]["in_stock"] is False
    assert d["d3"]["packaging"] is None
    assert d["d4"]["veg"] is True and d["d4"]["in_stock"] is True and d["d4"]["packaging"] == 15.0
    assert d["d5"]["veg"] is False and d["d5"]["in_stock"] is False and d["d5"]["price"] == 279.0
    closed = copy.deepcopy(MENU_DATA)
    closed["data"]["restaurant"]["isOpen"] = "CLOSED"
    assert food_search.extract_menu(closed, MENU_FM)["is_open"] is False
    assert food_search.extract_menu(MENU_DATA, MENU_FM | {"restaurant_open": None})["is_open"] is None


def test_extract_menu_drops_rows_without_id_name_or_price():
    data = {"items": [{"id": "a", "name": "Ok", "price": 100}, {"id": "b", "name": "Free", "price": 0},
                      {"name": "No id", "price": 100}, {"id": "c", "price": 100}, "junk"]}
    fm = {"dishes_path": "items.*", "dish_id": "id", "name": "name", "price": "price"}
    assert [d["dish_id"] for d in food_search.extract_menu(data, fm)["dishes"]] == ["a"]


def test_extract_restaurants_maps_locality_and_coords():
    rows = food_search.extract_restaurants(REST_DATA, REST_RECIPE["field_map"])
    assert [r["restaurant_id"] for r in rows] == ["101", "102", "103", "104"]  # banner without id dropped
    r = rows[0]
    assert r == {"restaurant_id": "101", "name": "Behrouz Biryani", "locality": "Gorwa", "lat": 22.3301,
                 "lon": 73.1532, "distance_km": 1.4, "is_open": True, "url": "/restaurants/101"}
    assert rows[1]["lat"] == 22.3105 and rows[1]["is_open"] is False
    assert rows[2]["distance_km"] == 2.1 and rows[2]["is_open"] is True
    assert rows[3]["locality"] is None and rows[3]["lat"] is None and rows[3]["is_open"] is True


def test_extract_fee_info_only_resolved_keys():
    assert food_search.extract_fee_info(REST_DATA, REST_RECIPE["fee_map"]) == {"delivery": 29.0, "member": True}
    assert food_search.extract_fee_info(MENU_DATA, MENU_FEES) == {
        "delivery": 40.0, "platform": 10.0, "small_order": 0.0, "member": False, "distance_km": 1.4}
    assert food_search.extract_fee_info(MENU_DATA, None) == {}


def test_to_stock_and_veg_values():
    assert [food_search.to_stock(v) for v in (None, 0, 3, "OUT_OF_STOCK", "Sold out", "available")] == \
        [True, False, True, False, False, True]
    assert [food_search.to_veg(v) for v in (None, True, 0, "VEG", "NON_VEG", "non-veg", "EGG", "?")] == \
        [None, True, False, True, False, False, False, None]


# --- replay on the user's session --------------------------------------------

def test_search_restaurants_fast_path_on_user_profile(monkeypatch, tmp_path):
    seen = fake_env(monkeypatch, fetch=lambda req: REST_DATA)
    out = food_search.search_restaurants("swiggy", "pizza", LOC, REST_RECIPE, tmp_path / "user-swiggy")
    assert [r["restaurant_id"] for r in out["restaurants"]] == ["101", "102", "103", "104"]
    assert out["restaurants"][0]["url"] == "https://www.swiggy.com/restaurants/101"
    assert out["restaurants"][2]["url"] == "https://www.swiggy.com/restaurants/103"
    assert out["fee_info"] == {"delivery": 29.0, "member": True}
    (opened,) = seen["open_site"]
    assert opened["profile_dir"] == tmp_path / "user-swiggy" and opened["location"] == LOC
    assert "login_mode" not in opened and opened["block_media"] is True
    assert opened["allowlist"] == []
    assert seen["goto"] == ["https://www.swiggy.com/"]  # home only: the fast path never opens a search page
    (req,) = seen["fetch"]
    q = dict(parse_qsl(urlsplit(req["url"]).query))
    assert q == {"str": "pizza", "lat": "22.33", "lng": "73.15"}
    assert req["method"] == "GET" and req["headers"] == {"x-app": "web"}


def test_search_restaurants_caps_results(monkeypatch, tmp_path):
    fake_env(monkeypatch, fetch=lambda req: REST_DATA)
    out = food_search.search_restaurants("swiggy", "pizza", LOC, REST_RECIPE, tmp_path, max_results=2)
    assert len(out["restaurants"]) == 2


def test_search_restaurants_falls_back_to_search_page_capture(monkeypatch, tmp_path):
    def on_goto(url):
        if "/search?" in url:
            return [captured("https://www.swiggy.com/dapi/restaurants/search?str=pizza", REST_DATA)]
        return []
    seen = fake_env(monkeypatch, fetch=None, on_goto=on_goto)
    out = food_search.search_restaurants("swiggy", "pizza", LOC, REST_RECIPE, tmp_path)
    assert len(out["restaurants"]) == 4
    assert seen["goto"][-1] == "https://www.swiggy.com/search?query=pizza"


def test_search_restaurants_post_body_binding(monkeypatch, tmp_path):
    recipe = REST_RECIPE | {
        "method": "POST", "query": {}, "body": {"q": {"text": "x"}, "geo": {"lat": 0, "lng": 0}},
        "query_param": "q.text",
        "location_inputs": [{"in": "body", "name": "geo.lat", "value": "lat"},
                            {"in": "body", "name": "geo.lng", "value": "lon"}],
        "read_post_allowlist": [{"url_regex": r"^https://www\.swiggy\.com/dapi/restaurants/search$", "why": "r"}]}
    seen = fake_env(monkeypatch, fetch=lambda req: REST_DATA)
    food_search.search_restaurants("swiggy", "pizza", LOC, recipe, tmp_path)
    (req,) = seen["fetch"]
    assert req["method"] == "POST" and req["body"] == {"q": {"text": "pizza"}, "geo": {"lat": 22.33, "lng": 73.15}}
    assert seen["open_site"][0]["allowlist"] == recipe["read_post_allowlist"]


def test_search_restaurants_without_saved_location_is_unserviceable(monkeypatch, tmp_path):
    fake_env(monkeypatch, fetch=lambda req: REST_DATA)
    with pytest.raises(food_search.Unserviceable):
        food_search.search_restaurants("swiggy", "pizza", {}, REST_RECIPE, tmp_path)


def test_fetch_menu_by_param_and_by_page_template(monkeypatch, tmp_path):
    seen = fake_env(monkeypatch, fetch=lambda req: MENU_DATA)
    out = food_search.fetch_menu("swiggy", "101", LOC, MENU_PARAM_RECIPE, tmp_path / "u")
    assert out["is_open"] is True and len(out["dishes"]) == 5
    assert out["fee_info"]["delivery"] == 40.0 and out["fee_info"]["member"] is False
    q = dict(parse_qsl(urlsplit(seen["fetch"][0]["url"]).query))
    assert q == {"restaurantId": "101", "lat": "22.33", "lng": "73.15"}
    assert seen["open_site"][0]["profile_dir"] == tmp_path / "u" and "login_mode" not in seen["open_site"][0]

    def on_goto(url):
        if url == "https://www.zomato.com/vadodara/restaurant/77/order":
            return [captured("https://www.zomato.com/webroutes/getPage?page_url=x", MENU_DATA)]
        return []
    seen = fake_env(monkeypatch, fetch=None, on_goto=on_goto)
    out = food_search.fetch_menu("zomato", "77", LOC, MENU_TEMPLATE_RECIPE, tmp_path / "u")
    assert out == {"is_open": True, "dishes": out["dishes"], "fee_info": {}} and len(out["dishes"]) == 5
    assert seen["fetch"] == []  # no restaurant_param: nothing to replay, the page is the only path
    assert seen["goto"][-1] == "https://www.zomato.com/vadodara/restaurant/77/order"


def test_fetch_menu_uses_listing_url_when_given(monkeypatch, tmp_path):
    def on_goto(url):
        return [captured("https://www.zomato.com/webroutes/getPage", MENU_DATA)] if "/the-place" in url else []
    seen = fake_env(monkeypatch, fetch=None, on_goto=on_goto)
    out = food_search.fetch_menu("zomato", "77", LOC, MENU_TEMPLATE_RECIPE, tmp_path,
                                 url="https://www.zomato.com/vadodara/the-place/order")
    assert len(out["dishes"]) == 5 and seen["goto"][-1].endswith("/the-place/order")


def test_food_logged_out_raises_session_expired(monkeypatch, tmp_path):
    seen = fake_env(monkeypatch, fetch=lambda req: MENU_DATA, logged_out=True)
    with pytest.raises(browser.SessionExpired):
        food_search.search_restaurants("swiggy", "pizza", LOC, REST_RECIPE, tmp_path)
    with pytest.raises(browser.SessionExpired):
        food_search.fetch_menu("swiggy", "101", LOC, MENU_PARAM_RECIPE, tmp_path)
    assert seen["fetch"] == []

    def expired(req):
        raise browser.SessionExpired("HTTP 401")
    fake_env(monkeypatch, fetch=expired)
    with pytest.raises(browser.SessionExpired):  # a 401 from the API is a logout, never drift
        food_search.fetch_menu("swiggy", "101", LOC, MENU_PARAM_RECIPE, tmp_path)


def raiser(exc):
    def fetch(req):
        raise exc
    return fetch


def test_menu_drift_when_both_paths_fail(monkeypatch, tmp_path):
    missing = {"data": {"changed": True}}  # a 2xx whose dishes path no longer resolves
    template = MENU_PARAM_RECIPE | {"page_url_template": "https://www.swiggy.com/restaurants/{restaurant_id}"}
    fake_env(monkeypatch, fetch=lambda req: missing, on_goto=lambda url: [])
    with pytest.raises(food_search.SearchDrift):
        food_search.fetch_menu("swiggy", "101", LOC, template, tmp_path)
    fake_env(monkeypatch, fetch=lambda req: missing, on_goto=lambda url: [])
    with pytest.raises(food_search.SearchDrift):  # no page to fall back to
        food_search.fetch_menu("swiggy", "101", LOC, MENU_PARAM_RECIPE, tmp_path)
    renamed = copy.deepcopy(MENU_DATA)
    for card in renamed["data"]["cards"]:
        card["dishes"] = card.pop("items", [])
    fake_env(monkeypatch, fetch=lambda req: renamed, on_goto=lambda url: [])
    with pytest.raises(food_search.SearchDrift):  # categories still there, items renamed: drift, not "closed"
        food_search.fetch_menu("swiggy", "101", LOC, MENU_PARAM_RECIPE, tmp_path)
    fake_env(monkeypatch, fetch=raiser(json.JSONDecodeError("x", "<html>", 0)), on_goto=lambda url: [])
    with pytest.raises(food_search.SearchTransient):  # a 2xx that is not JSON, alone: maybe a captive portal
        food_search.fetch_menu("swiggy", "101", LOC, MENU_PARAM_RECIPE, tmp_path)
    fake_env(monkeypatch, fetch=lambda req: {"data": {}}, on_goto=lambda url: [])
    with pytest.raises(food_search.SearchDrift):
        food_search.search_restaurants("swiggy", "pizza", LOC, REST_RECIPE, tmp_path)

    def on_goto(url):  # the page's own call returns 2xx without the path
        return [captured("https://www.swiggy.com/dapi/menu/pl?restaurantId=101", missing)]
    fake_env(monkeypatch, fetch=raiser(RuntimeError("HTTP 503: busy")), on_goto=on_goto)
    with pytest.raises(food_search.SearchDrift):
        food_search.fetch_menu("swiggy", "101", LOC, template, tmp_path)
    for cls in (food_search.SearchDrift, food_search.SearchTransient, food_search.Unserviceable):
        assert issubclass(cls, food_search.SearchError)


@pytest.mark.parametrize("exc", [RuntimeError("HTTP 503: busy"), RuntimeError("HTTP 429: slow down"),
                                 RuntimeError("HTTP 502: bad gateway"), Exception("net::ERR_CONNECTION_RESET"),
                                 TimeoutError("Timeout 60000ms exceeded")])
def test_transient_errors_are_not_drift(monkeypatch, tmp_path, exc):
    template = MENU_PARAM_RECIPE | {"page_url_template": "https://www.swiggy.com/restaurants/{restaurant_id}"}
    fake_env(monkeypatch, fetch=raiser(exc), on_goto=lambda url: [])
    with pytest.raises(food_search.SearchTransient):
        food_search.fetch_menu("swiggy", "101", LOC, template, tmp_path)
    fake_env(monkeypatch, fetch=raiser(exc), on_goto=lambda url: [])
    with pytest.raises(food_search.SearchTransient):
        food_search.search_restaurants("swiggy", "pizza", LOC, REST_RECIPE, tmp_path)


MENU_PAGE_RECIPE = MENU_PARAM_RECIPE | {"page_url_template": "https://www.swiggy.com/restaurants/{restaurant_id}"}
MENU_API = "https://www.swiggy.com/dapi/menu/pl?restaurantId=101"
SEARCH_API = "https://www.swiggy.com/dapi/restaurants/search?str=pizza"


def test_food_410_alone_is_drift(monkeypatch, tmp_path):
    fake_env(monkeypatch, fetch=raiser(RuntimeError("HTTP 410: gone")),
             on_goto=lambda url: [captured(MENU_API, {}, status=503)] if "/restaurants/" in url else [])
    with pytest.raises(food_search.SearchDrift):
        food_search.fetch_menu("swiggy", "101", LOC, MENU_PAGE_RECIPE, tmp_path)
    fake_env(monkeypatch, fetch=raiser(RuntimeError("HTTP 410: gone")))
    with pytest.raises(food_search.SearchDrift):
        food_search.fetch_menu("swiggy", "101", LOC, MENU_PARAM_RECIPE, tmp_path)  # no page at all


def test_food_404_is_drift_only_when_page_loads_without_calling_the_endpoint(monkeypatch, tmp_path):
    fake_env(monkeypatch, fetch=raiser(RuntimeError("HTTP 404: missing")), on_goto=lambda url: [])
    with pytest.raises(food_search.SearchDrift):
        food_search.search_restaurants("swiggy", "pizza", LOC, REST_RECIPE, tmp_path)
    fake_env(monkeypatch, fetch=raiser(RuntimeError("HTTP 404: missing")), on_goto=lambda url: [])
    with pytest.raises(food_search.SearchDrift):
        food_search.fetch_menu("swiggy", "101", LOC, MENU_PAGE_RECIPE, tmp_path)
    fake_env(monkeypatch, fetch=raiser(RuntimeError("HTTP 404: missing")))
    with pytest.raises(food_search.SearchTransient):  # no page to confirm the endpoint is gone
        food_search.fetch_menu("swiggy", "101", LOC, MENU_PARAM_RECIPE, tmp_path)
    fake_env(monkeypatch, fetch=raiser(RuntimeError("HTTP 400: bad request")), on_goto=lambda url: [])
    with pytest.raises(food_search.SearchTransient):  # any other 4xx
        food_search.search_restaurants("swiggy", "pizza", LOC, REST_RECIPE, tmp_path)


def test_food_unparseable_is_drift_only_with_page_2xx_missing_path(monkeypatch, tmp_path):
    not_json = raiser(json.JSONDecodeError("x", "<html>", 0))
    fake_env(monkeypatch, fetch=not_json, on_goto=lambda url: [captured(SEARCH_API, {"data": {}})])
    with pytest.raises(food_search.SearchDrift):
        food_search.search_restaurants("swiggy", "pizza", LOC, REST_RECIPE, tmp_path)
    fake_env(monkeypatch, fetch=not_json, on_goto=lambda url: [])
    with pytest.raises(food_search.SearchTransient):
        food_search.search_restaurants("swiggy", "pizza", LOC, REST_RECIPE, tmp_path)


def test_food_uses_the_grocery_search_errors_and_helpers():
    import search
    assert food_search.SearchDrift is search.SearchDrift and food_search.SearchTransient is search.SearchTransient
    assert food_search.Unserviceable is search.Unserviceable and food_search.to_stock is search.to_stock
    assert not hasattr(food_search, "_same_endpoint")


def test_transient_page_capture_and_navigation_errors(monkeypatch, tmp_path):
    def busy(url):
        if "/search?" in url:
            return [captured("https://www.swiggy.com/dapi/restaurants/search?str=pizza", {"err": 1}, status=503)]
        return []
    fake_env(monkeypatch, fetch=raiser(RuntimeError("HTTP 404: gone")), on_goto=busy)
    with pytest.raises(food_search.SearchTransient):
        food_search.search_restaurants("swiggy", "pizza", LOC, REST_RECIPE, tmp_path)

    seen = fake_env(monkeypatch, fetch=raiser(RuntimeError("HTTP 500: x")))
    real_goto = browser.goto

    def flaky_goto(p, url, *a, **k):
        if "/restaurants/" in url:
            raise Exception("net::ERR_TIMED_OUT")
        real_goto(p, url)
    monkeypatch.setattr(browser, "goto", flaky_goto)
    with pytest.raises(food_search.SearchTransient):
        food_search.fetch_menu("swiggy", "101", LOC, MENU_PARAM_RECIPE | {
            "page_url_template": "https://www.swiggy.com/restaurants/{restaurant_id}"}, tmp_path)
    assert seen["goto"] == ["https://www.swiggy.com/"]


@pytest.mark.parametrize("status", [401, 403])
def test_page_capture_logout_raises_session_expired(monkeypatch, tmp_path, status):
    def on_goto(url):
        if "/restaurants/101" in url:
            return [captured("https://www.swiggy.com/dapi/menu/pl?restaurantId=101", {"e": 1}, status=status)]
        if "/search?" in url:
            return [captured("https://www.swiggy.com/dapi/restaurants/search?str=p", {"e": 1}, status=status)]
        return []
    fake_env(monkeypatch, fetch=raiser(RuntimeError("HTTP 404: gone")), on_goto=on_goto)
    with pytest.raises(browser.SessionExpired):
        food_search.fetch_menu("swiggy", "101", LOC, MENU_PARAM_RECIPE | {
            "page_url_template": "https://www.swiggy.com/restaurants/{restaurant_id}"}, tmp_path)
    with pytest.raises(browser.SessionExpired):
        food_search.search_restaurants("swiggy", "pizza", LOC, REST_RECIPE, tmp_path)


def test_empty_menu_and_closed_restaurant_are_not_drift(monkeypatch, tmp_path):
    closed_empty = {"data": {"restaurant": {"isOpen": False}}}  # no categories at all
    no_cards = {"data": {"restaurant": {"isOpen": True}, "cards": []}}
    empty_cards = {"data": {"restaurant": {}, "cards": [{"title": "Biryani", "items": []}]}}
    for data in (closed_empty, no_cards, empty_cards):
        fake_env(monkeypatch, fetch=lambda req, d=data: d)
        out = food_search.fetch_menu("swiggy", "101", LOC, MENU_PARAM_RECIPE, tmp_path)
        assert out == {"is_open": False, "dishes": [], "fee_info": {}}
    closed_full = copy.deepcopy(MENU_DATA)
    closed_full["data"]["restaurant"]["isOpen"] = False
    fake_env(monkeypatch, fetch=lambda req: closed_full)
    out = food_search.fetch_menu("swiggy", "101", LOC, MENU_PARAM_RECIPE, tmp_path)
    assert out["is_open"] is False and len(out["dishes"]) == 5  # closed: dishes shown, never priced as open

    def on_goto(url):  # the page-capture path treats a closed restaurant the same way
        return [captured("https://www.zomato.com/webroutes/getPage", closed_empty)] if "/77/" in url else []
    fake_env(monkeypatch, fetch=None, on_goto=on_goto)
    out = food_search.fetch_menu("zomato", "77", LOC, MENU_TEMPLATE_RECIPE, tmp_path)
    assert out == {"is_open": False, "dishes": [], "fee_info": {}}


def test_fetch_menu_refuses_other_hosts(monkeypatch, tmp_path):
    seen = fake_env(monkeypatch, fetch=lambda req: MENU_DATA)
    for url in ("https://evil.example.com/restaurants/101", "https://swiggy.com.evil.in/r/1",
                "http://www.swiggy.com/restaurants/101", "javascript:alert(1)"):
        with pytest.raises(ValueError):
            food_search.fetch_menu("swiggy", "101", LOC, MENU_PARAM_RECIPE, tmp_path, url=url)
    with pytest.raises(ValueError):
        food_search.fetch_menu("zomato", "77", LOC, MENU_TEMPLATE_RECIPE | {
            "page_url_template": "https://www.swiggy.com/r/{restaurant_id}"}, tmp_path)
    assert seen["open_site"] == []  # refused before any browser opens
    out = food_search.fetch_menu("swiggy", "101", LOC, MENU_PARAM_RECIPE, tmp_path,
                                 url="https://swiggy.com/restaurants/101")
    assert len(out["dishes"]) == 5


def test_search_with_no_results_is_not_drift(monkeypatch, tmp_path):
    fake_env(monkeypatch, fetch=lambda req: {"data": {"restaurants": []}})
    out = food_search.search_restaurants("swiggy", "zzqx", LOC, REST_RECIPE, tmp_path)
    assert out == {"restaurants": [], "fee_info": {}}


def test_food_pages_and_sites():
    assert food_search.FOOD_SITES == ("swiggy", "zomato")
    assert food_search.RESTAURANT_PROBE == "pizza"
    assert food_search.food_page_url("swiggy", "veg pizza", LOC) == "https://www.swiggy.com/search?query=veg%20pizza"
    assert food_search.food_page_url("zomato", "pizza", LOC) == "https://www.zomato.com/vadodara/delivery?q=pizza"


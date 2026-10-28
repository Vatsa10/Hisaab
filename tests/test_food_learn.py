import contextlib
import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest

import agent
import browser
import food_learn
import food_search
import guard as guard_mod
import sync

FIX = Path(__file__).parent / "fixtures"
REST_DATA = json.loads((FIX / "restaurants_search.json").read_text(encoding="utf-8"))
MENU_DATA = json.loads((FIX / "menu_nested.json").read_text(encoding="utf-8"))
LOC = {"query": "Sunrise Society, Gorwa, Vadodara, Gujarat", "lat": 22.33, "lon": 73.15}

SEARCH_GET = "https://www.swiggy.com/dapi/restaurants/search?str=pizza&lat=22.33&lng=73.15&page=1"
SEARCH_POST = "https://www.swiggy.com/dapi/restaurants/search"
MENU_GET = "https://www.swiggy.com/dapi/menu/pl?restaurantId=101&lat=22.33&lng=73.15"
POST_REGEX = r"^https://www\.swiggy\.com/dapi/restaurants/search$"

REST_FM = {"restaurants_path": "data.restaurants", "restaurant_id": "info.id", "name": "info.name",
           "locality": "info.locality", "lat": "info.lat", "lon": "info.lng",
           "distance_km": "info.sla.lastMileTravel", "is_open": "info.isOpen", "url": "cta.link"}
MENU_FM = {"dishes_path": "data.cards.*.items.*", "dish_id": "id", "name": "name", "variant": "variant",
           "price": "price", "veg": "isVeg", "in_stock": "inStock", "packaging": "packagingCharges",
           "restaurant_open": "data.restaurant.isOpen", "price_divisor": 100}


def cap(url, data, method="GET", body=None):
    return {"url": url, "method": method, "status": 200, "request_headers": {"accept": "application/json"},
            "request_body": body, "sample": json.dumps(data)[:20000], "size": len(json.dumps(data)), "json": data}


def fake_learn_env(monkeypatch, post_search=False, logged_out=False, login_button=False):
    """Fake browser edges for learning and its replay. The probe page makes the site's search call (GET, or a POST
    the read-only guard blocks until its regex is allowlisted); a restaurant page makes the menu call.
    login_button: the page stays on the site but still shows a "Login" button (a logged-out profile)."""
    seen = {"open_site": [], "goto": [], "fetch": [], "guards": [], "snapshots": 0}

    def fake_snapshot(page):
        seen["snapshots"] += 1
        return {"elements": [{"text": "Login"}] if login_button else [{"text": "Search"}, {"text": "Account"}]}
    monkeypatch.setattr(agent, "snapshot", fake_snapshot)

    @contextlib.contextmanager
    def fake_open(site, **k):
        seen["open_site"].append({"site": site, **k})
        page = SimpleNamespace(url="about:blank", wait_for_timeout=lambda ms: None)
        g = SimpleNamespace(allowlist=list(k.get("allowlist") or []), blocked=[], login_mode=False)
        capture = SimpleNamespace(items=[])
        page._g, page._c = g, capture
        seen["guards"].append(g)
        yield None, page, g, capture

    def add(page, c):
        page._c.items.append(c | {"id": len(page._c.items)})

    def fake_goto(page, url, *a, **k):
        seen["goto"].append(url)
        page.url = "https://www.swiggy.com/auth/login" if logged_out else url
        if logged_out:
            return
        if "/search?query=" in url:
            add(page, cap("https://www.swiggy.com/dapi/landing/collections?lat=22.33", {"cards": [{"x": 1}]}))
            if post_search:
                body = json.dumps({"query": "pizza", "lat": 22.33, "lng": 73.15, "page": 1})
                if guard_mod.allowlisted(SEARCH_POST, body, page._g.allowlist):
                    add(page, cap(SEARCH_POST, REST_DATA, method="POST", body=body))
                else:
                    page._g.blocked.append({"method": "POST", "url": SEARCH_POST, "body": body,
                                            "reason": "POST not allowlisted"})
            else:
                add(page, cap(SEARCH_GET, REST_DATA))
        elif re.search(r"/restaurants/\d+$", url):
            rid = url.rsplit("/", 1)[1]
            add(page, cap(MENU_GET.replace("=101", f"={rid}"), MENU_DATA))

    def fake_fetch(page, req):
        seen["fetch"].append(copy.deepcopy(req))
        if "/dapi/restaurants/search" in req["url"]:
            return REST_DATA
        if "/dapi/menu/pl" in req["url"]:
            return MENU_DATA
        raise RuntimeError("HTTP 404")

    monkeypatch.setattr(browser, "open_site", fake_open)
    monkeypatch.setattr(browser, "goto", fake_goto)
    monkeypatch.setattr(browser, "fetch_in_page", fake_fetch)
    return seen


def candidate_id(prompt: str, url_part: str) -> int:
    return int(re.search(rf"#(\d+) \w+ \S*{re.escape(url_part)}", prompt)[1])


def stub_llm(calls, bad_first=False):
    def llm(messages):
        calls.append(copy.deepcopy(messages))
        prompt = messages[0]["content"]
        if "kind: restaurants" in prompt:
            if bad_first and sum("kind: restaurants" in c[0]["content"] for c in calls) == 1:
                return {"candidate": candidate_id(prompt, "/restaurants/search"),
                        "field_map": REST_FM | {"restaurants_path": "data.wrong"}, "fee_map": None}
            return {"candidate": candidate_id(prompt, "/restaurants/search"), "field_map": REST_FM,
                    "fee_map": {"delivery": "fees.deliveryFee", "member": "fees.isMember", "surge": "fees.nope",
                                "divisor": 100}}
        assert "kind: menu" in prompt
        return json.dumps({"candidate": candidate_id(prompt, "/menu/pl"), "field_map": MENU_FM,
                           "fee_map": {"delivery": "data.fees.deliveryFee", "platform": "data.fees.platformFee",
                                       "member": "data.fees.isGold", "distance_km": "data.fees.distanceKm",
                                       "packaging": "data.restaurant.name", "divisor": 100}})
    return llm


def test_learn_food_returns_both_recipes(monkeypatch, tmp_path):
    seen = fake_learn_env(monkeypatch)
    calls = []
    out = food_learn.learn_food("swiggy", LOC, tmp_path / "admin-swiggy", llm=stub_llm(calls))
    rest, menu = out["restaurants"], out["menu"]

    assert rest["kind"] == "restaurants" and rest["site"] == "swiggy"
    assert rest["search_page_url"] == "https://www.swiggy.com/search?query={q}"
    assert rest["endpoint"] == "https://www.swiggy.com/dapi/restaurants/search" and rest["method"] == "GET"
    assert rest["query_param"] == "str" and rest["query"]["page"] == "1"
    assert sorted((i["in"], i["name"], i["value"]) for i in rest["location_inputs"]) == [
        ("query", "lat", "lat"), ("query", "lng", "lon")]
    assert rest["field_map"] == REST_FM
    assert rest["fee_map"] == {"delivery": "fees.deliveryFee", "member": "fees.isMember", "divisor": 100}
    assert rest["read_post_allowlist"] == []

    assert menu["kind"] == "menu" and menu["site"] == "swiggy"
    assert menu["endpoint"] == "https://www.swiggy.com/dapi/menu/pl" and menu["restaurant_param"] == "restaurantId"
    assert menu["page_url_template"] == "https://www.swiggy.com/restaurants/{restaurant_id}"
    assert menu["field_map"] == MENU_FM
    assert menu["fee_map"] == {"delivery": "data.fees.deliveryFee", "platform": "data.fees.platformFee",
                               "member": "data.fees.isGold", "distance_km": "data.fees.distanceKm", "divisor": 100}
    assert food_learn.check_food_recipe(rest) is None and food_learn.check_food_recipe(menu) is None

    # learned on the admin's profile (never login mode), then validated by replay on that same profile
    assert {o["profile_dir"] for o in seen["open_site"]} == {tmp_path / "admin-swiggy"}
    assert len(seen["open_site"]) == 3 and all("login_mode" not in o for o in seen["open_site"])
    assert "https://www.swiggy.com/restaurants/101" in seen["goto"]  # the first open restaurant's page
    menu_replays = [dict(parse_qsl(urlsplit(r["url"]).query)) for r in seen["fetch"] if "/menu/" in r["url"]]
    assert menu_replays == [{"restaurantId": "103", "lat": "22.33", "lng": "73.15"}]  # a second open restaurant
    assert any("/restaurants/search" in r["url"] for r in seen["fetch"])
    assert len(calls) == 2


def test_learn_food_retries_field_map_with_error_feedback(monkeypatch, tmp_path):
    fake_learn_env(monkeypatch)
    calls = []
    out = food_learn.learn_food("swiggy", LOC, tmp_path, llm=stub_llm(calls, bad_first=True))
    assert out["restaurants"]["field_map"] == REST_FM
    assert len(calls) == 3
    retry = calls[1]
    assert retry[-1]["role"] == "user" and "Validation failed" in retry[-1]["content"]
    assert "data.wrong" in retry[-1]["content"] or "restaurants" in retry[-1]["content"]


def test_learn_food_post_needs_approval(monkeypatch, tmp_path):
    seen = fake_learn_env(monkeypatch, post_search=True)
    asked = []
    with pytest.raises(sync.RecipeError, match="POST allowlist rejected"):
        food_learn.learn_food("swiggy", LOC, tmp_path, llm=stub_llm([]),
                              approve=lambda entries: asked.append(entries) or False)
    # one call with the list of proposed entries (Task 4's approve(entries) -> bool)
    assert asked == [[{"url_regex": POST_REGEX, "why": "restaurant search (read-only)"}]]
    assert seen["guards"][0].allowlist == []  # a rejected entry never joins the guard

    seen = fake_learn_env(monkeypatch, post_search=True)
    asked = []
    out = food_learn.learn_food("swiggy", LOC, tmp_path, llm=stub_llm([]),
                                approve=lambda entries: asked.append(entries) or True)
    assert len(asked) == 1 and isinstance(asked[0], list)
    rest = out["restaurants"]
    assert rest["method"] == "POST" and rest["query_param"] == "query"
    assert rest["body"] == {"query": "pizza", "lat": 22.33, "lng": 73.15, "page": 1}
    assert sorted((i["in"], i["name"]) for i in rest["location_inputs"]) == [("body", "lat"), ("body", "lng")]
    assert rest["read_post_allowlist"] == [{"url_regex": POST_REGEX, "why": "restaurant search (read-only)"}]
    probe = "https://www.swiggy.com/search?query=pizza"
    assert seen["goto"].count(probe) == 2  # reloaded once the entry was approved
    # the replay opens with the approved entry so the POST is allowed
    assert seen["open_site"][1]["allowlist"] == rest["read_post_allowlist"]


def test_learn_food_post_without_approver_is_rejected(monkeypatch, tmp_path):
    fake_learn_env(monkeypatch, post_search=True)
    with pytest.raises(sync.RecipeError, match="POST allowlist rejected"):
        food_learn.learn_food("swiggy", LOC, tmp_path, llm=stub_llm([]))


def test_learn_food_preapproved_post_does_not_ask(monkeypatch, tmp_path):
    seen = fake_learn_env(monkeypatch, post_search=True)
    out = food_learn.learn_food("swiggy", LOC, tmp_path, llm=stub_llm([]), preapproved=(POST_REGEX,),
                                approve=lambda e: pytest.fail("must not ask"))
    assert out["restaurants"]["method"] == "POST"
    assert out["restaurants"]["read_post_allowlist"][0]["url_regex"] == POST_REGEX
    assert seen["guards"][0].allowlist[0]["url_regex"] == POST_REGEX


def test_learn_food_requires_logged_in_profile(monkeypatch, tmp_path):
    fake_learn_env(monkeypatch, logged_out=True)
    calls = []
    with pytest.raises(browser.SessionExpired):
        food_learn.learn_food("swiggy", LOC, tmp_path, llm=stub_llm(calls))
    assert calls == []


def test_learn_food_refuses_profile_showing_login_button(monkeypatch, tmp_path):
    seen = fake_learn_env(monkeypatch, login_button=True)
    calls = []
    with pytest.raises(browser.SessionExpired, match="Login"):
        food_learn.learn_food("swiggy", LOC, tmp_path, llm=stub_llm(calls))
    assert calls == [] and seen["snapshots"] == 1
    assert not any("/search?" in u for u in seen["goto"])  # checked before the probe


def test_learn_food_approve_must_be_exactly_true(monkeypatch, tmp_path):
    fake_learn_env(monkeypatch, post_search=True)
    with pytest.raises(sync.RecipeError, match="POST allowlist rejected"):
        food_learn.learn_food("swiggy", LOC, tmp_path, llm=stub_llm([]), approve=lambda entries: "yes")


def test_learn_food_fails_when_replay_finds_too_few_dishes(monkeypatch, tmp_path):
    fake_learn_env(monkeypatch)
    real = browser.fetch_in_page

    def thin(page, req):
        data = real(page, req)
        if "/menu/" in req["url"]:
            data = copy.deepcopy(data)
            data["data"]["cards"] = data["data"]["cards"][1:]  # 2 dishes
        return data
    monkeypatch.setattr(browser, "fetch_in_page", thin)
    with pytest.raises(sync.RecipeError, match="replay"):
        food_learn.learn_food("swiggy", LOC, tmp_path, llm=stub_llm([]))


def test_zomato_probe_needs_a_city(monkeypatch, tmp_path):
    fake_learn_env(monkeypatch)
    with pytest.raises(sync.RecipeError, match="city"):
        food_learn.learn_food("zomato", {"query": "22.3,73.1", "lat": 22.3, "lon": 73.1}, tmp_path,
                              llm=stub_llm([]))
    with pytest.raises(ValueError):
        food_learn.learn_food("instamart", LOC, tmp_path, llm=stub_llm([]))


def test_find_query_param_and_location_inputs():
    c = cap(SEARCH_GET, REST_DATA)
    assert food_learn.find_query_param(c, "pizza") == "str"
    assert food_learn.find_location_inputs(c, 22.33, 73.15) == [
        {"in": "query", "name": "lat", "value": "lat"}, {"in": "query", "name": "lng", "value": "lon"}]
    body = json.dumps({"variables": {"q": "pizza", "geo": {"latitude": "22.3300", "longitude": 73.1501}}})
    c = cap(SEARCH_POST, REST_DATA, method="POST", body=body)
    assert food_learn.find_query_param(c, "pizza") == "variables.q"
    assert food_learn.find_location_inputs(c, 22.33, 73.15) == [
        {"in": "body", "name": "variables.geo.latitude", "value": "lat"},
        {"in": "body", "name": "variables.geo.longitude", "value": "lon"}]
    c = cap("https://x.in/api/s", REST_DATA) | {"request_headers": {"x-lat": "22.33", "x-lng": "73.15"}}
    assert food_learn.find_location_inputs(c, 22.33, 73.15) == [
        {"in": "header", "name": "x-lat", "value": "lat"}, {"in": "header", "name": "x-lng", "value": "lon"}]
    assert food_learn.find_query_param(c, "pizza") is None


def test_check_food_recipe_rules():
    check = food_learn.check_food_recipe
    rest = {"kind": "restaurants", "site": "swiggy", "search_page_url": "https://www.swiggy.com/search?query={q}",
            "endpoint": "https://www.swiggy.com/dapi/restaurants/search", "method": "GET", "query": {}, "body": None,
            "headers": {}, "query_param": "str",
            "location_inputs": [{"in": "query", "name": "lat", "value": "lat"},
                                {"in": "query", "name": "lng", "value": "lon"}],
            "field_map": REST_FM, "fee_map": None, "read_post_allowlist": []}
    menu = {"kind": "menu", "site": "swiggy", "page_url_template": None,
            "endpoint": "https://www.swiggy.com/dapi/menu/pl", "method": "GET", "query": {}, "body": None,
            "headers": {}, "restaurant_param": "restaurantId", "location_inputs": [], "field_map": MENU_FM,
            "fee_map": None, "read_post_allowlist": []}
    assert check(rest) is None and check(menu) is None
    assert check(menu | {"restaurant_param": None,
                         "page_url_template": "https://www.swiggy.com/r/{restaurant_id}"}) is None
    assert "location" in check(rest | {"location_inputs": []})
    assert "location" in check(rest | {"location_inputs": [{"in": "query", "name": "lat", "value": "lat"}]})
    assert "restaurant_param" in check(menu | {"restaurant_param": None, "page_url_template": None})
    assert "{restaurant_id}" in check(menu | {"restaurant_param": None, "page_url_template": "https://x.in/r"})
    assert "query_param" in check(rest | {"query_param": None})
    assert "blocklist" in check(rest | {"endpoint": "https://www.swiggy.com/dapi/cart/search"})
    assert "covered" in check(rest | {"method": "POST"})
    assert "broad" in check(rest | {"method": "POST", "read_post_allowlist": [{"url_regex": ".*", "why": ""}]})
    # matches every path on the host except the bare root: caught by the https://{host}/x probe
    assert "broad" in check(rest | {"method": "POST", "read_post_allowlist": [
        {"url_regex": r"^https://www\.swiggy\.com/.+", "why": ""}]})
    assert "host" in check(menu | {"page_url_template": "https://evil.example.com/r/{restaurant_id}"})
    assert "kind" in check(rest | {"kind": "search"})
    assert "dish_id" in check(menu | {"field_map": MENU_FM | {"dish_id": None}})
    assert "restaurant_id" in check(rest | {"field_map": REST_FM | {"restaurant_id": ""}})


def test_food_search_module_errors_are_the_learn_errors():
    # learning and replay raise the same classes the worker catches
    assert food_learn.SearchDrift is food_search.SearchDrift
    assert food_learn.SearchTransient is food_search.SearchTransient

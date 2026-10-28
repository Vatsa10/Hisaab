"""Supervised search-recipe learning (browser edges and the LLM faked)."""
import contextlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest

import agent
import browser
import search
import search_learn
import sync

FIX = Path(__file__).parent / "fixtures"
GET = json.loads((FIX / "search_get.json").read_text(encoding="utf-8"))
STORE = json.loads((FIX / "search_store.json").read_text(encoding="utf-8"))
LAT, LON = 22.3301, 73.1502
LOC = {"query": "12 Test Society, Gorwa, Vadodara, Gujarat", "lat": LAT, "lon": LON}

SEARCH_GET = "https://api.zepto.com/api/v3/search"
LOOKUP = "https://api.zepto.com/api/v1/store/serviceability"
SEARCH_POST = "https://api.zepto.com/api/v3/search-graph"
POST_REGEX = r"^https://api\.zepto\.com/api/v3/search\-graph$"
FIELD_MAP = {"products_path": "data.widgets.*.items", "product_id": "product.id", "name": "product.name",
             "brand": "product.brand", "pack_size": "product.unit", "price": "pricing.selling_price",
             "mrp": "pricing.mrp", "in_stock": "inventory", "price_divisor": 100}


def item(url, method="GET", headers=None, body=None, data=None, status=200):
    return {"id": 0, "url": url, "method": method, "status": status, "request_headers": headers or {},
            "request_body": json.dumps(body) if body is not None else None,
            "sample": json.dumps(data)[:2000], "size": len(json.dumps(data)), "json": data}


def lookup_item():
    return item(f"{LOOKUP}?latitude={LAT}&longitude={LON}", data=STORE)


def get_search_item(q):
    return item(f"{SEARCH_GET}?query={q}&page=0", headers={"storeid": "store-77", "x-app": "web"}, data=GET)


def post_body(q):
    return {"variables": {"query": q, "pageNumber": 0}, "geo": {"lat": LAT, "lng": LON}}


class FakeSite:
    """The site's own page: navigating to a search page makes it call its APIs (captured), and a POST is only
    captured once the guard allows it; otherwise the guard records it as blocked."""

    def __init__(self, mode="get", product_count=3, replay_products=None):
        self.mode, self.seen = mode, {"open_site": [], "goto": [], "fetch": [], "approve": []}
        self.page = SimpleNamespace(url="about:blank", wait_for_timeout=lambda ms: None)
        self.capture = SimpleNamespace(items=[])
        self.capture.clear = lambda: self.capture.items.clear()
        self.guard = SimpleNamespace(allowlist=[], blocked=[])
        self.replay_products = replay_products
        self.login_redirect = False

    def install(self, monkeypatch):
        @contextlib.contextmanager
        def fake_open(site, **k):
            self.seen["open_site"].append({"site": site, **k})
            self.guard.allowlist = list(k.get("allowlist") or [])
            yield None, self.page, self.guard, self.capture

        monkeypatch.setattr(browser, "open_site", fake_open)
        monkeypatch.setattr(browser, "goto", self.goto)
        monkeypatch.setattr(browser, "fetch_in_page", self.fetch)
        monkeypatch.setattr(agent, "snapshot", lambda p: {"elements": []})
        monkeypatch.setattr(agent, "settle", lambda *a, **k: None)
        monkeypatch.setattr(search, "_pause", lambda: None)
        return self

    def goto(self, page, url, *a, **k):
        self.seen["goto"].append(url)
        page.url = "https://www.zepto.com/login" if self.login_redirect else url
        if self.login_redirect or "/search?query=" not in url:
            return
        q = dict(parse_qsl(urlsplit(url).query))["query"]
        self.capture.items.append(item("https://www.zepto.com/api/banners", data={"banners": [1, 2]}))
        self.capture.items.append(lookup_item())
        if self.mode == "get":
            self.capture.items.append(get_search_item(q))
        elif any(re.search(e["url_regex"], SEARCH_POST) for e in self.guard.allowlist):
            self.capture.items.append(item(SEARCH_POST, "POST", {"x-app": "web"}, post_body(q), GET))
        else:
            self.guard.blocked.append({"method": "POST", "url": SEARCH_POST, "body": json.dumps(post_body(q)),
                                       "reason": "POST not allowlisted"})

    def fetch(self, page, req):
        self.seen["fetch"].append(req)
        if req["url"].startswith(LOOKUP):
            return STORE
        if req["url"].startswith((SEARCH_GET, SEARCH_POST)):
            if self.replay_products is not None:
                data = json.loads(json.dumps(GET))
                data["data"]["widgets"][1]["items"] = data["data"]["widgets"][1]["items"][:self.replay_products]
                return data
            return GET
        raise RuntimeError("HTTP 404: unknown")


def fake_llm(field_maps, fee_map=None):
    """Returns field maps in order for field-map prompts; fee_map for the fee prompt. Records prompts."""
    prompts = {"field": [], "fee": []}
    queue = list(field_maps)

    def call(prompt):
        if prompt.startswith("FEE MAP TASK"):
            prompts["fee"].append(prompt)
            return {"fee_map": fee_map}
        prompts["field"].append(prompt)
        return json.dumps({"field_map": queue.pop(0) if len(queue) > 1 else queue[0]})
    call.prompts = prompts
    return call


FEES = {"source": "search", "delivery": "delivery_fee", "member": "user.pass_active", "divisor": 100}


# --- pure helpers ------------------------------------------------------------

def test_candidates_rank_product_responses_with_the_probe():
    items = [item("https://www.zepto.com/api/banners", data={"banners": [1]}), lookup_item(), get_search_item("milk"),
             item(f"{SEARCH_GET}?query=bread", data=GET)]
    got = search_learn.candidates(items, "milk")
    assert [c["url"] for c in got] == [f"{SEARCH_GET}?query=milk&page=0"]
    assert search_learn.PRODUCTISH_KEY.search("selling_price") and search_learn.SEARCH_PROBES == ("milk", "bread")


def test_find_location_inputs_query_header_body():
    c = item(f"{LOOKUP}?latitude=22.3301&longitude=73.1502&v=2", "POST",
             headers={"x-lat": "22.330", "x-app": "web"}, body={"geo": {"lat": 22.33, "lng": 73.15}, "n": 5})
    assert search_learn.find_location_inputs(c, LAT, LON) == [
        {"in": "query", "name": "latitude", "value": "lat"}, {"in": "query", "name": "longitude", "value": "lon"},
        {"in": "header", "name": "x-lat", "value": "lat"},
        {"in": "body", "name": "geo.lat", "value": "lat"}, {"in": "body", "name": "geo.lng", "value": "lon"}]
    assert search_learn.find_location_inputs(get_search_item("milk"), LAT, LON) == []


def test_find_store_binding():
    items = [lookup_item(), get_search_item("milk")]
    got = search_learn.find_store_binding(items, items[1], LAT, LON)
    assert got == {"lookup": {"method": "GET", "url": f"{LOOKUP}?latitude={LAT}&longitude={LON}", "body": None},
                   "location_inputs": [{"in": "query", "name": "latitude", "value": "lat"},
                                       {"in": "query", "name": "longitude", "value": "lon"}],
                   "store_id_path": "data.store.id", "inputs": [{"in": "header", "name": "storeid"}]}
    assert search_learn.find_store_binding([items[1]], items[1], LAT, LON) is None


def test_find_query_param_get_and_post():
    assert search_learn.find_query_param(get_search_item("milk"), "milk") == "query"
    post = item(SEARCH_POST, "POST", body=post_body("Milk"), data=GET)
    assert search_learn.find_query_param(post, "milk") == "variables.query"
    assert search_learn.find_query_param(item(f"{SEARCH_GET}?page=0", data=GET), "milk") is None


def test_find_fee_map_drops_unresolvable_paths():
    llm = fake_llm([FIELD_MAP], {"source": "search", "delivery": "delivery_fee", "handling": "fees.handling",
                                 "surge": "user.pass_name", "member": "user.pass_active", "small_cart": "=15",
                                 "divisor": 100})
    got = search_learn.find_fee_map([get_search_item("milk"), lookup_item()], LAT, LON, llm=llm)
    assert got == {"source": "search", "delivery": "delivery_fee", "member": "user.pass_active", "divisor": 100}
    assert "FEE MAP TASK" in llm.prompts["fee"][0]
    store_llm = fake_llm([FIELD_MAP], {"source": "store", "handling": "data.fees.handling", "divisor": 100})
    assert search_learn.find_fee_map([get_search_item("milk"), lookup_item()], LAT, LON, llm=store_llm) == \
        {"source": "store", "handling": "data.fees.handling", "divisor": 100}
    nothing = fake_llm([FIELD_MAP], {"source": "search", "delivery": "nope.nothing"})
    assert search_learn.find_fee_map([get_search_item("milk")], LAT, LON, llm=nothing) is None
    assert search_learn.find_fee_map([get_search_item("milk")], LAT, LON, llm=None) is None


def good_recipe(**over):
    r = {"kind": "search", "site": "zepto", "home_url": "https://www.zepto.com/",
         "search_page_url": "https://www.zepto.com/search?query={q}", "endpoint": SEARCH_GET, "method": "GET",
         "query": {"page": "0"}, "body": None, "headers": {}, "query_param": "query",
         "location_inputs": [{"in": "query", "name": "lat", "value": "lat"},
                             {"in": "query", "name": "lon", "value": "lon"}],
         "store": None, "field_map": dict(FIELD_MAP), "fee_map": None, "read_post_allowlist": []}
    r.update(over)
    return r


def test_check_accepts_good_recipe():
    assert search_learn.check_search_recipe(good_recipe()) is None


def test_check_rejects_recipe_without_location():
    err = search_learn.check_search_recipe(good_recipe(location_inputs=[], store=None))
    assert err and "location" in err


def test_check_rejects_blocked_path_and_broad_regex():
    assert "blocklist" in search_learn.check_search_recipe(good_recipe(endpoint="https://api.zepto.com/api/cart/search"))
    broad = [{"url_regex": r"^https://api\.zepto\.com/", "why": "x"}]
    assert "too broad" in search_learn.check_search_recipe(good_recipe(read_post_allowlist=broad))
    err = search_learn.check_search_recipe(good_recipe(method="POST", endpoint=SEARCH_POST, body={}))
    assert err and "not covered" in err
    assert search_learn.check_search_recipe(good_recipe(
        method="POST", endpoint=SEARCH_POST, body={}, read_post_allowlist=[{"url_regex": POST_REGEX, "why": "x"}])) is None


# --- learn_search ------------------------------------------------------------

def test_learn_get_search_with_store_binding(monkeypatch):
    site = FakeSite("get").install(monkeypatch)
    llm = fake_llm([FIELD_MAP], FEES)
    recipe = search_learn.learn_search("zepto", LOC, Path("/admin/profile"), llm=llm)
    assert recipe["kind"] == "search" and recipe["site"] == "zepto" and recipe["method"] == "GET"
    assert recipe["endpoint"] == SEARCH_GET and recipe["query_param"] == "query" and recipe["query"] == {"page": "0"}
    assert recipe["search_page_url"] == search.SEARCH_PAGES["zepto"]
    assert recipe["store"]["store_id_path"] == "data.store.id"
    assert recipe["store"]["inputs"] == [{"in": "header", "name": "storeid"}]
    assert "storeid" not in recipe["headers"]  # bound per user, never a learned constant
    assert recipe["fee_map"] == FEES and recipe["read_post_allowlist"] == []
    assert recipe["field_map"] == FIELD_MAP
    call = site.seen["open_site"][0]
    assert call["profile_dir"] == Path("/admin/profile") and call["location"] == LOC and "login_mode" not in call


def test_learn_post_search_needs_approval(monkeypatch):
    site = FakeSite("post").install(monkeypatch)
    asked = []
    with pytest.raises(sync.RecipeError, match="POST allowlist rejected"):
        search_learn.learn_search("zepto", LOC, Path("/admin/profile"),
                                  approve=lambda entries: asked.append(entries) or False, llm=fake_llm([FIELD_MAP]))
    assert asked == [[{"url_regex": POST_REGEX, "why": "product search (read-only)"}]]
    assert site.guard.allowlist == []


def test_learn_post_search_approved(monkeypatch):
    site = FakeSite("post").install(monkeypatch)
    recipe = search_learn.learn_search("zepto", LOC, Path("/admin/profile"), approve=lambda entries: True,
                                       llm=fake_llm([FIELD_MAP], FEES))
    assert recipe["method"] == "POST" and recipe["endpoint"] == SEARCH_POST
    assert recipe["query_param"] == "variables.query"
    assert recipe["location_inputs"] == [{"in": "body", "name": "geo.lat", "value": "lat"},
                                         {"in": "body", "name": "geo.lng", "value": "lon"}]
    assert recipe["read_post_allowlist"] == [{"url_regex": POST_REGEX, "why": "product search (read-only)"}]
    assert site.guard.allowlist == recipe["read_post_allowlist"]


def test_learn_preapproved_post_does_not_ask(monkeypatch):
    FakeSite("post").install(monkeypatch)

    def never(entries):
        raise AssertionError("must not ask")
    recipe = search_learn.learn_search("zepto", LOC, Path("/admin/profile"), approve=never,
                                       preapproved=(POST_REGEX,), llm=fake_llm([FIELD_MAP]))
    assert recipe["method"] == "POST" and recipe["read_post_allowlist"][0]["url_regex"] == POST_REGEX


def test_learn_requires_logged_in_profile(monkeypatch):
    site = FakeSite("get").install(monkeypatch)
    site.login_redirect = True
    with pytest.raises(browser.SessionExpired):
        search_learn.learn_search("zepto", LOC, Path("/admin/profile"), llm=fake_llm([FIELD_MAP]))


def test_learn_login_button_means_logged_out(monkeypatch):
    FakeSite("get").install(monkeypatch)
    monkeypatch.setattr(agent, "snapshot", lambda p: {"elements": [{"ref": "e1", "tag": "button", "text": "Login"}]})
    with pytest.raises(browser.SessionExpired):
        search_learn.learn_search("zepto", LOC, Path("/admin/profile"), llm=fake_llm([FIELD_MAP]))


def test_learn_validates_by_replay_with_second_probe(monkeypatch):
    site = FakeSite("get").install(monkeypatch)
    search_learn.learn_search("zepto", LOC, Path("/admin/profile"), llm=fake_llm([FIELD_MAP]))
    searched = [dict(parse_qsl(urlsplit(r["url"]).query)).get("query") for r in site.seen["fetch"]
                if r["url"].startswith(SEARCH_GET)]
    assert searched == ["bread"]
    replay = [r for r in site.seen["fetch"] if r["url"].startswith(SEARCH_GET)][0]
    assert replay["headers"]["storeid"] == "store-77"

    FakeSite("get", replay_products=2).install(monkeypatch)
    with pytest.raises(sync.RecipeError, match="replay"):
        search_learn.learn_search("zepto", LOC, Path("/admin/profile"), llm=fake_llm([FIELD_MAP]))


def test_learn_retries_field_map_with_error_feedback(monkeypatch):
    FakeSite("get").install(monkeypatch)
    bad = dict(FIELD_MAP, products_path="data.products")
    llm = fake_llm([bad, FIELD_MAP])
    recipe = search_learn.learn_search("zepto", LOC, Path("/admin/profile"), llm=llm)
    assert recipe["field_map"] == FIELD_MAP
    assert len(llm.prompts["field"]) == 2
    assert "Validation failed" in llm.prompts["field"][1] and "data.products" in llm.prompts["field"][1]

    FakeSite("get").install(monkeypatch)
    always_bad = fake_llm([bad])
    with pytest.raises(sync.RecipeError):
        search_learn.learn_search("zepto", LOC, Path("/admin/profile"), llm=always_bad)
    assert len(always_bad.prompts["field"]) == 3


def test_learn_needs_location_and_llm(monkeypatch):
    site = FakeSite("get").install(monkeypatch)
    with pytest.raises(sync.RecipeError, match="location"):
        search_learn.learn_search("zepto", {}, Path("/admin/profile"), llm=fake_llm([FIELD_MAP]))
    with pytest.raises(sync.RecipeError, match="LLM"):
        search_learn.learn_search("zepto", LOC, Path("/admin/profile"), llm=None)
    assert site.seen["open_site"] == []

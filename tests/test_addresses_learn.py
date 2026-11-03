"""Learning a site's `addresses` recipe (the read-only GET of the user's saved delivery addresses) on the admin's own
profile. Pages are local fixtures served through Playwright routing (no real site); Jev is offline and Claude is a
scripted fake. All addresses are made up (Testville)."""
import contextlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

import addresses
import agent
import browser
import guard as guard_mod
import llm
from sync import RecipeError

FIX = Path(__file__).parent / "fixtures" / "addresses_learn"
SAVED = json.loads((FIX / "saved_addresses.json").read_text(encoding="utf-8"))
NOISE = json.loads((FIX / "orders_noise.json").read_text(encoding="utf-8"))
GOOD_MAP = {"list_path": "data.addresses", "address_id": "id", "label": "address_type", "line": "full_address",
            "area": "locality", "city": "city", "lat": "latitude", "lon": "longitude", "is_default": "is_default"}
START = "https://www.zomato.com/account"


def _cap(url, data, method="GET"):
    return {"id": 0, "url": url, "method": method, "status": 200, "request_headers": {"accept": "application/json"},
            "request_body": None, "sample": json.dumps(data)[:300], "size": len(json.dumps(data)), "json": data}


# --- click policy ----------------------------------------------------------------

def _el(text, tag="button", **kw):
    return {"tag": tag, "text": text, "label": None, "type": "button"} | kw


@pytest.mark.parametrize("text", ["Add new address", "+ Add address", "Edit", "Delete", "Remove", "Save address",
                                  "Set as default", "Use this address", "Deliver here", "Change", "Log out",
                                  "Go to cart"])
def test_address_changing_and_write_clicks_are_refused(text):
    assert addresses.address_click_refusal(_el(text)) is not None


@pytest.mark.parametrize("text", ["Addresses", "Manage addresses", "Saved addresses", "My addresses", "Profile",
                                  "Order history"])
def test_navigation_to_the_saved_list_is_allowed(text):
    assert addresses.address_click_refusal(_el(text, tag="a", href="/account/addresses")) is None


def test_a_form_submit_and_a_checkout_link_stay_refused():
    assert addresses.address_click_refusal(_el("Addresses", type="submit")) is not None
    assert addresses.address_click_refusal(_el("Addresses", tag="a", href="/checkout/addresses")) is not None


# --- finding the response, mapping and checking -------------------------------------

def test_the_address_list_outscores_an_order_list_that_mentions_addresses():
    items = [_cap("https://www.zomato.com/webroutes/user/orders", NOISE),
             _cap("https://www.zomato.com/webroutes/user/saved_locations?client=web", SAVED)]
    best = addresses.best_address_response(items)
    assert best is items[1]
    assert addresses.best_address_response(items[:1]) is None  # orders with an address string are not a list


def test_the_field_map_is_retried_until_normalise_finds_the_addresses():
    replies = [{"field_map": GOOD_MAP | {"list_path": "data.nothing"}}, {"field_map": GOOD_MAP}]
    prompts = []

    def fake_llm(prompt):
        prompts.append(prompt)
        return replies.pop(0)
    c = _cap("https://www.zomato.com/webroutes/user/saved_locations", SAVED)
    fm = addresses.address_field_map("zomato", c, fake_llm)
    assert fm["list_path"] == "data.addresses" and len(prompts) == 2
    assert "Validation failed" in prompts[1]
    rows = addresses.normalise(SAVED, fm)
    assert len(rows) == 3 and all(r["line"] and r["lat"] is not None for r in rows)


def test_a_map_that_drops_the_pins_the_site_provides_is_rejected():
    c = _cap("https://www.zomato.com/webroutes/user/saved_locations", SAVED)
    no_pins = {k: v for k, v in GOOD_MAP.items() if k not in ("lat", "lon")}
    with pytest.raises(RecipeError):
        addresses.address_field_map("zomato", c, lambda p: {"field_map": no_pins})


def test_only_a_get_recipe_is_accepted():
    ok = {"kind": "addresses", "site": "zomato", "endpoint": "https://www.zomato.com/webroutes/user/saved_locations",
          "method": "GET", "field_map": GOOD_MAP, "read_post_allowlist": []}
    assert addresses.check_addresses_recipe(ok) is None
    assert "GET" in addresses.check_addresses_recipe(ok | {"method": "POST"})
    assert addresses.check_addresses_recipe(ok | {"read_post_allowlist": [{"url_regex": "x"}]}) is not None
    assert addresses.check_addresses_recipe(ok | {"field_map": {"list_path": "data"}}) is not None


# --- browser: local fixture pages -------------------------------------------------

pw_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def chromium():
    pw = pw_api.sync_playwright().start()
    try:
        try:
            b = pw.chromium.launch()
        except Exception:
            pytest.skip("no browser installed")
        yield b
        b.close()
    finally:
        pw.stop()


def _routed(chromium, monkeypatch, link="Addresses", start_page="account.html"):
    """Patch browser.open_site to a routed context with the real read-only Guard decision and Capture."""
    seen = {"requests": [], "open_site": []}
    html = (FIX / start_page).read_text(encoding="utf-8").replace("{{LINK}}", link)
    pages = {"/account": html, "/account/addresses": html}
    apis = {"/webroutes/user/orders": NOISE, "/webroutes/user/saved_locations": SAVED}

    @contextlib.contextmanager
    def fake_open(site, **k):
        seen["open_site"].append({"site": site, **k})
        ctx = chromium.new_context(viewport={"width": 1280, "height": 900})
        g, cap = guard_mod.Guard([]), browser.Capture()

        def handle(route):
            req = route.request
            ok, _ = guard_mod.decide(req.method, req.url, req.is_navigation_request(), req.post_data,
                                     False, g.allowlist)
            seen["requests"].append((req.method, urlsplit(req.url).path))
            u = urlsplit(req.url)
            if not ok:
                g.blocked.append({"method": req.method, "url": req.url, "body": "", "reason": "blocked"})
                return route.abort("blockedbyclient")
            if u.hostname != "www.zomato.com":
                return route.abort()  # nothing ever leaves the machine
            if u.path in apis:
                return route.fulfill(status=200, content_type="application/json", body=json.dumps(apis[u.path]))
            if u.path in pages:
                return route.fulfill(status=200, content_type="text/html", body=pages[u.path])
            return route.abort()
        ctx.route("**/*", handle)
        cap.install(ctx)
        page = ctx.new_page()
        try:
            yield ctx, page, g, cap
        finally:
            seen["writes"] = page.evaluate("window.__writes || []")
            ctx.close()
    monkeypatch.setattr(browser, "open_site", fake_open)
    return seen


class _Models:
    """Jev is offline; Claude (llm.create for browser steps) is scripted: it first tries the 'Add new address'
    button, then the link whose text matches `pick`."""

    def __init__(self, monkeypatch, pick=r"saved places"):
        self.claude, self.tried = 0, []
        models = self

        class Jev:
            def system_one(self, **_):
                raise RuntimeError("fake Jev is offline")
        monkeypatch.setattr(agent, "jev", lambda: Jev())

        def create(*a, messages, **k):
            models.claude += 1
            text = messages[0]["content"]
            lines = text.split("CLICKABLE ELEMENTS:\n", 1)[1].split("\n\nCall exactly", 1)[0].splitlines()

            def ref(pattern):
                return next((ln.split(": ", 1)[0] for ln in lines if re.search(pattern, ln, re.I)), None)

            def call(name, **args):
                return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=args)])
            target = ref(r"add new address") if not models.tried else ref(pick)
            models.tried.append(target)
            return call("click", element_ref=target) if target else call("wait")
        monkeypatch.setattr(llm, "create", create)


def _field_llm(prompt):
    assert "FIELD MAP" in prompt
    return {"field_map": GOOD_MAP}


def test_code_clicks_the_addresses_link_and_learns_a_get_recipe_without_any_model(chromium, monkeypatch, tmp_path):
    seen = _routed(chromium, monkeypatch)
    models = _Models(monkeypatch)
    recipe = addresses.learn_addresses("zomato", tmp_path / "profile", location={}, proxy_server=None,
                                       approve=lambda e: pytest.fail("a GET recipe never asks"), preapproved=(),
                                       llm=_field_llm, start_url=START, runs_dir=tmp_path)
    assert models.claude == 0  # the obvious step is code, not a model
    assert recipe["method"] == "GET" and recipe["kind"] == "addresses"
    assert recipe["endpoint"] == "https://www.zomato.com/webroutes/user/saved_locations"
    assert recipe["query"] == {"client": "web"} and recipe["read_post_allowlist"] == []
    assert recipe["field_map"] == GOOD_MAP
    assert addresses.check_addresses_recipe(recipe) is None
    assert seen["open_site"][0]["site"] == "zomato" and not seen["open_site"][0].get("block_media")
    assert all(m == "GET" for m, _ in seen["requests"])  # read-only from start to finish
    assert seen["writes"] == []


def test_the_model_fallback_never_clicks_add_new_address(chromium, monkeypatch, tmp_path):
    seen = _routed(chromium, monkeypatch, link="Your saved places")
    models = _Models(monkeypatch)
    recipe = addresses.learn_addresses("zomato", tmp_path / "profile", location={}, llm=_field_llm,
                                       start_url=START, runs_dir=tmp_path)
    assert models.tried[0] is not None  # Claude did ask for the Add button first ...
    assert models.claude >= 2 and recipe["method"] == "GET"
    assert seen["writes"] == []  # ... and it was never clicked
    assert all(m == "GET" for m, _ in seen["requests"])


def test_a_page_with_only_an_add_address_button_learns_nothing_and_clicks_nothing(chromium, monkeypatch, tmp_path):
    seen = _routed(chromium, monkeypatch, start_page="add_address.html")
    models = _Models(monkeypatch, pick=r"save address")
    monkeypatch.setattr(agent, "MAX_STEPS", 4)
    with pytest.raises(RecipeError):
        addresses.learn_addresses("zomato", tmp_path / "profile", location={}, llm=_field_llm,
                                  start_url=START, runs_dir=tmp_path)
    assert models.tried  # the model was asked and tried both buttons
    assert seen["writes"] == []


def test_learning_needs_an_llm():
    with pytest.raises(RecipeError):
        addresses.learn_addresses("zomato", None, location={}, llm=None, start_url=START)

"""Supervised cart learning (Plan 6 T4): the browser edges are faked; the CartGuard and its decisions are real."""
import contextlib
import json

import pytest

import agent
import browser
import cart_learn
import cart_recipe
from cart_guard import CartPolicy, decide_cart
from sync import RecipeError

LAT, LON = 12.9716, 77.5946
LOCATION = {"lat": LAT, "lon": LON, "label": "Home, Indiranagar"}
HOST = "https://www.zepto.com"
SEARCH_RECIPE = {"kind": "search", "site": "zepto", "home_url": HOST + "/",
                 "search_page_url": HOST + "/search?query={q}", "field_map": {"products_path": "items"},
                 "read_post_allowlist": [{"url_regex": r"^https://www\.zepto\.com/api/search$",
                                          "why": "product search (read-only)"}]}
GOOD_MAP = {"lines_path": "cart.items", "product_id": "id", "name": "name", "qty": "qty", "price": "price",
            "subtotal": "cart.subtotal", "total": "cart.total", "fees": {}, "out_of_stock": None}


class FakeSite:
    """A scripted page: clicks and navigations send requests through the real cart-guard decision."""

    def __init__(self, qty_step=2, add_body=None, elements=None, read_method="GET", loc_post=True,
                 current=True, add_returns_cart=False, separate_read=True):
        self.add_returns_cart, self.separate_read = add_returns_cart, separate_read
        self.qty = 0
        self.qty_step = qty_step  # what "+" sends: 2 (absolute) or 1 (increment)
        self.add_body = add_body
        self.read_method = read_method
        self.loc_post, self.current = loc_post, current
        self.elements = elements or [
            {"ref": "e1", "tag": "button", "text": "ADD", "row": "Amul Milk"},
            {"ref": "e2", "tag": "button", "text": "+", "row": "Amul Milk"},
        ]
        self.clicked: list[str] = []
        self.policies: list[CartPolicy] = []
        self.guard = self.capture = None
        self.page = type("Page", (), {"url": HOST + "/", "wait_for_timeout": lambda s, ms: None})()

    def send(self, method, url, body=None, resp=None):
        b = json.dumps(body) if isinstance(body, (dict, list)) else body
        ok, reason = decide_cart(method, url, False, b, self.guard.policy)
        if not ok:
            self.guard.blocked.append({"method": method, "url": url, "body": b or "", "reason": reason})
            return False
        self.capture.items.append({"id": len(self.capture.items), "url": url, "method": method, "status": 200,
                                   "request_headers": {"content-type": "application/json"},
                                   "request_body": b, "json": resp if resp is not None else {"ok": True}})
        return True

    # --- the location agent ---
    def agent_run(self, ag):
        assert ag.refuse_click.__func__ is cart_learn.CartLearnAgent.refuse_click
        if self.loc_post:
            self.send("POST", HOST + "/api/user/location/select", {"lat": LAT, "lon": LON})
        if self.current:
            self.send("GET", HOST + "/api/user/location/current",
                      resp={"store": {"id": "S1"}, "geo": {"lat": LAT, "lon": LON}})
        return {"success": True, "note": "location set"}

    # --- page edges ---
    def goto(self, page, url):
        if "/search" in url:
            self.send("GET", HOST + "/api/search?q=milk", resp={"items": [{"pid": "P1", "name": "Amul Milk"}]})

    def snapshot(self, page):
        return {"url": page.url, "elements": list(self.elements), "overlay": False, "aria": ""}

    def click_ref(self, page, ref):
        el = next(e for e in self.elements if e["ref"] == ref)
        assert agent.cart_click_refusal(el) is None, f"clicked a refused element {el}"
        self.clicked.append(el["text"])
        if not cart_learn.PLUS_BUTTON.match(el["text"]):
            value = 1
        else:
            value = self.qty_step if self.qty_step != 2 else self.qty + 1
        body = self.add_body(value) if self.add_body else {"product_id": "P1", "quantity": value, "store_id": "S1"}
        qty = value if self.qty_step == 2 else self.qty + value
        cart = {"cart": {"items": [{"id": "P1", "name": "Amul Milk", "qty": qty, "price": 30}],
                         "subtotal": 30 * qty, "total": 30 * qty + 15}}
        if self.send("POST", HOST + "/api/cart/add", body, resp=cart if self.add_returns_cart else None):
            self.qty = qty
            if not self.separate_read:
                pass
            elif self.read_method == "GET":
                self.send("GET", HOST + "/api/cart", resp=cart)
            else:
                self.send("POST", HOST + "/api/cart/view", {"v": 1}, resp=cart)
        return "click"


def fake_location_inputs(c, lat, lon):
    try:
        body = json.loads(c.get("request_body") or "")
    except ValueError:
        return []
    out = []
    for k, v in (body.items() if isinstance(body, dict) else []):
        if isinstance(v, (int, float)) and abs(v - lat) <= 0.01:
            out.append({"in": "body", "name": k, "value": "lat"})
        elif isinstance(v, (int, float)) and abs(v - lon) <= 0.01:
            out.append({"in": "body", "name": k, "value": "lon"})
    return out


@pytest.fixture
def site(monkeypatch):
    s = FakeSite()
    install(monkeypatch, s)
    return s


def install(monkeypatch, s):
    @contextlib.contextmanager
    def open_site(name, allowlist=None, login_mode=False, profile_dir=None, location=None, proxy_server=None,
                  block_media=False, cart_policy=None):
        assert cart_policy is not None and not login_mode
        from cart_guard import CartGuard
        s.policies.append(cart_policy)
        s.guard, s.capture = CartGuard(cart_policy, site=name), browser.Capture()
        yield None, s.page, s.guard, s.capture

    monkeypatch.setattr(browser, "open_site", open_site)
    monkeypatch.setattr(browser, "goto", s.goto)
    monkeypatch.setattr(agent, "snapshot", s.snapshot)
    monkeypatch.setattr(agent, "click_ref", s.click_ref)
    monkeypatch.setattr(agent, "settle", lambda *a, **k: None)
    monkeypatch.setattr(cart_learn.CartLearnAgent, "run", lambda self: s.agent_run(self))
    monkeypatch.setattr(cart_learn, "_products", lambda items, recipe: [("P1", "Amul Milk")])
    monkeypatch.setattr(cart_learn, "_store_id", lambda page, recipe, location: "S1")
    monkeypatch.setattr(cart_learn, "_location_inputs", fake_location_inputs)
    monkeypatch.setattr(cart_learn, "_in_row", lambda page, el, name, others: el.get("row") == name)


def good_llm(calls=None):
    def llm(prompt):
        if calls is not None:
            calls.append(prompt)
        return {"field_map": GOOD_MAP}
    return llm


def learn(approve=lambda entries: True, **kw):
    kw.setdefault("llm", good_llm())
    return cart_learn.learn_cart("zepto", "profile", LOCATION, SEARCH_RECIPE, approve, **kw)


# --- the happy path ------------------------------------------------------------------------------

def test_learn_records_add_location_and_read(site):
    asked = []
    out = learn(approve=lambda entries: asked.append([e["op"] for e in entries]) or True)
    recipe = out["recipe"]
    assert out["probe_item"] == "Amul Milk"
    assert cart_recipe.check_cart_recipe(recipe, "zepto") is None
    assert asked == [["location"], ["cart"]]
    add = recipe["add"]
    assert (add["product_id_path"], add["qty_path"], add["store_id_path"], add["qty_mode"]) == \
        ("product_id", "quantity", "store_id", "set")
    assert recipe["location"]["select"]["mode"] == "latlon"
    assert recipe["location"]["select"]["lat_input"]["name"] == "lat"
    assert recipe["location"]["current"]["store_id_path"] == "store.id"
    assert recipe["read"]["url"] == HOST + "/api/cart" and recipe["read"]["field_map"]["lines_path"] == "cart.items"
    assert [e["op"] for e in recipe["cart_allowlist"]] == ["cart"]
    assert recipe["read_post_allowlist"] == []  # the search recipe's read entries stay in the search recipe
    assert site.clicked == ["ADD", "ADD", "+"]


def test_increment_mode_learned(monkeypatch):
    s = FakeSite(qty_step=1)
    install(monkeypatch, s)
    assert learn()["recipe"]["add"]["qty_mode"] == "increment"


def test_policy_includes_search_read_allowlist(site):
    learn()
    assert SEARCH_RECIPE["read_post_allowlist"][0]["url_regex"] in [e["url_regex"] for e in site.policies[0].read]


# --- approvals -----------------------------------------------------------------------------------

def test_add_permission_needs_approval(site):
    with pytest.raises(RecipeError, match="cart permission rejected"):
        learn(approve=lambda entries: entries[0]["op"] != "cart")
    assert site.guard.policy.cart == ()
    assert site.clicked == ["ADD"]


def test_location_permission_needs_approval(site):
    with pytest.raises(RecipeError, match="location permission rejected"):
        learn(approve=lambda entries: False)
    assert site.guard.policy.location == ()


def test_preapproved_entries_not_asked_again(site):
    pre = [{"url_regex": r"^https://www\.zepto\.com/api/user/location/select$", "why": "pick location",
            "op": "location"},
           {"url_regex": r"^https://www\.zepto\.com/api/cart/add$", "why": "add to cart", "op": "cart"}]
    asked = []
    out = learn(approve=lambda entries: asked.append(entries) or True, preapproved=pre)
    assert asked == []
    assert out["recipe"]["cart_allowlist"][0]["url_regex"] == pre[1]["url_regex"]
    assert site.policies[0].cart == ()  # added through guard.allow's checks, not slipped into the policy
    assert site.guard.policy.cart[0]["url_regex"] == pre[1]["url_regex"]


def test_preapproved_entries_pass_guard_checks(site):
    for bad in ({"url_regex": r"^https://evil\.example/api/cart/add$", "why": "x", "op": "cart"},
                {"url_regex": r"^https://www\.zepto\.com/.*$", "why": "x", "op": "read"}):
        with pytest.raises(RecipeError, match="preapproved"):
            learn(preapproved=[bad])
    assert site.clicked == []


def test_read_post_needs_approval(monkeypatch):
    s = FakeSite(read_method="POST")
    install(monkeypatch, s)
    pre = [{"url_regex": r"^https://www\.zepto\.com/api/cart/view$", "why": "cart read", "op": "read"}]
    with pytest.raises(RecipeError, match="cart read permission rejected"):
        cart_learn.learn_cart("zepto", "p", LOCATION, SEARCH_RECIPE, lambda e: e[0]["op"] != "read",
                              preapproved=[], llm=good_llm())
    s2 = FakeSite(read_method="POST")
    install(monkeypatch, s2)
    out = cart_learn.learn_cart("zepto", "p", LOCATION, SEARCH_RECIPE, lambda e: True, preapproved=pre,
                                llm=good_llm())
    assert out["recipe"]["read"]["method"] == "POST"
    assert out["recipe"]["read_post_allowlist"][0]["url_regex"] == pre[0]["url_regex"]


# --- quantity rules ------------------------------------------------------------------------------

def test_qty_mode_from_rules():
    assert cart_learn.qty_mode_from(1, 2) == "set"
    assert cart_learn.qty_mode_from(1, 1) == "increment"
    with pytest.raises(RecipeError, match="couldn't tell how the cart counts quantities"):
        cart_learn.qty_mode_from(1, 5)


def test_odd_quantity_rejects_recipe(monkeypatch):
    s = FakeSite(qty_step=5)
    install(monkeypatch, s)
    with pytest.raises(RecipeError, match="quantities"):
        learn()


# --- clicks --------------------------------------------------------------------------------------

def test_learner_never_clicks_refused_elements(monkeypatch):
    s = FakeSite(elements=[{"ref": "c", "tag": "button", "text": "Proceed to Checkout"},
                           {"ref": "r", "tag": "button", "text": "Remove"},
                           {"ref": "m", "tag": "button", "text": "-", "row": "Amul Milk"},
                           {"ref": "o1", "tag": "button", "text": "ADD", "row": "Nandini Milk"},
                           {"ref": "o2", "tag": "button", "text": "+", "row": "Nandini Milk"},
                           {"ref": "e1", "tag": "button", "text": "ADD", "row": "Amul Milk"},
                           {"ref": "e2", "tag": "button", "text": "+", "row": "Amul Milk"}])
    install(monkeypatch, s)
    monkeypatch.setattr(cart_learn, "_products", lambda items, recipe: [("P1", "Amul Milk"), ("P2", "Nandini Milk")])
    learn()
    clicked_refs = s.clicked
    assert clicked_refs == ["ADD", "ADD", "+"]


def test_plus_only_in_the_probe_row(monkeypatch):
    s = FakeSite(elements=[{"ref": "e1", "tag": "button", "text": "ADD", "row": "Amul Milk"},
                           {"ref": "o2", "tag": "button", "text": "+", "row": "Nandini Milk"}])
    install(monkeypatch, s)
    with pytest.raises(RecipeError, match=r"no \+ button"):
        learn()
    assert s.clicked == ["ADD", "ADD"]


def test_no_add_button(monkeypatch):
    s = FakeSite(elements=[{"ref": "c", "tag": "button", "text": "Add address"}])
    install(monkeypatch, s)
    with pytest.raises(RecipeError, match="no Add button found"):
        learn()
    assert s.clicked == []


def test_agent_uses_cart_click_policy():
    a = cart_learn.CartLearnAgent("zepto", None, None, None, prompt=cart_learn._no_prompt, query="Home")
    for el in ({"text": "Proceed to Checkout", "tag": "button"}, {"text": "Remove"},
               {"text": "Use current location", "tag": "button"}):
        assert a.refuse_click(el) == agent.cart_click_refusal(el)
    for el in ({"text": "Add to cart", "tag": "button", "type": "submit"}, {"text": "ADD"}, {"text": "+"},
               {"text": "", "label": "add"}):
        assert a.refuse_click(el)  # the location agent never adds to the cart
    assert a.GOAL.startswith("Open the delivery-location picker") and "Home" in a.GOAL
    assert "Never add, edit, save or delete an address" in a.GOAL
    assert a.MAX_STEPS == 15


def test_agent_stops_after_15_steps(monkeypatch):
    a = cart_learn.CartLearnAgent("zepto", type("P", (), {"url": HOST})(), None, None,
                                  prompt=cart_learn._no_prompt, query="Home")
    monkeypatch.setattr(agent.Discovery, "observe", lambda self: None)
    for _ in range(15):
        a.observe()
        assert a.done is None
    a.observe()
    assert a.done and a.done["success"] is False


def test_agent_question_fails_learn(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("input() called"))
    a = cart_learn.CartLearnAgent("zepto", type("P", (), {"url": HOST})(), None, None,
                                  prompt=cart_learn._no_prompt, query="Home")
    a.ask_user("Which address?")
    assert a.done["note"] == "ask_timeout"

    s = FakeSite()
    install(monkeypatch, s)
    monkeypatch.setattr(cart_learn.CartLearnAgent, "run",
                        lambda self: {"success": False, "note": "ask_timeout", "url": HOST})
    with pytest.raises(RecipeError, match="help"):
        learn()


# --- rejected recipes ----------------------------------------------------------------------------

def test_missing_location_read_rejected(monkeypatch):
    s = FakeSite(current=False)
    install(monkeypatch, s)
    with pytest.raises(RecipeError, match="cart recipe can't read the app's location"):
        learn()


def test_no_location_post_means_no_select(monkeypatch):
    s = FakeSite(loc_post=False)
    install(monkeypatch, s)
    asked = []
    out = learn(approve=lambda e: asked.append(e[0]["op"]) or True)
    assert out["recipe"]["location"]["select"] is None and asked == ["cart"]


def test_whole_cart_add_rejected(monkeypatch):
    s = FakeSite(add_body=lambda q: {"items": [{"product_id": "P9", "quantity": 3},
                                               {"product_id": "P1", "quantity": q}]})
    install(monkeypatch, s)
    with pytest.raises(RecipeError, match="whole cart"):
        learn()


def test_read_field_map_retries_with_feedback(site):
    calls = []
    answers = iter([{"field_map": dict(GOOD_MAP, qty="nope")}, {"field_map": GOOD_MAP}])

    def llm(prompt):
        calls.append(prompt)
        return next(answers)
    out = learn(llm=llm)
    assert len(calls) == 2 and "failed" in calls[1].lower()
    assert out["recipe"]["read"]["field_map"]["qty"] == "qty"


def test_read_field_map_gives_up_after_3(site):
    calls = []
    with pytest.raises(RecipeError, match="cart"):
        learn(llm=lambda p: calls.append(p) or "not json")
    assert len(calls) == 3


# --- helpers -------------------------------------------------------------------------------------

NOT_LISTED = cart_learn.PROPOSABLE


def test_propose_dedupes_and_anchors():
    blocked = [{"method": "POST", "url": HOST + "/api/cart/add?x=1", "body": '{"product_id":"P1"}',
                "reason": NOT_LISTED},
               {"method": "POST", "url": HOST + "/api/cart/add?x=2", "body": '{"product_id":"P1"}',
                "reason": NOT_LISTED},
               {"method": "POST", "url": HOST + "/api/track", "body": '{"e":"view"}', "reason": NOT_LISTED},
               {"method": "GET", "url": HOST + "/api/P1", "body": "", "reason": NOT_LISTED},
               {"method": "POST", "url": HOST + "/api/cart/remove", "body": '{"product_id":"P1"}',
                "reason": "forbidden: cart removal"},
               {"method": "POST", "url": HOST + "/api/x", "body": '{"product_id":"P1"}', "reason": "binary body"},
               {"method": "POST", "url": HOST + "/gql", "body": '{"query":"mutation AddItem { add(id:\\"P1\\") }"}',
                "reason": NOT_LISTED}]
    out = cart_learn.propose(blocked, ["P1"], "cart", "add to cart")
    label = cart_learn.OP_LABELS["cart"] + ": add to cart"
    assert out == [{"url_regex": r"^https://www\.zepto\.com/api/cart/add$", "why": label, "op": "cart"},
                   {"url_regex": r"^https://www\.zepto\.com/gql$", "why": label, "op": "cart",
                    "operation": "AddItem"}]


def test_propose_never_offers_writes_as_reads():
    blocked = [{"method": "POST", "url": HOST + "/gql", "reason": NOT_LISTED,
                "body": '{"query":"mutation UpdateCart { cart(qty: 2) }"}'},
               {"method": "POST", "url": HOST + "/api/cart/view", "reason": NOT_LISTED, "body": '{"v":1}'}]
    out = cart_learn.propose(blocked, ["cart"], "read", "read the cart")
    assert [e["url_regex"] for e in out] == [r"^https://www\.zepto\.com/api/cart/view$"]
    assert out[0]["why"].startswith("READ")


# --- the read is never the add -------------------------------------------------------------------

def test_add_response_with_whole_cart_is_not_the_read(monkeypatch):
    """The reviewer's probe: "+" answered with the whole cart and no separate read must not save the add as read."""
    s = FakeSite(add_returns_cart=True, separate_read=False)
    install(monkeypatch, s)
    with pytest.raises(RecipeError, match="couldn't find the cart response"):
        learn()


def test_add_response_with_whole_cart_and_separate_read(monkeypatch):
    s = FakeSite(add_returns_cart=True)
    install(monkeypatch, s)
    r = learn()["recipe"]
    assert (r["read"]["method"], r["read"]["url"]) == ("GET", HOST + "/api/cart")
    assert r["read"]["url"] != r["add"]["url"]


def test_check_cart_recipe_refuses_add_as_read(site):
    r = learn()["recipe"]
    as_add = dict(r, read=dict(r["read"], method="POST", url=r["add"]["url"], body=r["add"]["body"]))
    assert cart_recipe.check_cart_recipe(as_add, "zepto") == "cart read is not a read POST"
    # even listed as a read entry, the cart entry decides first: still not a read
    listed = dict(as_add, read_post_allowlist=[{"url_regex": r["cart_allowlist"][0]["url_regex"], "why": "r"}])
    assert cart_recipe.check_cart_recipe(listed, "zepto") == "cart read is not a read POST"
    unlisted = dict(r, read=dict(r["read"], method="POST", url=HOST + "/api/cart/view", body={"v": 1}))
    assert cart_recipe.check_cart_recipe(unlisted, "zepto") == "cart read is not a read POST"


def test_find_value_path():
    data = {"a": {"b": [{"id": "x"}, {"id": "P1"}]}, "q": 1}
    assert cart_learn.find_value_path(data, lambda v: v == "P1") == "a.b.1.id"
    assert cart_learn.find_value_path(data, lambda v: v == 1) == "q"
    assert cart_learn.find_value_path(data, lambda v: v == "nope") is None
    assert cart_learn.find_value_path({"id": "P1"}, lambda v: v == "P1", path="body") == "body.id"


def test_module_never_checks_out_or_removes():
    src = open(cart_learn.__file__, encoding="utf-8").read().lower()
    for word in ("checkout(", "place_order", "remove_item", "pay("):
        assert word not in src

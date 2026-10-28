"""The learn approval hook (admin approves new POST-allowlist entries without a terminal), the drift error,
and the sync/learn wiring of media blocking."""
import contextlib
import json
from types import SimpleNamespace

import pytest
from llm_fakes import FakeClient, reply, text

import agent
import llm
import recipes
import sync

RX = r"^https://api\.zepto\.com/orders$"
RECIPE = {"endpoint": "https://api.zepto.com/orders", "method": "POST",
          "pagination": {"type": "page", "param": "page"},
          "field_map": {"orders_path": "orders", "order_id": "id", "placed_at": "at", "total_amount": "amt"},
          "read_post_allowlist": [{"url_regex": RX, "why": "order list"}]}


@pytest.fixture(autouse=True)
def _haiku(monkeypatch):
    monkeypatch.setattr(llm, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("no terminal prompt"))


# --- confirm_allowlist ------------------------------------------------------------------------------------

def test_confirm_allowlist_uses_approve_callback(no_terminal):
    seen = []
    approved = {"^a$"}
    assert agent.confirm_allowlist([{"url_regex": "^a$"}, {"url_regex": "^b$"}], approved,
                                   approve=lambda e: seen.append(e) or True)
    assert seen == [[{"url_regex": "^b$"}]] and approved == {"^a$", "^b$"}


def test_confirm_allowlist_rejected_by_callback_approves_nothing(no_terminal):
    approved = set()
    assert not agent.confirm_allowlist([{"url_regex": "^b$"}], approved, approve=lambda e: False)
    assert approved == set()


def test_confirm_allowlist_skips_callback_when_nothing_is_new(no_terminal):
    assert agent.confirm_allowlist([{"url_regex": "^a$"}], {"^a$"}, approve=lambda e: pytest.fail("asked"))


def test_confirm_allowlist_without_callback_still_asks_the_terminal(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    approved = set()
    assert agent.confirm_allowlist([{"url_regex": "^b$", "why": "w"}], approved)
    assert approved == {"^b$"} and "^b$" in capsys.readouterr().out


# --- validate / synthesize / discover ---------------------------------------------------------------------

def _guard():
    return SimpleNamespace(blocked=[], allowlist=[])


def test_discover_rejection_is_reported(no_terminal):
    orders, fast, err = agent.validate(dict(RECIPE), None, _guard(), SimpleNamespace(items=[]), set(),
                                       approve=lambda e: False)
    assert (orders, fast) == ([], False)
    assert err.startswith("user rejected the POST allowlist")


def test_synthesize_reports_rejection_to_the_model(monkeypatch, no_terminal):
    fake = FakeClient(*[reply(text(json.dumps(RECIPE))) for _ in range(3)])
    monkeypatch.setattr(llm, "client", lambda: fake)
    asked = []
    with pytest.raises(RuntimeError, match="could not produce a valid recipe"):
        agent.synthesize("zepto", None, _guard(), SimpleNamespace(items=[]), [], "https://www.zepto.com/orders",
                         approve=lambda e: asked.append(e) or False)
    assert len(asked) == 3
    assert "user rejected the POST allowlist" in fake.calls[1]["messages"][-1]["content"]


def test_synthesize_seeds_approved_with_preapproved(monkeypatch):
    fake = FakeClient(reply(text(json.dumps(RECIPE))))
    monkeypatch.setattr(llm, "client", lambda: fake)
    seen = {}

    def validate(recipe, page, guard, capture, approved, approve=None):
        seen.update(approved=set(approved), approve=approve)
        return [{"order_id": "1"}], True, None

    monkeypatch.setattr(agent, "validate", validate)
    approve = lambda e: True  # noqa: E731
    recipe = agent.synthesize("zepto", SimpleNamespace(url="u"), _guard(), SimpleNamespace(items=[]), [], "o",
                              approve=approve, preapproved=[RX])
    assert seen == {"approved": {RX}, "approve": approve} and recipe["endpoint"] == RECIPE["endpoint"]


def test_preapproved_entries_are_not_asked_again(no_terminal):
    approved = set([RX])
    assert agent.confirm_allowlist(RECIPE["read_post_allowlist"], approved, approve=lambda e: pytest.fail("asked"))


def test_discover_threads_approve_to_synthesize(monkeypatch, tmp_path):
    monkeypatch.setattr(recipes, "load_seed", lambda site: None)
    monkeypatch.setattr(recipes, "hint_path", lambda site: tmp_path / "none.txt")
    monkeypatch.setattr(agent, "goto", lambda page, url: None)

    class FakeDiscovery:
        def __init__(self, site, page, guard, capture):
            self.page, self.trace = page, []

        def run(self):
            return {"success": True, "note": "", "url": "https://www.zepto.com/orders"}

    seen = {}
    monkeypatch.setattr(agent, "Discovery", FakeDiscovery)
    monkeypatch.setattr(agent, "synthesize", lambda *a, **k: seen.update(k) or {"ok": True})
    approve = lambda e: True  # noqa: E731
    assert agent.discover("zepto", SimpleNamespace(url="u"), _guard(), None, approve=approve,
                          preapproved=("^x$",)) == {"ok": True}
    assert seen == {"approve": approve, "preapproved": ("^x$",)}


def test_discover_threads_approve_to_learn_from_seed(monkeypatch):
    monkeypatch.setattr(recipes, "load_seed", lambda site: {"seed": 1})
    seen = {}
    monkeypatch.setattr(agent, "learn_from_seed", lambda *a, **k: seen.update(k) or {"ok": True})
    approve = lambda e: True  # noqa: E731
    agent.discover("zepto", None, _guard(), None, approve=approve, preapproved=["^x$"])
    assert seen == {"approve": approve, "preapproved": ["^x$"]}


def test_learn_from_seed_rejected_allowlist_stops_before_fetching(monkeypatch, no_terminal):
    monkeypatch.setattr(agent, "goto", lambda *a: pytest.fail("navigated"))
    seed = dict(RECIPE, orders_page_url="https://www.zepto.com/orders")
    with pytest.raises(RuntimeError, match="rejected the POST allowlist"):
        agent.learn_from_seed("zepto", None, _guard(), None, seed, approve=lambda e: False)


def test_learn_from_seed_with_preapproved_entries_does_not_ask(monkeypatch, no_terminal):
    page = SimpleNamespace(url="https://www.zepto.com/orders")
    monkeypatch.setattr(agent, "goto", lambda *a: None)
    monkeypatch.setattr(agent, "settle", lambda *a: None)
    monkeypatch.setattr(agent, "snapshot", lambda p: {"elements": []})
    data = {"orders": [{"id": "1", "at": "2026-01-02T10:00:00", "amt": 100}]}
    monkeypatch.setattr(agent, "fetch_in_page", lambda p, req: data)
    monkeypatch.setattr(llm, "client", lambda: FakeClient(reply(text(json.dumps({"field_map": RECIPE["field_map"]})))))
    g = _guard()
    recipe = agent.learn_from_seed("zepto", page, g, None, dict(RECIPE, orders_page_url=page.url),
                                   approve=lambda e: pytest.fail("asked"), preapproved=[RX])
    assert recipe["read_post_allowlist"] == RECIPE["read_post_allowlist"]


# --- sync.learn ---------------------------------------------------------------------------------------------

def _fake_open_site(monkeypatch, seen):
    @contextlib.contextmanager
    def fake(site, **kw):
        seen.append(kw)
        yield None, SimpleNamespace(url="u"), _guard(), SimpleNamespace(items=[], clear=lambda: None)
    monkeypatch.setattr(sync, "open_site", fake)


def test_learn_threads_approval_and_never_blocks_media(monkeypatch, tmp_path):
    opened, discovered, saved = [], [], []
    _fake_open_site(monkeypatch, opened)
    monkeypatch.setattr(agent, "discover", lambda *a, **k: discovered.append(k) or {"site": "zepto"})
    monkeypatch.setattr(recipes, "save", lambda site, r: saved.append(site))
    approve = lambda e: True  # noqa: E731
    out = sync.learn("zepto", profile_dir=tmp_path, location={}, proxy_server="http://127.0.0.1:1",
                     approve=approve, preapproved=["^x$"], save=False)
    assert out == {"site": "zepto"} and saved == []
    assert opened == [{"profile_dir": tmp_path, "location": {}, "proxy_server": "http://127.0.0.1:1"}]
    assert not opened[0].get("block_media") and not opened[0].get("login_mode")
    assert discovered == [{"approve": approve, "preapproved": ["^x$"]}]


def test_learn_saves_by_default(monkeypatch):
    saved = []
    _fake_open_site(monkeypatch, [])
    monkeypatch.setattr(agent, "discover", lambda *a, **k: {"site": "zepto"})
    monkeypatch.setattr(recipes, "save", lambda site, r: saved.append(site))
    monkeypatch.setattr(recipes, "recipe_path", lambda site: "recipes/zepto.json")
    sync.learn("zepto")
    assert saved == ["zepto"]


# --- fetch_orders: drift and media blocking ---------------------------------------------------------------

def _drifting(monkeypatch, opened):
    _fake_open_site(monkeypatch, opened)
    monkeypatch.setattr(sync, "goto", lambda page, url: None)
    monkeypatch.setattr(sync, "raise_if_blocked", lambda page: None)

    def fail(*a, **k):
        raise sync.RecipeError("nothing")
    monkeypatch.setattr(sync, "fast_path", fail)
    monkeypatch.setattr(sync, "replay", fail)


def test_fetch_orders_drift_raises_recipe_drift(monkeypatch):
    _drifting(monkeypatch, [])
    monkeypatch.setattr(agent, "discover", lambda *a, **k: pytest.fail("user sync must not learn"))
    recipe = dict(RECIPE, orders_page_url="https://www.zepto.com/orders")
    with pytest.raises(sync.RecipeDrift, match="recipe drift for zepto") as e:
        sync.fetch_orders("zepto", None, allow_learn=False, recipe_loader=lambda s: recipe)
    assert isinstance(e.value, sync.RecipeError)


def test_fetch_orders_without_recipe_is_a_plain_recipe_error(monkeypatch):
    with pytest.raises(sync.RecipeError, match="no recipe") as e:
        sync.fetch_orders("zepto", None, allow_learn=False, recipe_loader=lambda s: None)
    assert not isinstance(e.value, sync.RecipeDrift)


def test_fetch_orders_passes_block_media(monkeypatch):
    opened = []
    _drifting(monkeypatch, opened)
    recipe = dict(RECIPE, orders_page_url="https://www.zepto.com/orders")
    for flag in (True, False):
        with pytest.raises(sync.RecipeDrift):
            sync.fetch_orders("zepto", None, allow_learn=False, recipe_loader=lambda s: recipe, block_media=flag)
    with pytest.raises(sync.RecipeDrift):
        sync.fetch_orders("zepto", None, allow_learn=False, recipe_loader=lambda s: recipe)
    assert [kw["block_media"] for kw in opened] == [True, False, False]

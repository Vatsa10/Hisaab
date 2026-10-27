"""Recipes come from Postgres in the cloud: sync.fetch_orders takes a loader, cli publish-recipe fills the table."""
import json

import pytest
from typer.testing import CliRunner

import recipes
import sync
from server import store

RECIPE = {"orders_page_url": "https://www.zepto.com/orders", "read_post_allowlist": [{"url": "x"}]}


class _Stop(Exception):
    pass


def test_fetch_orders_uses_recipe_loader(monkeypatch):
    seen = {}

    def fake_open(site, **k):
        seen.update(k)
        raise _Stop

    monkeypatch.setattr(sync, "open_site", fake_open)
    monkeypatch.setattr(sync.recipes, "load", lambda site: pytest.fail("file recipe must not be read"))
    with pytest.raises(_Stop):
        sync.fetch_orders("zepto", None, allow_learn=False, recipe_loader=lambda s: RECIPE,
                          proxy_server="http://127.0.0.1:1")
    assert seen["allowlist"] == RECIPE["read_post_allowlist"] and seen["proxy_server"] == "http://127.0.0.1:1"


def test_fetch_orders_loader_without_recipe_is_recipe_error(monkeypatch):
    monkeypatch.setattr(sync.recipes, "load", lambda site: pytest.fail("file recipe must not be read"))
    with pytest.raises(sync.RecipeError, match="no recipe for zepto"):
        sync.fetch_orders("zepto", None, allow_learn=False, recipe_loader=lambda s: None)


def test_fetch_orders_defaults_to_file_recipe(monkeypatch):
    monkeypatch.setattr(sync.recipes, "load", lambda site: RECIPE)
    monkeypatch.setattr(sync, "open_site", lambda site, **k: (_ for _ in ()).throw(_Stop()))
    with pytest.raises(_Stop):
        sync.fetch_orders("zepto", None, allow_learn=False)


def test_login_passes_proxy_server(monkeypatch):
    from helpers import fake_login_env
    seen = fake_login_env(monkeypatch, lambda self: {"success": False, "note": "stuck", "url": self.page.url})
    sync.login("zepto", "9876543210", prompt=lambda *a, **k: "", proxy_server="http://127.0.0.1:2",
               recipe_loader=lambda s: None)
    assert seen["open_site"][0]["proxy_server"] == "http://127.0.0.1:2"


def test_publish_recipe_cli(pg, monkeypatch, tmp_path):
    import cli
    monkeypatch.setattr(recipes, "RECIPES", tmp_path)
    (tmp_path / "zepto.json").write_text(json.dumps(RECIPE), encoding="utf-8")
    import os
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    r = CliRunner().invoke(cli.app, ["publish-recipe", "zepto"])
    assert r.exit_code == 0, r.output
    assert "version 1" in r.output
    assert store.get_recipe(pg, "zepto") == RECIPE
    r = CliRunner().invoke(cli.app, ["publish-recipe", "zepto"])
    assert "version 2" in r.output


def test_publish_recipe_cli_missing_file(monkeypatch, tmp_path):
    import cli
    monkeypatch.setattr(recipes, "RECIPES", tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    r = CliRunner().invoke(cli.app, ["publish-recipe", "zepto"])
    assert r.exit_code == 1 and "no learned recipe" in r.output


# ---------------------------------------------------------------- per-user orders page and safe publishing

def test_fetch_orders_orders_page_url_overrides_recipe(monkeypatch):
    import contextlib
    from types import SimpleNamespace
    went = []

    @contextlib.contextmanager
    def fake_open(site, **k):
        yield None, SimpleNamespace(url="about:blank"), None, None

    def fake_goto(page, url):
        went.append(url)
        raise _Stop
    monkeypatch.setattr(sync, "open_site", fake_open)
    monkeypatch.setattr(sync, "goto", fake_goto)
    mine = "https://www.zepto.com/orders?me=1"
    with pytest.raises(_Stop):
        sync.fetch_orders("zepto", None, allow_learn=False, recipe_loader=lambda s: RECIPE, orders_page_url=mine)
    with pytest.raises(_Stop):
        sync.fetch_orders("zepto", None, allow_learn=False, recipe_loader=lambda s: RECIPE)
    assert went == [mine, RECIPE["orders_page_url"]]


def test_login_reports_the_url_the_browser_landed_on(monkeypatch):
    from helpers import fake_login_env
    landed = "https://www.zepto.com/account/orders?u=42"
    pages = []

    def run(self):
        pages.append(self.page)
        return {"success": True, "note": "ok", "url": self.page.url}
    seen = fake_login_env(monkeypatch, run)
    monkeypatch.setattr(sync, "goto", lambda page, url: (seen["goto"].append(url), setattr(page, "url", landed)))
    got = []
    assert sync.login("zepto", "9876543210", prompt=lambda *a, **k: "", on_orders_url=got.append,
                      recipe_loader=lambda s: {"orders_page_url": "https://www.zepto.com/orders"})
    assert seen["goto"][-1] == "https://www.zepto.com/orders"
    assert got == [landed]


PERSONAL = {
    "orders_page_url": "https://www.zomato.com/users/jane-doe-123/ordering",
    "endpoint": "https://www.zomato.com/webroutes/order/history",
    "headers": {"Referer": "https://www.zomato.com/users/jane-doe-123/ordering", "ORIGIN": "https://www.zomato.com",
                "cookie": "zat=abc", "x-client": "web"},
    "nav_trace": [{"action": "navigate", "url": "https://www.zomato.com/"},
                  {"action": "click", "target": {"text": "Profile", "href": "/profile/jane-doe-123"},
                   "url_after": "/orders/123456789"}],
    "read_post_allowlist": [{"url": "https://www.zomato.com/api/x", "headers": {"Referer": "https://a/users/q"}}],
}


def _publish(args, monkeypatch, tmp_path, recipe):
    import cli
    monkeypatch.setattr(recipes, "RECIPES", tmp_path)
    (tmp_path / "zomato.json").write_text(json.dumps(recipe), encoding="utf-8")
    return CliRunner().invoke(cli.app, ["publish-recipe", "zomato", *args])


def test_publish_recipe_refuses_account_specific_values(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")  # never reached
    r = _publish([], monkeypatch, tmp_path, PERSONAL)
    assert r.exit_code != 0
    for path in ("$.orders_page_url", "$.nav_trace[1].target.href", "$.nav_trace[1].url_after"):
        assert path in r.output
    assert "$.headers" not in r.output and "$.read_post_allowlist" not in r.output  # dropped, not reported
    assert "jane-doe-123" not in r.output and "123456789" not in r.output
    assert "--force" in r.output


def test_publish_recipe_strips_identity_headers_and_force_publishes(pg, monkeypatch, tmp_path):
    import os
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    r = _publish(["--force"], monkeypatch, tmp_path, PERSONAL)
    assert r.exit_code == 0, r.output
    stored = store.get_recipe(pg, "zomato")
    assert stored["headers"] == {"x-client": "web"}
    assert stored["read_post_allowlist"] == [{"url": "https://www.zomato.com/api/x", "headers": {}}]
    assert stored["nav_trace"] == PERSONAL["nav_trace"]
    clean = {k: v for k, v in PERSONAL.items() if k not in ("orders_page_url", "nav_trace")}
    r = _publish([], monkeypatch, tmp_path, clean | {"orders_page_url": "https://www.zomato.com/orders"})
    assert r.exit_code == 0, r.output
    assert "version 2" in r.output

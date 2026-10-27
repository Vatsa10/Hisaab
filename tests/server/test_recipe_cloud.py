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

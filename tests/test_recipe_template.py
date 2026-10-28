import json

import pytest
from typer.testing import CliRunner

import recipes
from server import recipe_admin

SLUG = "test-user-123456789"
OTHER = "other-user-987654321"
NAME = "Fake Admin Name"
MSG = "zomato recipes are per-account: publish refuses --force; re-learn so the orders page can be templated."


def zomato(slug=SLUG):
    return {
        "site": "zomato",
        "orders_page_url": f"https://www.zomato.com/users/{slug}/ordering",
        "endpoint": "https://www.zomato.com/webroutes/order/history",
        "headers": {"x-client": "web"},
        "nav_trace": [
            {"action": "navigate", "url": "https://www.zomato.com/", "url_after": "/"},
            {"action": "click", "target": {"text": NAME}, "url_after": f"/users/{slug}"},
            {"action": "click", "target": {"text": "Orders"}, "url_after": f"/users/{slug}/ordering"},
            {"action": "scroll", "url_after": f"/users/{slug}/ordering"},
        ],
    }


def test_templatize_zomato_fixture():
    result = recipes.templatize(zomato())
    assert recipe_admin.account_specific(result) == []
    assert result["orders_page_url"] == "{orders_page_url}"
    assert SLUG not in json.dumps(result)
    assert result["nav_trace"][0] == {"action": "navigate", "url": "{orders_page_url}"}
    assert result["nav_trace"][1]["action"] == "scroll"
    assert result["nav_trace"][1]["url_after"] == "{account}/ordering"


def test_templatize_drops_name_clicks_before_orders_page():
    result = recipes.templatize(zomato())
    assert NAME not in json.dumps(result)
    assert len(result["nav_trace"]) == 2


def test_templatize_without_prefix_unchanged():
    r = {"orders_page_url": "https://www.zepto.com/orders", "nav_trace": []}
    assert recipes.templatize(r) == r


def test_bind_uses_each_users_orders_page():
    t = recipes.templatize(zomato())
    a = recipes.bind(t, f"https://www.zomato.com/users/{SLUG}/ordering")
    b = recipes.bind(t, f"https://www.zomato.com/users/{OTHER}/ordering")
    assert a["nav_trace"][0]["url"].endswith(f"/users/{SLUG}/ordering")
    assert b["nav_trace"][0]["url"].endswith(f"/users/{OTHER}/ordering")
    assert a["nav_trace"][1]["url_after"] == f"/users/{SLUG}/ordering"
    assert b["nav_trace"][1]["url_after"] == f"/users/{OTHER}/ordering"
    assert b["orders_page_url"].endswith(OTHER + "/ordering")
    assert "{account}" not in json.dumps(b) and "{orders_page_url}" not in json.dumps(b)


def test_bind_without_orders_page_raises():
    t = recipes.templatize(zomato())
    for bad in (None, "", "https://www.zomato.com/orders"):
        with pytest.raises(recipes.TemplateError):
            recipes.bind(t, bad)


def test_bind_plain_recipe_as_is():
    r = {"orders_page_url": "https://www.zepto.com/orders"}
    assert recipes.bind(r, None) == r


def test_publish_recipe_refuses_force_for_zomato(monkeypatch, tmp_path):
    import cli
    monkeypatch.setattr(recipes, "RECIPES", tmp_path)
    (tmp_path / "zomato.json").write_text(json.dumps(zomato()), encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    r = CliRunner().invoke(cli.app, ["publish-recipe", "zomato", "--force"])
    assert r.exit_code == 1 and MSG in r.output.replace("\n", "")
    # an un-templatable zomato recipe (still flagged) is refused too, and nothing is written
    bad = zomato() | {"orders_page_url": "https://www.zomato.com/ordering",
                      "endpoint": f"https://www.zomato.com/users/{SLUG}/x"}
    (tmp_path / "zomato.json").write_text(json.dumps(bad), encoding="utf-8")
    r = CliRunner().invoke(cli.app, ["publish-recipe", "zomato"])
    assert r.exit_code == 1 and MSG in r.output.replace("\n", "")


def test_sanitize_for_sharing_templates_zomato():
    clean, flagged = recipe_admin.sanitize_for_sharing(zomato(), "zomato")
    assert flagged == []
    assert clean["orders_page_url"] == "{orders_page_url}"
    _, flagged = recipe_admin.sanitize_for_sharing(zomato())
    assert flagged

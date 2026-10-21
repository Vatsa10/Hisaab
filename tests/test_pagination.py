import json
from urllib.parse import parse_qs, urlsplit

from recipes import first_request, next_request

GET_RECIPE = {"endpoint": "https://api.site.com/orders", "method": "GET", "query": {"limit": 10, "page": 1}}
POST_RECIPE = {"endpoint": "https://api.site.com/graphql", "method": "POST",
               "body": {"operationName": "Orders", "variables": {"offset": 0, "first": 10}}}


def q(req, key):
    return parse_qs(urlsplit(req["url"]).query).get(key, [None])[0]


def test_page_increments():
    req = first_request(GET_RECIPE)
    nxt = next_request(req, {"type": "page", "param": "page"}, {}, 10)
    assert q(nxt, "page") == "2" and q(nxt, "limit") == "10"
    assert q(next_request(nxt, {"type": "page", "param": "page"}, {}, 10), "page") == "3"


def test_page_missing_param_uses_start():
    req = first_request({"endpoint": "https://api.site.com/orders?x=1", "method": "GET"})
    assert q(next_request(req, {"type": "page", "param": "p", "start": 0}, {}, 5), "p") == "1"
    assert q(req, "x") == "1"


def test_offset_in_post_body():
    req = first_request(POST_RECIPE)
    nxt = next_request(req, {"type": "offset", "param": "variables.offset"}, {}, 10)
    assert nxt["body"]["variables"] == {"offset": 10, "first": 10}
    assert req["body"]["variables"]["offset"] == 0  # original untouched
    assert next_request(nxt, {"type": "offset", "param": "variables.offset"}, {}, 7)["body"]["variables"]["offset"] == 17


def test_cursor_from_response():
    req = first_request(GET_RECIPE)
    pag = {"type": "cursor", "param": "after", "next_path": "data.next_cursor"}
    nxt = next_request(req, pag, {"data": {"next_cursor": "abc"}}, 10)
    assert q(nxt, "after") == "abc"
    assert next_request(nxt, pag, {"data": {"next_cursor": None}}, 10) is None
    assert next_request(nxt, pag, {"data": {"next_cursor": "abc"}}, 10) is None  # repeated cursor


def test_stops_on_empty_page():
    assert next_request(first_request(GET_RECIPE), {"type": "page", "param": "page"}, {}, 0) is None


def test_post_body_string_is_handled():
    recipe = POST_RECIPE | {"body": json.dumps(POST_RECIPE["body"])}
    req = first_request(recipe)
    assert next_request(req, {"type": "offset", "param": "variables.offset"}, {}, 10)["body"]["variables"]["offset"] == 10


def test_page_two_check_flags_ignored_param(monkeypatch):
    import agent
    recipe = {"pagination": {"type": "page", "param": "page"},
              "field_map": {"orders_path": "o", "order_id": "id", "placed_at": "t", "total_amount": "a"}}
    page1 = {"o": [{"id": 1, "t": "2026-01-01", "a": 5}, {"id": 2, "t": "2026-01-02", "a": 6}]}
    req = first_request({"endpoint": "https://x.com/orders", "method": "GET", "query": {"page": 1}})
    orders = agent.recipes.valid_orders(agent.recipes.extract_orders(page1, recipe["field_map"]))
    monkeypatch.setattr(agent, "fetch_in_page", lambda page, r: page1)          # same page again
    assert "pagination does not work" in agent.check_page_two(None, recipe, req, page1, orders)
    monkeypatch.setattr(agent, "fetch_in_page", lambda page, r: {"o": [{"id": 3, "t": "2026-01-03", "a": 7}]})
    assert agent.check_page_two(None, recipe, req, page1, orders) is None
    monkeypatch.setattr(agent, "fetch_in_page", lambda page, r: {"o": []})     # history ends after page 1
    assert agent.check_page_two(None, recipe, req, page1, orders) is None

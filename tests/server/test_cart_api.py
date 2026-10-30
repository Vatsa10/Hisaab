"""Cart API (Plan 5 T6): grocery checks on connected apps, choices, and the admin fee and learn routes."""
import pytest
from helpers import A, B, add_user, auth_header
from test_cart_store import (FOOD, GROCERY, ITEMS, _pick, connect, grocery_user, matched, profile, recipe)

from server import admin, cart, limits, usage
from server.config import Settings

ADMIN = "00000000-0000-0000-0000-0000000000ad"
ADMIN_EMAIL = "admin@x.test"


@pytest.fixture
def admin_token(pg):
    add_user(pg, ADMIN, ADMIN_EMAIL)
    admin.add_admin(pg, ADMIN_EMAIL)
    return auth_header(ADMIN)


def _checks(pg, user=A):
    row = pg.execute("select cart_checks from usage where user_id=%s", (user,)).fetchone()
    return row[0] if row else 0


def _post(client, token, items=ITEMS):
    return client.post("/cart/checks", json={"items": items}, headers=token)


# --- POST /cart/checks ------------------------------------------------------------------------------------------

def test_check_starts_and_returns_pwa_shape(client, token_a, pg):
    grocery_user(pg)
    r = _post(client, token_a)
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"request_id", "sites", "cart_checks_left"}
    assert body["sites"] == ["zepto", "blinkit", "instamart"] and body["cart_checks_left"] == 9
    assert pg.execute("select count(*) from jobs where type='search'").fetchone()[0] == 2


def test_check_needs_address(client, token_a, pg):
    for site in cart.SEARCH_SITES:
        recipe(pg, site)
    connect(pg, A, "zepto")
    r = _post(client, token_a)
    assert (r.status_code, r.json()["detail"]) == (409, "Add your delivery address first.")
    assert _checks(pg) == 0


def test_check_needs_connected_app(client, token_a, pg):
    profile(pg, A)
    connect(pg, A, "zomato")
    r = _post(client, token_a)
    assert (r.status_code, r.json()["detail"]) == (409, "Connect Blinkit, Zepto or Instamart to compare.")
    assert _checks(pg) == 0 and pg.execute("select count(*) from cart_requests").fetchone()[0] == 0


def test_check_unavailable_is_503(client, token_a, pg):
    profile(pg, A)
    connect(pg, A, "zepto")                    # no search recipe learned yet
    r = _post(client, token_a)
    assert (r.status_code, r.json()["detail"]) == (503, "Cart checks are unavailable right now.")
    assert _checks(pg) == 0


def test_check_without_a_saved_address_here_is_409(client, token_a, pg):
    grocery_user(pg, sites=("zepto",))
    pg.execute("delete from app_addresses")
    r = _post(client, token_a)
    assert r.status_code == 409 and r.json()["detail"] == "Add this address in Zepto to compare here."
    assert _checks(pg) == 0


def test_second_check_while_running_is_409_and_not_counted(client, token_a, pg):
    grocery_user(pg)
    assert _post(client, token_a).status_code == 202
    r = _post(client, token_a)
    assert (r.status_code, r.json()["detail"]) == (409, "A cart check is already running.")
    assert _checks(pg) == 1


def test_eleventh_check_is_429(client, token_a, pg):
    grocery_user(pg)
    usage.record(pg, A, usage.today(None, "Asia/Kolkata"), cart_checks=10)
    r = _post(client, token_a)
    assert r.status_code == 429
    assert r.json()["detail"] == "You can check 10 carts a day. Try again tomorrow."
    assert int(r.headers["Retry-After"]) >= 1
    assert _checks(pg) == 10 and pg.execute("select count(*) from jobs").fetchone()[0] == 0


def test_check_paused_budget_is_503(client, token_a, pg):
    grocery_user(pg)
    pg.execute("insert into budget_state (month, level, spent_inr, budget_inr) values (%s, 'paused', 5000, 5000)",
               (usage.month_of(None, "Asia/Kolkata"),))
    r = _post(client, token_a)
    resumes = limits._first_of_next_month(None, "Asia/Kolkata")
    assert (r.status_code, r.json()["detail"]) == (503, f"Cart checks are paused until {resumes.day} {resumes:%B}.")
    assert _checks(pg) == 0 and pg.execute("select count(*) from cart_requests").fetchone()[0] == 0


def test_check_validates_items(client, token_a, pg):
    grocery_user(pg)
    bad = ([], [{"text": "", "qty": 1}], [{"text": "x" * 81, "qty": 1}], [{"text": "milk", "qty": 0}],
           [{"text": "milk", "qty": 21}], [{"text": f"item {i}", "qty": 1} for i in range(31)],
           [{"qty": 1}], "milk")
    for items in bad:
        assert _post(client, token_a, items).status_code == 422, items
    assert _checks(pg) == 0
    assert client.post("/cart/checks", json={"items": ITEMS}).status_code == 401


def test_usual_endpoint(client, token_a, pg):
    from test_cart_store import _order
    for n in range(3):
        _order(pg, A, "zepto", f"z{n}", n + 1, [("Farm Eggs", 1)])
    r = client.get("/cart/usual", headers=token_a)
    assert r.status_code == 200 and r.json() == {"items": [{"text": "Farm Eggs", "qty": 1, "times": 3}]}


def test_access_endpoint(client, token_a, pg):
    for site in cart.SEARCH_SITES:
        recipe(pg, site)
    recipe(pg, "swiggy", "restaurants")
    recipe(pg, "swiggy", "menu")
    connect(pg, A, "zepto")
    connect(pg, A, "swiggy", "needs_login")
    connect(pg, A, "zomato")
    r = client.get("/cart/access?mode=grocery", headers=token_a)
    assert r.json() == {"access": {"zepto": "ok", "blinkit": "not_connected", "instamart": "needs_login"}}
    r = client.get("/cart/access?mode=food", headers=token_a)
    assert r.json() == {"access": {"swiggy": "needs_login", "zomato": "unavailable"}}
    assert client.get("/cart/access", headers=token_a).json()["access"]["zepto"] == "ok"
    assert client.get("/cart/access?mode=pizza", headers=token_a).status_code == 422


# --- choices ----------------------------------------------------------------------------------------------------

def test_choice_endpoint_scoped_to_owner(client, token_a, token_b, pg):
    grocery_user(pg, sites=("zepto",))
    rid = _post(client, token_a).json()["request_id"]
    url = f"/cart/checks/{rid}/choices"
    body = {"idx": 0, "site": "zepto", "product_id": "z-milk"}
    r = client.post(url, json=body, headers=token_a)
    assert (r.status_code, r.json()["detail"]) == (409, "Prices are still loading.")

    cart.record_site_result(pg, rid, "zepto", "ok", [_pick(0), matched(1, "Farm Eggs 6 pcs", 50)])
    assert cart.finalize_if_complete(pg, rid, Settings())

    assert client.post(url, json=body, headers=token_b).status_code == 404
    assert client.post("/cart/checks/not-a-uuid/choices", json=body, headers=token_a).status_code == 404
    r = client.post(url, json={**body, "product_id": "nope"}, headers=token_a)
    assert (r.status_code, r.json()["detail"]) == (422, "That isn't one of the options.")
    r = client.post(url, json=body, headers=token_a)
    assert r.status_code == 200
    summary = r.json()["summary"]
    assert summary["request_id"] == rid and summary["cards"][0]["to_pick"] == 0
    assert {"cards", "table", "picks", "split", "best_site", "price_note"} <= set(summary)
    assert _checks(pg) == 1


# --- admin ------------------------------------------------------------------------------------------------------

def test_admin_routes_forbidden_for_non_admin(client, token_a, pg):
    add_user(pg, A, "user@x.test")
    for method, path in (("get", "/admin/fees"), ("put", "/admin/fees/zepto"), ("post", "/admin/learn/zepto"),
                         ("get", "/admin/sites")):
        kw = {"json": {"rules": GROCERY, "verified": True}} if method == "put" else {}
        r = getattr(client, method)(path, headers=token_a, **kw)
        assert (r.status_code, r.json()["detail"]) == (403, "admin only"), path


def test_admin_fees_get_put(client, admin_token, pg, fee_rules_snapshot):
    r = client.get("/admin/fees", headers=admin_token)
    assert r.status_code == 200
    rows = r.json()
    assert [x["site"] for x in rows] == ["zepto", "blinkit", "instamart", "swiggy", "zomato"]
    assert rows[3]["rules"]["kind"] == "food" and rows[0]["verified"] is False and rows[0]["version"] == 1
    assert rows[0]["kind"] == "grocery" and rows[3]["kind"] == "food"

    r = client.put("/admin/fees/zepto", json={"rules": GROCERY, "verified": True}, headers=admin_token)
    assert (r.status_code, r.json()) == (200, {"version": 2})
    r = client.put("/admin/fees/zomato", json={"rules": FOOD, "verified": False}, headers=admin_token)
    assert (r.status_code, r.json()) == (200, {"version": 2})
    r = client.put("/admin/fees/zomato", json={"rules": GROCERY, "verified": True}, headers=admin_token)
    assert (r.status_code, r.json()) == (422, {"detail": ["zomato needs food fee rules"]})
    r = client.put("/admin/fees/blinkit", json={"rules": {"handling": 5}, "verified": True}, headers=admin_token)
    assert r.status_code == 422 and isinstance(r.json()["detail"], list) and r.json()["detail"]
    assert all(isinstance(e, str) for e in r.json()["detail"])
    assert client.put("/admin/fees/amazon", json={"rules": GROCERY, "verified": True},
                      headers=admin_token).status_code == 404
    got = {x["site"]: x for x in client.get("/admin/fees", headers=admin_token).json()}
    assert got["zepto"]["rules"] == GROCERY and got["zepto"]["verified"] is True and got["zepto"]["version"] == 2


def test_admin_learn_search_requires_admin_connection(client, admin_token, pg):
    profile(pg, ADMIN)
    connect(pg, ADMIN, "zepto")
    r = client.post("/admin/learn/instamart?kind=search", headers=admin_token)
    assert (r.status_code, r.json()["detail"]) == (409, "Connect Swiggy on your own account first.")
    connect(pg, ADMIN, "swiggy", "needs_login")
    assert client.post("/admin/learn/instamart?kind=search", headers=admin_token).status_code == 409
    pg.execute("update connections set status='connected' where site='swiggy'")
    r = client.post("/admin/learn/instamart?kind=search", headers=admin_token)
    assert r.status_code == 202
    job = pg.execute("select type, site, profile, payload, user_id::text from jobs where id=%s",
                     (r.json()["job_id"],)).fetchone()
    assert job == ("learn", "instamart", "swiggy", {"reason": "manual", "kind": "search"}, ADMIN)
    r = client.post("/admin/learn/instamart?kind=search", headers=admin_token)
    assert r.status_code == 409                               # one learn at a time per site
    r = client.post("/admin/learn/zepto?kind=search", headers=admin_token)
    assert r.status_code == 202
    assert client.post("/admin/learn/swiggy?kind=search", headers=admin_token).status_code == 400
    assert client.post("/admin/learn/zepto?kind=menu", headers=admin_token).status_code == 422


def test_admin_learn_needs_an_address(client, admin_token, pg):
    connect(pg, ADMIN, "zepto")
    r = client.post("/admin/learn/zepto?kind=search", headers=admin_token)
    assert (r.status_code, r.json()["detail"]) == (409, "Add your delivery address first.")


def test_admin_learn_food_kind(client, admin_token, pg):
    profile(pg, ADMIN)
    connect(pg, ADMIN, "zomato")
    connect(pg, ADMIN, "zepto")
    r = client.post("/admin/learn/zomato?kind=food", headers=admin_token)
    assert r.status_code == 202
    assert pg.execute("select payload from jobs where id=%s", (r.json()["job_id"],)).fetchone()[0] == {
        "reason": "manual", "kind": "food"}
    assert client.post("/admin/learn/zepto?kind=food", headers=admin_token).status_code == 400
    r = client.post("/admin/learn/swiggy?kind=food", headers=admin_token)
    assert (r.status_code, r.json()["detail"]) == (409, "Connect Swiggy on your own account first.")
    # orders is the default kind
    r = client.post("/admin/learn/zepto", headers=admin_token)
    assert r.status_code == 202
    assert pg.execute("select payload from jobs where id=%s", (r.json()["job_id"],)).fetchone()[0] == {
        "reason": "manual", "kind": "orders"}
    assert client.post("/admin/learn/instamart", headers=admin_token).status_code == 400


def test_admin_sites_lists_search_and_food_rows(client, admin_token, pg):
    recipe(pg, "zepto", "search")
    recipe(pg, "swiggy", "menu", healthy=False)
    recipe(pg, "blinkit", "orders")
    connect(pg, ADMIN, "swiggy")
    for status in ("done", "done", "failed"):
        pg.execute("insert into jobs (user_id, type, site, profile, payload, status, finished_at) "
                   "values (%s, 'search', 'zepto', 'zepto', '{\"op\": \"products\"}', %s, now())", (B, status))
    pg.execute("insert into jobs (user_id, type, site, profile, payload, status, created_at) values "
               "(%s, 'search', 'zepto', 'zepto', '{\"op\": \"products\"}', 'done', now() - interval '8 days')", (B,))
    pg.execute("insert into jobs (user_id, type, site, profile, payload, status, finished_at) values "
               "(%s, 'search', 'swiggy', 'swiggy', '{\"op\": \"menu\"}', 'failed', now())", (B,))
    r = client.get("/admin/sites", headers=admin_token)
    assert r.status_code == 200
    rows = {(x["site"], x["kind"]): x for x in r.json()}
    assert {("zepto", "search"), ("blinkit", "search"), ("instamart", "search"), ("swiggy", "restaurants"),
            ("swiggy", "menu"), ("zomato", "restaurants"), ("zomato", "menu"), ("zepto", "orders"),
            ("blinkit", "orders"), ("swiggy", "orders"), ("zomato", "orders")} <= set(rows)
    z = rows[("zepto", "search")]
    assert (z["op"], z["db_site"], z["version"], z["healthy"], z["syncs_ok_7d"], z["syncs_failed_7d"]) == (
        "products", "zepto", 1, True, 2, 1)
    assert z["admin_connected"] is False and z["last_ok_at"] is not None
    menu = rows[("swiggy", "menu")]
    assert (menu["op"], menu["healthy"], menu["syncs_failed_7d"], menu["admin_connected"]) == ("menu", False, 1, True)
    inst = rows[("instamart", "search")]
    assert inst["db_site"] == "swiggy" and inst["admin_connected"] is True and inst["version"] is None
    assert rows[("zomato", "restaurants")]["admin_connected"] is False
    assert rows[("blinkit", "orders")]["version"] == 1

"""Fill API (Plan 6 T7): fill options, start, confirm, answers, limits and admin cart learning."""
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest
from helpers import A, B, add_user, auth_header
from psycopg.types.json import Jsonb
from test_cart_store import connect as connect_user
from test_cart_store import profile, recipe
from test_fill_store import cart_recipe, connect, make_request, make_result, outcome

from server import admin, fill
from server.config import OPEN_APP_URLS, Settings

ADMIN = "00000000-0000-0000-0000-0000000000ad"
TODAY_IST = "(now() at time zone 'Asia/Kolkata')::date"


def fresh():
    return datetime.now(timezone.utc) - timedelta(minutes=5)


@pytest.fixture
def admin_token(pg):
    add_user(pg, ADMIN, "admin@x.test")
    admin.add_admin(pg, "admin@x.test")
    return auth_header(ADMIN)


def ready(pg, site="zepto", user=A, **k):
    k.setdefault("priced_at", fresh())
    rid = make_request(pg, user=user, **k)
    make_result(pg, rid, site, user=user, priced_at=k["priced_at"])
    cart_recipe(pg, site)
    connect(pg, "swiggy" if site == "instamart" else site, user=user)
    return rid


def fills_used(pg, user=A):
    row = pg.execute("select cart_fills from usage where user_id = %s", (user,)).fetchone()
    return row[0] if row else 0


def post_fill(client, token, rid, site="zepto"):
    return client.post(f"/cart/checks/{rid}/fill", json={"site": site}, headers=token)


def detail(r):
    return (r.status_code, r.json()["detail"])


def test_fill_flow_returns_202_and_job(client, token_a, pg):
    rid = ready(pg)
    r = post_fill(client, token_a, rid)
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"fill_id", "job_id", "fills_left"} and body["fills_left"] == 4
    job = pg.execute("select type, site, user_id::text, payload from jobs where id = %s", (body["job_id"],)).fetchone()
    assert job == ("fill_cart", "zepto", A, {"fill_id": body["fill_id"]})
    assert fills_used(pg) == 1


def test_double_fill_is_409(client, token_a, pg):
    rid = ready(pg)
    assert post_fill(client, token_a, rid).status_code == 202
    pg.execute("update cart_fills set status = 'done'")
    pg.execute("update jobs set status = 'done'")
    assert detail(post_fill(client, token_a, rid)) == (409, "This cart was already filled on Zepto.")
    assert fills_used(pg) == 1


def test_fill_scoped_to_owner(client, token_a, token_b, pg):
    rid = ready(pg)
    assert detail(post_fill(client, token_b, rid)) == (404, "not found")
    assert client.get(f"/cart/checks/{rid}/fill", headers=token_b).status_code == 404
    fid = post_fill(client, token_a, rid).json()["fill_id"]
    assert client.post(f"/cart/fills/{fid}/confirm", headers=token_b).status_code == 404
    assert detail(post_fill(client, token_b, "not-a-uuid")) == (404, "not found")
    assert fills_used(pg, B) == 0


def test_fill_errors_exact_details(client, token_a, pg):
    rid = ready(pg, status="searching")
    assert detail(post_fill(client, token_a, rid)) == (409, "Prices are still loading.")
    assert client.get(f"/cart/checks/{rid}/fill", headers=token_a).status_code == 409
    food = ready(pg, mode="food")
    assert detail(post_fill(client, token_a, food)) == (422, "Filling isn't available for food orders.")
    rid = ready(pg)
    assert detail(post_fill(client, token_a, rid, "blinkit")) == (422, "Blinkit has no prices in this check.")
    pg.execute("update recipes set paused = true")
    assert detail(post_fill(client, token_a, rid)) == (503, "Filling carts on Zepto isn't available right now.")
    pg.execute("update recipes set paused = false")
    pg.execute("update connections set status = 'needs_login'")
    r = post_fill(client, token_a, rid)
    assert r.status_code == 409 and "Reconnect" in r.json()["detail"]
    pg.execute("delete from connections")
    assert detail(post_fill(client, token_a, rid)) == (409, "Connect Zepto first.")
    connect(pg, "zepto")
    stale = ready(pg, priced_at=datetime.now(timezone.utc) - timedelta(minutes=45))
    assert detail(post_fill(client, token_a, stale)) == (
        409, "These prices are over 30 minutes old. Check again first.")
    nothing = ready(pg)
    pg.execute("update cart_results set items = %s where request_id = %s",
               (Jsonb([{"idx": 0, "status": "not_found", "confidence": 0, "product": None, "packs": None,
                        "line_total": None, "swap_from": None, "candidates": []}]), nothing))
    assert detail(post_fill(client, token_a, nothing)) == (409, "Nothing to add on Zepto.")
    assert post_fill(client, token_a, rid).status_code == 202
    other = ready(pg)
    assert detail(post_fill(client, token_a, other)) == (409, "A cart is already being filled.")
    assert client.post(f"/cart/checks/{rid}/fill", json={}, headers=token_a).status_code == 422


def test_fill_limit_429_and_budget_503(client, token_a, pg):
    rid = ready(pg)
    pg.execute(f"insert into usage (user_id, day, cart_fills) values (%s, {TODAY_IST}, %s)",
               (A, Settings().cart_fills_per_day))
    r = post_fill(client, token_a, rid)
    assert r.status_code == 429 and "Retry-After" in r.headers
    assert pg.execute("select count(*) from cart_fills").fetchone()[0] == 0
    pg.execute("delete from usage")
    month = datetime.now(timezone.utc).astimezone().strftime("%Y-%m")
    pg.execute("insert into budget_state (month, level, spent_inr, budget_inr) values (%s,'paused',5000,5000)",
               (month,))
    r = post_fill(client, token_a, rid)
    assert r.status_code == 503 and r.json()["detail"].startswith("Filling carts is paused")
    assert fills_used(pg) == 0


def test_cooling_site_is_503_with_retry_after(client, token_a, pg):
    rid = ready(pg)
    pg.execute("insert into site_backoff (site, level, until) values ('zepto', 1, now() + interval '10 minutes')")
    r = post_fill(client, token_a, rid)
    assert r.status_code == 503 and "limiting requests" in r.json()["detail"] and "Retry-After" in r.headers
    assert fills_used(pg) == 0


def test_fill_options_endpoint(client, token_a, pg):
    rid = ready(pg)
    r = client.get(f"/cart/checks/{rid}/fill", headers=token_a)
    assert r.status_code == 200
    body = r.json()
    assert body["fills_left"] == 5 and body["fills_limit"] == 5
    assert body["sites"]["zepto"]["state"] == "ready" and body["sites"]["zepto"]["preview"]["add"]


def needs_confirmation(client, token_a, pg):
    rid = ready(pg)
    fid = post_fill(client, token_a, rid).json()["fill_id"]
    app = {"lat": 22.31, "lon": 73.17, "store_id": "S2", "area": "Alkapuri"}
    fill.finish_fill(pg, fid, outcome("needs_confirmation", [], app_location=app, repriced=[]),
                     OPEN_APP_URLS["zepto"], Settings())
    pg.execute("update jobs set status = 'done'")
    return rid, fid


def test_confirm_endpoint(client, token_a, pg):
    _, fid = needs_confirmation(client, token_a, pg)
    r = client.post(f"/cart/fills/{fid}/confirm", headers=token_a)
    assert r.status_code == 202 and set(r.json()) == {"fill_id", "job_id", "fills_left"}
    assert r.json()["fill_id"] != fid and fills_used(pg) == 1
    r = client.post(f"/cart/fills/{fid}/confirm", headers=token_a)
    assert detail(r) == (409, "These prices have expired. Check again.")


def test_confirm_errors(client, token_a, pg):
    _, fid = needs_confirmation(client, token_a, pg)
    pg.execute("update recipes set paused = true")
    assert detail(client.post(f"/cart/fills/{fid}/confirm", headers=token_a)) == (
        503, "Filling carts on Zepto isn't available right now.")
    pg.execute("update recipes set paused = false")
    pg.execute("delete from connections")
    assert detail(client.post(f"/cart/fills/{fid}/confirm", headers=token_a)) == (409, "Connect Zepto first.")
    assert client.post("/cart/fills/nope/confirm", headers=token_a).status_code == 404


def fill_job(pg, stage="question_needed", status="running", type="fill_cart"):
    return str(pg.execute(
        "insert into jobs (user_id, type, site, profile, payload, status, progress) "
        "values (%s,%s,'zepto','zepto','{}',%s,%s) returning id",
        (A, type, status, Jsonb({"stage": stage}))).fetchone()[0])


def test_answer_reaches_fill_job(client, token_a, pg, pg2):
    jid = fill_job(pg)
    chan = "otp_" + jid.replace("-", "")
    pg2.execute(f"listen {chan}")
    assert client.post(f"/jobs/{jid}/answer", json={"answer": "c1"}, headers=token_a).status_code == 204
    notes = list(pg2.notifies(timeout=2, stop_after=1))
    assert notes and notes[0].channel == chan and notes[0].payload == "c1"
    pg.execute("update jobs set status = 'done'")
    other = fill_job(pg, stage="filling")
    assert client.post(f"/jobs/{other}/answer", json={"answer": "x"}, headers=token_a).status_code == 404
    pg.execute("update jobs set status = 'done'")
    queued = fill_job(pg, status="queued")
    assert client.post(f"/jobs/{queued}/answer", json={"answer": "x"}, headers=token_a).status_code == 404


def test_answer_refused_for_sync_job(client, token_a, pg):
    jid = fill_job(pg, type="sync")
    assert client.post(f"/jobs/{jid}/answer", json={"answer": "x"}, headers=token_a).status_code == 404


def test_me_limits_has_fills(client, token_a, pg):
    body = client.get("/me/limits", headers=token_a).json()
    assert body["cart_fills_left"] == 5 and body["cart_fills_limit"] == 5
    pg.execute(f"insert into usage (user_id, day, cart_fills) values (%s, {TODAY_IST}, 2)", (A,))
    assert client.get("/me/limits", headers=token_a).json()["cart_fills_left"] == 3


def test_admin_costs_has_cart_fills(client, admin_token, pg):
    pg.execute(f"insert into usage (user_id, day, cart_fills) values (%s, {TODAY_IST}, 2)", (A,))
    users = client.get("/admin/costs", headers=admin_token).json()["users"]
    assert [u["cart_fills"] for u in users if u["user_id"] == A] == [2]


def test_admin_learn_cart_requires_connection_search_and_address(client, admin_token, pg):
    url = "/admin/learn/zepto?kind=cart"
    assert detail(client.post(url, headers=admin_token)) == (409, "Connect Zepto on your own account first.")
    connect_user(pg, ADMIN, "zepto")
    assert detail(client.post(url, headers=admin_token)) == (409, "Learn Zepto prices (search) first.")
    recipe(pg, "zepto", "search", healthy=False)
    assert detail(client.post(url, headers=admin_token)) == (409, "Learn Zepto prices (search) first.")
    pg.execute("update recipes set healthy = true")
    assert detail(client.post(url, headers=admin_token)) == (409, "Add your delivery address first.")
    profile(pg, ADMIN)
    r = client.post(url, headers=admin_token)
    assert r.status_code == 202
    job = pg.execute("select type, site, profile, payload, user_id::text from jobs where id = %s",
                     (r.json()["job_id"],)).fetchone()
    assert job == ("learn", "zepto", "zepto", {"reason": "manual", "kind": "cart"}, ADMIN)
    assert detail(client.post(url, headers=admin_token)) == (409, "A learn is already running.")
    assert client.post("/admin/learn/swiggy?kind=cart", headers=admin_token).status_code == 400


def test_admin_learn_cart_is_admin_only(client, token_a, pg):
    assert client.post("/admin/learn/zepto?kind=cart", headers=token_a).status_code == 403


def test_admin_learn_cart_instamart_needs_swiggy(client, admin_token, pg):
    profile(pg, ADMIN)
    recipe(pg, "instamart", "search")
    connect_user(pg, ADMIN, "zepto")
    assert detail(client.post("/admin/learn/instamart?kind=cart", headers=admin_token)) == (
        409, "Connect Swiggy on your own account first.")
    connect_user(pg, ADMIN, "swiggy")
    r = client.post("/admin/learn/instamart?kind=cart", headers=admin_token)
    assert r.status_code == 202
    assert pg.execute("select profile from jobs where id = %s", (r.json()["job_id"],)).fetchone()[0] == "swiggy"


def test_admin_sites_lists_cart_rows(client, admin_token, pg):
    connect_user(pg, ADMIN, "swiggy")
    cart_recipe(pg, "zepto")
    for status in ("done", "done", "failed"):
        pg.execute("insert into jobs (user_id, type, site, profile, payload, status, finished_at) "
                   "values (%s, 'fill_cart', 'zepto', 'zepto', '{}', %s, now())", (B, status))
    pg.execute("insert into jobs (user_id, type, site, profile, payload, status, finished_at) values "
               "(%s, 'fill_cart', 'zepto', 'zepto', '{}', 'done', now() - interval '9 days')", (B,))
    pg.execute("insert into recipe_approvals (site, kind, reason, status, proposed, diff) "
               "values ('blinkit', 'cart', 'learned', 'pending', '{}', '{}')")
    rows = {(x["site"], x["kind"]): x for x in client.get("/admin/sites", headers=admin_token).json()}
    assert {("zepto", "cart"), ("blinkit", "cart"), ("instamart", "cart")} <= set(rows)
    z = rows[("zepto", "cart")]
    assert (z["syncs_ok_7d"], z["syncs_failed_7d"], z["needs_account"], z["admin_connected"],
            z["pending_approval"], z["version"]) == (2, 1, True, False, False, 1)
    assert rows[("blinkit", "cart")]["pending_approval"] is True
    i = rows[("instamart", "cart")]
    assert i["db_site"] == "swiggy" and i["admin_connected"] is True and i["version"] is None


def test_only_tapped_routes_create_fill_jobs(client, pg):
    """Only cart_api's two fill routes call fill.start_fill / confirm_fill; nothing else enqueues a fill_cart."""
    server = pathlib.Path(__file__).resolve().parents[2] / "server"
    calls = re.compile(r"\bfill\.(start_fill|confirm_fill|_create)\(")
    callers = {f.name: calls.findall(f.read_text(encoding="utf-8")) for f in server.glob("*.py")}
    assert {n: c for n, c in callers.items() if c and n != "fill.py"} == {"cart_api.py": ["start_fill", "confirm_fill"]}
    enqueuers = [f.name for f in server.glob("*.py")
                 if re.search(r"enqueue\(.*[\"']fill_cart[\"']", f.read_text(encoding="utf-8"))]
    assert enqueuers == ["fill.py"]

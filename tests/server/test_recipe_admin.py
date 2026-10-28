"""Recipe approvals (live POST-allowlist decisions and pending learned recipes), site health and admin learners."""
import threading
import time

import pytest
from helpers import A, B, add_user
from psycopg.types.json import Jsonb

from server import jobs, recipe_admin, store

C = "00000000-0000-0000-0000-00000000000c"
POST = {"url_regex": "^https://www\\.zepto\\.com/api/orders$", "why": "order list"}
NEW_POST = {"url_regex": "^https://www\\.zepto\\.com/api/v2/orders$", "why": "order list v2"}
BASE = {"orders_page_url": "https://www.zepto.com/orders", "endpoint": "https://www.zepto.com/api/orders",
        "method": "POST", "read_post_allowlist": [POST]}


def _admin(conn, user_id, email, *, listed=None):
    add_user(conn, user_id, email)
    conn.execute("insert into admins(email) values (%s)", ((listed or email).lower(),))


def _running_job(conn, user_id=A, site="zepto", type="learn"):
    jid = jobs.enqueue(conn, user_id, type, site)
    assert jobs.claim(conn, "w1", (type,)).id == jid
    return jid


def _approval(conn, approval_id):
    return conn.execute("select status, reason, decided_by, proposed, diff from recipe_approvals where id=%s",
                        (approval_id,)).fetchone()


def _recipe_row(conn, site="zepto", kind="orders"):
    return conn.execute("select version, healthy, paused, health_note from recipes where site=%s and kind=%s",
                        (site, kind)).fetchone()


# ---------------------------------------------------------------- admins

def test_is_admin_case_insensitive(pg):
    _admin(pg, A, "Owner@Example.com")
    add_user(pg, B, "someone@example.com")
    assert recipe_admin.is_admin(pg, A) is True
    assert recipe_admin.is_admin(pg, B) is False
    assert recipe_admin.is_admin(pg, C) is False  # no auth.users row at all


def test_admin_learner_needs_connected_account(pg):
    _admin(pg, A, "first@example.com")
    _admin(pg, B, "second@example.com")
    add_user(pg, C, "user@example.com")
    store.set_connection(pg, C, "zepto", status="connected")  # not an admin
    assert recipe_admin.admin_learner(pg, "zepto") is None
    store.set_connection(pg, A, "zepto", status="needs_login")
    store.set_connection(pg, B, "zepto", status="connected")
    assert recipe_admin.admin_learner(pg, "zepto") == B
    store.set_connection(pg, A, "zepto", status="connected")
    assert recipe_admin.admin_learner(pg, "zepto") == A  # the oldest admin wins
    assert recipe_admin.admin_learner(pg, "blinkit") is None


# ---------------------------------------------------------------- sanitising and diffs

def test_sanitize_matches_publish_recipe():
    import cli
    recipe = {
        "orders_page_url": "https://www.zomato.com/users/123/ordering",
        "endpoint": "https://www.zomato.com/webroutes/order/history",
        "headers": {"Referer": "https://www.zomato.com/users/123/ordering", "x-client": "web"},
    }
    clean, flagged = recipe_admin.sanitize_for_sharing(recipe)
    assert clean["headers"] == {"x-client": "web"}
    assert clean["orders_page_url"] == recipe["orders_page_url"]  # flagged, not rewritten
    assert flagged == ["$.orders_page_url"]
    assert recipe["headers"]["Referer"]  # the input is not mutated
    # publish-recipe uses the very same functions
    assert cli._strip_identity_headers is recipe_admin.strip_identity_headers
    assert cli._account_specific is recipe_admin.account_specific
    assert (cli._strip_identity_headers(recipe), cli._account_specific(clean)) == (clean, flagged)


def test_diff_reports_add_remove_change():
    old = {"a": {"b": [1, 2, 3]}, "gone": "x", "same": {"k": 1}, "n": 1}
    new = {"a": {"b": [1, 5]}, "added": {"z": True}, "same": {"k": 1}, "n": 2}
    assert recipe_admin.diff(old, new) == [
        {"op": "change", "path": "$.a.b[1]", "old": 2, "new": 5},
        {"op": "remove", "path": "$.a.b[2]", "old": 3},
        {"op": "add", "path": "$.added", "new": {"z": True}},
        {"op": "remove", "path": "$.gone", "old": "x"},
        {"op": "change", "path": "$.n", "old": 1, "new": 2},
    ]
    assert recipe_admin.diff(None, {"x": 1}) == [{"op": "add", "path": "$.x", "new": 1}]
    assert recipe_admin.diff(new, new) == []
    many = recipe_admin.diff({"l": []}, {"l": list(range(11))})
    assert [d["path"] for d in many][-2:] == ["$.l[9]", "$.l[10]"]  # indices sort numerically


# ---------------------------------------------------------------- applying learned recipes

def test_apply_without_new_posts_auto_applies(pg):
    store.put_recipe(pg, "zepto", BASE)
    learned = BASE | {"orders_page_url": "https://www.zepto.com/account/orders",
                      "headers": {"Origin": "https://www.zepto.com", "accept": "json"}}
    jid = _running_job(pg)
    res = recipe_admin.apply_learned(pg, "zepto", "orders", learned, set(), jid, "drift_relearn")
    assert res["status"] == "auto_applied" and res["version"] == 2
    stored = store.get_recipe(pg, "zepto")
    assert stored["headers"] == {"accept": "json"}  # sanitised before publishing
    status, reason, _, proposed, diff = _approval(pg, res["approval_id"])
    assert (status, reason, proposed) == ("auto_applied", "drift_relearn", stored)
    assert {"op": "change", "path": "$.orders_page_url", "old": BASE["orders_page_url"],
            "new": "https://www.zepto.com/account/orders"} in diff
    assert pg.execute("select job_id::text from recipe_approvals").fetchone()[0] == jid


def test_apply_with_unapproved_post_is_pending(pg):
    store.put_recipe(pg, "zepto", BASE)
    learned = BASE | {"read_post_allowlist": [POST, NEW_POST]}
    res = recipe_admin.apply_learned(pg, "zepto", "orders", learned, set(), None, "learned")
    assert res["status"] == "pending" and res["version"] is None
    assert store.get_recipe(pg, "zepto") == BASE and _recipe_row(pg)[0] == 1
    status, reason, _, proposed, diff = _approval(pg, res["approval_id"])
    assert (status, reason, proposed) == ("pending", "post_allowlist", learned)
    assert diff == [{"op": "add", "path": "$.read_post_allowlist[1]", "new": NEW_POST}]


def test_apply_with_approved_post_applies(pg):
    store.put_recipe(pg, "zepto", BASE)
    learned = BASE | {"read_post_allowlist": [POST, NEW_POST]}
    res = recipe_admin.apply_learned(pg, "zepto", "orders", learned, {NEW_POST["url_regex"]}, None, "learned")
    assert res["status"] == "auto_applied" and res["version"] == 2
    assert store.get_recipe(pg, "zepto") == learned
    # first recipe for a site: its POST entries count as new unless approved in the job
    res = recipe_admin.apply_learned(pg, "blinkit", "orders", BASE, set(), None, "learned")
    assert res["status"] == "pending" and store.get_recipe(pg, "blinkit") is None
    res = recipe_admin.apply_learned(pg, "blinkit", "orders", BASE, {POST["url_regex"]}, None, "learned")
    assert res["status"] == "auto_applied" and res["version"] == 1


def test_account_specific_recipe_is_pending(pg):
    learned = {"orders_page_url": "https://www.zomato.com/users/123/ordering"}
    res = recipe_admin.apply_learned(pg, "zomato", "orders", learned, set(), None, "learned")
    assert res["status"] == "pending"
    assert _approval(pg, res["approval_id"])[1] == "account_specific"
    assert store.get_recipe(pg, "zomato") is None


def test_cart_recipe_always_pending(pg):
    cart = {"add_url": "https://www.zepto.com/cart"}
    res = recipe_admin.apply_learned(pg, "zepto", "cart", cart, set(), None, "learned")
    assert res["status"] == "pending" and res["version"] is None
    assert _approval(pg, res["approval_id"])[1] == "cart"
    assert store.get_recipe(pg, "zepto", kind="cart") is None


# ---------------------------------------------------------------- live POST-allowlist approvals

def _decide_when_asked(conn, jid, decision, admin=A, timeout=10.0):
    """In a thread: wait for the job to ask, then decide from this (second) connection."""
    out = {}

    def run():
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            p = conn.execute("select progress from jobs where id=%s", (jid,)).fetchone()[0]
            if p.get("stage") == "approval_needed":
                out["progress"] = p
                out["status"] = recipe_admin.decide(conn, p["approval_id"], decision, admin)
                return
            time.sleep(0.05)
    t = threading.Thread(target=run)
    t.start()
    return t, out


def test_approver_round_trip(pg, pg2, pg_listen):
    _admin(pg, A, "owner@example.com")
    jid = _running_job(pg)
    approve = recipe_admin.make_approver(pg_listen, pg, jid, "zepto", "orders", timeout_s=10)
    t, out = _decide_when_asked(pg2, jid, "approve")
    assert approve([NEW_POST]) is True
    t.join()
    assert out["status"] == "approved"
    asked = out["progress"]
    assert asked["entries"] == [NEW_POST]
    assert approve.approved == {NEW_POST["url_regex"]}
    status, reason, decided_by, proposed, _ = _approval(pg, asked["approval_id"])
    assert (status, reason, str(decided_by)) == ("approved", "post_allowlist", A)
    assert proposed == {"read_post_allowlist": [NEW_POST]}
    assert pg.execute("select progress->>'stage' from jobs where id=%s", (jid,)).fetchone()[0] == "approval_received"

    t, out = _decide_when_asked(pg2, jid, "reject")
    assert approve([POST]) is False
    t.join()
    assert out["status"] == "rejected"
    assert approve.approved == {NEW_POST["url_regex"]}
    assert approve([]) is True  # nothing to ask


def test_approver_timeout_expires_row(pg, pg_listen):
    jid = _running_job(pg)
    approve = recipe_admin.make_approver(pg_listen, pg, jid, "zepto", "orders", timeout_s=0.3)
    assert approve([NEW_POST]) is False
    assert approve.approved == set()
    aid = pg.execute("select progress->>'approval_id' from jobs where id=%s", (jid,)).fetchone()[0]
    assert _approval(pg, aid)[0] == "expired"
    with pytest.raises(recipe_admin.NotPending):
        recipe_admin.decide(pg, aid, "approve", A)


def test_deny_new_posts():
    assert recipe_admin.deny_new_posts([NEW_POST]) is False


def test_decide_ended_job_expires(pg):
    jid = _running_job(pg)
    jobs.finish(pg, jid, error="boom")
    aid = pg.execute(  # as an Approver files it
        "insert into recipe_approvals (site, kind, reason, proposed, job_id, live_request) "
        "values ('zepto', 'orders', 'post_allowlist', %s, %s, true) returning id::text",
        (Jsonb({"read_post_allowlist": [NEW_POST]}), jid)).fetchone()[0]
    with pytest.raises(recipe_admin.JobEnded):
        recipe_admin.decide(pg, aid, "approve", A)
    assert _approval(pg, aid)[0] == "expired"
    with pytest.raises(recipe_admin.NotPending):
        recipe_admin.decide(pg, aid, "approve", A)


def test_decide_pending_recipe_publishes_and_unpauses(pg):
    store.put_recipe(pg, "zepto", BASE)
    recipe_admin.mark_drift(pg, "zepto", "orders", "no orders found")
    recipe_admin.pause_site(pg, "zepto", "orders", "re-learn needs approval")
    learned = BASE | {"read_post_allowlist": [POST, NEW_POST]}
    jid = _running_job(pg)
    res = recipe_admin.apply_learned(pg, "zepto", "orders", learned, set(), jid, "drift_relearn")
    aid = res["approval_id"]
    assert _approval(pg, aid)[1] == "post_allowlist"  # a whole recipe waiting, not a live request
    jobs.finish(pg, jid)  # the learn job is over long before the admin looks
    assert recipe_admin.decide(pg, aid, "approve", A) == "approved"
    assert store.get_recipe(pg, "zepto") == learned
    assert _recipe_row(pg) == (2, True, False, None)
    status, _, decided_by, _, _ = _approval(pg, aid)
    assert (status, str(decided_by)) == ("approved", A)
    with pytest.raises(recipe_admin.NotPending):
        recipe_admin.decide(pg, aid, "reject", A)

    other = recipe_admin.apply_learned(pg, "zepto", "cart", {"x": 1}, set(), None, "learned")["approval_id"]
    assert recipe_admin.decide(pg, other, "reject", B) == "rejected"
    assert store.get_recipe(pg, "zepto", kind="cart") is None
    with pytest.raises(ValueError):
        recipe_admin.decide(pg, other, "maybe", B)


def test_live_request_is_marked_explicitly(pg, pg_listen):
    jid = _running_job(pg)
    approve = recipe_admin.make_approver(pg_listen, pg, jid, "zepto", "orders", timeout_s=0.2)
    approve([NEW_POST])
    held = recipe_admin.apply_learned(pg, "blinkit", "orders", {"read_post_allowlist": [POST]}, set(), jid,
                                      "learned")
    rows = dict(pg.execute("select site, live_request from recipe_approvals").fetchall())
    assert rows == {"zepto": True, "blinkit": False}
    # A held-back first recipe can have exactly the live-request shape (only read_post_allowlist, no current,
    # a running job): it is still a recipe, so approving publishes it instead of notifying the job.
    assert recipe_admin.decide(pg, held["approval_id"], "approve", A) == "approved"
    assert store.get_recipe(pg, "blinkit") == {"read_post_allowlist": [POST]}


def test_decide_refuses_when_recipe_version_changed(pg):
    store.put_recipe(pg, "zepto", BASE)
    learned = BASE | {"read_post_allowlist": [POST, NEW_POST]}
    held = recipe_admin.apply_learned(pg, "zepto", "orders", learned, set(), None, "learned")["approval_id"]
    assert pg.execute("select base_version from recipe_approvals where id=%s", (held,)).fetchone() == (1,)
    newer = BASE | {"orders_page_url": "https://www.zepto.com/account/orders"}
    store.put_recipe(pg, "zepto", newer)  # published by another learn after this one was held back
    with pytest.raises(recipe_admin.RecipeChanged) as e:
        recipe_admin.decide(pg, held, "approve", A)
    assert (e.value.base_version, e.value.current_version) == (1, 2)
    assert store.get_recipe(pg, "zepto") == newer and _recipe_row(pg)[0] == 2
    assert _approval(pg, held)[0] == "expired"  # out of the pending queue; a re-learn proposes it again

    # a first recipe held back while another one got published: also stale
    first = recipe_admin.apply_learned(pg, "blinkit", "orders", BASE, set(), None, "learned")["approval_id"]
    store.put_recipe(pg, "blinkit", {"x": 1})
    with pytest.raises(recipe_admin.RecipeChanged):
        recipe_admin.decide(pg, first, "approve", A)

    # rejecting a stale proposal is always allowed
    other = recipe_admin.apply_learned(pg, "zepto", "orders", learned, set(), None, "learned")["approval_id"]
    store.put_recipe(pg, "zepto", BASE)
    assert recipe_admin.decide(pg, other, "reject", A) == "rejected"


# ---------------------------------------------------------------- site health

def test_mark_drift_transitions_once(pg):
    assert recipe_admin.mark_drift(pg, "zepto", "orders", "no recipe yet") is False
    store.put_recipe(pg, "zepto", BASE)
    assert recipe_admin.mark_drift(pg, "zepto", "orders", "endpoint returned 404") is True
    assert recipe_admin.mark_drift(pg, "zepto", "orders", "again") is False
    assert _recipe_row(pg) == (1, False, False, "endpoint returned 404")
    assert recipe_admin.is_paused(pg, "zepto") is False


def test_pause_and_resume_site(pg):
    assert recipe_admin.is_paused(pg, "zepto") is False  # no recipe row
    store.put_recipe(pg, "zepto", BASE)
    recipe_admin.mark_drift(pg, "zepto", "orders", "drift")
    recipe_admin.pause_site(pg, "zepto", "orders", "no admin connected")
    assert recipe_admin.is_paused(pg, "zepto") is True
    assert recipe_admin.is_paused(pg, "zepto", kind="cart") is False
    assert _recipe_row(pg) == (1, False, True, "no admin connected")
    recipe_admin.resume_site(pg, "zepto", "orders")
    assert _recipe_row(pg) == (1, True, False, None)


def test_put_recipe_clears_pause(pg):
    store.put_recipe(pg, "zepto", BASE)
    recipe_admin.mark_drift(pg, "zepto", "orders", "drift")
    recipe_admin.pause_site(pg, "zepto", "orders", "paused")
    assert store.put_recipe(pg, "zepto", BASE) == 2
    assert _recipe_row(pg) == (2, True, False, None)

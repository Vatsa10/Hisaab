"""Shared login recipes: only an admin's own login proposes one, the admin approves it, drift marks it stale."""
import json
from pathlib import Path

from helpers import A, B, add_user

from server import login_recipes, recipe_admin, store

SCRIPT = json.loads((Path(__file__).parent.parent / "fixtures" / "login_replay" / "recipe.json")
                    .read_text(encoding="utf-8"))["steps"]


def _admin(conn, user_id=A, email="admin@example.com"):
    add_user(conn, user_id, email)
    conn.execute("insert into admins(email) values (%s)", (email,))


def _approvals(conn):
    return conn.execute("SELECT reason, status, proposed FROM recipe_approvals WHERE kind='login' "
                        "ORDER BY created_at").fetchall()


def test_a_normal_users_login_never_proposes(pg):
    add_user(pg, B, "user@example.com")
    assert "on_login_script" not in login_recipes.login_kwargs(pg, B, "zomato", None)
    assert login_recipes.propose(pg, B, "zomato", SCRIPT, False, None) is None
    assert _approvals(pg) == []


def test_an_admin_login_is_held_then_approval_publishes_it(pg):
    _admin(pg)
    hook = login_recipes.login_kwargs(pg, A, "zomato", None)
    assert hook["login_recipe"] is None
    hook["on_login_script"](SCRIPT, False)
    [(reason, status, proposed)] = _approvals(pg)
    assert (reason, status) == ("login_learned", "pending")
    assert proposed["summary"][1] == "Type the user's phone number into the box 'phone'"
    assert store.get_recipe(pg, "zomato", "login") is None  # nothing is live before the admin decides
    approval_id = pg.execute("SELECT id FROM recipe_approvals WHERE kind='login'").fetchone()[0]
    assert recipe_admin.decide(pg, approval_id, "approve", A) == "approved"
    assert login_recipes.load(pg, "zomato")["steps"] == SCRIPT
    assert login_recipes.propose(pg, A, "zomato", SCRIPT, False, None) is None  # same as the live one


def test_the_same_pending_script_is_filed_once(pg):
    _admin(pg)
    assert login_recipes.propose(pg, A, "zomato", SCRIPT, False, None)
    assert login_recipes.propose(pg, A, "zomato", SCRIPT, False, None) is None


def test_invalid_scripts_are_not_proposed(pg):
    _admin(pg)
    assert login_recipes.propose(pg, A, "zomato", [SCRIPT[0] | {"name": "9876543210"}, *SCRIPT[1:]], False,
                                 None) is None
    assert _approvals(pg) == []


def test_a_drifted_replay_marks_the_old_script_stale_and_proposes_a_replacement(pg):
    _admin(pg)
    store.put_recipe(pg, "zomato", {"steps": SCRIPT, "summary": []}, kind="login")
    new = [*SCRIPT[:2], SCRIPT[2] | {"name": "get code"}]
    assert login_recipes.propose(pg, A, "zomato", new, True, None)
    assert login_recipes.load(pg, "zomato") is None  # stale: no user replays it any more
    [(reason, status, proposed)] = _approvals(pg)
    assert (reason, status) == ("login_drift", "pending") and proposed["steps"] == new

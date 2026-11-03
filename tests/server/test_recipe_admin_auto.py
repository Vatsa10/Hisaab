"""A learn the admin started needs no second approval: its POSTs go through (recorded as auto_applied) and a valid
cart recipe is published. Value-level holds (account-specific values) still apply."""
from helpers import B, add_user

from server import recipe_admin


def test_an_auto_approver_says_yes_without_waiting_and_records_it(pg):
    add_user(pg, B, "admin@example.com")
    approve = recipe_admin.make_approver(None, pg, None, "swiggy", "restaurants", 1, auto=True)
    entry = {"url_regex": r"^https://www\.swiggy\.com/api/search$", "why": "restaurant search (read-only)"}
    assert approve([entry]) is True
    assert approve.approved == {entry["url_regex"]}
    row = pg.execute("select status, live_request, reason from recipe_approvals").fetchone()
    assert row == ("auto_applied", False, "post_allowlist")


def test_an_ordinary_approver_still_asks(pg):
    approve = recipe_admin.make_approver(None, pg, None, "swiggy", "restaurants", 1)
    assert approve.auto is False


def test_a_value_tied_to_one_account_is_held_even_for_the_admin(pg):
    recipe = {"kind": "orders", "site": "swiggy", "endpoint": "https://www.swiggy.com/u/user/987654321/orders",
              "method": "GET", "query": {}, "headers": {}, "body": None, "field_map": {}}
    out = recipe_admin.apply_learned(pg, "swiggy", "orders", recipe, set(), None, reason="learned", trusted=True)
    assert out["status"] == "pending"

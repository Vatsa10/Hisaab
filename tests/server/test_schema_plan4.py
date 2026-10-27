"""Migration 0007: usage, admin tables, notifications, budget state and the invite-only sign-up hook."""
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg
import pytest
from helpers import A, B, add_user
from psycopg.types.json import Jsonb

ADMIN_TABLES = ("invites", "waitlist", "admins", "recipe_approvals", "notifications", "push_subscriptions",
                "budget_state")
NEW_TABLES = ("usage",) + ADMIN_TABLES


def hook(pg, email):
    return pg.execute("select before_user_created(%s)", (Jsonb({"user": {"email": email}}),)).fetchone()[0]


def ist_month():
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m")


def pause_budget(pg):
    pg.execute("insert into budget_state(month, level, spent_inr, budget_inr) values (%s, 'paused', 5000, 5000)",
               (ist_month(),))


def test_hook_rejects_and_waitlists_unknown_email(pg):
    assert hook(pg, "New@X.com")["error"]["message"] == "invite_only"
    assert hook(pg, " new@x.com ")["error"]["http_code"] == 403
    assert pg.execute("select email from waitlist").fetchall() == [("new@x.com",)]


def test_hook_rejects_missing_email_without_waitlisting(pg):
    assert hook(pg, None)["error"]["message"] == "invite_only"
    assert pg.execute("select count(*) from waitlist").fetchone() == (0,)


def test_hook_allows_invited_email_and_marks_accepted(pg):
    pg.execute("insert into invites(email) values ('friend@x.com')")
    assert hook(pg, "Friend@X.com") == {}
    assert pg.execute("select accepted_at is not null from invites").fetchone() == (True,)
    assert pg.execute("select count(*) from waitlist").fetchone() == (0,)


def test_hook_keeps_first_accepted_at(pg):
    pg.execute("insert into invites(email, accepted_at) values ('friend@x.com', '2026-01-01T00:00:00Z')")
    assert hook(pg, "friend@x.com") == {}
    assert pg.execute("select accepted_at = '2026-01-01T00:00:00Z' from invites").fetchone() == (True,)


def test_hook_blocks_invited_when_budget_paused(pg):
    pg.execute("insert into invites(email) values ('friend@x.com')")
    pause_budget(pg)
    out = hook(pg, "friend@x.com")
    assert out == {"error": {"http_code": 403, "message": "signups_paused"}}
    assert pg.execute("select accepted_at from invites").fetchone() == (None,)


def test_hook_ignores_paused_level_of_another_month(pg):
    pg.execute("insert into invites(email) values ('friend@x.com')")
    pg.execute("insert into budget_state(month, level, spent_inr, budget_inr) values ('2000-01', 'paused', 1, 1)")
    assert hook(pg, "friend@x.com") == {}


def test_hook_allows_admin_even_when_paused(pg):
    pg.execute("insert into admins(email) values ('boss@x.com')")
    pause_budget(pg)
    assert hook(pg, "Boss@X.com") == {}
    assert pg.execute("select count(*) from waitlist").fetchone() == (0,)


def test_hook_not_executable_by_users(pg, as_user):
    with as_user(pg, A), pytest.raises(psycopg.errors.InsufficientPrivilege):
        hook(pg, "new@x.com")


def test_hook_not_executable_by_anon(pg):
    pg.execute("begin")
    try:
        pg.execute("set local role anon")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            hook(pg, "new@x.com")
    finally:
        pg.execute("rollback")


def test_hook_runs_as_auth_admin(pg):
    """GoTrue calls the hook as supabase_auth_admin; security definer lets it write the waitlist."""
    pg.execute("begin")
    try:
        pg.execute("set local role supabase_auth_admin")
        assert hook(pg, "new@x.com")["error"]["message"] == "invite_only"
        pg.execute("commit")
    except Exception:
        pg.execute("rollback")
        raise
    assert pg.execute("select email from waitlist").fetchall() == [("new@x.com",)]


def test_hook_is_security_definer_with_search_path(pg):
    row = pg.execute("select prosecdef, proconfig from pg_proc where proname = 'before_user_created'").fetchone()
    assert row[0] is True
    assert "search_path=public, pg_temp" in row[1]


def test_usage_rls_own_rows(pg, as_user):
    pg.execute("insert into usage(user_id, day, proxy_mb) values (%s, '2026-10-27', 1.5), (%s, '2026-10-27', 9)",
               (A, B))
    with as_user(pg, A):
        assert pg.execute("select user_id::text, proxy_mb from usage").fetchall() == [(A, 1.5)]
    with as_user(pg, A), pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg.execute("insert into usage(user_id, day) values (%s, '2026-10-28')", (A,))


def test_usage_counters_default_to_zero(pg):
    pg.execute("insert into usage(user_id, day) values (%s, '2026-10-27')", (A,))
    assert pg.execute("select proxy_mb, ai_tokens, worker_seconds, syncs, cart_checks from usage").fetchone() == \
        (0, 0, 0, 0, 0)
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg.execute("insert into usage(user_id, day) values (%s, '2026-10-27')", (A,))


@pytest.mark.parametrize("table", ADMIN_TABLES)
def test_admin_tables_hidden_from_users(pg, as_user, table):
    with as_user(pg, A), pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg.execute(f"select 1 from {table}")


@pytest.mark.parametrize("table", NEW_TABLES)
def test_new_tables_hidden_from_anon(pg, table):
    pg.execute("begin")
    try:
        pg.execute("set local role anon")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pg.execute(f"select 1 from {table}")
    finally:
        pg.execute("rollback")


@pytest.mark.parametrize("table", NEW_TABLES)
def test_new_tables_have_rls(pg, table):
    assert pg.execute("select relrowsecurity from pg_class where relname = %s and relnamespace = 'public'::regnamespace",
                      (table,)).fetchone() == (True,)


@pytest.mark.parametrize("table", ("admins", "invites", "waitlist"))
def test_emails_must_be_lowercase(pg, table):
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(f"insert into {table}(email) values ('Mixed@X.com')")


def test_notifications_need_exactly_one_recipient(pg):
    cols = "kind, title, body, dedupe_key"
    pg.execute(f"insert into notifications(user_id, {cols}) values (%s, 'k', 't', 'b', 'd1')", (A,))
    pg.execute(f"insert into notifications(email, {cols}) values ('a@x.com', 'k', 't', 'b', 'd2')")
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(f"insert into notifications(user_id, email, {cols}) values (%s, 'a@x.com', 'k', 't', 'b', 'd3')", (A,))
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute(f"insert into notifications({cols}) values ('k', 't', 'b', 'd4')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg.execute(f"insert into notifications(user_id, {cols}) values (%s, 'k', 't', 'b', 'd1')", (A,))
    assert pg.execute("select url, status, attempts from notifications where dedupe_key = 'd1'").fetchone() == \
        ("/", "pending", 0)


def test_recipe_approvals_defaults_and_checks(pg):
    pg.execute("insert into recipe_approvals(site, kind, reason, proposed) values ('zepto', 'orders', 'learned', '{}')")
    assert pg.execute("select status, diff from recipe_approvals").fetchone() == ("pending", [])
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute("insert into recipe_approvals(site, kind, reason, proposed) values ('zepto', 'orders', 'whim', '{}')")


def test_budget_state_month_format(pg):
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute("insert into budget_state(month, level, spent_inr, budget_inr) values ('2026-1', 'ok', 0, 5000)")
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute("insert into budget_state(month, level, spent_inr, budget_inr) values ('2026-10', 'meh', 0, 5000)")


def test_recipes_have_paused_and_health_note(pg):
    pg.execute("insert into recipes(site, kind, recipe) values ('zepto', 'orders', '{}')")
    assert pg.execute("select paused, health_note from recipes").fetchone() == (False, None)


def test_jobs_user_site_created_index(pg):
    assert pg.execute("select indexdef from pg_indexes where indexname = 'jobs_user_site_created'").fetchone()[0] \
        .endswith("(user_id, site, created_at)")


def test_add_user_helper(pg):
    add_user(pg, A, "a@x.com")
    assert pg.execute("select email from auth.users where id = %s", (A,)).fetchone() == ("a@x.com",)


def test_auth_users_truncated_between_tests(pg):
    assert pg.execute("select count(*) from auth.users").fetchone() == (0,)
    add_user(pg, A, "A@X.com")
    assert pg.execute("select email from auth.users").fetchone() == ("A@X.com",)

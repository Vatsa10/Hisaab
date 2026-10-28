"""Migration 0010: hardening."""
import psycopg
import pytest
from helpers import A, add_user


def test_jobs_have_attempts_and_run_after(pg):
    add_user(pg, A, "a@example.com")
    jid = pg.execute("insert into jobs(user_id, type, site, profile) values (%s,'sync','zepto','p') returning id", (A,)).fetchone()[0]
    assert pg.execute("select attempts, run_after from jobs where id=%s", (jid,)).fetchone() == (0, None)


def test_jobs_type_finished_index(pg):
    assert pg.execute("select indexdef from pg_indexes where indexname='jobs_type_finished'").fetchone()[0].endswith(
        "(type, site, finished_at)")


def test_connections_needs_login_at(pg):
    add_user(pg, A, "a@example.com")
    pg.execute("insert into connections(user_id, site) values (%s,'zepto')", (A,))
    assert pg.execute("select needs_login_at from connections").fetchone() == (None,)


@pytest.mark.parametrize("table", ["site_backoff", "usage_erased"])
def test_new_tables_hidden_from_users(pg, as_user, table):
    add_user(pg, A, "a@example.com")
    assert pg.execute("select relrowsecurity from pg_class where relname=%s", (table,)).fetchone() == (True,)
    with as_user(pg, A):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pg.execute(f"select * from {table}")


def test_usage_erased_defaults_to_zero(pg):
    pg.execute("insert into usage_erased(day) values ('2026-10-01')")
    assert pg.execute("select proxy_mb, ai_tokens, worker_seconds, syncs, cart_checks from usage_erased").fetchone() \
        == (0, 0, 0, 0, 0)


def test_usage_erased_day_is_unique(pg):
    pg.execute("insert into usage_erased(day) values ('2026-10-01')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg.execute("insert into usage_erased(day) values ('2026-10-01')")


def test_pause_toggle_stamps_paused_changed_at(pg):
    pg.execute("insert into recipes(site, kind, recipe, version) values ('zepto','orders','{}',1)")
    assert pg.execute("select paused_changed_at from recipes").fetchone() == (None,)
    pg.execute("update recipes set paused = true where site='zepto'")
    assert pg.execute("select paused_changed_at is not null from recipes").fetchone() == (True,)


def test_unrelated_recipe_update_keeps_stamp(pg):
    pg.execute("insert into recipes(site, kind, recipe, version) values ('zepto','orders','{}',1)")
    pg.execute("update recipes set paused = true where site='zepto'")
    before = pg.execute("select paused_changed_at from recipes").fetchone()[0]
    pg.execute("update recipes set health_note = 'x' where site='zepto'")
    assert pg.execute("select paused_changed_at from recipes").fetchone()[0] == before


def test_backoff_kind_checked(pg):
    pg.execute("insert into site_backoff(site, last_kind) values ('zepto','challenge')")
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute("insert into site_backoff(site, last_kind) values ('blinkit','x')")


def test_migration_is_idempotent(pg):
    from conftest import MIGRATIONS
    pg.execute((MIGRATIONS / "0010_hardening.sql").read_text(encoding="utf-8"))

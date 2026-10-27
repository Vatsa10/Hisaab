import psycopg
import pytest
from helpers import A, B


def test_user_sees_only_own_orders(pg, as_user):
    pg.execute("insert into orders(user_id, site, order_id, total_amount) values (%s,'zepto','1',100),(%s,'zepto','2',200)", (A, B))
    with as_user(pg, A):
        assert pg.execute("select order_id from orders").fetchall() == [("1",)]


def test_user_cannot_write_orders(pg, as_user):
    with as_user(pg, A), pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg.execute("insert into orders(user_id, site, order_id) values (%s,'zepto','9')", (A,))


def test_one_active_job_per_user_site_type(pg):
    pg.execute("insert into jobs(user_id, type, site, profile) values (%s,'connect','swiggy','swiggy')", (A,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg.execute("insert into jobs(user_id, type, site, profile) values (%s,'connect','swiggy','swiggy')", (A,))


def test_recipes_readable_by_any_user(pg, as_user):
    pg.execute("insert into recipes(site, kind, recipe, version) values ('zepto','orders','{}',1)")
    with as_user(pg, A):
        assert pg.execute("select count(*) from recipes").fetchone() == (1,)


def test_one_running_job_per_user_profile(pg):
    pg.execute("insert into jobs(user_id, type, site, profile, status) values (%s,'connect','zepto','zepto','running')", (A,))
    pg.execute("insert into jobs(user_id, type, site, profile, status) values (%s,'sync','zepto','zepto','queued')", (A,))
    pg.execute("insert into jobs(user_id, type, site, profile, status) values (%s,'sync','blinkit','blinkit','running')", (A,))
    with pytest.raises(psycopg.errors.UniqueViolation) as e:
        pg.execute("update jobs set status='running' where type='sync' and site='zepto'")
    assert e.value.diag.constraint_name == "one_running_job_per_profile"


def test_instamart_and_swiggy_share_a_running_slot(pg):
    pg.execute("insert into jobs(user_id, type, site, profile, status) values (%s,'sync','swiggy','swiggy','running')", (A,))
    with pytest.raises(psycopg.errors.UniqueViolation) as e:
        pg.execute("insert into jobs(user_id, type, site, profile, status) values (%s,'sync','instamart','swiggy','running')", (A,))
    assert e.value.diag.constraint_name == "one_running_job_per_profile"


def test_jobs_have_heartbeat_column(pg):
    assert pg.execute("select count(*) from information_schema.columns where table_name='jobs' "
                      "and column_name in ('heartbeat_at','profile')").fetchone() == (2,)


TABLES = ("profiles", "connections", "orders", "order_items", "item_categories", "recipes", "jobs", "consents")


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("stmt", ["truncate {t}", "delete from {t}", "update {t} set {c} = {c}"])
def test_authenticated_cannot_modify(pg, as_user, table, stmt):
    col = {"item_categories": "category", "recipes": "kind"}.get(table, "user_id")
    with as_user(pg, A), pytest.raises(psycopg.errors.InsufficientPrivilege):
        pg.execute(stmt.format(t=table, c=col))


@pytest.mark.parametrize("table", TABLES)
def test_anon_has_no_access(pg, table):
    pg.execute("begin")
    try:
        pg.execute("set local role anon")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pg.execute(f"select 1 from {table}")
    finally:
        pg.execute("rollback")

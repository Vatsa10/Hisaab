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
    pg.execute("insert into jobs(user_id, type, site) values (%s,'connect','swiggy')", (A,))
    with pytest.raises(psycopg.errors.UniqueViolation):
        pg.execute("insert into jobs(user_id, type, site) values (%s,'connect','swiggy')", (A,))


def test_recipes_readable_by_any_user(pg, as_user):
    pg.execute("insert into recipes(site, kind, recipe, version) values ('zepto','orders','{}',1)")
    with as_user(pg, A):
        assert pg.execute("select count(*) from recipes").fetchone() == (1,)


def test_one_running_job_per_user_site(pg):
    pg.execute("insert into jobs(user_id, type, site, status) values (%s,'connect','zepto','running')", (A,))
    pg.execute("insert into jobs(user_id, type, site, status) values (%s,'sync','zepto','queued')", (A,))
    pg.execute("insert into jobs(user_id, type, site, status) values (%s,'sync','blinkit','running')", (A,))
    with pytest.raises(psycopg.errors.UniqueViolation) as e:
        pg.execute("update jobs set status='running' where type='sync' and site='zepto'")
    assert e.value.diag.constraint_name == "one_running_job_per_profile"

"""Migration 0008: cart requests and results, fee rules, food caches, kind checks."""
import psycopg
import pytest
from helpers import A, B, add_user
from psycopg.types.json import Jsonb

from server import fees


def request(pg, user):
    return pg.execute(
        "insert into cart_requests(user_id, items, sites, area, lat, lon) values (%s,%s,%s,'Indiranagar',12.97,77.64) "
        "returning id", (user, Jsonb([{"name": "milk"}]), ["zepto"])).fetchone()[0]


def result(pg, rid, user, site="zepto", status="ok"):
    pg.execute("insert into cart_results(request_id, user_id, site, status) values (%s,%s,%s,%s)",
               (rid, user, site, status))


def test_fee_rule_seeds(_applied):
    # read with a fresh connection before any truncate
    with psycopg.connect(_applied) as c:
        rows = dict(c.execute("select site, rules->>'platform' from fee_rules where rules->>'kind'='food'").fetchall())
        assert rows == {"swiggy": "20", "zomato": "10"}
        assert c.execute("select count(*) from fee_rules").fetchone()[0] == 5
        for site, rules, verified, source in c.execute("select site, rules, verified, source from fee_rules"):
            assert fees.validate(rules) == [], site
            assert verified is False and source == "seed"
            assert rules["surge"] == {"possible": True} and rules["source_url"] is None
            assert rules["note"] == "Seed values; verify against the app before launch."


def test_cart_tables_rls_own_rows(pg, as_user):
    add_user(pg, A, "a@example.com"), add_user(pg, B, "b@example.com")
    ra = request(pg, A)
    result(pg, ra, A)
    with as_user(pg, A):
        assert pg.execute("select count(*) from cart_requests").fetchone()[0] == 1
        assert pg.execute("select count(*) from cart_results").fetchone()[0] == 1
    with as_user(pg, B):
        assert pg.execute("select count(*) from cart_requests").fetchone()[0] == 0
        assert pg.execute("select count(*) from cart_results").fetchone()[0] == 0


def test_users_cannot_write_cart_tables(pg, as_user):
    add_user(pg, A, "a@example.com")
    ra = request(pg, A)
    result(pg, ra, A)
    stmts = [
        f"insert into cart_requests(user_id, items, sites, area, lat, lon) values ('{A}','[]','{{zepto}}','x',1,1)",
        "update cart_requests set area='y'", "delete from cart_requests",
        f"insert into cart_results(request_id, user_id, site, status) values ('{ra}','{A}','blinkit','ok')",
        "update cart_results set status='failed'", "delete from cart_results",
    ]
    for sql in stmts:
        with as_user(pg, A):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                pg.execute(sql)


@pytest.mark.parametrize("table", ["fee_rules", "restaurant_cache", "menu_cache"])
def test_fee_rules_and_caches_hidden_from_users(pg, as_user, table):
    with as_user(pg, A):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            pg.execute(f"select * from {table}")


def test_recipe_kinds_include_food(pg):
    pg.execute("insert into recipes(site, kind, recipe) values ('swiggy','menu','{}'), ('swiggy','restaurants','{}')")
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute("insert into recipes(site, kind, recipe) values ('swiggy','x','{}')")
    for kind in ("menu", "restaurants"):
        pg.execute("insert into recipe_approvals(site, kind, reason, proposed) values ('zomato',%s,'learned','{}')",
                   (kind,))
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute("insert into recipe_approvals(site, kind, reason, proposed) values ('zomato','x','learned','{}')")


def test_results_cascade_and_checks(pg):
    add_user(pg, A, "a@example.com")
    rid = request(pg, A)
    for site, status in (("zepto", "needs_login"), ("blinkit", "closed"), ("swiggy", "not_listed")):
        result(pg, rid, A, site, status)
    with pytest.raises(psycopg.errors.CheckViolation):
        result(pg, rid, A, "amazon")
    with pytest.raises(psycopg.errors.CheckViolation):
        result(pg, rid, A, "zomato", "bogus")
    with pytest.raises(psycopg.errors.UniqueViolation):
        result(pg, rid, A, "zepto")
    pg.execute("delete from cart_requests")
    assert pg.execute("select count(*) from cart_results").fetchone()[0] == 0


def test_request_defaults_and_checks(pg):
    add_user(pg, A, "a@example.com")
    rid = request(pg, A)
    assert pg.execute("select mode, status, choices from cart_requests where id=%s", (rid,)).fetchone() == (
        "grocery", "searching", {})
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute("insert into cart_requests(user_id, items, sites, area, lat, lon) "
                   "values (%s,'{}','{zepto}','x',1,1)", (A,))
    with pytest.raises(psycopg.errors.CheckViolation):
        pg.execute("insert into cart_requests(user_id, mode, items, sites, area, lat, lon) "
                   "values (%s,'bad','[]','{zepto}','x',1,1)", (A,))


def test_fee_rules_snapshot_restores(pg, fee_rules_snapshot):
    assert len(fee_rules_snapshot) == 5
    pg.execute("delete from fee_rules")

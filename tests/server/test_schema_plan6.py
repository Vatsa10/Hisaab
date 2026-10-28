"""Migration 0009: cart fills."""
import psycopg
import pytest
from helpers import A, B, add_user
from psycopg.types.json import Jsonb


def request(pg, user):
    return pg.execute(
        "insert into cart_requests(user_id, items, sites, area, lat, lon) values (%s,%s,%s,'Indiranagar',12.97,77.64) "
        "returning id", (user, Jsonb([{"name": "milk"}]), ["zepto"])).fetchone()[0]


def ins(pg, rid, user=A, site="zepto", status="queued", **kw):
    cols = {"request_id": rid, "user_id": user, "site": site, "status": status,
            "plan": Jsonb([]), "priced_location": Jsonb({"lat": 12.97, "lon": 77.64})}
    cols.update(kw)
    return pg.execute(
        f"insert into cart_fills({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))}) returning id",
        tuple(cols.values())).fetchone()[0]


def test_cart_fills_rls_own_rows(pg, as_user):
    add_user(pg, A, "a@example.com"), add_user(pg, B, "b@example.com")
    ins(pg, request(pg, A))
    with as_user(pg, A):
        assert pg.execute("select count(*) from cart_fills").fetchone()[0] == 1
    with as_user(pg, B):
        assert pg.execute("select count(*) from cart_fills").fetchone()[0] == 0


def test_users_cannot_write_cart_fills(pg, as_user):
    add_user(pg, A, "a@example.com")
    rid = request(pg, A)
    ins(pg, rid)
    stmts = [
        f"insert into cart_fills(request_id, user_id, site, plan, priced_location) "
        f"values ('{rid}','{A}','blinkit','[]','{{}}')",
        "update cart_fills set status='done'", "delete from cart_fills",
    ]
    for sql in stmts:
        with as_user(pg, A):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                pg.execute(sql)


def test_one_fill_per_check_and_site(pg):
    add_user(pg, A, "a@example.com")
    rid = request(pg, A)
    ins(pg, rid, status="done")
    ins(pg, rid, status="failed")  # failed does not hold the slot
    with pytest.raises(psycopg.errors.UniqueViolation):
        ins(pg, rid, status="queued")
    ins(pg, rid, site="blinkit", status="queued")  # another site is free


@pytest.mark.parametrize("held", ["queued", "filling", "done", "partial", "interrupted"])
def test_holding_statuses_block_a_second_fill(pg, held):
    add_user(pg, A, "a@example.com")
    rid = request(pg, A)
    ins(pg, rid, status=held)
    with pytest.raises(psycopg.errors.UniqueViolation):
        ins(pg, rid, status="queued")


def test_needs_confirmation_does_not_hold_the_slot(pg):
    add_user(pg, A, "a@example.com")
    rid = request(pg, A)
    ins(pg, rid, status="needs_confirmation")
    ins(pg, rid, status="queued")
    ins(pg, rid, status="needs_confirmation")


def test_fill_status_and_site_checked(pg):
    add_user(pg, A, "a@example.com")
    rid = request(pg, A)
    with pytest.raises(psycopg.errors.CheckViolation):
        ins(pg, rid, site="zomato")
    with pytest.raises(psycopg.errors.CheckViolation):
        ins(pg, rid, status="paid")
    with pytest.raises(psycopg.errors.CheckViolation):
        ins(pg, rid, plan=Jsonb({"a": 1}))


def test_fill_defaults(pg):
    add_user(pg, A, "a@example.com")
    fid = ins(pg, request(pg, A))
    row = pg.execute("select status, items, location_selected, counted, confirmed_from, created_at "
                     "from cart_fills where id=%s", (fid,)).fetchone()
    assert row[0] == "queued" and row[1] == [] and row[2] is False and row[3] is True
    assert row[4] is None and row[5] is not None


def test_confirmed_from_nulls_on_delete(pg):
    add_user(pg, A, "a@example.com")
    rid = request(pg, A)
    first = ins(pg, rid, status="needs_confirmation")
    second = ins(pg, rid, status="queued", confirmed_from=first)
    pg.execute("delete from cart_fills where id=%s", (first,))
    assert pg.execute("select confirmed_from from cart_fills where id=%s", (second,)).fetchone()[0] is None


def test_fills_cascade_with_request(pg):
    add_user(pg, A, "a@example.com")
    rid = request(pg, A)
    ins(pg, rid)
    pg.execute("delete from cart_requests where id=%s", (rid,))
    assert pg.execute("select count(*) from cart_fills").fetchone()[0] == 0


def test_usage_has_cart_fills(pg):
    add_user(pg, A, "a@example.com")
    pg.execute("insert into usage(user_id, day) values (%s, current_date)", (A,))
    assert pg.execute("select cart_fills from usage").fetchone()[0] == 0


def test_cart_fills_in_realtime_publication(_applied):
    with psycopg.connect(_applied) as c:
        if not c.execute("select 1 from pg_publication where pubname='supabase_realtime'").fetchone():
            pytest.skip("no supabase_realtime publication in the test database")
        assert c.execute("select 1 from pg_publication_tables where pubname='supabase_realtime' "
                         "and tablename='cart_fills'").fetchone()

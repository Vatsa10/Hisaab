"""Account deletion and app disconnect leave nothing behind (Plan 7 T11, R1-R4)."""
import threading
import time

import helpers
import pytest
from helpers import A, B

from server import budget, erase, jobs, vault
from server.config import Settings

EA, EB = "a7@e2e.test", "b7@e2e.test"


@pytest.fixture(autouse=True)
def _fee_rules(fee_rules_snapshot):
    """seed_everything touches fee_rules (not truncated per test): restore it afterwards."""


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(erase, "_sleep", lambda s: None)


def _count(pg, sql, *p):
    return pg.execute(sql, p).fetchone()[0]


def test_delete_account_leaves_nothing(pg, tmp_path):
    store = vault.LocalStore(tmp_path)
    helpers.seed_everything(pg, A, EA, store)
    helpers.seed_everything(pg, B, EB, store)
    assert erase.remaining(pg, store, A, EA) != []
    out = erase.delete_account(pg, store, A)
    assert out["rows"] > 0 and out["blobs"] == 1
    assert erase.remaining(pg, store, A, EA) == []
    assert erase.remaining(pg, store, B, EB) != []          # B untouched
    assert _count(pg, "select count(*) from orders where user_id = %s", B) == 2
    assert store.list_prefix(f"{B}/") != []


def test_delete_account_nulls_actor_columns_and_email_rows(pg, tmp_path):
    store = vault.LocalStore(tmp_path)
    helpers.seed_everything(pg, A, EA, store)
    helpers.seed_everything(pg, B, EB, store)
    erase.delete_account(pg, store, A)
    assert _count(pg, "select count(*) from invites where lower(email) = %s", EA) == 0
    assert _count(pg, "select count(*) from admins where email = %s", EA) == 0
    assert _count(pg, "select count(*) from waitlist where email = %s", EA) == 0
    assert _count(pg, "select count(*) from notifications where email = %s", EA) == 0
    assert _count(pg, "select count(*) from recipe_approvals where decided_by is not null") == 1   # B's
    assert _count(pg, "select count(*) from auth.users where id = %s", A) == 0


def test_user_tables_cover_every_user_id_table(pg):
    expected = {r[0] for r in pg.execute(
        "select c.table_name from information_schema.columns c join information_schema.tables t "
        "on t.table_schema = c.table_schema and t.table_name = c.table_name and t.table_type = 'BASE TABLE' "
        "where c.table_schema = 'public' and c.column_name = 'user_id'").fetchall()}
    tables = erase.user_tables(pg)
    assert set(tables) == expected and len(tables) == len(expected)
    assert {"cart_fills", "cart_requests", "cart_results", "restaurant_cache", "menu_cache", "push_subscriptions",
            "notifications", "usage"} <= expected
    assert tables.index("order_items") < tables.index("orders")
    assert tables.index("cart_fills") < tables.index("cart_requests")
    assert tables.index("cart_results") < tables.index("cart_requests")


def test_tables_added_later_are_erased_too(pg, tmp_path):
    """A table with a user_id column (like app_addresses, added by a later migration) is found without a code change."""
    pg.execute("create table zz_later (user_id uuid not null, note text)")
    try:
        store = vault.LocalStore(tmp_path)
        helpers.seed_everything(pg, A, EA, store)
        pg.execute("insert into zz_later values (%s, 'x')", (A,))
        assert "zz_later" in erase.user_tables(pg)
        erase.delete_account(pg, store, A)
        assert erase.remaining(pg, store, A, EA) == []
    finally:
        pg.execute("drop table zz_later")


def test_app_addresses_are_erased(pg, tmp_path):
    store = vault.LocalStore(tmp_path)
    helpers.seed_everything(pg, A, EA, store)
    helpers.seed_everything(pg, B, EB, store)
    pg.execute("insert into app_addresses(user_id, site, address_id, line) values (%s, 'swiggy', 'a2', '12 Some Road')",
               (A,))
    assert "app_addresses" in erase.user_tables(pg)
    erase.delete_account(pg, store, A)
    assert _count(pg, "select count(*) from app_addresses where user_id = %s", A) == 0
    assert _count(pg, "select count(*) from app_addresses where user_id = %s", B) == 1
    assert erase.remaining(pg, store, A, EA) == []


def test_deleted_usage_still_counts_toward_budget(pg, tmp_path):
    s = Settings()
    store = vault.LocalStore(tmp_path)
    helpers.seed_everything(pg, A, EA, store)
    month = pg.execute("select to_char(current_date, 'YYYY-MM')").fetchone()[0]
    before = budget.month_cost(pg, s, month)
    assert before > 0
    erase.delete_account(pg, store, A)
    assert _count(pg, "select count(*) from usage") == 0
    assert budget.month_cost(pg, s, month) == pytest.approx(before)
    row = pg.execute("select proxy_mb, ai_tokens, syncs, cart_checks, cart_fills from usage_erased").fetchone()
    assert row == (5, 100, 1, 1, 1)
    # the folded row carries no id: the table has no user column
    assert "user_id" not in [r[0] for r in pg.execute(
        "select column_name from information_schema.columns where table_name = 'usage_erased'").fetchall()]


def test_delete_account_waits_for_in_flight_write(pg, pg2, tmp_path):
    store = vault.LocalStore(tmp_path)
    helpers.seed_everything(pg, A, EA, store)
    job = pg.execute("insert into jobs(user_id, type, site, profile, status, worker_id) "
                     "values (%s, 'sync', 'zepto', 'zepto', 'running', 'w1') returning id", (A,)).fetchone()[0]
    done = threading.Event()
    result = {}

    def delete():
        result["out"] = erase.delete_account(pg, store, A)
        done.set()

    with jobs.owned(pg2, str(job), "w1"):
        t = threading.Thread(target=delete)
        t.start()
        time.sleep(0.6)
        assert not done.is_set()                       # blocked on the job row
        pg2.execute("insert into orders(user_id, site, order_id) values (%s, 'zepto', 'late')", (A,))
    t.join(10)
    assert done.is_set()
    assert _count(pg, "select count(*) from orders where order_id = 'late'") == 0
    assert erase.remaining(pg, store, A, EA) == []


class _FailingStore:
    def __init__(self, fails):
        self.fails, self.calls = fails, 0

    def delete_prefix(self, prefix):
        self.calls += 1
        if self.calls <= self.fails:
            raise OSError("storage down")
        return 1


def test_delete_account_blob_failure_is_retryable(pg, tmp_path):
    helpers.seed_everything(pg, A, EA, vault.LocalStore(tmp_path))
    bad = _FailingStore(fails=3)
    with pytest.raises(erase.BlobDeleteFailed):
        erase.delete_account(pg, bad, A)
    assert bad.calls == 3
    assert _count(pg, "select count(*) from orders where user_id = %s", A) == 0     # database side is done
    out = erase.delete_account(pg, bad, A)                                          # 2nd call: only blobs
    assert out == {"rows": 0, "blobs": 1}


def test_delete_account_blob_retry_recovers_within_one_call(pg, tmp_path):
    helpers.seed_everything(pg, A, EA, vault.LocalStore(tmp_path))
    flaky = _FailingStore(fails=2)
    assert erase.delete_account(pg, flaky, A)["blobs"] == 1 and flaky.calls == 3


def _extra(pg, user):
    pg.execute("insert into jobs(user_id, type, site, profile, status) values (%s, 'sync', 'instamart', 'swiggy', 'done')",
               (user,))
    pg.execute("insert into consents(user_id, site, text_version) values (%s, 'instamart', 'v1')", (user,))


def test_disconnect_swiggy_wipes_instamart_too(pg, tmp_path):
    store = vault.LocalStore(tmp_path)
    for u, e in ((A, EA), (B, EB)):
        helpers.seed_everything(pg, u, e, store)
        _extra(pg, u)
        store.put(vault.blob_key(u, "zomato"), b"z")
    out = erase.disconnect_site(pg, store, A, "swiggy")
    assert out == {"orders_deleted": 1, "blobs": 1}
    for table in ("orders", "order_items", "connections", "consents", "jobs"):
        assert _count(pg, f"select count(*) from {table} where user_id = %s and site in ('swiggy', 'instamart')", A) == 0
    assert _count(pg, "select count(*) from orders where user_id = %s and site = 'zomato'", A) == 1
    assert _count(pg, "select count(*) from connections where user_id = %s and site = 'zomato'", A) == 1
    assert _count(pg, "select count(*) from consents where user_id = %s and site = 'zomato'", A) == 1
    assert store.get(vault.blob_key(A, "swiggy")) is None and store.get(vault.blob_key(A, "zomato")) is not None
    assert _count(pg, "select count(*) from orders where user_id = %s", B) == 2
    assert _count(pg, "select count(*) from jobs where user_id = %s", B) == 3
    assert store.get(vault.blob_key(B, "swiggy")) is not None
    # instamart names the same group; running it again is a no-op
    assert erase.disconnect_site(pg, store, A, "instamart") == {"orders_deleted": 0, "blobs": 0}


def test_disconnect_keeps_cart_results_and_usage(pg, tmp_path):
    store = vault.LocalStore(tmp_path)
    helpers.seed_everything(pg, A, EA, store)
    erase.disconnect_site(pg, store, A, "swiggy")
    for table in ("cart_requests", "cart_results", "cart_fills", "usage", "profiles", "push_subscriptions",
                  "restaurant_cache", "menu_cache"):
        assert _count(pg, f"select count(*) from {table} where user_id = %s", A) == 1, table
    assert _count(pg, "select count(*) from auth.users where id = %s", A) == 1


def test_disconnect_waits_for_in_flight_job_write(pg, pg2, tmp_path):
    store = vault.LocalStore(tmp_path)
    helpers.seed_everything(pg, A, EA, store)
    job = pg.execute("insert into jobs(user_id, type, site, profile, status, worker_id) "
                     "values (%s, 'sync', 'swiggy', 'swiggy', 'running', 'w1') returning id", (A,)).fetchone()[0]
    t = threading.Thread(target=lambda: erase.disconnect_site(pg, store, A, "swiggy"))
    with jobs.owned(pg2, str(job), "w1"):
        t.start()
        time.sleep(0.5)
        assert t.is_alive()
        pg2.execute("insert into orders(user_id, site, order_id) values (%s, 'swiggy', 'late')", (A,))
    t.join(10)
    assert _count(pg, "select count(*) from orders where order_id = 'late'") == 0
    with pytest.raises(jobs.JobGone):                  # a later write finds no job row
        with jobs.owned(pg2, str(job), "w1"):
            pass


def test_enqueue_blocked_behind_deletion_inserts_nothing(pg, pg2, tmp_path):
    """A job enqueued while the account is being deleted must not outlive it (the user row is the gate)."""
    store = vault.LocalStore(tmp_path)
    helpers.seed_everything(pg, A, EA, store)
    out = {}

    def enqueue():
        try:
            out["id"] = jobs.enqueue(pg, A, "sync", "zepto")
        except jobs.NoSuchUser as e:
            out["err"] = e

    with pg2.transaction():
        erase.delete_account(pg2, store, A)          # not committed yet: holds the auth.users row lock
        t = threading.Thread(target=enqueue)
        t.start()
        time.sleep(0.6)
        assert t.is_alive()                            # waiting behind the deletion
    t.join(10)
    assert "id" not in out and isinstance(out.get("err"), jobs.NoSuchUser)
    assert _count(pg, "select count(*) from jobs where user_id = %s", A) == 0
    assert erase.remaining(pg, store, A, EA) == []


def test_enqueue_for_unknown_user_raises(pg):
    with pytest.raises(jobs.NoSuchUser):
        jobs.enqueue(pg, "00000000-0000-0000-0000-0000000000ff", "sync", "zepto")
    assert _count(pg, "select count(*) from jobs") == 0

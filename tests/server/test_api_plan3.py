"""Plan 3 API: question answers, delivery profile, Ask (scoped analyst SQL), CORS."""
import json
import logging
import os
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from helpers import A, B, order

import browser
from server import ask, store


def _running_connect(client, pg, token, site="zepto", stage=None):
    client.post("/consents", json={"site": site}, headers=token)
    jid = client.post(f"/connections/{site}/connect", json={"phone": "9876543210"}, headers=token).json()["job_id"]
    pg.execute("update jobs set status='running', progress=%s::jsonb where id=%s",
               (json.dumps({"stage": stage} if stage else {}), jid))
    return jid


# --- POST /jobs/{id}/answer ---------------------------------------------------------------------------------

def test_answer_reaches_listener_for_question_stage(client, token_a, pg, pg2):
    jid = _running_connect(client, pg, token_a, stage="question_needed")
    channel = "otp_" + jid.replace("-", "")
    pg2.execute("listen " + channel)
    assert client.post(f"/jobs/{jid}/answer", json={"answer": "Bengaluru"}, headers=token_a).status_code == 204
    got = [(n.channel, n.payload) for n in pg2.notifies(timeout=3, stop_after=1)]
    assert got == [(channel, "Bengaluru")]
    row = pg.execute("select payload::text, progress::text, coalesce(error,'') from jobs where id=%s", (jid,)).fetchone()
    assert not any("Bengaluru" in col for col in row)


def test_answer_too_long_is_422(client, token_a, pg):
    jid = _running_connect(client, pg, token_a, stage="question_needed")
    assert client.post(f"/jobs/{jid}/answer", json={"answer": "x" * 501}, headers=token_a).status_code == 422
    assert client.post(f"/jobs/{jid}/answer", json={"answer": ""}, headers=token_a).status_code == 422
    assert client.post(f"/jobs/{jid}/answer", json={"answer": "x" * 500}, headers=token_a).status_code == 204


def test_answer_checks_owner_type_status_and_stage(client, token_a, token_b, pg):
    jid = _running_connect(client, pg, token_a, stage="question_needed")
    assert client.post(f"/jobs/{jid}/answer", json={"answer": "x"}, headers=token_b).status_code == 404  # not B's
    pg.execute("update jobs set progress='{\"stage\": \"otp_needed\"}'::jsonb where id=%s", (jid,))
    assert client.post(f"/jobs/{jid}/answer", json={"answer": "x"}, headers=token_a).status_code == 404  # no question
    pg.execute("update jobs set progress='{\"stage\": \"question_needed\"}'::jsonb, status='queued' where id=%s", (jid,))
    assert client.post(f"/jobs/{jid}/answer", json={"answer": "x"}, headers=token_a).status_code == 404  # not running
    pg.execute("update jobs set status='running', type='sync' where id=%s", (jid,))
    assert client.post(f"/jobs/{jid}/answer", json={"answer": "x"}, headers=token_a).status_code == 404  # not connect
    assert client.post("/jobs/not-a-uuid/answer", json={"answer": "x"}, headers=token_a).status_code == 404
    assert client.post(f"/jobs/{jid}/answer", json={"answer": "x"}).status_code == 401


def test_answer_body_is_never_logged(client, token_a, pg, caplog):
    jid = _running_connect(client, pg, token_a, stage="question_needed")
    caplog.set_level("DEBUG")
    client.post(f"/jobs/{jid}/answer", json={"answer": "secret-city-answer"}, headers=token_a)
    assert "secret-city-answer" not in caplog.text


# --- PUT /profile -------------------------------------------------------------------------------------------

def test_profile_geocodes_and_upserts(client, token_a, pg, monkeypatch):
    seen = []

    def fake_resolve(text):
        seen.append(text)
        return {"query": text.strip(), "lat": 12.97, "lon": 77.59}

    monkeypatch.setattr(browser, "resolve_location", fake_resolve)
    r = client.put("/profile", json={"address": "  HSR Layout, Bengaluru, Karnataka ", "display_name": "Asha"},
                   headers=token_a)
    assert r.status_code == 200
    assert r.json() == {"address": "HSR Layout, Bengaluru, Karnataka", "lat": 12.97, "lon": 77.59}
    assert seen == ["HSR Layout, Bengaluru, Karnataka"]
    assert pg.execute("select user_id::text, display_name, address, lat, lon from profiles").fetchall() == [
        (A, "Asha", "HSR Layout, Bengaluru, Karnataka", 12.97, 77.59)]

    # Second call updates in place; a missing display_name keeps the stored one.
    monkeypatch.setattr(browser, "resolve_location", lambda text: {"query": text, "lat": 19.07, "lon": 72.87})
    r = client.put("/profile", json={"address": "Bandra West, Mumbai, Maharashtra"}, headers=token_a)
    assert r.status_code == 200 and r.json() == {"address": "Bandra West, Mumbai, Maharashtra", "lat": 19.07,
                                                 "lon": 72.87}
    assert pg.execute("select user_id::text, display_name, address, lat, lon from profiles").fetchall() == [
        (A, "Asha", "Bandra West, Mumbai, Maharashtra", 19.07, 72.87)]


def test_profile_not_found_is_422_and_stores_nothing(client, token_a, pg, monkeypatch):
    def nothing(text):
        raise ValueError(f"could not find {text!r}")

    monkeypatch.setattr(browser, "resolve_location", nothing)
    assert client.put("/profile", json={"address": "Nowhere at all"}, headers=token_a).status_code == 422
    assert pg.execute("select count(*) from profiles").fetchone() == (0,)


def test_profile_validates_address_length(client, token_a, monkeypatch):
    monkeypatch.setattr(browser, "resolve_location", lambda text: pytest.fail("must not geocode"))
    assert client.put("/profile", json={"address": "abcd"}, headers=token_a).status_code == 422
    assert client.put("/profile", json={"address": "x" * 201}, headers=token_a).status_code == 422
    assert client.put("/profile", json={"address": "HSR Layout"}).status_code == 401


# --- scoped_sql ---------------------------------------------------------------------------------------------

ASK_PASSWORD = "ask-test-password-7f3a9c"


@pytest.fixture
def ask_url(pg):
    """The ask_reader login (migration 0006 creates it NOLOGIN; production sets the password out of band)."""
    pg.execute(f"alter role ask_reader login password '{ASK_PASSWORD}'")
    return make_conninfo(os.environ["TEST_DATABASE_URL"], user="ask_reader", password=ASK_PASSWORD)


@pytest.fixture
def ask_conn(ask_url):
    with psycopg.connect(ask_url, autocommit=True) as c:
        yield c


@pytest.fixture(params=["superuser", "ask_reader"])
def any_conn(request, pg):
    """The escape tests run on a superuser session (the worst case) and on the real ask_reader login."""
    return pg if request.param == "superuser" else request.getfixturevalue("ask_conn")


def _two_users(pg):
    store.upsert_orders(pg, A, "zepto", [order("1", 100)])
    store.upsert_orders(pg, B, "zepto", [order("2", 999)])


def test_ask_sql_is_scoped_to_caller(pg):
    _two_users(pg)
    assert ask.scoped_sql(pg, A, "select sum(total_amount) from orders")["rows"] == [(100.0,)]
    assert ask.scoped_sql(pg, B, "select sum(total_amount) from orders;")["rows"] == [(999.0,)]
    assert ask.scoped_sql(pg, A, "select count(*) from orders where merchant like '%tor%'")["rows"] == [(1,)]


def test_ask_sql_is_scoped_on_the_ask_reader_login(pg, ask_conn):
    _two_users(pg)
    assert ask.scoped_sql(ask_conn, A, "select sum(total_amount) from orders")["rows"] == [(100.0,)]
    assert ask.scoped_sql(ask_conn, A, "select current_user")["rows"] == [("authenticated",)]
    assert ask_conn.execute("select session_user::text, current_user::text").fetchone() == ("ask_reader",) * 2


def test_ask_sql_is_read_only(pg):
    store.upsert_orders(pg, A, "zepto", [order("1", 100)])
    assert "error" in ask.scoped_sql(pg, A, "delete from orders")
    assert "error" in ask.scoped_sql(pg, A, "with x as (delete from orders returning 1) select * from x")
    assert "error" in ask.scoped_sql(pg, A, "select 1; delete from orders")
    assert "error" in ask.scoped_sql(pg, A, "select * from orders for update")
    assert "error" in ask.scoped_sql(pg, A, "select * from orders for share")
    assert "error" in ask.scoped_sql(pg, A, "select * into stolen from orders")
    assert pg.execute("select count(*) from orders").fetchone() == (1,)


def test_ask_scope_is_authenticated_read_only_with_the_callers_claims(pg):
    pg.execute("begin read only")
    try:
        ask._enter_scope(pg, A)
        user, read_only, timeout, sub, claims, path = pg.execute(
            "select current_user::text, current_setting('transaction_read_only'), "
            "current_setting('statement_timeout'), current_setting('request.jwt.claim.sub'), "
            "current_setting('request.jwt.claims'), current_setting('search_path')").fetchone()
    finally:
        pg.execute("rollback")
    assert (user, read_only, timeout, sub) == ("authenticated", "on", "5s", A)
    assert json.loads(claims) == {"sub": A, "role": "authenticated"}
    assert path == "pg_catalog, public"


def test_ask_sql_leaves_the_connection_clean(pg):
    assert ask.scoped_sql(pg, A, "select current_user")["rows"] == [("authenticated",)]
    assert pg.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    user, sub = pg.execute("select current_user::text, current_setting('request.jwt.claim.sub', true)").fetchone()
    assert user != "authenticated" and sub in (None, "")


ESCAPES = [
    # set_config anywhere: the reviewer's exploits hide it where EXPLAIN VERBOSE prints nothing (a multi-row
    # VALUES list, a LIMIT expression), switch the claim to B (or the role to postgres) and leak through an error.
    "select v.x, (select (sum(total_amount)::text||'LEAK')::int from orders where v.x='{B}') "
    "from (values ('a'),(set_config('request.jwt.claim.sub','{B}',true))) v(x)",
    "select v.x, (select (sum(total_amount)::text||'LEAK')::int from orders where v.x='postgres') "
    "from (values ('a'),(set_config('role','postgres',true))) v(x)",
    "select sum(total_amount) from orders limit (select length(set_config('request.jwt.claim.sub','{B}',true)))",
    "select sum(total_amount) from orders limit (select length(set_config('role','postgres',true)))",
    # Claim to B and back to A inside one statement, which a post-check alone cannot see.
    "select set_config('request.jwt.claim.sub','{B}',true), (select sum(total_amount) from orders), "
    "set_config('request.jwt.claim.sub','{A}',true)",
    "select set_config('request.jwt.claim.sub', '{B}', true), (select sum(total_amount) from orders)",
    "select pg_catalog.set_config('request.jwt.claim.sub', '{B}', false)",
    'select U&"set\\005Fconfig"(\'role\', \'postgres\', true)',
    """select x."a'b" || set_config('role', 'postgres', true) from (select 'v' as "a'b") x""",
    "select current_setting('request.jwt.claims')",
    "select * from orders where user_id::text = current_setting('request.jwt.claim.sub')",
    # Schema-qualified and non-allowlisted functions.
    "select auth.uid()",
    "select public.norm_name('x')",
    "select pg_catalog.norm_name('x')",
    "select query_to_xml('select * from orders', true, true, '')",
    "select * from ts_stat('select to_tsvector(merchant) from orders')",
    "select pg_sleep(0)",
    "select pg_notify('otp_x', 'y')",
    "select lo_import('/etc/passwd')",
    "select g from generate_series(1, 3) g",
    # Functional notation: Postgres reads x.f and (x).f as f(x), with no FuncCall in the parse tree.
    "select o.to_jsonb from orders o",
    "select (o).row_to_json from orders o",
    "select o.pg_typeof from orders o",
    "select o.num_nonnulls from orders o",
    "select o.pg_column_size from orders o",
    "select (o.merchant).norm_name from orders o",
    "select orders.to_jsonb from orders",
    "select x.to_jsonb from (select 1 as y) x",
    # ... even when the query defines an output with that name somewhere else.
    "select o.to_jsonb, 1 as to_jsonb from orders o",
    "select o.concat from orders o, (select concat('a') from orders) c",
    "select (o).* from orders o",
    "select o.public.orders from orders o",
    # Whole rows as values.
    "select o from orders o",
    "select concat(o) from orders o",
    "select count(o.*) from orders o",
    "select (o.*)::text from orders o",
    "select m from (select * from orders) m",
    # Casts to anything but plain scalars reach the catalog.
    "select 'pg_authid'::regclass",
    "select 'set_config'::regproc",
    "select null::jobs",
    "select null::orders",
    "select '1'::int[]",
    "select 'x'::pg_catalog.regclass",
    "select 'x'::public.text",
    # Tables outside the allowlist.
    "select * from jobs",
    "select * from profiles",
    "select * from connections",
    "select * from consents",
    "select * from pg_catalog.pg_roles",
    "select * from pg_roles",
    "select * from public.jobs",
    "select * from auth.users",
    # A CTE named like a real table reaches that table wherever the CTE is out of scope.
    "select * from (with jobs as (select 1 as x) select * from jobs) a, jobs",
    # Locking, INTO, other statements.
    "select * from orders for update",
    "select * from orders for no key update",
    "select * into stolen from orders",
    "with x as (update orders set total_amount = 0 returning 1) select * from x",
    "set role postgres",
    "reset role",
    "explain select 1",
    "copy orders to stdout",
    "select 1; select 2",
    "",
    "   ",
]


@pytest.mark.parametrize("query", ESCAPES)
def test_ask_sql_refuses_escapes(pg, any_conn, query):
    _two_users(pg)
    r = ask.scoped_sql(any_conn, A, query.replace("{B}", B).replace("{A}", A))
    assert "error" in r and "rows" not in r
    assert "999" not in r["error"] and "LEAK" not in r["error"]
    assert ask.scoped_sql(any_conn, A, "select sum(total_amount) from orders")["rows"] == [(100.0,)]
    assert pg.execute("select count(*) from orders").fetchone() == (2,)


def test_ask_sql_hides_database_text_for_errors_raised_while_running(pg):
    store.upsert_orders(pg, A, "zepto", [order("1", 100)])  # merchant "Store"
    r = ask.scoped_sql(pg, A, "select merchant::int from orders")
    assert "error" in r and "Store" not in r["error"] and "InvalidTextRepresentation" in r["error"]
    # Errors raised before anything runs (parse, plan) are returned so the model can fix its query.
    r = ask.scoped_sql(pg, A, "select nope from orders")
    assert r == {"error": 'column "nope" does not exist'}


@pytest.mark.parametrize("query, check", [
    ("select month, site, orders, amount from monthly_spend order by month", lambda rows: rows[0][1] == "zepto"),
    ("select category, amount from category_spend", lambda rows: rows == [("dairy", 120)]),
    ("select merchant, orders from merchant_spend", lambda rows: rows == [("Store", 2)]),
    ("select to_char(date_trunc('month', placed_at::timestamp), 'YYYY-MM') as m, sum(total_amount) "
     "from orders group by 1 order by 1", lambda rows: rows == [("2025-01", 220.0)]),
    ("select extract(year from placed_at::timestamp)::int, count(*) from orders group by 1",
     lambda rows: rows == [(2025, 2)]),
    ("select order_id, total_amount, rank() over (order by total_amount desc), "
     "sum(total_amount) over (partition by site) from orders order by 1",
     lambda rows: rows == [("1", 100.0, 2, 220.0), ("2", 120.0, 1, 220.0)]),
    ("with m as (select substr(placed_at, 1, 7) as month, sum(total_amount) as amount from orders "
     "where status not in ('cancelled', 'refunded', 'failed') group by 1) "
     "select month, amount, lag(amount) over (order by month) from m", lambda rows: rows == [("2025-01", 220.0, None)]),
    ("select c.category, count(*) from order_items i left join item_categories c "
     "on c.name_norm = norm_name(i.name) group by 1", lambda rows: rows == [("dairy", 1)]),
    ("select coalesce(merchant, '?'), round(avg(total_amount)::numeric, 1), max(placed_at) from orders "
     "group by 1 having count(*) > 1", lambda rows: rows[0][:2] == ("Store", 110)),
    ("select site from orders where placed_at::date between current_date - 10000 and current_date "
     "and lower(site) in (select lower(site) from monthly_spend) limit 1", lambda rows: rows == [("zepto",)]),
    # Qualified columns: real ones, CTE outputs, subquery aliases, column lists; o.* as a select-list entry.
    ("select o.merchant from orders o where o.order_id = '1'", lambda rows: rows == [("Store",)]),
    ("with c as (select sum(total_amount) as total from orders) select c.total from c",
     lambda rows: rows == [(220.0,)]),
    ("select x.amount from (select sum(total_amount) as amount from orders) x", lambda rows: rows == [(220.0,)]),
    ("with c(total) as (select sum(total_amount) from orders) select c.total from c", lambda rows: rows == [(220.0,)]),
    ("select v.n from (values (1), (2)) v(n) order by v.n", lambda rows: rows == [(1,), (2,)]),
    ("select i.name, i.qty from order_items i", lambda rows: rows == [("Amul Milk!", None)]),
    ("select m.orders from monthly_spend m where m.site in (select site from orders)", lambda rows: rows == [(2,)]),
    ("select o.* from orders o where o.order_id = '1'", lambda rows: rows[0][2] == "1"),
    ("select total_amount::numeric(10, 2), placed_at::timestamp with time zone, '7'::double precision, "
     "'2025-01-01'::date + interval '1 month', 'x'::varchar(3), 1::bigint, true::boolean from orders limit 1",
     lambda rows: len(rows) == 1),
])
def test_ask_sql_allows_analyst_queries(pg, ask_conn, query, check):
    store.upsert_orders(pg, A, "zepto", [order("1", 100), order("2", 120, items=[{"name": "Amul Milk!"}])])
    pg.execute("insert into item_categories (name_norm, category) values ('amul milk', 'dairy')")
    store.upsert_orders(pg, B, "zepto", [order("3", 999)])
    r = ask.scoped_sql(ask_conn, A, query)
    assert "error" not in r, r
    assert check(r["rows"]), r["rows"]


def test_ask_sql_caps_rows(pg):
    values = ",".join(f"({i})" for i in range(500))
    r = ask.scoped_sql(pg, A, f"select x from (values {values}) v(x)")
    assert len(r["rows"]) == 200 and r["truncated"] is True and r["columns"] == ["x"]
    r = ask.scoped_sql(pg, A, "select x from (values (1), (2), (3)) v(x)")
    assert r["rows"] == [(1,), (2,), (3,)] and r["truncated"] is False


def test_ask_sql_refuses_a_non_autocommit_connection(_applied):
    with psycopg.connect(_applied) as c:  # autocommit off: a BEGIN would be swallowed by psycopg's own
        with pytest.raises(ValueError):
            ask.scoped_sql(c, A, "select 1")


def test_reader_problem_refuses_superuser_and_accepts_ask_reader(pg, ask_conn):
    assert "superuser" in ask.reader_problem(pg)
    assert ask.reader_problem(ask_conn) is None
    attrs = pg.execute("select rolsuper, rolbypassrls, rolcreaterole, rolcreatedb, rolinherit from pg_roles "
                       "where rolname = 'ask_reader'").fetchone()
    assert attrs == (False, False, False, False, False)


def test_ask_schema_describes_tables_and_views():
    for name in ("orders", "order_items", "item_categories", "monthly_spend", "category_spend", "merchant_spend"):
        assert name in ask.SCHEMA


# --- POST /ask ----------------------------------------------------------------------------------------------

class _Msg(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        d = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [{"id": tc.id, "type": "function",
                                "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                               for tc in self.tool_calls]
        return {k: v for k, v in d.items() if v is not None}


class StubLLM:
    """OpenAI-shaped client: first asks for one run_sql call, then answers from the tool result."""

    def __init__(self):
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, model, messages, tools=None, **_):
        self.requests.append([dict(m) if isinstance(m, dict) else m for m in messages])
        if len(self.requests) == 1:
            call = SimpleNamespace(id="call_1", function=SimpleNamespace(
                name="run_sql", arguments=json.dumps({"query": "select sum(total_amount) as total from orders"})))
            msg = _Msg(content=None, tool_calls=[call])
        else:
            result = json.loads(messages[-1]["content"])
            msg = _Msg(content=f"You spent {result['rows'][0][0]:.0f}", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


@pytest.fixture
def ask_env(ask_url, monkeypatch):
    monkeypatch.setenv("ASK_DATABASE_URL", ask_url)
    monkeypatch.delenv("ASK_FAKE", raising=False)
    return ask_url


def test_ask_endpoint_uses_stub_llm(client, token_a, pg, ask_env, monkeypatch, caplog):
    store.upsert_orders(pg, A, "zepto", [order("1", 100)])
    store.upsert_orders(pg, B, "zepto", [order("2", 999)])
    stub = StubLLM()
    monkeypatch.setattr(ask, "default_llm", lambda: stub)
    caplog.set_level(logging.DEBUG, logger="insights")
    r = client.post("/ask", json={"question": "How much did I spend?"}, headers=token_a)
    assert r.status_code == 200 and r.json() == {"answer": "You spent 100"}
    assert len(stub.requests) == 2
    tool_msgs = [m for m in stub.requests[1] if isinstance(m, dict) and m.get("role") == "tool"]
    assert len(tool_msgs) == 1  # scoped_sql ran exactly once
    system = stub.requests[0][0]["content"]
    assert "monthly_spend" in system and "PostgreSQL" in system
    sql_logs = [rec for rec in caplog.records if rec.getMessage().startswith("sql:")]
    assert sql_logs and all(rec.levelno == logging.DEBUG for rec in sql_logs)  # the model's SQL: DEBUG only


@pytest.mark.parametrize("which", ["unset", "superuser"])
def test_ask_refused_without_a_safe_ask_login(client, token_a, monkeypatch, caplog, which):
    monkeypatch.delenv("ASK_FAKE", raising=False)
    monkeypatch.setattr(ask, "default_llm", lambda: pytest.fail("must not call the LLM"))
    superuser_url = os.environ["TEST_DATABASE_URL"]
    if which == "unset":
        monkeypatch.delenv("ASK_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("ASK_DATABASE_URL", superuser_url)
    r = client.post("/ask", json={"question": "How much did I spend?"}, headers=token_a)
    assert r.status_code == 503
    assert "/ask refused" in caplog.text and superuser_url not in caplog.text


def test_ask_startup_logs_a_misconfigured_ask_login_without_secrets(pg, monkeypatch, caplog):
    from fastapi.testclient import TestClient

    from server.api import app
    monkeypatch.delenv("ASK_FAKE", raising=False)
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    url = make_conninfo(os.environ["TEST_DATABASE_URL"], password="postgres-secret-should-not-show")
    monkeypatch.setenv("ASK_DATABASE_URL", url)
    with TestClient(app):  # the API still starts; only Ask is disabled
        pass
    assert "Ask is disabled" in caplog.text and "postgres-secret-should-not-show" not in caplog.text
    monkeypatch.setenv("ASK_DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    caplog.clear()
    with TestClient(app):
        pass
    assert "Ask is disabled" in caplog.text and "superuser" in caplog.text


def test_ask_startup_is_quiet_with_the_ask_reader_login(ask_env, monkeypatch, caplog):
    from fastapi.testclient import TestClient

    from server.api import app
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    with TestClient(app):
        pass
    assert "Ask is disabled" not in caplog.text


def test_ask_validates_question(client, token_a, ask_env, monkeypatch):
    monkeypatch.setattr(ask, "default_llm", lambda: pytest.fail("must not call the LLM"))
    assert client.post("/ask", json={"question": ""}, headers=token_a).status_code == 422
    assert client.post("/ask", json={"question": "x" * 1001}, headers=token_a).status_code == 422
    assert client.post("/ask", json={"question": "hi"}).status_code == 401


def test_ask_fake_returns_markdown_table(client, token_a, pg, ask_env, monkeypatch):
    store.upsert_orders(pg, A, "zepto", [order("1", 100)])
    store.upsert_orders(pg, A, "zomato", [order("2", 250)])
    store.upsert_orders(pg, B, "zepto", [order("3", 999)])
    monkeypatch.setenv("ASK_FAKE", "1")
    monkeypatch.setattr(ask, "default_llm", lambda: pytest.fail("ASK_FAKE must not call the LLM"))
    r = client.post("/ask", json={"question": "anything"}, headers=token_a)
    assert r.status_code == 200
    answer = r.json()["answer"]
    assert "| site | amount |" in answer
    assert "| zepto | 100 |" in answer and "| zomato | 250 |" in answer and "999" not in answer


def test_ask_fake_refused_unless_database_is_local(monkeypatch):
    from fastapi.testclient import TestClient

    from server.api import app
    monkeypatch.delenv("ASK_DATABASE_URL", raising=False)
    monkeypatch.setenv("ASK_FAKE", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:pw@db.example.com:5432/postgres")
    with pytest.raises(RuntimeError, match="ASK_FAKE"):
        with TestClient(app):
            pass
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:pw@localhost:54329/postgres")
    with TestClient(app):  # local: starts
        pass
    monkeypatch.delenv("ASK_FAKE")
    monkeypatch.setenv("DATABASE_URL", "postgresql://postgres:pw@db.example.com:5432/postgres")
    with TestClient(app):  # not asked for: no check
        pass


# --- CORS ---------------------------------------------------------------------------------------------------

def _preflight(client, origin, method="POST"):
    return client.options("/ask", headers={"Origin": origin, "Access-Control-Request-Method": method,
                                           "Access-Control-Request-Headers": "authorization,content-type"})


def test_cors_allows_pwa_origin(client, monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    r = _preflight(client, "http://localhost:3000")
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
    methods = {m.strip() for m in r.headers["access-control-allow-methods"].split(",")}
    assert {"GET", "POST", "PUT"} <= methods and "DELETE" not in methods
    allowed = r.headers["access-control-allow-headers"].lower()
    assert "authorization" in allowed and "content-type" in allowed
    assert "access-control-allow-origin" not in _preflight(client, "https://evil.example").headers
    assert _preflight(client, "http://localhost:3000", "DELETE").status_code == 400


def test_cors_origins_from_env(client, monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example.in, http://192.168.1.5:3000")
    assert _preflight(client, "https://app.example.in").headers["access-control-allow-origin"] == \
        "https://app.example.in"
    assert _preflight(client, "http://192.168.1.5:3000").headers["access-control-allow-origin"] == \
        "http://192.168.1.5:3000"
    assert "access-control-allow-origin" not in _preflight(client, "http://localhost:3000").headers
    r = client.get("/healthz", headers={"Origin": "https://app.example.in"})
    assert r.headers["access-control-allow-origin"] == "https://app.example.in"

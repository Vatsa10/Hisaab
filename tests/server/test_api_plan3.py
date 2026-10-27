"""Plan 3 API: question answers, delivery profile, Ask (scoped analyst SQL), CORS."""
import json
from types import SimpleNamespace

import psycopg
import pytest

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

def test_ask_sql_is_scoped_to_caller(pg):
    store.upsert_orders(pg, A, "zepto", [order("1", 100)])
    store.upsert_orders(pg, B, "zepto", [order("2", 999)])
    assert ask.scoped_sql(pg, A, "select sum(total_amount) from orders")["rows"] == [(100.0,)]
    assert ask.scoped_sql(pg, B, "select sum(total_amount) from orders;")["rows"] == [(999.0,)]
    assert ask.scoped_sql(pg, A, "select count(*) from orders where merchant like '%tor%'")["rows"] == [(1,)]


def test_ask_sql_is_read_only(pg):
    store.upsert_orders(pg, A, "zepto", [order("1", 100)])
    assert "error" in ask.scoped_sql(pg, A, "delete from orders")
    assert "error" in ask.scoped_sql(pg, A, "with x as (delete from orders returning 1) select * from x")
    assert "error" in ask.scoped_sql(pg, A, "select 1; delete from orders")
    assert "error" in ask.scoped_sql(pg, A, "select * from orders for update")
    assert "error" in ask.scoped_sql(pg, A, "select * into stolen from orders")
    assert pg.execute("select count(*) from orders").fetchone() == (1,)


def test_ask_sql_runs_as_authenticated_in_a_read_only_transaction(pg):
    r = ask.scoped_sql(pg, A, "select current_user::text, current_setting('transaction_read_only'), "
                              "current_setting('statement_timeout'), current_setting('request.jwt.claim.sub'), "
                              "current_setting('request.jwt.claims')")
    user, read_only, timeout, sub, claims = r["rows"][0]
    assert (user, read_only, timeout, sub) == ("authenticated", "on", "5s", A)
    assert json.loads(claims) == {"sub": A, "role": "authenticated"}
    # Nothing leaks into the caller's connection afterwards.
    assert pg.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert pg.execute("select current_user::text, current_setting('request.jwt.claim.sub', true)").fetchone()[1] in (
        None, "")
    assert pg.execute("select current_user::text").fetchone()[0] != "authenticated"


@pytest.mark.parametrize("query", [
    # Impersonating another user by rewriting the claim RLS reads.
    "select set_config('request.jwt.claim.sub', '{B}', true), (select sum(total_amount) from orders)",
    "select pg_catalog.set_config('request.jwt.claim.sub', '{B}', false)",
    'select U&"set\\005Fconfig"(\'role\', \'postgres\', true)',
    "select \"SET_CONFIG\"('role', 'postgres', true)",
    # A double-quoted identifier holding a single quote must not hide the call that follows it.
    """select x."a'b" || set_config('role', 'postgres', true) from (select 'v' as "a'b") x""",
    # Dynamic SQL executed at run time, where a static check could not see it.
    "select query_to_xml('select * from orders', true, true, '')",
    "select * from ts_stat('select to_tsvector(merchant) from orders')",
    # Server-admin and side-effecting functions.
    "select pg_sleep(0)",
    "select pg_notify('otp_x', 'y')",
    "select lo_import('/etc/passwd')",
    "select nextval('x')",
])
def test_ask_sql_refuses_escapes(pg, query):
    store.upsert_orders(pg, A, "zepto", [order("1", 100)])
    store.upsert_orders(pg, B, "zepto", [order("2", 999)])
    r = ask.scoped_sql(pg, A, query.replace("{B}", B))
    assert "error" in r and "rows" not in r
    assert ask.scoped_sql(pg, A, "select sum(total_amount) from orders")["rows"] == [(100.0,)]


def test_ask_sql_caps_rows(pg):
    r = ask.scoped_sql(pg, A, "select g from generate_series(1, 500) g")
    assert len(r["rows"]) == 200 and r["truncated"] is True and r["columns"] == ["g"]
    r = ask.scoped_sql(pg, A, "select g from generate_series(1, 3) g")
    assert r["rows"] == [(1,), (2,), (3,)] and r["truncated"] is False


def test_ask_sql_rejects_non_select(pg):
    for q in ["", "   ", "update orders set total_amount = 0", "set role postgres", "reset role",
              "explain select 1", "copy orders to stdout"]:
        assert "error" in ask.scoped_sql(pg, A, q), q


def test_ask_sql_refuses_a_non_autocommit_connection(_applied):
    with psycopg.connect(_applied) as c:  # autocommit off: a BEGIN would be swallowed by psycopg's own
        with pytest.raises(ValueError):
            ask.scoped_sql(c, A, "select 1")


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


def test_ask_endpoint_uses_stub_llm(client, token_a, pg, monkeypatch):
    store.upsert_orders(pg, A, "zepto", [order("1", 100)])
    store.upsert_orders(pg, B, "zepto", [order("2", 999)])
    stub = StubLLM()
    monkeypatch.delenv("ASK_FAKE", raising=False)
    monkeypatch.setattr(ask, "default_llm", lambda: stub)
    r = client.post("/ask", json={"question": "How much did I spend?"}, headers=token_a)
    assert r.status_code == 200 and r.json() == {"answer": "You spent 100"}
    assert len(stub.requests) == 2
    tool_msgs = [m for m in stub.requests[1] if isinstance(m, dict) and m.get("role") == "tool"]
    assert len(tool_msgs) == 1  # scoped_sql ran exactly once
    system = stub.requests[0][0]["content"]
    assert "monthly_spend" in system and "PostgreSQL" in system


def test_ask_validates_question(client, token_a, monkeypatch):
    monkeypatch.setattr(ask, "default_llm", lambda: pytest.fail("must not call the LLM"))
    assert client.post("/ask", json={"question": ""}, headers=token_a).status_code == 422
    assert client.post("/ask", json={"question": "x" * 1001}, headers=token_a).status_code == 422
    assert client.post("/ask", json={"question": "hi"}).status_code == 401


def test_ask_fake_returns_markdown_table(client, token_a, pg, monkeypatch):
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

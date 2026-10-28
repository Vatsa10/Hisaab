"""Every LLM caller goes through llm.py with Claude request and response shapes (fake client, no API)."""
import json
from types import SimpleNamespace

import pytest
from llm_fakes import FakeClient, reply, text, thinking, tool_use

import categorize
import db
import insights
import llm
from agent import TOOLS, LoginAgent
from test_db import order


@pytest.fixture(autouse=True)
def _haiku(monkeypatch):
    monkeypatch.setattr(llm, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)


# --- the browser-step fallback after Jev ------------------------------------------------------------------

def _login_agent():
    return LoginAgent("zepto", SimpleNamespace(url="https://www.zepto.com/"), None, None, "9000000000")


def test_browser_step_fallback_is_one_claude_tool_call(monkeypatch):
    fake = FakeClient(reply(thinking(), tool_use("tu_1", "click", {"element_ref": "e3"}), stop="tool_use"))
    monkeypatch.setattr(llm, "client", lambda: fake)
    la = _login_agent()
    assert la.decide_llm() == ("click", {"element_ref": "e3"}, "claude")
    req = fake.calls[0]
    assert req["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}  # forced choice may 400
    assert req["output_config"] == {"effort": "low"}
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in req["tools"])
    assert {t["name"] for t in req["tools"]} == {t["name"] for t in LoginAgent.TOOLS}
    assert "Call exactly one tool" in req["messages"][-1]["content"] and req["messages"][-1]["role"] == "user"
    assert req["system"] == la.system_prompt()


def test_browser_step_without_a_tool_call_waits(monkeypatch):
    monkeypatch.setattr(llm, "client", lambda: FakeClient(reply(text("Hmm, let me think."))))
    assert _login_agent().decide_llm() == ("wait", {}, "claude")


def test_tool_lists_use_the_claude_shape():
    assert "list_captured_json" in {t["name"] for t in TOOLS}
    assert "list_captured_json" not in {t["name"] for t in LoginAgent.TOOLS}
    assert "type_value" in {t["name"] for t in LoginAgent.TOOLS}
    assert all("function" not in t for t in LoginAgent.TOOLS)


# --- the analyst loop -------------------------------------------------------------------------------------

def _sql(query):
    return {"columns": ["total"], "rows": [[123.0]], "truncated": False}


def test_analyst_loop_runs_tools_and_answers():
    fake = FakeClient(reply(thinking(), tool_use("tu_1", "run_sql", {"query": "select 1"}), stop="tool_use"),
                      reply(text("You spent ₹123")))
    queries = []
    answer = insights.ask("How much?", sql_runner=lambda q: queries.append(q) or _sql(q), client=fake)
    assert answer == "You spent ₹123" and queries == ["select 1"]
    first, second = fake.calls
    assert first["system"] == insights.ANALYST and first["tools"] == [insights.RUN_SQL]
    assert first["output_config"] == {"effort": "medium"}
    assert first["messages"] == [{"role": "user", "content": "How much?"}]
    # append-only history: the second request extends the first, thinking blocks passed back untouched
    assert second["messages"][:1] == first["messages"]
    assistant, results = second["messages"][1:]
    assert assistant["role"] == "assistant" and assistant["content"][0].type == "thinking"
    assert results["role"] == "user" and results["content"][0]["tool_use_id"] == "tu_1"
    assert json.loads(results["content"][0]["content"])["rows"] == [[123.0]]


def test_analyst_tool_definition_is_strict():
    assert insights.RUN_SQL["strict"] is True and insights.RUN_SQL["name"] == "run_sql"
    assert insights.RUN_SQL["input_schema"]["required"] == ["query"]


def test_analyst_trims_new_tool_results_to_stay_under_the_cap():
    """Near the cap a new result is cut to the room left, never by editing earlier turns."""
    big = {"columns": ["c"], "rows": [["y" * 100] for _ in range(1000)], "truncated": False}
    used = llm.INPUT_CAP - 8_000
    fake = FakeClient(reply(tool_use("tu_1", "run_sql", {"query": "q"}), stop="tool_use", input_tokens=used,
                            output_tokens=500),
                      reply(text("done")))
    assert insights.ask("q?", sql_runner=lambda q: big, client=fake) == "done"
    result = fake.calls[1]["messages"][2]["content"][0]["content"]
    assert len(result.encode("utf-8")) <= 8_000 - 500 - insights.ANSWER_RESERVE
    assert "trimmed" in result


def test_analyst_stops_calling_tools_when_the_budget_is_spent():
    fake = FakeClient(reply(tool_use("tu_1", "run_sql", {"query": "q"}), stop="tool_use",
                            input_tokens=llm.INPUT_CAP - 1_000),
                      reply(text("best effort")))
    assert insights.ask("q?", sql_runner=_sql, client=fake) == "best effort"
    last = fake.calls[1]
    assert last["tool_choice"] == {"type": "none"} and last["tools"] == [insights.RUN_SQL]
    assert last["messages"][-1] == {"role": "user", "content": "Answer now with what you have."}
    assert "token budget" in last["messages"][2]["content"][0]["content"]


def test_analyst_out_of_calls_asks_for_an_answer_without_tools():
    loop = [reply(tool_use(f"tu_{i}", "run_sql", {"query": "q"}), stop="tool_use") for i in range(2)]
    fake = FakeClient(*loop, reply(text("final")))
    assert insights.ask("q?", max_calls=2, sql_runner=_sql, client=fake) == "final"
    assert fake.calls[-1]["tool_choice"] == {"type": "none"}


# --- categorisation ---------------------------------------------------------------------------------------

def test_categorize_uses_structured_output(tmp_path, monkeypatch):
    con = db.connect(tmp_path / "t.db")
    db.upsert_orders(con, "zepto", [order("1", 100, items=[{"name": "Amul Milk"}, {"name": "Lays"}])])

    def answer(kw):
        names = json.loads(kw["messages"][0]["content"])
        return reply(text(json.dumps({"items": [{"name": n, "category": "dairy"} for n in names]})))
    fake = FakeClient(answer)
    monkeypatch.setattr(llm, "client", lambda: fake)
    assert categorize.categorize(con) == 2
    req = fake.calls[0]
    assert req["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": categorize.SCHEMA}}
    assert categorize.SCHEMA["properties"]["items"]["items"]["properties"]["category"]["enum"] == categorize.CATEGORIES
    assert {r[0] for r in con.execute("select category from item_categories")} == {"dairy"}


def test_server_classifier_uses_claude(monkeypatch):
    from server import categorize_pg
    fake = FakeClient(reply(text(json.dumps({"items": [{"name": "Amul Milk", "category": "dairy"}]}))))
    monkeypatch.setattr(llm, "client", lambda: fake)
    assert categorize_pg.categorize_new_items.__defaults__[0] is None
    assert categorize_pg.llm_classify(["Amul Milk"]) == {"Amul Milk": "dairy"}
    assert fake.calls[0]["system"] == categorize_pg.PROMPT
    assert fake.calls[0]["output_config"]["format"]["schema"] == categorize.SCHEMA

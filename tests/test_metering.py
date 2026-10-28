"""Per-job metering: Claude tokens (via llm.on_usage) and Jev calls, scoped by a ContextVar."""
import threading
from types import SimpleNamespace

import pytest
from llm_fakes import FakeClient, reply, text, tool_use

import agent
import categorize
import insights
import llm
import metering


@pytest.fixture(autouse=True)
def _haiku(monkeypatch):
    monkeypatch.setattr(llm, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)


def _usage(i, o):
    return llm.CallUsage(purpose="t", model="claude-haiku-5-5", input_tokens=i, output_tokens=o, stop_reason="end_turn")


def test_claude_usage_and_jev_calls_are_counted():
    with metering.meter() as m:
        metering._on_usage(_usage(1000, 234))
        metering.count_jev()
        metering.count_jev(2)
    assert (m.tokens, m.jev_calls) == (1234, 3)


def test_ai_tokens_counts_each_jev_call_as_the_given_token_equivalent():
    m = metering.Meter(tokens=500, jev_calls=3)
    assert m.ai_tokens(1000) == 3500


def test_no_meter_is_a_noop():
    metering._on_usage(_usage(10, 10))
    metering.count_jev()
    with metering.meter() as m:
        pass
    assert (m.tokens, m.jev_calls) == (0, 0)


def test_nested_meter_restores_the_previous_one():
    with metering.meter() as outer:
        metering.count_jev()
        with metering.meter() as inner:
            metering.count_jev()
        metering.count_jev()
    assert (outer.jev_calls, inner.jev_calls) == (2, 1)
    assert metering.current() is None


def test_meter_is_not_shared_across_threads():
    with metering.meter() as m:
        t = threading.Thread(target=metering.count_jev)
        t.start()
        t.join()
    assert m.jev_calls == 0


def test_insights_counts_tokens():
    fake = FakeClient(reply(tool_use("tu_1", "run_sql", {"query": "select 1"}), stop="tool_use",
                            input_tokens=40, output_tokens=10),
                      reply(text("done"), input_tokens=40, output_tokens=10))
    with metering.meter() as m:
        insights.ask("How much?", sql_runner=lambda q: {"columns": [], "rows": [], "truncated": False}, client=fake)
    assert m.tokens == 50 * len(fake.calls) == 100


def test_categorize_counts_tokens():
    fake = FakeClient(reply(text('{"items": []}'), input_tokens=70, output_tokens=30))
    with metering.meter() as m:
        categorize.classify(["Amul Milk"], fake)
    assert m.tokens == 100


class _Jev:
    def __init__(self, answers=None, fail=False):
        self.calls, self.answers, self.fail = 0, answers or {}, fail

    def system_one(self, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        return SimpleNamespace(answers=self.answers)


def _discovery():
    d = agent.Discovery.__new__(agent.Discovery)
    d.secrets, d.snap = {}, {"elements": []}
    d.state = lambda: {}
    return d


def test_jev_verify_counts_a_call(monkeypatch):
    jev = _Jev({"ok": SimpleNamespace(noul=0.9)})
    monkeypatch.setattr(agent, "jev", lambda: jev)
    with metering.meter() as m:
        assert _discovery().verify("orders?") == 0.9
    assert m.jev_calls == 1


def test_failed_jev_call_still_counts(monkeypatch):
    monkeypatch.setattr(agent, "jev", lambda: _Jev(fail=True))
    with metering.meter() as m:
        assert _discovery().decide_jev() is None
    assert m.jev_calls == 1


def test_no_jev_client_counts_nothing(monkeypatch):
    monkeypatch.setattr(agent, "jev", lambda: None)
    with metering.meter() as m:
        assert _discovery().verify("orders?") is None
    assert m.jev_calls == 0

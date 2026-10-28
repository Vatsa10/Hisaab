"""The cost policy: only models under $1 / MTok, and no prompt past the 100K-token price step."""
import asyncio
import logging

import anthropic
import httpx
import pytest
from llm_fakes import FakeClient, reply, text, thinking

import llm

SMALL = [{"role": "user", "content": "hello"}]


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """The owner's real .env must not leak into these tests."""
    monkeypatch.setattr(llm, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)


def _create(client, **kw):
    return llm.create("test", client=client, **({"effort": "low", "max_tokens": 1024, "messages": SMALL} | kw))


# --- the model -------------------------------------------------------------------------------------------

def test_haiku_is_allowed_and_the_default(monkeypatch):
    assert llm.model() == "claude-haiku-5-5"
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-5-5")
    assert llm.model() == "claude-haiku-5-5"
    assert llm.MODEL_PRICES == {"claude-haiku-5-5": (0.10, 0.50)}


@pytest.mark.parametrize("name", ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-4-5"])
def test_model_priced_at_or_above_a_dollar_is_refused(monkeypatch, name):
    monkeypatch.setenv("ANTHROPIC_MODEL", name)
    with pytest.raises(llm.LLMPolicyError):
        llm.model()


@pytest.mark.parametrize("prices", [(2.00, 10.00), (0.10, 1.00), (1.00, 0.50)])
def test_price_table_entry_at_or_above_a_dollar_is_refused(monkeypatch, prices):
    """Adding a pricey model to the table does not let it through either: the price check is separate."""
    monkeypatch.setitem(llm.MODEL_PRICES, "pricey", prices)
    monkeypatch.setenv("ANTHROPIC_MODEL", "pricey")
    with pytest.raises(llm.LLMPolicyError):
        llm.model()


@pytest.mark.parametrize("name", ["gpt-6-luna", "claude-haiku-5-5-20990101", "made-up", ""])
def test_unknown_model_is_refused(monkeypatch, name):
    monkeypatch.setenv("ANTHROPIC_MODEL", name)
    if name == "":  # an empty value means "unset": the default
        assert llm.model() == "claude-haiku-5-5"
        return
    with pytest.raises(llm.LLMPolicyError):
        llm.model()


def test_refused_model_never_reaches_the_api(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    fake = FakeClient(reply(text("hi")))
    with pytest.raises(llm.LLMPolicyError):
        _create(fake)
    with pytest.raises(llm.LLMPolicyError):
        llm.client()
    assert fake.calls == [] and fake.counts == []


def test_worker_refuses_to_start_on_a_pricey_model(monkeypatch):
    from server import worker
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5-5")
    monkeypatch.setattr(worker, "settings", lambda **k: pytest.fail("policy is checked before anything else"))
    with pytest.raises(llm.LLMPolicyError):
        worker.main(max_loops=0)


def test_api_refuses_to_start_on_a_pricey_model(monkeypatch):
    from server import api
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5-5")

    async def start():
        async with api.lifespan(api.app):
            pass
    with pytest.raises(llm.LLMPolicyError):
        asyncio.run(start())


# --- the input cap ---------------------------------------------------------------------------------------

def test_prompt_over_the_cap_is_refused():
    big = [{"role": "user", "content": "x" * 400_000}]
    fake = FakeClient(reply(text("never")), count=95_000)
    with pytest.raises(llm.LLMPolicyError, match="prompt too large for the cost policy"):
        _create(fake, messages=big)
    assert len(fake.counts) == 1 and fake.calls == []
    assert fake.counts[0]["model"] == "claude-haiku-5-5"


def test_large_prompt_under_the_cap_after_counting_goes_ahead():
    big = [{"role": "user", "content": "x" * 400_000}]
    fake = FakeClient(reply(text("ok")), count=80_000)
    assert llm.text(_create(fake, messages=big)) == "ok"
    assert len(fake.counts) == 1 and len(fake.calls) == 1


def test_obviously_small_prompt_skips_the_token_count():
    fake = FakeClient(reply(text("ok")))
    _create(fake, system="be brief", tools=[llm.tool("t", "a tool", {"q": {"type": "string"}}, ["q"])])
    assert fake.counts == [] and len(fake.calls) == 1


def test_upper_bound_counts_utf8_bytes_not_characters():
    """Byte-level tokens: every token covers at least one byte, so UTF-8 bytes bound the token count."""
    assert llm.upper_bound(messages=[{"role": "user", "content": "₹" * 1000}]) >= 3000


def test_cap_leaves_room_under_the_price_step():
    assert llm.INPUT_CAP == 90_000 and llm.INPUT_CAP < 100_000


# --- the request ------------------------------------------------------------------------------------------

def test_request_shape_follows_the_haiku_rules():
    fake = FakeClient(reply(thinking(), text("ok")))
    assert llm.text(_create(fake, effort="medium")) == "ok"  # read by type: thinking comes first
    req = fake.calls[0]
    assert req["model"] == "claude-haiku-5-5"
    assert req["output_config"] == {"effort": "medium"}
    assert req["thinking"] == {"type": "adaptive"}
    for banned in ("temperature", "top_p", "top_k", "fallbacks"):
        assert banned not in req


def test_structured_output_goes_through_output_config_format():
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"],
              "additionalProperties": False}
    fake = FakeClient(reply(text('{"a": "b"}')))
    assert llm.json_reply(_create(fake, output_schema=schema)) == {"a": "b"}
    assert fake.calls[0]["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": schema}}


def test_assistant_prefill_is_refused():
    fake = FakeClient(reply(text("never")))
    with pytest.raises(ValueError):
        _create(fake, messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "{"}])
    assert fake.calls == []


def test_unknown_effort_is_refused():
    with pytest.raises(ValueError):
        _create(FakeClient(reply(text("x"))), effort="turbo")


@pytest.mark.parametrize("stop, error", [("refusal", llm.LLMRefusal), ("max_tokens", llm.LLMTruncated)])
def test_stop_reason_is_checked_before_content(stop, error):
    with pytest.raises(error):
        _create(FakeClient(reply(text("partial"), stop=stop)))


def test_tool_helper_is_strict():
    t = llm.tool("run_sql", "Run SQL.", {"query": {"type": "string"}}, ["query"])
    assert t == {"name": "run_sql", "description": "Run SQL.", "strict": True, "input_schema": {
        "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
        "additionalProperties": False}}


@pytest.mark.parametrize("raw", ['{"a": 1}', '```json\n{"a": 1}\n```', 'Here it is:\n{"a": 1}\nDone.'])
def test_json_reply_tolerates_fences_and_preamble(raw):
    assert llm.json_reply(reply(text(raw))) == {"a": 1}


# --- usage and logging ------------------------------------------------------------------------------------

def test_usage_hook_sees_every_call_and_can_be_removed():
    seen = []
    remove = llm.on_usage(seen.append)
    try:
        _create(FakeClient(reply(text("ok"), input_tokens=321, output_tokens=12)))
    finally:
        remove()
    _create(FakeClient(reply(text("ok"))))
    assert seen == [llm.CallUsage(purpose="test", model="claude-haiku-5-5", input_tokens=321, output_tokens=12,
                                  stop_reason="end_turn")]


def test_usage_is_recorded_even_when_the_reply_is_refused():
    seen = []
    remove = llm.on_usage(seen.append)
    try:
        with pytest.raises(llm.LLMRefusal):
            _create(FakeClient(reply(stop="refusal")))
    finally:
        remove()
    assert [u.stop_reason for u in seen] == ["refusal"]


def test_a_failing_usage_hook_does_not_break_the_call(caplog):
    def boom(_):
        raise RuntimeError("meter down")
    remove = llm.on_usage(boom)
    try:
        assert llm.text(_create(FakeClient(reply(text("ok"))))) == "ok"
    finally:
        remove()
    assert "RuntimeError" in caplog.text


def test_logs_carry_counts_but_never_prompts_or_replies(caplog):
    caplog.set_level(logging.DEBUG, logger="llm")
    _create(FakeClient(reply(text("REPLY-SECRET"), input_tokens=77)),
            messages=[{"role": "user", "content": "PROMPT-SECRET"}], system="SYSTEM-SECRET")
    assert "77" in caplog.text and "end_turn" in caplog.text and "claude-haiku-5-5" in caplog.text
    for secret in ("REPLY-SECRET", "PROMPT-SECRET", "SYSTEM-SECRET"):
        assert secret not in caplog.text


def test_typed_api_errors_are_logged_by_class_and_reraised(caplog):
    def fail(kw):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    with pytest.raises(anthropic.APIConnectionError):
        _create(FakeClient(fail))
    assert "APIConnectionError" in caplog.text

"""Opt-in live smoke test: ANTHROPIC_LIVE=1 pytest tests/test_llm_live.py. One tiny real call (max_tokens 50)."""
import os

import pytest

import llm

pytestmark = pytest.mark.skipif(os.getenv("ANTHROPIC_LIVE") != "1", reason="live API call: set ANTHROPIC_LIVE=1")


def test_one_tiny_real_call():
    seen = []
    remove = llm.on_usage(seen.append)
    try:
        resp = llm.create("smoke", effort="low", max_tokens=50,
                          messages=[{"role": "user", "content": "Reply with the single word: ok"}])
        assert llm.text(resp).strip()
    except llm.LLMTruncated:
        pass  # adaptive thinking can use the 50 tokens; the call itself still went through and was metered
    finally:
        remove()
    assert len(seen) == 1
    assert seen[0].model.startswith("claude-haiku-5-5") and seen[0].input_tokens > 0

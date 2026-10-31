"""Per-job metering of AI use: Claude tokens (every llm.py reply, through llm.on_usage) and Jev calls.

A job opens `with meter() as m:`; everything the job's thread does inside that block adds to m. Outside a meter
counting is a no-op. Jev replies carry no token count, so they are counted as calls; Meter.ai_tokens(per_call)
turns them into token-equivalents with the caller's JEV_TOKENS_PER_CALL (held only in server/config.py).
"""
import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass

import llm


@dataclass
class Meter:
    input_tokens: int = 0
    output_tokens: int = 0
    jev_calls: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def ai_tokens(self, jev_tokens_per_call: int) -> int:
        """Claude tokens plus each Jev call as jev_tokens_per_call token-equivalents."""
        return self.tokens + self.jev_calls * jev_tokens_per_call


_current: ContextVar[Meter | None] = ContextVar("meter", default=None)


def current() -> Meter | None:
    return _current.get()


@contextlib.contextmanager
def meter() -> Iterator[Meter]:
    """A fresh Meter active for this block; the previous one (if any) is restored on exit."""
    m = Meter()
    token = _current.set(m)
    try:
        yield m
    finally:
        _current.reset(token)


def count_jev(n: int = 1) -> None:
    if (m := _current.get()) is not None:
        m.jev_calls += n


def _on_usage(usage: llm.CallUsage) -> None:
    if (m := _current.get()) is not None:
        m.input_tokens += usage.input_tokens or 0
        m.output_tokens += usage.output_tokens or 0


llm.on_usage(_on_usage)

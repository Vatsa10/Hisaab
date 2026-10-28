"""An Anthropic-shaped fake client for tests: no test ever calls the real API."""
from types import SimpleNamespace


def text(t: str):
    return SimpleNamespace(type="text", text=t)


def thinking():
    return SimpleNamespace(type="thinking", thinking="", signature="sig")


def tool_use(id_: str, name: str, input_: dict):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


def reply(*blocks, stop="end_turn", input_tokens=100, output_tokens=20, model="claude-haiku-5-5"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop, model=model,
                           usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                                                 cache_read_input_tokens=0, cache_creation_input_tokens=0))


class FakeClient:
    """replies: Message-like objects (or callables taking the request kwargs) returned in order by
    messages.create; count: what messages.count_tokens reports."""

    def __init__(self, *replies, count: int = 0):
        self.replies = list(replies)
        self.count = count
        self.calls: list[dict] = []
        self.counts: list[dict] = []
        self.messages = SimpleNamespace(create=self._create, count_tokens=self._count)

    def _create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})  # a snapshot: the caller keeps appending
        r = self.replies.pop(0)
        return r(kw) if callable(r) else r

    def _count(self, **kw):
        self.counts.append(kw)
        return SimpleNamespace(input_tokens=self.count)

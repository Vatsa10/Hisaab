"""Jev judgments for product matching and thin-page checks: thresholds, one call for many products, and every failure
mode (no key, an error, unsure) falling back to the existing path instead of accepting a product or failing a job."""
from types import SimpleNamespace

import pytest

import browser
import jev_tools
import sync
from server import worker


class FakeJev:
    """system_one returns the probability for each question name from `probs` (by question order)."""

    def __init__(self, probs=None, error=None):
        self.probs, self.error, self.calls = probs or [], error, []

    def system_one(self, state, questions, timeout=None):
        self.calls.append((state, list(questions), timeout))
        if self.error:
            raise self.error
        return SimpleNamespace(answers={n: SimpleNamespace(noul=p) for n, p in zip(questions, self.probs)})


@pytest.fixture
def jev(monkeypatch):
    def install(client):
        monkeypatch.setattr(jev_tools.agent, "jev", lambda: client)
        return client
    return install


def test_probabilities_map_to_yes_no_or_unsure(jev):
    client = jev(FakeJev([0.95, 0.05, 0.5]))
    assert jev_tools.judge_match("amul butter 500g", ["Amul Butter 500 g", "Mother Dairy Butter", "Amul Butter"]) \
        == [True, False, None]
    assert len(client.calls) == 1 and client.calls[0][1] == ["p0", "p1", "p2"]  # one call for all products
    assert client.calls[0][2] == jev_tools.TIMEOUT_S


def test_the_edges_of_the_thresholds(jev):
    jev(FakeJev([0.8, 0.2, 0.79, 0.21]))
    assert jev_tools.judge_match("x", ["a", "b", "c", "d"]) == [True, False, None, None]


def test_no_key_or_an_error_returns_none_and_never_raises(jev):
    jev(None)
    assert jev_tools.judge_match("x", ["a"]) is None
    jev(FakeJev(error=RuntimeError("boom")))
    assert jev_tools.judge_match("x", ["a"]) is None
    assert jev_tools.page_is_challenge("t", "x") is None


def test_no_products_means_no_call(jev):
    client = jev(FakeJev())
    assert jev_tools.judge_match("x", []) == [] and client.calls == []


def test_the_judge_uses_jev_alone_when_it_is_sure(jev, monkeypatch):
    jev(FakeJev([0.9, 0.1]))
    monkeypatch.setattr(worker.llm, "create", lambda *a, **k: pytest.fail("Claude must not be asked"))
    assert worker._judge()("milk", ["Amul Milk", "Soya Milk"]) == [True, False]


def test_claude_answers_only_what_jev_was_unsure_about(jev, monkeypatch):
    jev(FakeJev([0.95, 0.5]))
    monkeypatch.setattr(worker.llm, "create", lambda *a, **k: object())
    monkeypatch.setattr(worker.llm, "json_reply", lambda resp: {"answers": [False, True]})
    assert worker._judge()("milk", ["Amul Milk", "Toned Milk"]) == [True, True]  # Jev's yes kept; Claude settles #2


def test_claude_is_the_fallback_when_jev_is_off(jev, monkeypatch):
    jev(None)
    monkeypatch.setattr(worker.llm, "create", lambda *a, **k: object())
    monkeypatch.setattr(worker.llm, "json_reply", lambda resp: {"answers": [True, False]})
    assert worker._judge()("milk", ["a", "b"]) == [True, False]


def test_both_failing_never_accepts(jev, monkeypatch):
    jev(FakeJev(error=RuntimeError("x")))

    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(worker.llm, "create", boom)
    assert worker._judge()("milk", ["a", "b"]) == [None, None]


# ---- the thin-page check in a sync -------------------------------------------------------------------------

def _page(text):
    return SimpleNamespace(evaluate=lambda js: text)


def test_a_thin_page_that_jev_calls_a_challenge_raises(jev):
    jev(FakeJev([0.93]))
    with pytest.raises(browser.SiteThrottled):
        sync._check_thin_page(_page("Just a sec\nChecking that you are not a bot"))


def test_a_thin_page_jev_is_unsure_about_passes(jev):
    jev(FakeJev([0.5]))
    sync._check_thin_page(_page("Loading\n"))


def test_a_normal_page_never_costs_a_jev_call(jev):
    client = jev(FakeJev([0.99]))
    sync._check_thin_page(_page("Your orders\n" + "order line " * 200))
    assert client.calls == []


def test_a_failing_page_read_does_not_fail_the_sync(jev):
    jev(FakeJev([0.99]))

    def boom(js):
        raise RuntimeError("detached")
    sync._check_thin_page(SimpleNamespace(evaluate=boom))

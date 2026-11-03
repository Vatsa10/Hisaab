"""A Playwright driver that dies at startup is retried with a fresh one; any other error is not."""
import pytest

import browser


class _Mgr:
    def __init__(self, outcomes):
        self.outcomes = outcomes

    def start(self):
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


def _install(monkeypatch, outcomes):
    mgr = _Mgr(outcomes)
    monkeypatch.setattr(browser, "sync_playwright", lambda: mgr)
    monkeypatch.setattr(browser.time, "sleep", lambda s: None)
    return mgr


def test_a_driver_that_dies_once_is_replaced(monkeypatch):
    pw = object()
    mgr = _install(monkeypatch, [Exception("Connection.init: Connection closed while reading from the driver"), pw])
    assert browser._start_playwright() is pw and mgr.outcomes == []


def test_a_driver_that_keeps_dying_raises_after_the_last_try(monkeypatch):
    boom = Exception("Connection closed while reading from the driver")
    mgr = _install(monkeypatch, [boom] * browser.PLAYWRIGHT_START_TRIES)
    with pytest.raises(Exception, match="Connection closed"):
        browser._start_playwright()
    assert mgr.outcomes == []


def test_another_error_is_not_retried(monkeypatch):
    mgr = _install(monkeypatch, [RuntimeError("no browsers installed"), object()])
    with pytest.raises(RuntimeError):
        browser._start_playwright()
    assert len(mgr.outcomes) == 1

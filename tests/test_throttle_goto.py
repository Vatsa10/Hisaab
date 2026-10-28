from pathlib import Path

import pytest

import browser
import search
from test_search import LOC, fake_env, failing_api, get_recipe


class _Resp:
    def __init__(self, status, body="", headers=None):
        self.status, self._body, self.headers = status, body, headers or {}

    def text(self):
        return self._body


class _GotoPage:
    url = "https://x/"

    def __init__(self, *resps):
        self.resps, self.sleeps, self.calls = list(resps), [], 0

    def goto(self, url, **k):
        self.calls += 1
        return self.resps.pop(0)

    def wait_for_timeout(self, ms):
        self.sleeps.append(ms)


def test_goto_429_raises_immediately_without_sleep():
    page = _GotoPage(_Resp(429, "", {"retry-after": "45"}))
    with pytest.raises(browser.SiteThrottled) as e:
        browser.goto(page, "https://x/orders")
    assert e.value.kind == "rate_limited" and e.value.retry_after_s == 45
    assert page.sleeps == [] and page.calls == 1


def test_goto_challenge_body_raises_immediately_without_sleep():
    page = _GotoPage(_Resp(403, "<html>cf-chl-bypass</html>"))
    with pytest.raises(browser.SiteThrottled) as e:
        browser.goto(page, "https://x/orders")
    assert e.value.kind == "challenge" and page.sleeps == [] and page.calls == 1


def test_goto_5xx_still_retries():
    page = _GotoPage(_Resp(503, "down"), _Resp(200))
    browser.goto(page, "https://x/orders")
    assert page.calls == 2 and 4000 in page.sleeps


def test_raise_if_blocked_scans_title_and_visible_text():
    class P:
        js = None

        def evaluate(self, js, arg=None):
            P.js = js
            return ""
    browser.raise_if_blocked(P())
    assert "innerText" in P.js and "outerHTML" not in P.js


def test_throttle_in_store_lookup_and_fast_path_propagates(monkeypatch):
    def throttled(*a, **k):
        raise browser.SiteThrottled("rate_limited", 30)
    fake_env(monkeypatch, fetch=throttled)
    with pytest.raises(browser.SiteThrottled):
        search.search_products("zepto", ["milk"], LOC, get_recipe(), Path("/u/profile"))
    with pytest.raises(browser.SiteThrottled):
        search.fast_attempt(object(), {"method": "GET", "url": "u", "headers": {}})


def test_throttle_on_page_goto_propagates(monkeypatch):
    fake_env(monkeypatch, fetch=failing_api(RuntimeError("HTTP 502: x")))

    def goto(p, url, *a, **k):
        raise browser.SiteThrottled("challenge")
    monkeypatch.setattr(browser, "goto", goto)
    with pytest.raises(browser.SiteThrottled):
        search.search_products("zepto", ["milk"], LOC, get_recipe(), Path("/u/profile"))

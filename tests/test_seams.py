import pytest
from playwright.sync_api import sync_playwright

import browser
from agent import LoginAgent


class _Dead:
    """Popen stand-in for a Chrome that exited at startup."""

    def poll(self):
        return 1

    def wait(self, *a, **k):
        pass

    def kill(self):
        pass


@pytest.fixture
def otp_page():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        page = b.new_page()
        page.set_content("<input autocomplete=one-time-code>")
        yield page
        b.close()


def test_otp_comes_from_prompt_not_input(monkeypatch, otp_page):
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("input() called"))
    la = LoginAgent("zepto", otp_page, None, None, "9000000000", prompt=lambda q: "4321")
    la.phone_typed = True
    la.on_observe()
    assert la.otp_entered and la.secrets["OTP"] == "4321"


def test_prompt_timeout_ends_run_cleanly(otp_page):
    def slow(q):
        raise TimeoutError

    la = LoginAgent("zepto", otp_page, None, None, "9000000000", prompt=slow)
    la.phone_typed = True
    la.on_observe()
    assert la.done == {"success": False, "note": "otp_timeout", "url": otp_page.url}


def test_open_site_uses_given_profile_dir(monkeypatch, tmp_path):
    seen = {}

    def fake_popen(args, **k):
        seen["args"] = args
        return _Dead()

    monkeypatch.setattr(browser.subprocess, "Popen", fake_popen)
    with pytest.raises(RuntimeError):
        with browser.open_site("zepto", profile_dir=tmp_path / "u1-zepto"):
            pass
    assert f"--user-data-dir={(tmp_path / 'u1-zepto').resolve()}" in seen["args"]

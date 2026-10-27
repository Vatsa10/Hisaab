"""The real sync.login with only the browser edges faked (no database needed)."""
import pytest
from helpers import fake_login_env

import sync


def _ends(note):
    def run(self):
        return {"success": False, "note": note, "url": self.page.url}
    return run


def _never_called(text, kind="otp"):
    raise AssertionError(f"prompt must not be called ({kind}: {text!r})")


@pytest.mark.parametrize("note, kind", [("otp_timeout", "otp"), ("ask_timeout", "question")])
def test_unattended_timeout_raises_without_manual_fallback(monkeypatch, note, kind):
    fake_login_env(monkeypatch, _ends(note))
    with pytest.raises(sync.OtpTimeout) as e:
        sync.login("zepto", "9876543210", prompt=_never_called, location={})
    assert e.value.kind == kind


def test_unattended_agent_failure_returns_false(monkeypatch):
    fake_login_env(monkeypatch, _ends("stuck"))
    assert sync.login("zepto", "9876543210", prompt=_never_called, location={}) is False


def test_unattended_without_phone_returns_false(monkeypatch):
    fake_login_env(monkeypatch, _ends("stuck"))
    assert sync.login("zepto", None, prompt=_never_called, location={}) is False


def test_cli_default_keeps_manual_fallback(monkeypatch):
    fake_login_env(monkeypatch, _ends("stuck"))
    asked = []
    monkeypatch.setattr("builtins.input", lambda text="": asked.append(text) or "")
    assert sync.login("zepto", "9876543210") is True
    assert len(asked) == 1 and "press Enter" in asked[0]


def test_enter_otp_asks_with_kind_otp(monkeypatch):
    kinds = []

    def run(self):
        self.enter_otp()
        return self.done or {"success": False, "note": "stuck", "url": self.page.url}

    def prompt(text, kind="otp"):
        kinds.append(kind)
        raise sync.OtpTimeout(kind)
    fake_login_env(monkeypatch, run)
    with pytest.raises(sync.OtpTimeout) as e:
        sync.login("zepto", "9876543210", prompt=prompt, location={})
    assert kinds == ["otp"] and e.value.kind == "otp"


def test_ask_user_asks_with_kind_question(monkeypatch):
    kinds = []

    def run(self):
        self.ask_user("captcha?")
        return self.done or {"success": False, "note": "stuck", "url": self.page.url}

    def prompt(text, kind="otp"):
        kinds.append(kind)
        raise sync.OtpTimeout(kind)
    fake_login_env(monkeypatch, run)
    with pytest.raises(sync.OtpTimeout) as e:
        sync.login("zepto", "9876543210", prompt=prompt, location={})
    assert kinds == ["question"] and e.value.kind == "question"


SAVED = {"query": "Sunrise Apts, Akota, Vadodara, Gujarat", "lat": 22.3, "lon": 73.2}


def test_empty_location_does_not_leak_the_cli_location(monkeypatch):
    seen = fake_login_env(monkeypatch, _ends("stuck"), settings_location=SAVED)
    assert sync.login("zomato", "9876543210", prompt=_never_called, location={}) is False
    assert seen["agents"][0].location == ""
    assert seen["goto"] == ["https://www.zomato.com/"]
    assert seen["agents"][0].home == "https://www.zomato.com/"  # where it backs off to on a blocked page
    assert seen["open_site"][0]["location"] == {}


def test_given_location_drives_address_and_city(monkeypatch):
    seen = fake_login_env(monkeypatch, _ends("stuck"), settings_location=SAVED)
    mine = {"query": "Flat 2, Indiranagar, Bengaluru, Karnataka", "lat": 12.9, "lon": 77.6}
    assert sync.login("zomato", "9876543210", prompt=_never_called, location=mine) is False
    assert seen["agents"][0].location == mine["query"]
    assert seen["goto"] == ["https://www.zomato.com/bangalore/restaurants"]


def test_cli_without_location_uses_saved_settings(monkeypatch):
    seen = fake_login_env(monkeypatch, _ends("stuck"), settings_location=SAVED)
    monkeypatch.setattr("builtins.input", lambda text="": "")
    monkeypatch.setattr(sync.agent, "site_allows_host", lambda site, url: True)
    sync.login("zomato", "9876543210")
    assert seen["agents"][0].location == SAVED["query"]
    assert seen["goto"][0] == "https://www.zomato.com/vadodara/restaurants"
    assert seen["open_site"][0]["location"] is None

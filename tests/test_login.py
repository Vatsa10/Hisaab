import pytest

from agent import click_refusal, mask
from sync import normalize_phone

CONTINUE_IN_FORM = {"tag": "button", "text": "Continue", "in_form": True, "type": None}


@pytest.mark.parametrize("raw", ["9876543210", "+91 98765 43210", "919876543210", "98765-43210"])
def test_normalize_phone(raw):
    assert normalize_phone(raw) == "9876543210"


@pytest.mark.parametrize("raw", ["12345", "1234567890", "+1 415 555 0100"])
def test_normalize_phone_rejects(raw):
    with pytest.raises(ValueError):
        normalize_phone(raw)


def test_form_submit_only_allowed_in_login():
    assert click_refusal(CONTINUE_IN_FORM) == "would submit a form"
    assert click_refusal(CONTINUE_IN_FORM, login=True) is None


def test_write_actions_blocked_even_in_login():
    assert click_refusal({"tag": "button", "text": "Add to cart"}, login=True)
    assert click_refusal({"tag": "a", "text": "Saved", "href": "/checkout"}, login=True)


def test_mask_hides_secrets():
    assert mask('textbox "Phone": "9876543210" otp 4321', {"PHONE": "9876543210", "OTP": "4321"}) == \
        'textbox "Phone": "<PHONE>" otp <OTP>'


class FakeResp:
    def __init__(self, status):
        self.status = status


class FakePage:
    url = "https://site.com/"

    def __init__(self, statuses):
        self.statuses, self.calls = list(statuses), 0

    def goto(self, url, wait_until=None):
        self.calls += 1
        s = self.statuses.pop(0)
        if s == "err":
            raise RuntimeError("Page.goto: net::ERR_HTTP_RESPONSE_CODE_FAILURE at " + url)
        return FakeResp(s)

    def wait_for_timeout(self, ms):
        pass


def test_goto_retries_transient_403():
    from browser import goto
    page = FakePage(["err", 403, 200])
    goto(page, "https://www.swiggy.com/")
    assert page.calls == 3


def test_goto_gives_up_after_tries():
    from browser import goto
    with pytest.raises(RuntimeError):
        goto(FakePage(["err", "err", "err"]), "https://www.swiggy.com/")


def test_finish_false_refused_twice_then_accepted():
    from agent import Discovery
    d = Discovery("zepto", FakePage([]), None, None)
    assert "error" in d.finish(False, "login dialog did not open")
    assert "error" in d.finish(False, "still stuck")
    assert d.finish(False, "still stuck") == {"ok": True} and d.done["success"] is False


def test_logged_out_finish_is_immediate():
    from agent import Discovery
    d = Discovery("zepto", FakePage([]), None, None)
    assert d.finish(False, "LOGGED_OUT") == {"ok": True}


def test_ask_user_stop_ends_run(monkeypatch):
    from agent import Discovery
    d = Discovery("zepto", FakePage([]), None, None)
    monkeypatch.setattr("builtins.input", lambda *a: "stop")
    d.ask_user("captcha?")
    assert d.done and d.done["success"] is False


def test_ask_user_non_interactive_continues(monkeypatch):
    from agent import Discovery
    d = Discovery("zepto", FakePage([]), None, None)
    d.page.wait_for_load_state = lambda *a, **k: None
    d.page.evaluate = lambda *a: "same"
    def eof(*a):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert d.ask_user("captcha?") == {"answer": ""} and d.done is None


def test_unverified_success_is_refused(monkeypatch):
    from agent import Discovery
    d = Discovery("zepto", FakePage([]), None, type("Cap", (), {"items": []})())
    d.loads, d.refusals = 2, 2          # premature() passes
    monkeypatch.setattr(d, "verify", lambda q: 0.1)
    assert "independent check" in d.finish(True, "done")["error"]
    monkeypatch.setattr(d, "verify", lambda q: 0.9)
    assert d.finish(True, "done") == {"ok": True}


def test_unsure_verifier_cannot_block_forever(monkeypatch):
    from agent import Discovery
    d = Discovery("zepto", FakePage([]), None, type("Cap", (), {"items": []})())
    d.loads, d.refusals = 2, 2
    monkeypatch.setattr(d, "verify", lambda q: 0.1)
    assert "error" in d.finish(True, "done") and "error" in d.finish(True, "done")
    assert d.finish(True, "done") == {"ok": True}


def test_sibling_sites_cannot_share_an_endpoint(monkeypatch):
    import agent
    monkeypatch.setattr(agent.recipes, "load", lambda s: {"endpoint": "https://www.swiggy.com/dapi/order/all"}
                        if s == "swiggy" else None)
    assert agent.sibling_endpoint("instamart", {"endpoint": "https://www.swiggy.com/dapi/order/all?x=1"}) == "swiggy"
    assert agent.sibling_endpoint("instamart", {"endpoint": "https://www.swiggy.com/api/instamart/orders"}) is None

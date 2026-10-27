import contextlib

import agent
import recipes
import sync
from agent import Discovery
from test_login import FakePage


def fake_page():
    return FakePage([])


def test_ask_user_answer_reaches_history_with_secrets_masked():
    d = Discovery("zepto", fake_page(), None, None,
                  prompt=lambda q, kind="question": "click Orders, my number is 9876543210")
    d.secrets["PHONE"] = "9876543210"
    d.ask_user("stuck?")
    assert d.history[-1] == "user answered: click Orders, my number is <PHONE>"


def test_runs_dir_overrides_global_run_log(tmp_path):
    d = Discovery("zepto", fake_page(), None, None, runs_dir=tmp_path)
    d.run_log = None
    d.record({"step": 1})  # record() must create the file under runs_dir
    assert list(tmp_path.glob("zepto-learn-*.jsonl"))


def test_login_agent_accepts_runs_dir(tmp_path):
    la = agent.LoginAgent("zepto", fake_page(), None, None, "9876543210", runs_dir=tmp_path)
    assert la.runs_dir == tmp_path


def test_login_reports_orders_url_without_writing_hint(monkeypatch, tmp_path, capsys):
    url = "https://www.zepto.com/account/orders"
    monkeypatch.setattr(recipes, "RECIPES", tmp_path / "recipes")
    page = fake_page()
    page.url = url

    class FakeGuard:
        login_mode = True

    class FakeCapture:
        items = []

        def clear(self):
            pass

    @contextlib.contextmanager
    def fake_open_site(*a, **k):
        yield None, page, FakeGuard(), FakeCapture()

    seen_kwargs = {}

    class FakeLogin:
        def __init__(self, *a, **k):
            seen_kwargs.update(k)
            self.page = page
            self.secrets = {"PHONE": "9876543210", "OTP": "1234"}

        def run(self):
            return {"success": False, "note": "bad 9876543210 otp 1234", "url": url}

    monkeypatch.setattr(sync, "open_site", fake_open_site)
    monkeypatch.setattr(sync.browser, "goto", lambda p, u: None)
    monkeypatch.setattr(sync, "goto", lambda p, u: None)
    monkeypatch.setattr(sync.browser, "home_url", lambda *a, **k: url)
    monkeypatch.setattr(sync.browser, "effective_location", lambda loc: {})
    monkeypatch.setattr(agent, "LoginAgent", FakeLogin)
    monkeypatch.setattr(agent, "snapshot", lambda p: {"elements": []})

    # a failed agent run in unattended mode returns False; the note is printed with phone and OTP masked
    assert sync.login("zepto", phone="9876543210", prompt=lambda t, k: "", runs_dir=tmp_path) is False
    out = capsys.readouterr().out
    assert "9876543210" not in out and "1234" not in out and "<PHONE>" in out and "<OTP>" in out
    assert seen_kwargs.get("runs_dir") == tmp_path

    # a successful login hands the verified orders URL to the callback and leaves no hint file
    FakeLogin.run = lambda self: {"success": True, "note": "ok", "url": url}
    got = []
    assert sync.login("zepto", phone="9876543210", prompt=lambda t, k: "", on_orders_url=got.append) is True
    assert got == [url]
    assert not recipes.hint_path("zepto").exists()

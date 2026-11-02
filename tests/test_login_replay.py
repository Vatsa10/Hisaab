"""Learned login scripts: recorded from a fast-path/model login, validated, replayed with no model, and a
drifted page falls back to the model. Pages are local fixtures served through Playwright routing: no real site."""
import json
import logging
import re
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

import agent
import llm
import login_replay

FIX = Path(__file__).parent / "fixtures" / "login_replay"
PHONE = "9000000000"  # fake
OTP = "123456"  # fake
RECIPE = json.loads((FIX / "recipe.json").read_text(encoding="utf-8"))


def _step(**kw):
    return {"action": "click", "field": None, "role": "button", "name": "log in", "tag": "button",
            "input_type": None} | kw


# --- validator ----------------------------------------------------------------

def test_the_fixture_recipe_is_valid():
    assert login_replay.validate(RECIPE["steps"]) is None


@pytest.mark.parametrize("script", [
    [],                                                                          # empty
    RECIPE["steps"][:1] * 9,                                                     # too many steps
    [_step(action="navigate"), *RECIPE["steps"][1:]],                            # action not allowed
    [_step(name="https://www.zomato.com/login"), *RECIPE["steps"][1:]],         # a URL
    [_step(name="www.zomato.com"), *RECIPE["steps"][1:]],                       # a bare host
    [_step(name="call 98765 43210"), *RECIPE["steps"][1:]],                     # a phone-like digit run
    [_step(name="x" * 81), *RECIPE["steps"][1:]],                               # name too long
    [_step(name="Log In"), *RECIPE["steps"][1:]],                               # not normalised
    [_step(name="log out"), *RECIPE["steps"][1:]],                              # the click policy refuses it
    [RECIPE["steps"][0], RECIPE["steps"][1] | {"field": "OTP"}, RECIPE["steps"][2]],   # types an OTP
    [RECIPE["steps"][0], RECIPE["steps"][1] | {"value": PHONE}, RECIPE["steps"][2]],   # carries a value
    [RECIPE["steps"][0], RECIPE["steps"][2] | {"field": "PHONE"}, RECIPE["steps"][2]],  # click with a value
    RECIPE["steps"][:2],                                                         # never sends the OTP
    [RECIPE["steps"][0], RECIPE["steps"][2]],                                    # no phone step
    "not a list",
])
def test_the_validator_rejects_bad_recipes(script):
    assert login_replay.validate(script) is not None


def test_derive_stops_at_the_send_click_and_drops_a_location_login():
    el = lambda tag, text="", **kw: {"tag": tag, "text": text, "role": None, "label": None, "placeholder": None,
                                     "type": None} | kw
    steps = [login_replay.step_of("click", el("button", "Log in")),
             login_replay.step_of("type", el("input", type="tel", placeholder="Phone"), "PHONE"),
             login_replay.step_of("click", el("button", "Send One Time Password")),
             login_replay.step_of("click", el("a", "Help"))]
    assert login_replay.derive(steps) == RECIPE["steps"]
    located = [login_replay.step_of("type", el("input", placeholder="Search"), "LOCATION"), *steps]
    assert login_replay.derive(located) is None


def test_plain_words_for_the_approval_card():
    assert login_replay.make_recipe(RECIPE["steps"])["summary"] == [
        "Click the button 'log in'",
        "Type the user's phone number into the box 'phone'",
        "Click the button 'send one time password'",
    ]


# --- browser: local fixture pages -------------------------------------------------

pw_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture
def browser():
    pw = pw_api.sync_playwright().start()
    try:
        try:
            b = pw.chromium.launch()
        except Exception:
            pytest.skip("no browser installed")
        yield b
        b.close()
    finally:
        pw.stop()


def _page(browser, send="Send One Time Password"):
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    pages = {("www.zomato.com", "/"): (FIX / "home.html").read_text(encoding="utf-8"),
             ("www.zomato.com", "/orders"): (FIX / "orders.html").read_text(encoding="utf-8"),
             ("accounts.zomato.com", "/login"):
                 (FIX / "frame.html").read_text(encoding="utf-8").replace("{{SEND}}", send)}

    def handle(route):
        u = urlsplit(route.request.url)
        body = pages.get((u.hostname, u.path))
        if body is None:
            return route.abort()  # nothing ever leaves the machine
        route.fulfill(status=200, content_type="text/html", body=body)

    ctx.route("**/*", handle)
    page = ctx.new_page()
    page.goto("https://www.zomato.com/")
    return page


class _Models:
    """Jev fails every call (so it is counted, then Claude decides); Claude is a scripted fake."""

    def __init__(self, monkeypatch):
        self.jev = 0
        self.claude = 0
        models = self

        class Jev:
            def system_one(self, **_):
                models.jev += 1
                raise RuntimeError("fake Jev is offline")

        monkeypatch.setattr(agent, "jev", lambda: Jev())
        monkeypatch.setattr(llm, "create", self.create)

    def create(self, *a, messages, **k):
        self.claude += 1
        text = messages[0]["content"]
        state = json.loads(text.split("STATE:\n", 1)[1].split("\n\nCLICKABLE ELEMENTS:", 1)[0])
        lines = text.split("CLICKABLE ELEMENTS:\n", 1)[1].split("\n\nCall exactly", 1)[0].splitlines()

        def ref(pattern):
            return next((ln.split(": ", 1)[0] for ln in lines if re.search(pattern, ln, re.I)), None)

        def call(name, **args):
            return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=args)])

        prog = state["progress"]
        if prog["otp_entered"]:
            return call("finish", success=True, note="orders shown")
        if not prog["phone_typed"]:
            if r := ref(r"tel input"):
                return call("type_value", element_ref=r, field="PHONE")
            return call("wait")
        if r := ref(r"button '(get code|send one time password)'"):
            return call("click", element_ref=r)
        return call("wait")


def _agent(page, prompt, **kw):
    return agent.LoginAgent("zomato", page, None, SimpleNamespace(items=[]), PHONE, prompt=prompt, **kw)


def test_a_fast_path_login_records_a_script_without_the_phone_or_otp(browser, monkeypatch, tmp_path):
    models = _Models(monkeypatch)
    la = _agent(_page(browser), lambda text, kind: OTP, runs_dir=tmp_path)
    done = la.run()
    assert done["success"], done
    script, drifted = la.learned_script()
    assert script == RECIPE["steps"] and drifted is False
    blob = json.dumps(script)
    assert PHONE not in blob and OTP not in blob and "zomato.com" not in blob
    assert login_replay.validate(script) is None
    assert models.claude >= 1  # only after the OTP: the orders page is still found by the model


def test_an_approved_script_replays_to_the_otp_prompt_with_no_model(browser, monkeypatch, tmp_path, caplog):
    models = _Models(monkeypatch)
    at_prompt = {}
    page = _page(browser)
    t0 = time.monotonic()

    def prompt(text, kind):
        at_prompt.update(jev=models.jev, claude=models.claude, s=time.monotonic() - t0, kind=kind)
        raise TimeoutError  # the test stops at the OTP prompt

    la = _agent(page, prompt, runs_dir=tmp_path, login_recipe=RECIPE)
    with caplog.at_level(logging.INFO):
        la.run()
    assert at_prompt["kind"] == "otp"
    assert at_prompt["jev"] == 0 and at_prompt["claude"] == 0
    assert at_prompt["s"] < 8
    assert la.replay_state == "replayed" and la.learned_script() is None
    assert any("replayed login:" in r.getMessage() and "to OTP prompt" in r.getMessage() for r in caplog.records)
    assert all(PHONE not in r.getMessage() for r in caplog.records)


def test_a_renamed_button_falls_back_to_the_model_and_records_a_replacement(browser, monkeypatch, tmp_path, caplog):
    models = _Models(monkeypatch)
    la = _agent(_page(browser, send="Get code"), lambda text, kind: OTP, runs_dir=tmp_path, login_recipe=RECIPE)
    with caplog.at_level(logging.INFO):
        done = la.run()
    assert done["success"], done
    assert la.replay_state == "drift"
    assert any("login replay fell back at step 3" in r.getMessage() for r in caplog.records)
    assert models.claude >= 2  # the send click and the finish came from the model
    script, drifted = la.learned_script()
    assert drifted is True and script[-1]["name"] == "get code"
    assert PHONE not in json.dumps(script) and OTP not in json.dumps(script)
    assert all(PHONE not in r.getMessage() and OTP not in r.getMessage() for r in caplog.records)


def test_dry_run_replay_still_stops_at_the_phone_input(browser, monkeypatch, tmp_path):
    models = _Models(monkeypatch)
    la = _agent(_page(browser), lambda text, kind: OTP, runs_dir=tmp_path, login_recipe=RECIPE, dry_run=True)
    done = la.run()
    assert done["success"] and "dry run" in done["note"]
    assert not la.phone_typed and models.jev == 0 and models.claude == 0
    assert la.page.locator("iframe").count() == 1


def test_a_replay_that_drifts_is_tried_only_once(browser, monkeypatch, tmp_path):
    _Models(monkeypatch)
    la = _agent(_page(browser, send="Get code"), lambda text, kind: OTP, runs_dir=tmp_path, login_recipe=RECIPE)
    calls = []
    real = la.replay
    monkeypatch.setattr(la, "replay", lambda: calls.append(1) or real())
    la.run()
    assert calls == [1]

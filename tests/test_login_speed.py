"""Login speed: settle before asking Claude, deterministic phone/login fast paths, timing marks, no secrets in logs."""
import logging
from types import SimpleNamespace

import pytest

import agent
import llm
from server import timing

PHONE = "9000000000"  # fake


def _el(ref, tag="button", text="", **kw):
    base = {"ref": ref, "tag": tag, "role": None, "text": text, "label": None, "href": None, "placeholder": None,
            "type": None, "name": None, "autocomplete": None, "maxlength": None, "filled": None if tag != "input"
            else False, "focused": False, "where": "middle-center", "in_form": False}
    return base | kw


class _Page:
    url = "https://www.zomato.com/"

    def __init__(self):
        self.sigs = iter(["a", "b", "b", "b"] + ["b"] * 50)
        self.main_frame = SimpleNamespace(evaluate=lambda js: False)  # no OTP boxes on screen
        self.frames = [self.main_frame]

    def evaluate(self, js, *a):
        return next(self.sigs)

    def wait_for_timeout(self, ms):
        pass

    def wait_for_load_state(self, *a, **k):
        pass

    def title(self):
        return "Zomato"


def _choice(choice, conf):
    return SimpleNamespace(choice=choice, confidence=conf, probabilities={choice: conf})


class _Jev:
    """Unsure on the first look, sure afterwards."""

    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def system_one(self, state, questions, timeout):
        self.calls += 1
        kind, conf, item = self.script.pop(0) if self.script else ("done", 0.9, None)
        answers = {"kind": _choice(kind, conf)}
        if "item" in questions:
            answers["item"] = _choice(item or next(iter(questions["item"].criteria)), 0.9)
        return SimpleNamespace(answers=answers)


def _agent(monkeypatch, snaps, jev_script, **kw):
    page = _Page()
    la = agent.LoginAgent("zomato", page, None, SimpleNamespace(items=[]), PHONE, runs_dir=None, **kw)
    snaps = iter(snaps)
    state = {"snap": None}

    def observe(self=la):
        state["snap"] = next(snaps, state["snap"])
        self.snap = state["snap"]
        self.refs = {e["ref"]: e for e in self.snap["elements"]}
        self.on_observe()

    monkeypatch.setattr(la, "observe", observe)
    monkeypatch.setattr(la, "record", lambda e: None)
    fake = _Jev(jev_script)
    monkeypatch.setattr(agent, "jev", lambda: fake)
    monkeypatch.setattr(agent, "settle", lambda *a, **k: None)
    claude = []

    def create(*a, **k):
        claude.append(1)
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="wait", input={})])

    monkeypatch.setattr(llm, "create", create)
    return la, fake, claude


def _snap(*els, overlay=False):
    return {"url": "https://www.zomato.com/", "title": "Zomato", "elements": list(els), "overlay": overlay, "aria": ""}


def test_unsure_after_a_click_settles_and_relooks_instead_of_asking_claude(monkeypatch):
    # Jev: click a link (sure) -> unsure (page still loading) -> sure -> done
    other = _snap(_el("e1", "a", "Offers", href="/offers"), _el("e2", "a", "Dining", href="/dining"))
    la, fake, claude = _agent(monkeypatch, [other] * 6,
                              [("click", 0.9, "e1"), ("wait", 0.1, None), ("scroll", 0.9, None), ("done", 0.9, None)])
    monkeypatch.setattr(la, "click", lambda element_ref: {"ok": True})
    monkeypatch.setattr(la, "scroll", lambda: {"ok": True})
    monkeypatch.setattr(la, "finish", lambda success, note: setattr(la, "done", {"success": success}) or {"ok": 1})
    monkeypatch.setattr(agent, "MAX_STEPS", 3)
    la.run()
    assert claude == []  # before this change: 1 Claude call for the unsure look
    assert fake.calls == 4


def test_still_unsure_after_settling_asks_claude_once(monkeypatch):
    other = _snap(_el("e1", "a", "Offers", href="/offers"))
    la, fake, claude = _agent(monkeypatch, [other] * 6, [("click", 0.9, "e1"), ("wait", 0.1, None), ("wait", 0.1, None)])
    monkeypatch.setattr(la, "click", lambda element_ref: {"ok": True})
    monkeypatch.setattr(agent, "MAX_STEPS", 2)
    la.run()
    assert claude == [1]


def test_settle_dom_stops_when_signature_is_stable():
    page = _Page()
    waited = agent.settle_dom(page, max_ms=2000, poll_ms=150)
    assert waited <= 600


def test_phone_fast_path_types_then_sends_without_any_model(monkeypatch):
    phone = _el("e1", "input", type="tel", placeholder="Phone")
    send = _el("e2", "button", "Send One Time Password")
    la, fake, claude = _agent(monkeypatch, [_snap(phone, send), _snap(_el("e1", "input", type="tel", filled=True), send)],
                              [])
    typed, clicked = [], []

    def type_value(element_ref, field):
        typed.append((element_ref, field))
        la.phone_typed = True
        return {"typed": f"<{field}>"}

    monkeypatch.setattr(la, "type_value", type_value)
    monkeypatch.setattr(la, "click", lambda element_ref: clicked.append(element_ref) or {"ok": True})
    monkeypatch.setattr(agent, "MAX_STEPS", 2)
    la.run()
    assert typed == [("e1", "PHONE")] and clicked == ["e2"]
    assert fake.calls == 0 and claude == []


def test_login_button_fast_path_clicks_when_no_phone_input(monkeypatch):
    la, fake, claude = _agent(monkeypatch, [_snap(_el("e1", "a", "Log in"), _el("e2", "a", "Offers"))], [])
    clicked = []
    monkeypatch.setattr(la, "click", lambda element_ref: clicked.append(element_ref) or {"ok": True})
    monkeypatch.setattr(agent, "MAX_STEPS", 1)
    la.run()
    assert clicked == ["e1"] and fake.calls == 0 and claude == []


@pytest.mark.parametrize("snap", [
    _snap(_el("e1", "input", type="tel"), _el("e2", "input", name="mobile")),                 # two phone inputs
    _snap(_el("e1", "a", "Log in"), _el("e2", "button", "Sign in")),                            # two login buttons
])
def test_ambiguous_pages_fall_back_to_the_model(monkeypatch, snap):
    la, fake, claude = _agent(monkeypatch, [snap], [])
    assert la.fast_decision() is None


def test_send_ambiguity_falls_back(monkeypatch):
    la, *_ = _agent(monkeypatch, [], [])
    la.phone_typed = True
    la.snap = _snap(_el("e1", "button", "Continue"), _el("e2", "button", "Next"))
    la.refs = {e["ref"]: e for e in la.snap["elements"]}
    assert la.fast_decision() is None


def test_dry_run_still_stops_at_the_phone_input(monkeypatch):
    phone = _el("e1", "input", type="tel")
    la, fake, claude = _agent(monkeypatch, [_snap(phone, _el("e2", "button", "Continue"))], [], dry_run=True)
    monkeypatch.setattr(agent, "MAX_STEPS", 3)
    done = la.run()
    assert done["success"] and "dry run" in done["note"] and not la.phone_typed


def test_otp_prompt_mark_and_log_line_carry_no_secrets(monkeypatch, caplog):
    rec = timing.Recorder()
    la, *_ = _agent(monkeypatch, [], [], prompt=lambda q, kind: "")
    la.phone_typed = True
    with timing.recording(rec), caplog.at_level(logging.INFO):
        la.enter_otp(SimpleNamespace())
    assert "login_otp_prompt" in rec.phase_ms()
    assert any("to OTP prompt" in r.getMessage() for r in caplog.records)
    assert all(PHONE not in r.getMessage() for r in caplog.records)


def test_mark_is_a_noop_outside_a_job():
    timing.mark("login_phone", 1.5)

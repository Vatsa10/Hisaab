import json

import agent
from agent import Discovery
from test_login import FakePage


def test_mask_log_hides_digit_runs_and_secrets():
    assert agent.mask_log("otp 482910, call 98765 43210 or 2", {"PHONE": "9999999999"}) == "otp <DIGITS>, call <DIGITS> or 2"


def test_mask_log_masks_secret_first():
    assert agent.mask_log("me 9999999999", {"PHONE": "9999999999"}) == "me <PHONE>"


def test_record_masks_user_answer_digits(tmp_path):
    d = Discovery("zepto", FakePage([]), None, None, runs_dir=tmp_path, prompt=lambda q, kind="question": "it's 4821")
    d.ask_user("captcha?")
    d.record({"step": 1, "outcome": "user answered: it's 4821"})
    text = next(tmp_path.glob("zepto-learn-*.jsonl")).read_text(encoding="utf-8")
    assert "<DIGITS>" in text and "4821" not in text
    assert json.loads(text)["step"] == 1
    assert d.history[-1] == "user answered: it's 4821"

import pytest

import browser


class _Page:
    def __init__(self, text):
        self.text = text

    def evaluate(self, js, arg=None):
        return self.text


@pytest.mark.parametrize("text", [
    "Just a moment...", "Checking your browser before accessing", "Please Verify you are human",
    "Press & Hold to confirm", "Enable JavaScript and cookies to continue"])
def test_visible_text_interstitials_are_challenges(text):
    with pytest.raises(browser.SiteThrottled) as e:
        browser.raise_if_blocked(_Page(f"Title\n{text}"))
    assert e.value.kind == "challenge"
    assert browser.classify_block(200, text) == "challenge"


@pytest.mark.parametrize("text", [
    "Ordered Just a moment ago", "Please verify your phone number", "Your order, just a moment!",
    "Press and hold the button", "Checking your order status"])
def test_ordinary_text_is_not_a_challenge(text):
    browser.raise_if_blocked(_Page(text))

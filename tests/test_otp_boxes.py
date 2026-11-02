"""The OTP step is recognised from a row of short empty boxes, including Zomato's two-character ones."""
import pytest

import agent

playwright = pytest.importorskip("playwright.sync_api")


def _found(html):
    pw = playwright.sync_playwright().start()
    try:
        try:
            b = pw.chromium.launch()
        except Exception:
            pytest.skip("no browser installed")
        try:
            p = b.new_page()
            p.set_content(html)
            return p.evaluate(agent._FIND_OTP_JS)
        finally:
            b.close()
    finally:
        pw.stop()


def _boxes(n, maxlength, extra=""):
    return "".join(f'<input type="text" maxlength="{maxlength}" {extra}>' for _ in range(n))


@pytest.mark.parametrize("n,maxlength", [(4, 1), (6, 1), (4, 2), (6, 2)])
def test_a_row_of_short_boxes_is_an_otp_entry(n, maxlength):
    assert _found(_boxes(n, maxlength)) is True


@pytest.mark.parametrize("html", [
    _boxes(1, 2),                       # a lone country-code box
    _boxes(3, 2),                       # too few
    _boxes(9, 1),                       # too many to be an OTP
    '<input type="text" maxlength="10"><input type="text" maxlength="10">',  # phone and name
    _boxes(4, 2, 'value="1"'),          # already filled
])
def test_other_forms_are_not_an_otp_entry(html):
    assert _found(html) is False

"""Every script we inject into a page must parse in a real browser. A Python escape slip (a '\n' that becomes a real
line break inside a JS string) only shows up there: the fake-page tests stub evaluate, so nothing else catches it."""
import pytest

import agent
import browser
import cart_learn

playwright = pytest.importorskip("playwright.sync_api")

SCRIPTS = {name: getattr(mod, name)
           for mod in (agent, browser, cart_learn)
           for name in dir(mod)
           if name.endswith("_JS") and isinstance(getattr(mod, name), str)}

CHECK = """(src) => { try { (0, eval)('(' + src + ')'); return null; } catch (e) { return String(e.message || e); } }"""


def test_the_scripts_were_found():
    assert {"_SNAPSHOT_JS", "_FIND_OTP_JS", "_MARK_PAGER_JS", "_PAGE_BLOCK_JS", "_FETCH_JS"} <= set(SCRIPTS)


def test_every_injected_script_parses_in_a_browser():
    pw = playwright.sync_playwright().start()
    try:
        try:
            b = pw.chromium.launch()
        except Exception:
            pytest.skip("no browser installed")
        try:
            page = b.new_page()
            page.set_content("<html><title>t</title><body>hello</body></html>")
            bad = {name: err for name, src in SCRIPTS.items() if (err := page.evaluate(CHECK, src))}
            assert not bad, bad
        finally:
            b.close()
    finally:
        pw.stop()


def test_the_checker_catches_the_slip_it_exists_for():
    pw = playwright.sync_playwright().start()
    try:
        try:
            b = pw.chromium.launch()
        except Exception:
            pytest.skip("no browser installed")
        try:
            page = b.new_page()
            assert page.evaluate(CHECK, "() => 'a" + chr(10) + "b'")  # a real line break inside a JS string
            assert page.evaluate(CHECK, "() => 'a" + chr(92) + "nb'") is None
        finally:
            b.close()
    finally:
        pw.stop()


def test_the_block_scan_runs_and_sees_title_and_body():
    pw = playwright.sync_playwright().start()
    try:
        try:
            b = pw.chromium.launch()
        except Exception:
            pytest.skip("no browser installed")
        try:
            page = b.new_page()
            page.set_content("<html><title>Access Denied</title><body>Reference #18.abc</body></html>")
            with pytest.raises(browser.SiteThrottled):
                browser.raise_if_blocked(page)
            page.set_content("<html><title>Your orders</title><body>Pizza</body></html>")
            browser.raise_if_blocked(page)  # a normal page passes
        finally:
            b.close()
    finally:
        pw.stop()

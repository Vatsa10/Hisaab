"""Browser agents: Discovery (finds the order-history API) and LoginAgent (phone + OTP).

Each step: observe only what a user could actually hit -> code-level checks (OTP field, logged out)
-> one fast Jev decision (kind + item, ~0.5 s) -> act -> wait for the DOM to settle.
Claude (one tool call through llm.py) is the fallback when Jev is unavailable, unsure, or repeating something that just failed.
"""
import functools
import json
import logging
import os
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

import llm
import recipes
from browser import SITES, SessionExpired, fetch_in_page, goto, home_url, scrub_headers
from guard import BLOCKED_URL, redact_url

log = logging.getLogger("agent")

MAX_STEPS = 40
TIME_BUDGET_S = 300
# Pointer's asymmetric floors: an action that lands somewhere (click/type) needs more confidence than one that
# can't do harm (scroll/wait/Escape). Below the floor one Claude call decides the step.
JEV_MIN_CONFIDENCE = 0.4
JEV_SAFE_MIN_CONFIDENCE = 0.3
SAFE_KINDS = {"scroll", "wait", "close_popup"}
RUNS = Path(__file__).parent / "runs"
_WRITE_WORDS = (r"add|cart|checkout|pay\w*|place order|cancel\w*|reorder|repeat|buy again|delete|remove|"
                r"log ?out|sign ?out|address\w*|rate|rating|review|download|invoice|chat|support")
CLICK_BLOCK = re.compile(rf"\b({_WRITE_WORDS}|submit|confirm)\b", re.I)
LOGIN_CLICK_BLOCK = re.compile(rf"\b({_WRITE_WORDS})\b", re.I)  # login needs "Continue"/"Verify" buttons
LOGIN_BUTTON = re.compile(r"^\s*(log ?in|sign ?in|log ?in\s*/\s*sign ?up|sign ?in\s*/\s*sign ?up|login or signup)\s*$", re.I)
BLOCK_TITLE = re.compile(r"access denied|forbidden|403|not authori[sz]ed", re.I)
BLOCK_BODY = re.compile(r"max challenge attempts|access denied|you don't have authori[sz]ation|request blocked", re.I)
MORE_BUTTON = re.compile(r"^\s*((load|show|view|see)\s+more(\s+orders)?|next( page)?|older orders|previous orders|[›»>])\s*$",
                         re.I)
PAGE_NUMBER = re.compile(r"^\s*([2-9]|[1-9]\d)\s*$")  # numbered pagers (Zomato: 1 2 3 ... 18)


@functools.cache
def jev():
    """TypeSafe Jev client, or None (no key / SDK) -> every step uses Claude."""
    load_dotenv()
    if not os.getenv("TYPESAFE_API_KEY"):
        log.info("TYPESAFE_API_KEY not set: using Claude for every step")
        return None
    try:
        from typesafe_sdk import TypeSafeClient
        return TypeSafeClient()
    except Exception as e:
        log.warning("Jev unavailable (%s): using Claude for every step", e)
        return None


def mask(text: str, secrets: dict[str, str]) -> str:
    """Hide typed phone/OTP values from anything sent to a model or the logs."""
    for name, value in secrets.items():
        if value:
            text = text.replace(value, f"<{name}>")
    return text


# --- page helpers (also used by sync replay) --------------------------------

# Only elements a user could actually hit: the centre point must land on the element itself, so things
# behind a modal/overlay are dropped. While an overlay is up, below-the-fold elements are dropped too.
_SNAPSHOT_JS = """(prefix) => {
  document.querySelectorAll('[data-ea-ref]').forEach(e => e.removeAttribute('data-ea-ref'));
  const vw = innerWidth, vh = innerHeight;
  const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  const hit = el => { const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x >= vw || y >= vh) return null;          // off screen: unknown
    const t = document.elementFromPoint(x, y);
    return !!t && (t === el || el.contains(t) || t.contains(el)); };
  const where = el => { const r = el.getBoundingClientRect();
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (y >= vh) return 'below the fold';
    if (y < 0) return 'above (scrolled past)';
    return ['top', 'middle', 'bottom'][Math.min(2, Math.floor(3 * y / vh))] + '-' +
           ['left', 'center', 'right'][Math.min(2, Math.max(0, Math.floor(3 * x / vw)))]; };
  const els = new Set(document.querySelectorAll('a[href],button,input:not([type=hidden]),textarea,select,' +
    '[role=button],[role=link],[role=tab],[role=menuitem],[role=option],[onclick]'));
  for (const el of document.querySelectorAll('body *')) {   // SPA divs that only look clickable
    if (getComputedStyle(el).cursor !== 'pointer' || !el.parentElement) continue;
    // Outermost clickable box, plus clickable children with their own text: a dropdown such as
    // "Profile | Reviews | Settings | Log out" must offer each item, not only the whole menu.
    const own = (el.innerText || '').trim(), parentText = (el.parentElement.innerText || '').trim();
    if (getComputedStyle(el.parentElement).cursor !== 'pointer' || (own && own !== parentText)) els.add(el);
  }
  const kept = []; let covered = 0;
  for (const el of els) {
    if (!visible(el)) continue;
    const h = hit(el);
    if (h === false) { covered++; continue; }
    kept.push([el, h === null]);
  }
  const out = []; let n = 0;
  for (const [el, off] of kept) {
    if (off && covered > 3) continue;
    const ref = prefix + 'e' + (++n);
    el.setAttribute('data-ea-ref', ref);
    const tag = el.tagName.toLowerCase(), isInput = tag === 'input' || tag === 'textarea';
    out.push({ref, tag, role: el.getAttribute('role'),
      text: isInput ? '' : (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 80),
      // icon-only controls inherit a label from their image/svg (Pointer's label inheritance)
      label: el.getAttribute('aria-label') || el.getAttribute('title') ||
             (el.querySelector('img[alt]') || {}).alt || (el.querySelector('svg title') || {}).textContent || null,
      href: el.getAttribute('href'),
      placeholder: el.getAttribute('placeholder'), type: el.getAttribute('type'),
      name: isInput ? (el.getAttribute('name') || el.id || null) : null,
      autocomplete: isInput ? el.getAttribute('autocomplete') : null,
      maxlength: isInput && el.maxLength > 0 ? el.maxLength : null,
      filled: isInput ? !!el.value : null, focused: el === document.activeElement,
      where: where(el), in_form: !!el.closest('form')});
    if (n >= 150) break;
  }
  return {elements: out, overlay: covered > 3};
}"""

# OTP field: autocomplete=one-time-code, an otp/code-ish name, or a row of >=4 single-character boxes.
_FIND_OTP_JS = """() => {
  document.querySelectorAll('[data-ea-otp]').forEach(e => e.removeAttribute('data-ea-otp'));
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 &&
    getComputedStyle(el).visibility !== 'hidden'; };
  const ins = [...document.querySelectorAll('input')].filter(i => vis(i) && !['hidden','checkbox','radio'].includes(i.type));
  const words = i => [i.name, i.id, i.placeholder, i.getAttribute('aria-label'), i.getAttribute('data-testid')].join(' ');
  let otp = ins.find(i => i.autocomplete === 'one-time-code' || /otp|one.?time|verif|passcode/i.test(words(i)));
  if (!otp) { const singles = ins.filter(i => i.maxLength === 1); if (singles.length >= 4) otp = singles[0]; }
  if (!otp || otp.value) return false;
  otp.setAttribute('data-ea-otp', '1');
  return true;
}"""

_DOM_SIG_JS = "() => document.body ? document.body.innerText.length + ':' + document.getElementsByTagName('*').length : ''"


_REF_FRAMES: dict[str, object] = {}  # ref -> Playwright Frame, rebuilt by every snapshot()


def _site_key(url: str) -> str:
    return ".".join((urlsplit(url).hostname or "").split(".")[-2:])


def frames(page) -> list[tuple[str, object]]:
    """Main frame plus visible same-site child frames - login forms live in iframes on some sites
    (Zomato's accounts.zomato.com). Ads and third-party widgets are ignored."""
    out = [("", page.main_frame)]
    for i, f in enumerate(page.frames[1:], 1):
        try:
            if f.is_detached() or _site_key(f.url) != _site_key(page.url):
                continue
            box = f.frame_element().bounding_box()
            if box and box["width"] > 50 and box["height"] > 50:
                out.append((f"f{i}", f))
        except Exception:
            continue
    return out


def snapshot(page) -> dict:
    elements, overlay, aria = [], False, []
    _REF_FRAMES.clear()
    for prefix, frame in frames(page):
        try:
            found = frame.evaluate(_SNAPSHOT_JS, prefix)
        except Exception:
            if not prefix:
                raise
            continue
        for e in found["elements"]:
            _REF_FRAMES[e["ref"]] = frame
        elements += found["elements"]
        overlay = overlay or found["overlay"] or bool(prefix)  # a visible same-site iframe is a dialog
        try:
            aria.append(frame.locator("body").aria_snapshot(timeout=3000))
        except Exception:
            pass
    return {"url": page.url, "title": page.title(), "elements": elements[:150],
            "overlay": overlay, "aria": "\n".join(reversed(aria))[:6000]}  # dialog text first


def locate(page, ref: str):
    """Locator for a snapshot ref, in whichever frame the element lives."""
    return _REF_FRAMES.get(ref, page.main_frame).locator(f'[data-ea-ref="{ref}"]').first


def shows_login_button(elements: list[dict]) -> bool:
    return any(LOGIN_BUTTON.match(e.get("text") or e.get("label") or "") for e in elements)


def settle(page, max_ms: int = 3000) -> None:
    """Wait until the DOM stops changing (two equal signatures 250 ms apart) instead of a fixed sleep."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=3000)
    except Exception:
        pass
    last, waited = None, 0
    while waited < max_ms:
        page.wait_for_timeout(250)
        waited += 250
        try:
            sig = page.evaluate(_DOM_SIG_JS)
        except Exception:  # navigation in progress
            continue
        if sig == last and waited >= 500:
            return
        last = sig


def click_ref(page, ref: str) -> str:
    """Click like a user: normal click, else a real mouse click at the element's centre, else a DOM click."""
    loc = locate(page, ref)
    try:
        loc.click(timeout=2500)
        how = "click"
    except Exception:
        try:
            loc.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass
        box = loc.bounding_box(timeout=1500)
        if box:
            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            how = "mouse"
        else:
            loc.dispatch_event("click", timeout=1500)
            how = "dom"
    settle(page)
    return how


def click_refusal(el: dict, login: bool = False) -> str | None:
    """Why an element must not be clicked, or None if it's safe. Login may submit its own form."""
    blob = " ".join(str(el.get(k) or "") for k in ("text", "label"))
    if (LOGIN_CLICK_BLOCK if login else CLICK_BLOCK).search(blob):
        return f"text looks like a write action: {blob[:60]!r}"
    if el.get("href") and BLOCKED_URL.search(el["href"]):
        return "link points to a blocked URL"
    if not login and (el.get("type") == "submit" or (el.get("in_form") and el.get("tag") in ("button", "input")
                                                     and el.get("type") != "button")):
        return "would submit a form"
    return None


def site_allows_host(site: str, url: str) -> bool:
    base = urlsplit(SITES[site]["home"]).hostname.removeprefix("www.")
    host = urlsplit(url).hostname or ""
    return host == base or host.endswith("." + base)


def wheel(page) -> None:
    vp = page.viewport_size or {"width": 1280, "height": 900}
    page.mouse.move(vp["width"] / 2, vp["height"] / 2)
    page.mouse.wheel(0, 2500)
    settle(page, 2000)


def describe(el: dict) -> str:
    """One short line per element, like `button 'Login' (top-right)`."""
    is_input = el["tag"] in ("input", "textarea")
    kind = f"{el.get('type') or 'text'} input" if is_input else (el.get("role") or el["tag"])
    name = (el.get("text") or el.get("label") or el.get("placeholder") or el.get("name")
            or (el.get("href") or "")[:60] or "(no label)")
    extras = [el["where"]] if el.get("where") else []
    if is_input:
        if el.get("placeholder") and name != el["placeholder"]:
            extras.append(f"placeholder {el['placeholder']!r}")
        if el.get("autocomplete"):
            extras.append(f"autocomplete {el['autocomplete']}")
        if el.get("maxlength"):
            extras.append(f"maxlength {el['maxlength']}")
        extras.append("filled" if el.get("filled") else "empty")
    if el.get("focused"):
        extras.append("focused")
    if el.get("href") and not is_input and name != el["href"][:60] and urlsplit(el["href"]).path.strip("/"):
        extras.append(f"-> {urlsplit(el['href']).path[:50]}")
    return f"{kind} {name[:70]!r} ({'; '.join(extras)})"


# --- the step loop -----------------------------------------------------------

_fn = llm.tool  # strict Claude tools: the input always matches the schema

_REF = {"element_ref": {"type": "string"}}
TOOLS = [
    _fn("navigate", "Open a URL on this site.", {"url": {"type": "string"}}, ["url"]),
    _fn("click", "Click an element by ref.", _REF, ["element_ref"]),
    _fn("scroll", "Scroll down to load more content."),
    _fn("close_popup", "Press Escape to dismiss a popup/overlay."),
    _fn("wait", "Wait for the page to finish loading or changing."),
    _fn("list_captured_json", "JSON API responses captured so far (id, method, url, status, size, preview)."),
    _fn("ask_user", "Ask the user one short question in their terminal when stuck (CAPTCHA, unclear page). "
        "They can answer or fix it in the browser.", {"question": {"type": "string"}}, ["question"]),
    _fn("finish", "Stop: goal reached, or impossible.",
        {"success": {"type": "boolean"}, "note": {"type": "string"}}, ["success", "note"]),
]

COMMON_KINDS = {
    "click": "Click one element on the page (chosen in the item question).",
    "close_popup": "Press Escape to dismiss a popup, banner or overlay that is in the way and not needed.",
    "scroll": "Scroll down to reveal more of the page or load more items.",
    "wait": "Nothing to do yet: the page is still loading or changing.",
    "stuck": ("Nothing on screen helps any more: a CAPTCHA, an error page, or every route was already tried "
              "(the user is asked for help)."),
}

SYSTEM = """You operate a real browser that is logged in to the user's own {site} account.
Goal: reach the ORDER HISTORY page and load at least 2 more pages of past orders: scroll, or click a
"Load More" / "Show more" button, or the next page number ("2", then "3") / "Next" of a numbered pager at the
bottom of the list - scrolling alone does not load more on such pages.
Rules: you are strictly read-only. Never click anything that adds to cart, reorders, pays, cancels, rates,
changes addresses, downloads invoices, or logs out. There is no typing tool.
Order history usually lives under an account/profile menu (often an icon top-right).
If the site shows you are logged out (a Login/Sign in button instead of an account menu), call
finish(success=false, note="LOGGED_OUT").
{popups}"""

POPUPS = """Popups: the element list only contains what can be clicked right now; if a modal is open, only its
contents are listed. Location popups: the browser already shares the user's location (permission granted), so
click "Detect my location" / "Use current location" / "Locate me". Otherwise use close_popup or a close (x) button.
Never repeat an action that just failed; try a different element or route."""


class Discovery:
    SYSTEM, TOOLS, login = SYSTEM, TOOLS, False
    KINDS = COMMON_KINDS | {
        "done": ("The order history list is shown and further pages of orders were already loaded (via scrolling, "
                 "a 'Load More' button, or clicking page '2' / 'Next' of a numbered pager) at least twice."),
        "logged_out": "The site shows the user is logged out: a Login/Sign in button instead of an account menu.",
    }
    GOAL = "Open this site's order history page and load more past orders (scroll / show more) at least twice."

    def __init__(self, site, page, guard, capture, prompt: Callable[[str, str], str] | None = None,
                 home: str | None = None, runs_dir: Path | None = None):
        self.site, self.page, self.guard, self.capture = site, page, guard, capture
        self._prompt = prompt
        self.runs_dir = runs_dir  # per-run step-log dir (cloud worker); None: the global RUNS
        self.run_log: Path | None = None
        self.home = home  # start page for recovery; None: home_url from the CLI's saved location
        self.refs: dict[str, dict] = {}
        self.snap: dict = {"elements": [], "aria": "", "overlay": False}
        self.trace: list[dict] = []
        self.history: list[str] = []
        self.secrets: dict[str, str] = {}
        self.giveups_left = 2
        self.refusals = 0
        self.waits = 0
        self.loads = 0
        self.force_llm = False
        self.last_key = None
        self.asks = 0
        self.verify_refusals = 0
        self.last_probs: dict = {}
        if goal := SITES[site].get("goal"):
            self.GOAL = f"{self.GOAL} Target: {goal}"
        self.done = None

    # --- actions (also exposed to Claude as tools) ---

    def prompt(self, text: str, kind: str = "question") -> str:
        """Every human-input point goes through here. kind is "otp" or "question"; an injected prompt gets it,
        the CLI default is builtins.input (looked up per call, so patchable)."""
        if self._prompt is None:
            return input(text)
        return self._prompt(text, kind)

    def ask_user(self, question: str):
        """Friday's computer_ask: hand a question to the human instead of failing, then carry on."""
        self.asks += 1
        if self.asks > 3:
            return {"error": "already asked the user 3 times; finish(success=false) if still stuck"}
        try:
            answer = self.prompt(f"\n[{self.site}] The agent needs help: {question}\n"
                                 "Answer, or fix it in the browser and press Enter (type 'stop' to give up): ",
                                 "question").strip()
        except EOFError:  # non-interactive run
            answer = ""
        except TimeoutError:  # unattended run: nobody answered in time
            self.done = {"success": False, "note": "ask_timeout", "url": self.page.url}
            return {"ok": True}
        if answer.lower() == "stop":
            self.done = {"success": False, "note": "user stopped the agent", "url": self.page.url}
            return {"ok": True}
        settle(self.page)
        # The model gets the answer text, with the phone/OTP masked out.
        self.history.append(f"user answered: {mask(answer, self.secrets)}" if answer
                            else "user answered: (no answer; may have acted in the browser)")
        return {"answer": answer}

    def follow_new_tab(self) -> None:
        """Links with target=_blank open a new tab: continue there. A closed tab falls back to the last open one."""
        pages = [p for p in self.page.context.pages if not p.is_closed()]
        if pages and (self.page.is_closed() or pages[-1] is not self.page) and pages[-1].url != "about:blank":
            self.page = pages[-1]
            self.page.bring_to_front()
            settle(self.page)

    def _record(self, step: dict) -> None:
        step["url_after"] = urlsplit(self.page.url).path
        self.trace.append(step)

    def navigate(self, url: str):
        if BLOCKED_URL.search(url):
            return {"error": "URL is on the blocklist"}
        if not site_allows_host(self.site, url):
            return {"error": "only this site's own domain is allowed"}
        goto(self.page, url)
        settle(self.page)
        self._record({"action": "navigate", "url": url})
        return {"url": self.page.url}

    def click(self, element_ref: str):
        el = self.refs.get(element_ref)
        if not el:
            return {"error": "unknown ref"}
        if why := click_refusal(el, self.login):
            return {"error": f"refused: {why}"}
        how = click_ref(self.page, element_ref)
        self.follow_new_tab()
        if MORE_BUTTON.search(el.get("text") or "") or PAGE_NUMBER.match(el.get("text") or ""):
            self.loads += 1
        self._record({"action": "click", "target": {k: el.get(k) for k in ("tag", "text", "label", "href")}})
        return {"url": self.page.url, "clicked_via": how}

    def scroll(self):
        wheel(self.page)
        self.loads += 1
        self._record({"action": "scroll"})
        return {"url": self.page.url, "captured": len(self.capture.items)}

    def close_popup(self):
        self.page.keyboard.press("Escape")
        settle(self.page, 1500)
        self._record({"action": "escape"})
        return {"url": self.page.url}

    def wait(self):
        self.waits += 1
        if self.waits > 3:
            return {"error": "waited 3 times and nothing changed; act on the page instead (pick an element)"}
        settle(self.page, 2500)
        return {"url": self.page.url}

    def list_captured_json(self):
        return [summarize(c) for c in self.capture.items[-60:]]

    def finish(self, success: bool, note: str):
        if success and (why := self.premature()):
            return {"error": f"Not done: {why}"}
        if success and self.verify_refusals < 2 and (p := self.verify(self.DONE_CHECK)) is not None and p < 0.5:
            # Friday: never claim a success that was not checked. An independent Jev yes/no on the page;
            # it may veto twice, after that the agent's own judgement stands (Jev can be unsure too).
            self.verify_refusals += 1
            return {"error": f"Not done: an independent check says the goal is not visible yet (p={p:.2f})"}
        # Models give up after one failed click; make them try other routes before handing back to the user.
        if not success and "LOGGED_OUT" not in note and self.giveups_left > 0:
            self.giveups_left -= 1
            return {"error": "Not yet. Try a different approach first: close popups, scroll, or use another "
                             "button/link/icon for the same thing. Give up again only if still stuck."}
        self.done = {"success": success, "note": note, "url": self.page.url}
        return {"ok": True}

    DONE_CHECK = "Does this page show a list of the user's own past orders (order history)?"

    def verify(self, question: str) -> float | None:
        """Probability (Jev Noul) that `question` holds for the current page; None if Jev is unavailable."""
        client = jev()
        if client is None:
            return None
        from typesafe_sdk import Noul
        state = self.state() | {"clickable": [mask(describe(e), self.secrets) for e in self.snap["elements"][:60]]}
        try:
            return client.system_one(state=state, questions={"ok": Noul(instructions=question)},
                                     timeout=6).answers["ok"].noul
        except Exception as e:
            log.warning("Jev verify failed (%s)", type(e).__name__)
            return None

    def order_responses(self) -> int:
        """Responses that really carry order records (not just an 'order' in the URL, e.g. an ads call)."""
        return sum(any(sc >= 3 for sc, *_ in _collections(c["json"])) for c in self.capture.items)

    def auto_load_more(self) -> bool:
        """Code, not the model, clicks a visible 'Load More' / 'Next' / next page number (as with the OTP)."""
        elements = [e for e in self.snap["elements"] if not click_refusal(e, self.login)]
        nums = sorted(int(e["text"].strip()) for e in elements if PAGE_NUMBER.match(e.get("text") or ""))
        target = (next((e for e in elements if MORE_BUTTON.search(e.get("text") or "")), None) or
                  next((e for e in elements if nums and (e.get("text") or "").strip() == str(nums[0])), None))
        if not target:
            return False
        before = self.order_responses()
        self.click(target["ref"])
        settle(self.page)
        self.history.append(f"system clicked {describe(target)} to load more orders")
        log.info("[%s] auto-clicked %s", self.site, describe(target))
        return self.order_responses() > before

    def premature(self) -> str | None:
        # Scrolling alone proves nothing (Zepto pages with a "Load More" button): more order data must arrive.
        # After two refusals accept anyway - the history may really be a single page.
        if (self.loads >= 2 and self.order_responses() >= 2) or self.refusals >= 2:
            return None
        if self.auto_load_more() and self.order_responses() >= 2:
            return None
        self.refusals += 1
        return ("no further page of orders has loaded yet; click a 'Load More' / 'Show more' button or the next "
                "page number ('2', '3') / 'Next' of the pager below the list if there is one, or keep scrolling")

    # --- observe / decide ---

    def progress(self) -> dict:
        return {"more_orders_loaded": self.loads, "order_api_responses_seen": self.order_responses()}

    def on_observe(self) -> None:
        if not self.snap["overlay"] and shows_login_button(self.snap["elements"]):
            self.done = {"success": False, "note": "LOGGED_OUT", "url": self.page.url}

    def blocked_page(self) -> bool:
        if self.page.url.startswith("chrome-error://") or BLOCK_TITLE.search(self.page.title()):
            return True
        try:
            text = self.page.evaluate("document.body ? document.body.innerText.slice(0, 400) : ''")
        except Exception:
            return False
        return bool(BLOCK_BODY.search(text))

    def recover_blocked(self) -> None:
        """Bot filters (403s, WAF challenge pages) often clear after a pause: back off and reload the home page."""
        for wait_s in (5, 15, 30):
            log.warning("[%s] site returned an error/blocked page; retrying in %ds", self.site, wait_s)
            self.page.wait_for_timeout(wait_s * 1000)
            try:
                goto(self.page, self.home or home_url(self.site))
            except Exception:
                continue
            if not self.blocked_page():
                return
        self.done = {"success": False, "note": "SITE_BLOCKED: the site keeps returning an error page (HTTP 403)",
                     "url": self.page.url}

    def observe(self) -> None:
        self.follow_new_tab()
        for attempt in range(3):
            try:
                if self.blocked_page():
                    self.recover_blocked()
                    if self.done:
                        return
                self.snap = snapshot(self.page)
                break
            except SessionExpired:
                raise
            except Exception:  # page navigated while being read: let it settle and look again
                if attempt == 2:
                    raise
                settle(self.page)
        self.refs = {e["ref"]: e for e in self.snap["elements"]}
        self.on_observe()

    def state(self) -> dict:
        return {
            "goal": self.GOAL,
            "site": self.site,
            "url_path": urlsplit(self.page.url).path,
            "page_title": self.snap.get("title"),
            "popup_open": self.snap["overlay"],
            "progress": self.progress(),
            "previous_actions": self.history[-8:],
            "page_text": mask(self.snap["aria"][:2500], self.secrets),
        }

    def decide_jev(self):
        client = jev()
        if client is None:
            return None
        from typesafe_sdk import Choice
        questions = {"kind": Choice(
            instructions=("You are driving this web page one action at a time toward the goal. Which kind of "
                          "action makes the most progress right now? Do not repeat an action that just failed."),
            criteria=self.KINDS)}
        elements = self.snap["elements"]
        if elements:
            questions["item"] = Choice(
                instructions="If the action targets an element (click or typing), which element?",
                criteria={e["ref"]: mask(describe(e), self.secrets) for e in elements})
        try:
            answers = client.system_one(state=self.state(), questions=questions, timeout=6).answers
        except Exception as e:
            log.warning("Jev call failed (%s); asking Claude", type(e).__name__)
            return None
        kind, item = answers["kind"], answers.get("item")
        self.last_probs = {"kind": dict(kind.probabilities)} | ({"item_top": item.choice, "item_p": item.confidence}
                                                               if item else {})
        targeted = kind.choice in ("click", "type_phone", "type_location")
        conf = min(kind.confidence, item.confidence) if targeted and item else kind.confidence
        floor = JEV_SAFE_MIN_CONFIDENCE if kind.choice in SAFE_KINDS else JEV_MIN_CONFIDENCE
        if conf < floor or (targeted and not item):
            log.info("Jev unsure (%s %.2f); asking Claude", kind.choice, conf)
            return None
        ref = item.choice if item else None
        call = {
            "click": ("click", {"element_ref": ref}),
            "type_phone": ("type_value", {"element_ref": ref, "field": "PHONE"}),
            "type_location": ("type_value", {"element_ref": ref, "field": "LOCATION"}),
            "close_popup": ("close_popup", {}),
            "scroll": ("scroll", {}),
            "wait": ("wait", {}),
            "done": ("finish", {"success": True, "note": "done"}),
            "stuck": ("ask_user", {"question": f"I'm stuck on {urlsplit(self.page.url).path or '/'}. "
                                               "What should I do next?"}) if self.asks < 2
                     else ("finish", {"success": False, "note": "stuck"}),
            "logged_out": ("finish", {"success": False, "note": "LOGGED_OUT"}),
        }[kind.choice]
        return (*call, f"jev {conf:.2f}")

    def decide_llm(self):
        """One Claude tool call picks the step. Forced tool_choice may 400 on current models, so it is auto plus
        an instruction; strict tools keep the input schema-valid, and it arrives already parsed from JSON."""
        lines = "\n".join(f"{e['ref']}: {mask(describe(e), self.secrets)}" for e in self.snap["elements"])
        user = (f"STATE:\n{json.dumps(self.state(), ensure_ascii=False)}\n\nCLICKABLE ELEMENTS:\n{lines or '(none)'}"
                "\n\nCall exactly one tool for the next action.")
        resp = llm.create("browser_step", effort="low", max_tokens=4096, system=self.system_prompt(),
                          tools=self.TOOLS, tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                          messages=[{"role": "user", "content": user}])
        call = next((b for b in resp.content if b.type == "tool_use"), None)
        if call is None:
            return "wait", {}, "claude"
        return call.name, dict(call.input), "claude"

    def system_prompt(self) -> str:
        return self.SYSTEM.format(site=self.site, popups=POPUPS)

    def _target(self, args: dict) -> str:
        el = self.refs.get(args.get("element_ref", ""))
        rest = {k: v for k, v in args.items() if k != "element_ref"}
        return " ".join(filter(None, [describe(el) if el else "", json.dumps(rest) if rest else ""]))

    def record(self, entry: dict) -> None:
        """Pointer-style replayable run log: one JSON line per step in runs/<site>-<time>.jsonl."""
        try:
            if self.run_log is None:
                self.run_log = self._new_run_log()
            self.run_log.parent.mkdir(parents=True, exist_ok=True)
            with self.run_log.open("a", encoding="utf-8") as f:
                f.write(mask(json.dumps(entry, ensure_ascii=False, default=str), self.secrets) + "\n")
        except OSError:
            pass

    def _new_run_log(self) -> Path:
        kind = "login" if self.login else "learn"
        return (self.runs_dir or RUNS) / f"{self.site}-{kind}-{datetime.now():%Y%m%d-%H%M%S}.jsonl"

    def run(self) -> dict:
        started = time.time()
        self.run_log = self._new_run_log()
        for step in range(1, MAX_STEPS + 1):
            if time.time() - started > TIME_BUDGET_S:
                break
            self.observe()
            if self.done:
                break
            t0 = time.time()
            self.last_probs = {}
            decision = None if self.force_llm else self.decide_jev()
            name, args, source = decision or self.decide_llm()
            t_decide = time.time() - t0
            target = mask(self._target(args), self.secrets)
            if name != "wait":
                self.waits = 0
            try:
                if name not in {tool["name"] for tool in self.TOOLS}:
                    raise ValueError(f"unknown action {name}")
                result = getattr(self, name)(**args)
            except SessionExpired:
                raise
            except Exception as e:
                result = {"error": f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"}
            # A failure, or the same action on the same page again, means the fast path is looping: let Claude think.
            key = (name, target, self.page.url)
            self.force_llm = "error" in result or (key == self.last_key and name not in ("scroll", "wait"))
            self.last_key = key
            outcome = mask(result["error"] if "error" in result else "ok", self.secrets)
            self.history.append(f"{name} {target} -> {outcome}")
            log.info("[%s] %d %s: %s %s -> %s (%.1fs)", self.site, step, source, name, target[:80], outcome[:100],
                     time.time() - t0)
            self.record({"step": step, "url": urlsplit(self.page.url).path, "source": source, "action": name,
                         "target": target, "outcome": outcome, "probabilities": self.last_probs,
                         "decide_s": round(t_decide, 2), "total_s": round(time.time() - t0, 2),
                         "progress": self.progress(), "elements": len(self.snap["elements"])})
            if self.done:
                break
        return self.done or {"success": False, "note": "step/time limit", "url": self.page.url}


LOGIN_SYSTEM = """You operate a real browser on {site}. Log the user into their OWN account with phone + OTP,
then open their order history.
1. If a location popup is shown, set the location first: click "Detect my location" / "Use current location" /
   "Locate me" (the browser already shares the user's location). If that does not work, call
   type_value(field="LOCATION") on the location search box and click the suggestion that best matches
   "{location}". Then confirm the location if asked.
2. If already logged in (account/profile menu visible, no Login/Sign in button), skip to step 5.
3. Open the login dialog, call type_value(field="PHONE") on the phone/mobile input, then click Continue / Get OTP.
4. The OTP is filled in automatically by the system once its input appears (the user types it in their
   terminal). Then click Verify/Continue if it did not submit by itself.
5. Open the order history page (usually under the account/profile menu) and call finish(success=true).
On a CAPTCHA, a new-account form (name/email) or anything you cannot get past, call ask_user with a short
question - the user can solve it in the browser - and only finish(success=false) if they say stop.
You never see the phone number or OTP; they show as <PHONE>/<OTP>. Never type anything else.
{popups}"""

TYPE_VALUE = _fn("type_value", "Type one of the user's preset values (phone number, OTP, or saved location) into an input.",
                 _REF | {"field": {"type": "string", "enum": ["PHONE", "OTP", "LOCATION"]}}, ["element_ref", "field"])


class LoginAgent(Discovery):
    """Drives the login form. It can only type the phone number, an OTP the user enters, or the saved location."""
    SYSTEM, login = LOGIN_SYSTEM, True
    TOOLS = [t for t in TOOLS if t["name"] != "list_captured_json"] + [TYPE_VALUE]
    KINDS = COMMON_KINDS | {
        "type_phone": "Type the user's mobile number into the EMPTY phone/mobile number input (chosen in the item question).",
        "type_location": ("Type the user's delivery address into an EMPTY location/address search box (chosen in the "
                          "item question). Only after 'use current location' was tried or when there is none."),
        "done": "The user is logged in (no Login/Sign in button, account/profile visible) and the order history is shown.",
    }

    def __init__(self, site, page, guard, capture, phone: str, location: str = "", dry_run: bool = False,
                 prompt: Callable[[str, str], str] | None = None, home: str | None = None,
                 runs_dir: Path | None = None):
        super().__init__(site, page, guard, capture, prompt, home, runs_dir)
        self.secrets["PHONE"] = phone
        self.location, self.dry_run = location, dry_run
        self.phone_typed = self.otp_entered = False
        self.GOAL = (f"Log in to the user's own {site} account with phone + OTP (set the delivery location first if a "
                     "location popup asks), then open the order history page.")

    DONE_CHECK = ("Is the user logged in (their name/account/profile visible, no Login or Sign in button) and is "
                  "their order history shown?")

    def system_prompt(self) -> str:
        return LOGIN_SYSTEM.replace("{location}", self.location or "(not set)").format(site=self.site, popups=POPUPS)

    def progress(self) -> dict:
        return {"delivery_address": self.location or None, "phone_typed": self.phone_typed,
                "otp_entered": self.otp_entered}

    def premature(self) -> str | None:
        if self.phone_typed and not self.otp_entered:
            return "the OTP was never entered; finish the login first"
        if shows_login_button(self.snap["elements"]):
            return "a Login/Sign in button is still visible, so the user is not logged in yet"
        return None

    def on_observe(self) -> None:
        # Code, not the model, notices the OTP field: as soon as it appears, ask the user and fill it in.
        if not self.phone_typed:
            return
        for _, frame in frames(self.page):  # the OTP form may sit in a login iframe
            try:
                found = frame.evaluate(_FIND_OTP_JS)
            except Exception:
                continue
            if found:
                self.enter_otp(frame)
                self.snap = snapshot(self.page)
                self.refs = {e["ref"]: e for e in self.snap["elements"]}
                return

    def enter_otp(self, frame=None) -> None:
        frame = frame or self.page.main_frame
        try:
            otp = self.prompt(f"\n[{self.site}] OTP sent to your phone. Enter it here (blank to skip): ", "otp").strip()
        except TimeoutError:
            self.done = {"success": False, "note": "otp_timeout", "url": self.page.url}
            return
        if not otp:
            self.history.append("user skipped the OTP")
            return
        self.secrets["OTP"] = otp
        loc = frame.locator('[data-ea-otp="1"]').first
        try:
            loc.click(timeout=2500)
        except Exception:
            loc.focus()
        self.page.keyboard.type(otp, delay=60)  # keystrokes also walk split one-digit boxes
        boxes = frame.locator("input[maxlength='1']:visible")
        if boxes.count() >= len(otp) >= 4 and "".join(boxes.nth(i).input_value() for i in range(len(otp))) != otp:
            for i, digit in enumerate(otp):  # boxes that don't auto-advance: one digit each
                boxes.nth(i).click(timeout=2000)
                boxes.nth(i).fill("")
                self.page.keyboard.type(digit)
        settle(self.page)
        self.otp_entered = True
        self.history.append("system entered the OTP the user provided")
        log.info("[%s] OTP entered", self.site)

    def type_value(self, element_ref: str, field: str):
        el = self.refs.get(element_ref)
        if not el or el.get("tag") not in ("input", "textarea"):
            return {"error": "that element is not a text input"}
        if field == "PHONE" and self.dry_run:  # test mode: prove the phone input is reachable, send no OTP
            self.done = {"success": True, "note": "dry run: reached the phone input", "url": self.page.url}
            return {"ok": True}
        if field == "OTP":
            try:
                otp = self.prompt(f"\n[{self.site}] Enter the OTP you received (blank to skip): ", "otp").strip()
            except TimeoutError:
                self.done = {"success": False, "note": "otp_timeout", "url": self.page.url}
                return {"error": "timed out waiting for the OTP"}
            if not otp:
                return {"error": "user did not provide an OTP"}
            self.secrets["OTP"], value = otp, otp
        elif field == "PHONE":
            value = self.secrets["PHONE"]
        elif field == "LOCATION" and self.location:
            value = self.location
        else:
            return {"error": f"no value for {field}"}
        loc = locate(self.page, element_ref)
        click_ref(self.page, element_ref)
        if field == "PHONE":
            loc.fill(value)
            if not re.sub(r"\D", "", loc.input_value()).endswith(value):  # masked inputs ignore fill()
                loc.fill("")
                self.page.keyboard.type(value, delay=40)
            self.phone_typed = True
        else:
            self.page.keyboard.type(value, delay=40)  # autocomplete boxes / split OTP boxes want keystrokes
            if field == "OTP":
                self.otp_entered = True
        settle(self.page)
        return {"typed": f"<{field}>", "url": self.page.url}


# --- recipe synthesis --------------------------------------------------------

ORDERISH_KEY = re.compile(r"order|total|amount|price|cost|date|time|placed|item|dish|status|restaurant|store", re.I)


def _collections(data, path: str = "", out: list | None = None, depth: int = 0) -> list:
    """Order-like collections anywhere in a JSON body: lists of objects, or id->object maps (Zomato's
    entities.ORDER), whose keys look like order fields. Returns (score, path, count, first item)."""
    out = [] if out is None else out
    if depth > 8:
        return out
    items = None
    if isinstance(data, list) and data and all(isinstance(x, dict) for x in data[:3]):
        items = data
    elif isinstance(data, dict) and len(data) >= 2 and all(isinstance(v, dict) for v in list(data.values())[:3]):
        items = list(data.values())
    if items:
        keys = set().union(*(i.keys() for i in items[:3]))
        score = sum(bool(ORDERISH_KEY.search(k)) for k in keys)
        if score >= 2:
            out.append((score, path or "(root)", len(items), items[0]))
    children = data.items() if isinstance(data, dict) else enumerate(data[:3]) if isinstance(data, list) else ()
    for k, v in children:
        _collections(v, f"{path}.{k}" if path else str(k), out, depth + 1)
    return out


def shape_hint(data) -> str:
    """Where the orders are in a (possibly huge) response, so the model never maps fields from a truncated blob."""
    found = sorted(_collections(data), key=lambda x: (-x[0], -x[2]))[:3]
    return "\n".join(f"order-like collection at {p!r} ({n} items); first item: "
                     f"{json.dumps(first, ensure_ascii=False)[:3500]}" for _, p, n, first in found)


def summarize(c: dict) -> dict:
    return {"id": c["id"], "method": c["method"], "url": c["url"][:200], "status": c["status"],
            "size": c["size"], "preview": c["sample"][:300]}


def score(c: dict) -> int:
    s = 5 if re.search(r"order", c["url"], re.I) else 0
    s += 3 if re.search(r"order", c.get("request_body") or "", re.I) else 0
    s += 6 if any(sc >= 3 for sc, *_ in _collections(c["json"])) else 0  # actually contains order-like records
    return s + min(c["size"] // 5000, 4)


RECIPE_PROMPT = """Below are JSON API calls captured while browsing {site} order history, plus POST requests the
read-only guard blocked (no response available). Pick the ONE endpoint that returns the paginated order list and
return a recipe as a JSON object with exactly these keys:
{{
 "orders_page_url": str,             // page URL where the order history is shown
 "endpoint": str,                    // full URL WITHOUT the query string
 "method": "GET" | "POST",
 "query": {{}},                      // query params for page 1
 "body": object | null,              // JSON body for page 1 (POST only)
 "headers": {{}},                    // non-secret request headers the API needs (never cookies/auth/tokens)
 "pagination": {{"type": "cursor"|"offset"|"page", "param": str, "next_path": str|null, "start": int|null}},
 "field_map": {{
   "orders_path": str,               // dotted path to the order array in the response, e.g. "data.orders"
   "order_id": str, "placed_at": str, "placed_at_format": str|null,
   "total_amount": str, "amount_divisor": 1|100, "currency": str,   // currency may be a literal like "=INR"
   "status": str, "store_or_restaurant": str|null, "sub_platform": str|null,
   "items_path": str, "items": {{"name": str, "qty": str, "price": str|null}}, "price_is_unit": bool
 }},
 "read_post_allowlist": [{{"url_regex": str, "why": str}}]  // only if method is POST; narrow regex for that endpoint
}}
Paths inside field_map are relative to ONE order object (items fields relative to one item).
{field_rules}
For POST bodies, "param" is a dotted path inside the body (e.g. "variables.page"). Amounts in paise -> divisor 100.
Reply with JSON only.

CAPTURED (summary):
{summary}

TOP CANDIDATES (samples):
{samples}

BLOCKED POSTS:
{blocked}
"""


def _same_endpoint(c: dict, recipe: dict) -> bool:
    a, b = urlsplit(c["url"]), urlsplit(recipe["endpoint"])
    if (a.netloc, a.path) != (b.netloc, b.path) or c["method"].upper() != recipe.get("method", "GET").upper():
        return False
    op = (recipe.get("body") or {}).get("operationName") if isinstance(recipe.get("body"), dict) else None
    return not op or op in (c.get("request_body") or "")


def captured_for(recipe: dict, items: list[dict]) -> list[dict]:
    """Successful responses of the recipe's endpoint (our own rejected 401 replays are captured too - skip them)."""
    return [c for c in items if _same_endpoint(c, recipe) and 200 <= c["status"] < 300]


def confirm_allowlist(entries: list[dict], approved: set[str]) -> bool:
    new = [e for e in entries if e["url_regex"] not in approved]
    if not new:
        return True
    print("\nThe recipe wants these read-only POST requests allowed:")
    for e in new:
        print(f"  {e['url_regex']}   ({e.get('why', '')})")
    ok = input("Allow them? Bodies containing 'mutation' stay blocked. [y/N] ").strip().lower() == "y"
    if ok:
        approved.update(e["url_regex"] for e in new)
    return ok


def check_structure(recipe: dict) -> str | None:
    fm = recipe.get("field_map") or {}
    for k in ("endpoint", "method", "pagination", "field_map"):
        if not recipe.get(k):
            return f"missing {k}"
    for k in ("order_id", "placed_at", "total_amount"):
        if not fm.get(k):
            return f"field_map.{k} missing"
    if BLOCKED_URL.search(urlsplit(recipe["endpoint"]).path):
        return "endpoint path matches the blocklist"
    host = urlsplit(recipe["endpoint"]).netloc
    for e in recipe.get("read_post_allowlist") or []:
        try:
            rx = re.compile(e["url_regex"])
        except (re.error, KeyError):
            return f"bad allowlist entry {e}"
        if rx.search(f"https://{host}/") or rx.search("https://example.com/x"):
            return f"allowlist regex too broad: {e['url_regex']}"
    if recipe["method"].upper() == "POST" and not any(
            re.search(e["url_regex"], recipe["endpoint"]) for e in recipe.get("read_post_allowlist") or []):
        return "POST endpoint is not covered by read_post_allowlist"
    return None


def check_page_two(page, recipe: dict, req: dict, data, orders: list[dict]) -> str | None:
    """Pagination must actually move: page 2 returning page 1's orders means the param is ignored
    (Zomato's getPage ignores ?page=; its pager calls a different endpoint)."""
    nxt = recipes.next_request(req, recipe.get("pagination") or {}, data, len(orders))
    if not nxt:
        return None
    try:
        page2 = recipes.valid_orders(recipes.extract_orders(fetch_in_page(page, nxt), recipe["field_map"]))
    except Exception:
        return None  # can't tell; sync will stop at the first failing page
    if page2 and {o["order_id"] for o in page2} <= {o["order_id"] for o in orders}:
        return ("pagination does not work: page 2 returned the same orders as page 1, so this endpoint ignores "
                f"{recipe['pagination'].get('param')!r}. Use the request the site makes when its 'next page' / "
                "page number / 'Load More' is clicked (look for it among the captured calls).")
    return None


def validate(recipe: dict, page, guard, capture, approved: set[str]) -> tuple[list[dict], bool, str | None]:
    """Replay page 1. Returns (valid orders, fast_path_ok, error)."""
    if err := check_structure(recipe):
        return [], False, err
    if not confirm_allowlist(recipe.get("read_post_allowlist") or [], approved):
        return [], False, "user rejected the POST allowlist; choose a GET endpoint if one exists"
    guard.allowlist = list(recipe.get("read_post_allowlist") or [])
    fm, sample = recipe["field_map"], ""
    try:
        req = recipes.first_request(recipe)
        data = fetch_in_page(page, req)
        orders = recipes.valid_orders(recipes.extract_orders(data, fm))
        if orders:
            if err := check_page_two(page, recipe, req, data, orders):
                return [], False, err
            return orders, True, None
        sample = json.dumps(data, ensure_ascii=False)[:6000]
    except SessionExpired as e:
        # The page is logged in (discovery saw the orders), so a 401 here means the API wants the site's
        # own auth headers, which we never copy. Fall back to the passively captured response.
        sample = f"in-page replay rejected ({e}); the site signs its own requests"
    except Exception as e:
        sample = f"fetch failed: {e}"
    hits = captured_for(recipe, capture.items)
    if not hits and recipe.get("read_post_allowlist"):
        # The guard blocked this POST during discovery; now that it is allowed, let the site's own code make
        # the (signed) request again and capture the real response.
        goto(page, recipe.get("orders_page_url") or page.url)
        settle(page)
        wheel(page)
        hits = captured_for(recipe, capture.items)
    for c in hits:  # fast path failed; can passive capture work?
        orders = recipes.valid_orders(recipes.extract_orders(c["json"], fm))
        if orders:
            return orders, False, None
    if hits:
        sample = (f"what your field_map extracts: {recipes.diagnose(hits[0]['json'], fm)}\n"
                  f"captured response of this endpoint: {shape_hint(hits[0]['json']) or hits[0]['sample'][:8000]}")
    return [], False, ("field_map extracted no order with order_id, placed_at and total_amount "
                       "(placed_at must parse as a date - give placed_at_format as a strptime format). "
                       f"Page-1 replay: {sample}")


def synthesize(site: str, page, guard, capture, trace: list[dict], orders_url: str) -> dict:
    client = llm.client()
    ranked = sorted(capture.items, key=score, reverse=True)
    samples = "\n\n".join(f"#{c['id']} {c['method']} {c['url']}\nrequest_body: {(c['request_body'] or '')[:1500]}\n"
                          f"headers: {json.dumps(c['request_headers'])[:800]}\n"
                          f"{shape_hint(c['json']) or 'no order-like collection found'}\n"
                          f"response start: {c['sample'][:3000]}"
                          for c in ranked[:6])
    blocked = "\n".join(f"{b['method']} {b['url'][:200]} body={b['body'][:600]}"
                        for b in guard.blocked if b["method"] == "POST")[:6000]
    goal = SITES[site].get("goal")
    messages = [{"role": "user", "content": (f"Only this order list counts: {goal}\n\n" if goal else "") + RECIPE_PROMPT.format(
        site=site, summary=json.dumps([summarize(c) for c in ranked[:80]])[:20000],
        samples=samples, blocked=blocked or "(none)", field_rules=FIELD_RULES)}]
    approved: set[str] = set()
    for attempt in range(3):
        resp = llm.create("recipe", effort="medium", max_tokens=16_000, messages=messages, client=client)
        messages.append({"role": "assistant", "content": llm.text(resp)})
        try:
            recipe = llm.json_reply(resp)
            if other := sibling_endpoint(site, recipe):
                raise ValueError(f"that endpoint is {other}'s order feed; find {site}'s own order list "
                                 f"({SITES[site].get('goal', '')})")
            orders, fast_ok, err = validate(recipe, page, guard, capture, approved)
        except SessionExpired:
            raise
        except Exception as e:  # any bad field in the recipe goes back to the model, not up the stack
            orders, fast_ok, err = [], False, f"invalid recipe: {type(e).__name__}: {e}"
        if orders:
            recipe.update(site=site, fast_path_ok=fast_ok, nav_trace=trace,
                          headers=scrub_headers(recipe.get("headers") or {}),
                          learned_at=datetime.now().isoformat(timespec="seconds"))
            recipe["orders_page_url"] = recipe.get("orders_page_url") or orders_url
            log.info("recipe validated: %d orders on page 1 via %s (fast path %s)",
                     len(orders), redact_url(recipe["endpoint"]), fast_ok)
            return recipe
        log.warning("recipe attempt %d failed: %s", attempt + 1, err[:200])
        messages.append({"role": "user", "content": f"Validation failed: {err}\nReturn a corrected recipe JSON."})
    raise RuntimeError(f"could not produce a valid recipe for {site}")


FIELD_MAP_PROMPT = """This JSON is page 1 of the user's {site} order history, from {endpoint}.
Return a JSON object {{"field_map": {{...}}}} where field_map has exactly these keys:
 "orders_path": str (dotted path to the order array, e.g. "data.orders"),
 "order_id": str, "placed_at": str, "placed_at_format": str|null,
 "total_amount": str (what the user actually paid), "amount_divisor": 1|100, "currency": str (may be a literal "=INR"),
 "status": str, "store_or_restaurant": str, "sub_platform": str|null,
 "items_path": str, "items": {{"name": str, "qty": str, "price": str}}, "price_is_unit": bool
Paths are relative to ONE order object (item fields relative to one item). Amounts in paise -> divisor 100.
{field_rules}
Reply with JSON only.

RESPONSE:
{sample}"""

FIELD_RULES = """store_or_restaurant: a human-readable store/restaurant NAME; null if the order only has an id/uuid.
sub_platform: only when one endpoint mixes different platforms (e.g. food delivery vs grocery); null for
delivery modes, store types or marketplace tiers. items.price: null if items carry no price."""


def learn_from_seed(site: str, page, guard, capture, seed: dict) -> dict:
    """Known endpoint + pagination (recipes/<site>.seed.json): fetch page 1 inside the logged-in tab and
    only learn the field_map from the real response, then validate it."""
    orders_url = seed["orders_page_url"]
    goto(page, orders_url)
    settle(page)
    if shows_login_button(snapshot(page)["elements"]):
        raise SessionExpired("not logged in")
    recipe = dict(seed)
    data = fetch_in_page(page, recipes.first_request(recipe))
    client = llm.client()
    messages = [{"role": "user", "content": FIELD_MAP_PROMPT.format(
        site=site, endpoint=redact_url(recipe["endpoint"]),
        sample=f"{shape_hint(data)}\n\nresponse start: {json.dumps(data, ensure_ascii=False)[:12000]}",
        field_rules=FIELD_RULES)}]
    for attempt in range(3):
        resp = llm.create("field_map", effort="medium", max_tokens=16_000, messages=messages, client=client)
        messages.append({"role": "assistant", "content": llm.text(resp)})
        try:
            fm = llm.json_reply(resp)["field_map"]
            orders = recipes.valid_orders(recipes.extract_orders(data, fm))
            err = None if orders else ("field_map extracted no order with order_id, placed_at and total_amount: "
                                       f"{recipes.diagnose(data, fm)}")
        except Exception as e:
            orders, err = [], f"invalid field_map: {type(e).__name__}: {e}"
        if orders:
            recipe.update(site=site, field_map=fm, fast_path_ok=True, read_post_allowlist=seed.get("read_post_allowlist", []),
                          nav_trace=[{"action": "navigate", "url": orders_url, "url_after": urlsplit(page.url).path}],
                          learned_at=datetime.now().isoformat(timespec="seconds"))
            log.info("seed recipe validated: %d orders on page 1 via %s", len(orders), redact_url(recipe["endpoint"]))
            return recipe
        log.warning("field_map attempt %d failed: %s", attempt + 1, err[:200])
        messages.append({"role": "user", "content": f"Validation failed: {err}. Return a corrected JSON."})
    raise RuntimeError(f"could not map the {site} order response")


def discover(site: str, page, guard, capture) -> dict:
    """Return a validated recipe (not yet saved): from a seed if one exists, else by running the agent."""
    if seed := recipes.load_seed(site):
        return learn_from_seed(site, page, guard, capture, seed)
    hint = recipes.hint_path(site)
    start = hint.read_text(encoding="utf-8").strip() if hint.exists() else home_url(site)
    goto(page, start)
    d = Discovery(site, page, guard, capture)
    d.trace.append({"action": "navigate", "url": start, "url_after": urlsplit(page.url).path})
    result = d.run()
    log.info("discovery finished: %s", result)
    if not result["success"] and "LOGGED_OUT" in result["note"]:
        raise SessionExpired("not logged in")
    return synthesize(site, d.page, guard, capture, d.trace, result["url"])  # d.page: the agent may switch tabs


def sibling_endpoint(site: str, recipe: dict) -> str | None:
    """Sites stored under one db site (Swiggy food + Instamart) must not share an order endpoint, or one
    feed's orders get relabelled as the other's."""
    db_site = SITES[site].get("db_site", site)
    for other, cfg in SITES.items():
        if other != site and cfg.get("db_site", other) == db_site and (r := recipes.load(other)):
            if urlsplit(r["endpoint"])[1:3] == urlsplit(recipe.get("endpoint", ""))[1:3]:
                return other
    return None

"""Login, learn and sync orchestration."""
import logging
import random
import re
import time
from urllib.parse import urlsplit

import agent
import db
import recipes
import browser
from browser import LOGIN_URL, SITES, SessionExpired, fetch_in_page, open_site

log = logging.getLogger("sync")

MAX_PAGES = 200
MAX_REPLAY_SCROLLS = 30


class RecipeError(Exception):
    pass


class OtpTimeout(TimeoutError):
    """Nobody answered an unattended prompt in time. kind is "otp" or "question"."""

    def __init__(self, kind: str = "otp", message: str | None = None):
        super().__init__(message or f"no {kind} answer received in time")
        self.kind = kind


# LoginAgent notes for a prompt that timed out -> the OtpTimeout kind.
_TIMEOUT_NOTES = {"otp_timeout": "otp", "ask_timeout": "question"}


def ensure_logged_in(page) -> None:
    if LOGIN_URL.search(urlsplit(page.url).path):
        raise SessionExpired(f"redirected to {urlsplit(page.url).path}")


def goto(page, url: str) -> None:
    browser.goto(page, url)
    ensure_logged_in(page)


# --- login -------------------------------------------------------------------

def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if not re.fullmatch(r"[6-9]\d{9}", digits):
        raise ValueError("expected a 10-digit Indian mobile number")
    return digits


def login(site: str, phone: str | None = None, dry_run: bool = False, prompt=None,
          profile_dir=None, location: dict | None = None) -> bool:
    """With a phone number the LoginAgent fills the form and asks for the OTP; otherwise (or if it gets
    stuck) you log in by hand. The number is never stored or logged. dry_run stops at the phone input.

    An injected prompt(text, kind) means an unattended run: there is no manual fallback. A prompt that timed
    out raises OtpTimeout; any other failure returns False."""
    unattended = prompt is not None
    recipe = recipes.load(site)
    home = browser.home_url(site, location)  # location None: the CLI's saved one; {}: none
    with open_site(site, login_mode=True, profile_dir=profile_dir, location=location) as (_, page, guard, capture):
        try:
            browser.goto(page, home)
        except Exception as e:  # e.g. a bot-filter 403; the agent backs off and retries from the error page
            log.warning("%s home page failed to load: %s", site, str(e).splitlines()[0])
        done = None
        if phone:
            address = browser.effective_location(location).get("query") or ""
            la = agent.LoginAgent(site, page, guard, capture, phone, address, dry_run, prompt, home=home)
            try:
                done = la.run()
            except Exception as e:
                done = {"success": False, "note": f"{type(e).__name__}: {str(e)[:200]}"}
            page = la.page  # the agent may have followed a new tab
            if dry_run:
                print(f"[{site}] dry run {'PASSED' if done['success'] else 'FAILED'}: "
                      f"{agent.mask(done['note'], {'PHONE': phone})}")
                return done["success"]
            if not done["success"]:
                print(f"\n[{site}] automatic login stopped: {agent.mask(done['note'], {'PHONE': phone})}")
        if not done or not done["success"]:
            if unattended:
                if done and done["note"] in _TIMEOUT_NOTES:
                    raise OtpTimeout(_TIMEOUT_NOTES[done["note"]])
                return False
            input(f"\nLog in to {site} in the browser window (phone + OTP).\n"
                  "Then open your Orders / order history page and press Enter here... ")
            while not agent.site_allows_host(site, page.url):  # still on an error page / elsewhere
                input(f"The browser is on {page.url[:60]}, not {site}. Open {site}'s Orders page, then press Enter... ")
        orders_url = recipe["orders_page_url"] if recipe else page.url
        guard.login_mode = False
        capture.clear()
        goto(page, orders_url)
        if any(c["status"] in (401, 403) for c in capture.items if agent.site_allows_host(site, c["url"])):
            raise SessionExpired("orders page returned 401/403")
        if agent.shows_login_button(agent.snapshot(page)["elements"]):
            raise SessionExpired("the site still shows a Login button")
        if not recipe:
            recipes.RECIPES.mkdir(exist_ok=True)
            recipes.hint_path(site).write_text(orders_url, encoding="utf-8")
    print(f"Session OK for {site}; orders page {urlsplit(orders_url).path}")
    return True


# --- learn -------------------------------------------------------------------

def learn(site: str) -> dict:
    with open_site(site) as (_, page, guard, capture):
        recipe = agent.discover(site, page, guard, capture)
    recipes.save(site, recipe)
    print(f"Saved {recipes.recipe_path(site)}")
    return recipe


# --- fetch paths -------------------------------------------------------------

def _stop(batch: list[dict], since: str | None) -> bool:
    return bool(since) and min(o["placed_at"] for o in batch) < since


def fast_path(page, recipe: dict, since: str | None) -> list[dict]:
    """Call the learned endpoint from inside the page and paginate."""
    req, seen, out = recipes.first_request(recipe), set(), []
    for n in range(MAX_PAGES):
        data = fetch_in_page(page, req)
        raw = recipes.extract_orders(data, recipe["field_map"])
        batch = [o for o in recipes.valid_orders(raw) if o["order_id"] not in seen]
        if n == 0 and not batch:
            raise RecipeError("page 1 returned no valid orders")
        if not batch:
            break
        seen.update(o["order_id"] for o in batch)
        out += batch
        log.info("page %d: %d orders", n + 1, len(batch))
        if _stop(batch, since):
            break
        req = recipes.next_request(req, recipe["pagination"], data, len(raw))
        if not req:
            break
        time.sleep(random.uniform(1, 2))
    return out


def _find_target(page, target: dict) -> str | None:
    elements = agent.snapshot(page)["elements"]
    for key in ("href", "text", "label"):
        if target.get(key):
            for el in elements:
                if el.get(key) == target[key] and not agent.click_refusal(el):
                    return el["ref"]
    return None


def replay(page, capture, recipe: dict, since: str | None) -> list[dict]:
    """Deterministically replay nav_trace and extract orders from passively captured responses."""
    capture.clear()
    for step in recipe.get("nav_trace") or []:
        if step["action"] == "navigate":
            goto(page, step["url"])
        elif step["action"] == "click":
            ref = _find_target(page, step["target"])
            if not ref:
                raise RecipeError(f"replay: element not found {step['target']}")
            agent.click_ref(page, ref)
        elif step["action"] == "scroll":
            agent.wheel(page)
        elif step["action"] == "escape":
            page.keyboard.press("Escape")
            page.wait_for_timeout(1000)
        ensure_logged_in(page)
        if step.get("url_after") and urlsplit(page.url).path != step["url_after"]:
            raise RecipeError(f"replay checkpoint mismatch: {urlsplit(page.url).path} != {step['url_after']}")

    orders: dict[str, dict] = {}
    seen_responses, idle = 0, 0
    # Numbered pagers: continue after the highest page number the recorded trace already clicked.
    clicked = [int(s["target"]["text"]) for s in recipe.get("nav_trace") or []
               if s["action"] == "click" and agent.PAGE_NUMBER.match(str((s.get("target") or {}).get("text") or ""))]
    next_page = max(clicked, default=1) + 1
    for _ in range(MAX_REPLAY_SCROLLS):
        hits = agent.captured_for(recipe, capture.items)
        if any(c["status"] in (401, 403) for c in hits):
            raise SessionExpired("order API returned 401/403")
        for c in hits[seen_responses:]:
            for o in recipes.valid_orders(recipes.extract_orders(c["json"], recipe["field_map"])):
                orders[o["order_id"]] = o
        idle = idle + 1 if len(hits) == seen_responses else 0
        seen_responses = len(hits)
        if idle >= 2 or (orders and _stop(list(orders.values()), since)):
            break
        agent.wheel(page)
        elements = [e for e in agent.snapshot(page)["elements"] if not agent.click_refusal(e)]
        # "Load More" / "Next" buttons, else the next page number of a numbered pager - never a random "2".
        more = ([e for e in elements if agent.MORE_BUTTON.search(e.get("text") or "")] or
                [e for e in elements if (e.get("text") or "").strip() == str(next_page)])
        if more:
            agent.click_ref(page, more[0]["ref"])
            if (more[0].get("text") or "").strip() == str(next_page):
                next_page += 1
        time.sleep(random.uniform(1, 2))
    if not orders:
        raise RecipeError("replay captured no valid orders")
    return list(orders.values())


def fetch_orders(site: str, since: str | None, profile_dir=None, location: dict | None = None,
                 allow_learn: bool = True) -> list[dict]:
    """Fast path -> nav_trace replay -> re-learn; returns the orders (no database).
    allow_learn=False (a user's own sync) never runs the exploring discovery agent: it raises RecipeError instead."""
    recipe = recipes.load(site)
    if not recipe:
        if not allow_learn:
            raise RecipeError(f"no recipe for {site}; an admin must learn it")
        recipe = learn(site)
    with open_site(site, allowlist=recipe.get("read_post_allowlist"), profile_dir=profile_dir,
                   location=location) as (_, page, guard, capture):
        orders = None
        goto(page, recipe["orders_page_url"])
        if recipe.get("fast_path_ok", True):
            try:
                orders = fast_path(page, recipe, since)
            except SessionExpired:
                raise
            except Exception as e:
                log.warning("%s fast path failed: %s", site, e)
        if not orders:
            try:
                orders = replay(page, capture, recipe, since)
            except SessionExpired:
                raise
            except Exception as e:
                log.warning("%s nav_trace replay failed: %s", site, e)
        if not orders and not allow_learn:
            raise RecipeError(f"recipe drift for {site}: fast path and replay both failed; an admin must re-learn it")
        if not orders:
            log.warning("DRIFT: %s recipe no longer works; re-running discovery", site)
            capture.clear()
            recipe = agent.discover(site, page, guard, capture)
            recipes.save(site, recipe)
            orders = fast_path(page, recipe, since) if recipe["fast_path_ok"] else replay(page, capture, recipe, since)
    if since:
        orders = [o for o in orders if o["placed_at"] >= since]
    return orders


def sync_site(site: str, since: str | None, con) -> int:
    orders = fetch_orders(site, since)
    cfg = SITES[site]
    n = db.upsert_orders(con, cfg.get("db_site", site), orders, cfg.get("sub_platform"))
    print(f"{site}: {n} orders stored")
    return n

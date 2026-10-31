"""Persistent headful Chrome per site, with the read-only guard and JSON response capture."""
import contextlib
import contextvars
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from cart_guard import CartGuard
from guard import Guard

log = logging.getLogger("browser")

ROOT = Path(__file__).parent
PROFILES = ROOT / "profiles"
SETTINGS = ROOT / "settings.json"

# Only entry points - order pages and APIs are learned at runtime.
SITES = {
    "blinkit": {"home": "https://blinkit.com/", "profile": "blinkit"},
    "zepto": {"home": "https://www.zepto.com/", "profile": "zepto"},
    "swiggy": {"home": "https://www.swiggy.com/", "profile": "swiggy"},
    # Instamart shares Swiggy's login; stored as site=swiggy, sub_platform=instamart.
    "instamart": {"home": "https://www.swiggy.com/instamart", "profile": "swiggy",
                  "db_site": "swiggy", "sub_platform": "instamart",
                  # Swiggy's account page lists FOOD orders; Instamart's grocery orders are a separate list.
                  "goal": "Instamart (grocery) order history only - not the Swiggy food order list."},
    # zomato.com itself is an app-download landing page; the city page has the web login and account menu.
    "zomato": {"home": "https://www.zomato.com/{city}/restaurants", "profile": "zomato"},
}
ZOMATO_CITY_ALIASES = {"bengaluru": "bangalore", "delhi": "ncr", "new-delhi": "ncr", "gurugram": "ncr",
                       "gurgaon": "ncr", "noida": "ncr", "ghaziabad": "ncr", "faridabad": "ncr"}


def effective_location(location: dict | None) -> dict:
    """None means "not given": the CLI's saved location (settings.json). A dict is used as is, so {} means
    "no location" - a per-user run must never fall back to the CLI owner's address."""
    if location is None:
        return load_settings().get("location") or {}
    return location


def city_slug(location: dict | None = None) -> str | None:
    """City from the address 'society, area, city, state' -> 'vadodara' (location as in effective_location)."""
    loc = effective_location(location)
    parts = [p.strip() for p in (loc.get("query") or "").split(",") if p.strip()]
    if len(parts) < 3 or re.fullmatch(r"-?[\d.]+", parts[-1]):
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", parts[-2].lower()).strip("-")
    return ZOMATO_CITY_ALIASES.get(slug, slug)


def home_url(site: str, location: dict | None = None) -> str:
    home = SITES[site]["home"]
    if "{city}" in home:
        city = city_slug(location)
        return home.format(city=city) if city else home.split("{city}")[0]
    return home

LOGIN_URL = re.compile(r"login|signin|sign-in|/auth\b|otp", re.I)


class SessionExpired(Exception):
    pass


class SiteThrottled(Exception):
    """The site rate-limited us (429) or served a bot challenge. kind: "rate_limited" | "challenge"."""

    def __init__(self, kind: str, retry_after_s: int | None = None):
        super().__init__(f"site throttled: {kind}")
        self.kind = kind
        self.retry_after_s = retry_after_s


CHALLENGE_MARKERS = re.compile(
    r"cf-chl|challenge-platform|cf_chl_opt|Attention Required! \| Cloudflare|px-captcha|_pxCaptcha|perimeterx"
    r"|captcha-delivery|datadome|Access Denied.{0,200}Reference #|Request unsuccessful\. Incapsula"
    r"|Just a moment\.\.\.|Checking your browser|Verify you are human|Press & Hold"
    r"|Enable JavaScript and cookies to continue",
    re.I | re.S)
RETRY_AFTER_CAP = 3600
BLOCK_SCAN_CHARS = 20_000


def classify_block(status: int, text: str) -> str | None:
    if status == 429:
        return "rate_limited"
    if CHALLENGE_MARKERS.search(text or ""):
        return "challenge"
    return None


def parse_retry_after(value: str | None, now: datetime | None = None) -> int | None:
    """Retry-After as seconds or an HTTP-date -> seconds from now, capped at 3600; None if unparseable."""
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return min(int(value), RETRY_AFTER_CAP)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0, min(int((when - now).total_seconds()), RETRY_AFTER_CAP))


_PAGE_BLOCK_JS = "() => document.title + '\n' + ((document.body && document.body.innerText) || '').slice(0, %d)" % BLOCK_SCAN_CHARS


def raise_if_blocked(page) -> None:
    """One cheap evaluate on the loaded page (safe on a reused warm Chrome); raises SiteThrottled on a challenge."""
    text = page.evaluate(_PAGE_BLOCK_JS)
    if CHALLENGE_MARKERS.search(text or ""):
        raise SiteThrottled("challenge")


# Writes (any method but GET/HEAD) never follow a redirect: redirect 'manual' hands back an opaque redirect, which
# fetch_in_page raises as RedirectedWrite before the redirected request could be sent anywhere.
_FETCH_JS = """async ({url, method, headers, body}) => {
  const m = (method || 'GET').toUpperCase();
  const redirect = (m === 'GET' || m === 'HEAD') ? 'follow' : 'manual';
  const r = await fetch(url, {method, headers, body, credentials: 'include', redirect, signal: AbortSignal.timeout(60000)});
  if (r.type === 'opaqueredirect' || (r.status >= 300 && r.status < 400))
    return {status: r.status, url: r.url, text: '', retryAfter: null, redirected: true};
  return {status: r.status, url: r.url, text: await r.text(), retryAfter: r.headers.get('retry-after')};
}"""


class RedirectedWrite(RuntimeError):
    """A write we sent was answered with a redirect; it was not followed (stop: redirected_write)."""


RETRY_STATUS = {403, 429, 500, 502, 503, 504}


def _raise_if_throttled_response(r) -> None:
    """A 429 or a bot-challenge body is never retried (the 4 s / 8 s waits only make it worse and cost sync time)."""
    if r.status == 429:
        raise SiteThrottled("rate_limited", parse_retry_after((r.headers or {}).get("retry-after")))
    try:
        body = r.text()[:BLOCK_SCAN_CHARS]
    except Exception:
        return
    if CHALLENGE_MARKERS.search(body):
        raise SiteThrottled("challenge")


def goto(page, url: str, tries: int = 3) -> None:
    """page.goto that retries transient bot-filter/5xx responses (e.g. Swiggy's first-load 403)."""
    for i in range(tries):
        try:
            r = page.goto(url, wait_until="commit")
            if not r or r.status not in RETRY_STATUS:
                break
            _raise_if_throttled_response(r)
            log.warning("HTTP %s on %s, retrying", r.status, urlsplit(url).path)
        except SiteThrottled:
            raise
        except Exception as e:
            if "ERR_ABORTED" in str(e) or "interrupted by another navigation" in str(e):
                # SPAs (Zomato) replace a navigation with their own client-side one: fine if we landed on the site.
                page.wait_for_timeout(1500)
                if not page.url.startswith(("chrome-error://", "about:")):
                    break
            if "ERR_HTTP_RESPONSE_CODE_FAILURE" not in str(e) and "ERR_ABORTED" not in str(e) or i == tries - 1:
                raise
            log.warning("navigation to %s failed (%s), retrying", urlsplit(url).path, str(e).split(" at ")[0][-40:])
        page.wait_for_timeout(4000 * (i + 1))
    _settle(page)


def _settle(page) -> None:
    """Wait for the document to be interactive (a load-state wait, not a fixed sleep); best effort."""
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass


def on_origin(page, url: str) -> bool:
    """Whether the page is already on url's origin (so goto can be skipped)."""
    try:
        a, b = urlsplit(page.url), urlsplit(url)
    except Exception:
        return False
    return bool(b.netloc) and (a.scheme, a.netloc) == (b.scheme, b.netloc)


MAX_PARALLEL_FETCH = 4
_FETCH_MANY_JS = """async ({reqs}) => Promise.all(reqs.map(async ({url, method, headers, body}) => {
  try {
    const m = (method || 'GET').toUpperCase();
    const redirect = (m === 'GET' || m === 'HEAD') ? 'follow' : 'manual';
    const r = await fetch(url, {method, headers, body, credentials: 'include', redirect, signal: AbortSignal.timeout(60000)});
    if (r.type === 'opaqueredirect' || (r.status >= 300 && r.status < 400))
      return {status: r.status, url: r.url, text: '', retryAfter: null, redirected: true};
    return {status: r.status, url: r.url, text: await r.text(), retryAfter: r.headers.get('retry-after')};
  } catch (e) { return {status: 0, url, text: String(e), retryAfter: null, error: true}; }
}))"""


def _prepare(req: dict) -> dict:
    headers = {k: v for k, v in (req.get("headers") or {}).items() if not SECRET_HEADER.search(k)
               and not k.lower().startswith(("sec-", "content-length", "host", "accept-encoding"))}
    body = req.get("body")
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
        headers.setdefault("content-type", "application/json")
    return {"url": req["url"], "method": req["method"], "headers": headers, "body": body}


def _parse_fetch(r: dict) -> dict:
    if r.get("error"):
        raise RuntimeError("fetch failed")
    if r.get("redirected"):
        raise RedirectedWrite("redirected_write")
    if kind := classify_block(r["status"], r["text"]):
        raise SiteThrottled(kind, parse_retry_after(r.get("retryAfter")))
    if r["status"] in (401, 403) or LOGIN_URL.search(r["url"]):
        raise SessionExpired(f"HTTP {r['status']}")
    if r["status"] >= 400:
        raise RuntimeError(f"HTTP {r['status']}: {r['text'][:500]}")
    return json.loads(r["text"])


def fetch_many_in_page(page, reqs: list[dict]) -> list:
    """Run independent requests inside the one page, at most MAX_PARALLEL_FETCH at a time (Promise.all per
    batch). One entry per request: the parsed JSON, or the exception for that request (SessionExpired and
    SiteThrottled included; the caller re-raises those)."""
    out: list = []
    for i in range(0, len(reqs), MAX_PARALLEL_FETCH):
        batch = [_prepare(r) for r in reqs[i:i + MAX_PARALLEL_FETCH]]
        for r in page.evaluate(_FETCH_MANY_JS, {"reqs": batch}):
            try:
                out.append(_parse_fetch(r))
            except Exception as e:
                out.append(e)
    return out


class ChromePool:
    """One warm Chrome handle, reused by consecutive jobs for the same profile key and closed after idle_s
    without use. A different key closes the old handle first (one Chrome per worker)."""

    def __init__(self, idle_s: float = 120, clock=time.monotonic):
        self.idle_s, self.clock = idle_s, clock
        self.key = self.handle = None
        self.last_used = 0.0

    def get(self, key, start, close):
        if self.handle is not None and self.key == key and self.clock() - self.last_used <= self.idle_s:
            self.last_used = self.clock()
            return self.handle
        self.close(close)
        self.key, self.handle, self.last_used = key, start(key), self.clock()
        return self.handle

    def close_idle(self, close) -> None:
        if self.handle is not None and self.clock() - self.last_used > self.idle_s:
            self.close(close)

    def close(self, close) -> None:
        h, self.handle, self.key = self.handle, None, None
        if h is not None:
            close(h)


def fetch_in_page(page, req: dict) -> dict:
    """Run a request from inside the page so the site's own cookies apply. Returns parsed JSON."""
    r = page.evaluate(_FETCH_JS, _prepare(req))
    if r.get("redirected"):
        raise RedirectedWrite("redirected_write")
    if kind := classify_block(r["status"], r["text"]):
        raise SiteThrottled(kind, parse_retry_after(r.get("retryAfter")))
    if r["status"] in (401, 403) or LOGIN_URL.search(r["url"]):
        raise SessionExpired(f"HTTP {r['status']}")
    if r["status"] >= 400:
        raise RuntimeError(f"HTTP {r['status']}: {r['text'][:500]}")
    return json.loads(r["text"])


SECRET_HEADER = re.compile(r"cookie|auth|token|session|csrf|xsrf|api-?key|secret|signature|^:", re.I)
SAMPLE_LIMIT = 20_000


def load_settings() -> dict:
    return json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}


def save_location(loc: dict) -> None:
    SETTINGS.write_text(json.dumps(load_settings() | {"location": loc}, indent=2), encoding="utf-8")


def resolve_location(text: str) -> dict:
    """'lat,lon' or an address/pincode (geocoded via OpenStreetMap Nominatim) -> {query, lat, lon}."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", text)
    if m:
        return {"query": text.strip(), "lat": float(m[1]), "lon": float(m[2])}
    # Societies are often unknown to OSM: drop leading parts (society -> area -> city) until something matches.
    parts = [p.strip() for p in text.split(",") if p.strip()]
    for i in range(len(parts)):
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
            {"q": ", ".join(parts[i:]), "format": "json", "limit": 1, "countrycodes": "in"})
        req = urllib.request.Request(url, headers={"User-Agent": "expense-agent/1.0 (local personal use)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            hits = json.loads(r.read().decode("utf-8"))
        if hits:
            # The full text is kept as "query": that is what gets typed into a site's location search.
            return {"query": text.strip(), "lat": float(hits[0]["lat"]), "lon": float(hits[0]["lon"]),
                    "display": hits[0].get("display_name")}
    raise ValueError(f"could not find {text!r}; check spelling or paste 'lat,lon' from Google Maps")


def location_is_detailed(loc: dict | None) -> bool:
    """Need society/area + city + state; a bare 'City, State' lands on the city centre."""
    return bool(loc) and (len([p for p in loc["query"].split(",") if p.strip()]) >= 3
                          or bool(re.fullmatch(r"\s*-?\d+(\.\d+)?\s*,\s*-?\d+(\.\d+)?\s*", loc["query"])))


def scrub_headers(headers: dict) -> dict:
    return {k: v for k, v in headers.items() if not SECRET_HEADER.search(k)}


class Capture:
    """Collects every JSON response seen by the context."""

    def __init__(self):
        self.items: list[dict] = []

    def install(self, context) -> None:
        context.on("response", self._on_response)

    def _on_response(self, response) -> None:
        try:
            if "json" not in (response.headers.get("content-type") or ""):
                return
            text = response.text()
            data = json.loads(text)
        except Exception:
            return
        req = response.request
        try:
            body = req.post_data
        except Exception:
            body = None
        self.items.append({
            "id": len(self.items),
            "url": response.url,
            "method": req.method,
            "status": response.status,
            "request_headers": scrub_headers(req.headers),
            "request_body": body,
            "sample": text[:SAMPLE_LIMIT],
            "size": len(text),
            "json": data,  # full body, in memory only
        })

    def clear(self) -> None:
        self.items.clear()


CHROME_ENV_ALLOW = tuple(
    "PATH HOME USER LOGNAME DISPLAY TZ LANG LANGUAGE LC_ALL XDG_RUNTIME_DIR XDG_CONFIG_HOME XDG_CACHE_HOME TMPDIR "
    "FONTCONFIG_PATH FONTCONFIG_FILE SYSTEMROOT WINDIR COMSPEC PATHEXT LOCALAPPDATA APPDATA USERPROFILE TEMP TMP "
    "PROGRAMFILES PROGRAMFILES(X86) PROGRAMDATA NUMBER_OF_PROCESSORS PROCESSOR_ARCHITECTURE".split())
_ENV_ALLOWED = frozenset(CHROME_ENV_ALLOW)


def chrome_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Only the allow-listed variables: Chrome (and any page script) must never see DATABASE_URL, keys, proxy creds."""
    return {k: v for k, v in (os.environ if base is None else base).items() if k.upper() in _ENV_ALLOWED}


_CHROME_PATHS = (r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                 r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                 r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")


def find_chrome() -> str:
    load_dotenv()
    for p in (os.getenv("CHROME_PATH"), *_CHROME_PATHS):
        if p and Path(os.path.expandvars(p)).is_file():
            return os.path.expandvars(p)
    if found := shutil.which("chrome") or shutil.which("google-chrome"):
        return found
    raise RuntimeError("Google Chrome not found; install it or set CHROME_PATH in .env")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_cdp(port: int, proc: subprocess.Popen, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            # Chrome hands off to an instance already using this profile and exits.
            raise RuntimeError("Chrome exited at startup: close any Chrome window still open on this "
                               "site's profile (from an earlier run) and try again")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).close()
            return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("Chrome did not open its debugging port in time")


class _BypassRecord:
    """The pages already set to bypass service workers, held by identity. It keeps the page objects themselves
    (never id(page), which a closed page can hand on to a new one); it lives for one cart-mode open only."""

    def __init__(self):
        self._pages: list = []

    def __contains__(self, page) -> bool:
        return any(p is page for p in self._pages)

    def add(self, page) -> None:
        if page not in self:
            self._pages.append(page)


def _bypass_service_workers(context, page, done: _BypassRecord) -> None:
    """Cart mode: make the page's requests bypass service workers, so every fetch goes through context.route."""
    if page in done:
        return
    session = context.new_cdp_session(page)
    session.send("Network.enable")
    session.send("Network.setBypassServiceWorker", {"bypass": True})
    done.add(page)


_UNREGISTER_SW_JS = """async () => {
  if (!('serviceWorker' in navigator)) return 0;
  const regs = await navigator.serviceWorker.getRegistrations();
  const done = await Promise.all(regs.map((r) => r.unregister()));
  return done.filter(Boolean).length;
}"""


def unregister_service_workers(page) -> int:
    """Cart mode: unregister the page origin's service workers, so none can send a background fetch that skips
    context.route (background or periodic sync). Returns how many were removed; raises when it cannot run."""
    return int(page.evaluate(_UNREGISTER_SW_JS) or 0)


def _block_all(route, request=None) -> None:
    """The idle route of a parked warm Chrome: nothing leaves it until the next job's guard is in place."""
    with contextlib.suppress(Exception):
        route.abort()


POOL = ChromePool()
_warm_key: contextvars.ContextVar = contextvars.ContextVar("warm_chrome_key", default=None)


class WarmChrome:
    """A Chrome the worker keeps open between jobs for one (user, profile, mode). Owns its Playwright, and
    `cleanups` (the proxy relay, the profile directory) that run when it is finally closed."""

    def __init__(self, key, proc, pw, browser, cdp, context, profile, proxy_server):
        self.key, self.proc, self.pw, self.browser, self.cdp = key, proc, pw, browser, cdp
        self.context, self.profile, self.proxy_server = context, profile, proxy_server
        self.cleanups: list = []
        self.idle_route = None  # the block-all route while parked between jobs (None while a job uses it)
        self.relay = None  # the proxy relay this Chrome was launched through (metered by delta per job)
        self.version = None  # the vault object version this profile was restored from
        self.saver = None  # saves the profile to the vault; runs once, after Chrome has exited

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self, save: bool = True) -> None:
        """Close Chrome, then (save=True) run the saver, then the cleanups (relay, profile directory). The save
        happens only after Chrome has exited, so the profile is never packed under a live browser."""
        with contextlib.suppress(Exception):
            self.cdp.send("Browser.close")
        try:
            self.proc.wait(timeout=10)
        except Exception:
            with contextlib.suppress(Exception):
                self.proc.kill()
        with contextlib.suppress(Exception):
            self.pw.stop()
        saver, self.saver = self.saver, None
        if save and saver is not None and self.proc.poll() is not None:
            try:
                saver()
            except Exception as e:
                log.warning("warm profile not saved (%s)", type(e).__name__)
        for fn in reversed(self.cleanups):
            with contextlib.suppress(Exception):
                fn()


@contextlib.contextmanager
def warm_scope(key):
    """Jobs that open a site inside this scope keep their Chrome warm in POOL under `key`."""
    token = _warm_key.set(key)
    try:
        yield
    finally:
        _warm_key.reset(token)


def warm_handle(key) -> "WarmChrome | None":
    """The pooled Chrome for `key` if it is still open and fresh; no other key matches."""
    h = POOL.handle
    if h is not None and POOL.key == key and h.alive() and POOL.clock() - POOL.last_used <= POOL.idle_s:
        return h
    return None


def close_warm(save: bool = True) -> None:
    """Close the pooled Chrome. save=True (idle, key switch, shutdown) stores its profile in the vault; save=False
    (a failed job, needs_login, throttling) discards what the browser holds."""
    POOL.close(lambda h: h.close(save=save))


def close_idle_warm() -> None:
    POOL.close_idle(lambda h: h.close())


def _launch_warm(key, profile: Path, proxy_server: str | None) -> WarmChrome:
    profile.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    chrome_args = [find_chrome(), f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                   f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                   "--hide-crash-restore-bubble", "--window-size=1280,960"]
    if proxy_server:
        chrome_args += [f"--proxy-server={proxy_server}", "--proxy-bypass-list=<-loopback>",
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
    chrome_args += os.environ.get("CHROME_EXTRA_ARGS", "").split()
    proc = subprocess.Popen(chrome_args + ["about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=chrome_env())
    pw = None
    try:
        _wait_for_cdp(port, proc)
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        context = browser.contexts[0]
        cdp = browser.new_browser_cdp_session()
        # Read-only: no downloads, browser-wide and for the default context. A failure here aborts the launch.
        cdp.send("Browser.setDownloadBehavior", {"behavior": "deny"})
        for ctx_id in {t["browserContextId"] for t in cdp.send("Target.getTargets")["targetInfos"]
                       if t.get("browserContextId")}:
            cdp.send("Browser.setDownloadBehavior", {"behavior": "deny", "browserContextId": ctx_id})
        return WarmChrome(key, proc, pw, browser, cdp, context, profile, proxy_server)
    except BaseException:
        with contextlib.suppress(Exception):
            proc.kill()
        if pw is not None:
            with contextlib.suppress(Exception):
                pw.stop()
        raise


def _park(context, h: "WarmChrome", guard, capture) -> None:
    """Hand a read-mode warm Chrome back for idle: every page on about:blank, the extra pages closed, the job's
    geolocation grant cleared, and a block-all route installed before the job's guard is removed, so the idle
    browser sends nothing and there is never a moment without a route."""
    pages = list(context.pages)
    for p in pages[1:]:
        p.close()
    if pages:
        pages[0].goto("about:blank")
    context.clear_permissions()
    context.remove_listener("response", capture._on_response)
    context.route("**/*", _block_all)
    h.idle_route = _block_all
    context.unroute("**/*", guard._handle)


@contextlib.contextmanager
def _open_site_warm(key, site, allowlist, profile_dir, location, proxy_server, block_media):
    """Read mode only: a cart-mode Chrome is never kept warm (open_site opens it cold)."""
    h = warm_handle(key)
    if h is None:
        close_warm()
        h = _launch_warm(key, (profile_dir or PROFILES / SITES[site]["profile"]).resolve(), proxy_server)
        POOL.key, POOL.handle = key, h
    POOL.last_used = POOL.clock()
    context = h.context
    guard = Guard(allowlist, False, block_media)
    capture = Capture()
    loc = effective_location(location)
    try:
        guard.install(context)
        if h.idle_route is not None:  # the block-all route goes only now that this job's guard is in place
            context.unroute("**/*", h.idle_route)
            h.idle_route = None
        if loc.get("lat") is not None and loc.get("lon") is not None:
            context.grant_permissions(["geolocation"])
            context.set_geolocation({"latitude": loc["lat"], "longitude": loc["lon"]})
        capture.install(context)
        page = context.pages[0] if context.pages else context.new_page()
        yield context, page, guard, capture
    except BaseException:
        close_warm(save=False)  # a job that blew up leaves no half-used browser behind
        raise
    finally:
        if POOL.handle is h:  # still pooled: park it behind a block-all route for the next job
            try:
                _park(context, h, guard, capture)
            except Exception as e:
                log.warning("warm Chrome not parked (%s); closing it", type(e).__name__)
                close_warm()  # the job itself succeeded: the profile is saved per the usual close rules
            else:
                POOL.last_used = POOL.clock()


def open_site(site: str, allowlist: list[dict] | None = None, login_mode: bool = False,
              profile_dir: Path | None = None, location: dict | None = None,
              proxy_server: str | None = None,
              block_media: bool = False, cart_policy=None):
    """open_site, or its warm variant inside a warm_scope (never for a login, never in cart mode): same yield, but
    Chrome stays open in POOL for the worker's next job for the same (user, profile, mode). A cart-mode Chrome
    always opens cold and exits as soon as the fill or cart learn ends."""
    key = _warm_key.get()
    if key is not None and not login_mode and cart_policy is None:
        return _open_site_warm(key, site, allowlist, profile_dir, location, proxy_server, block_media)
    return _open_site_cold(site, allowlist, login_mode, profile_dir, location, proxy_server, block_media,
                           cart_policy)


@contextlib.contextmanager
def _open_site_cold(site: str, allowlist: list[dict] | None = None, login_mode: bool = False,
              profile_dir: Path | None = None, location: dict | None = None,
              proxy_server: str | None = None,
              block_media: bool = False, cart_policy=None):
    """Yield (context, page, guard, capture) for a site's persistent profile.

    profile_dir / location override the per-site profile folder and the saved delivery location (per-user runs);
    location None uses the saved one, {} means no location (no geolocation grant or override).
    block_media aborts image/media/font requests (user syncs, to save proxy bytes); the guard ignores it in
    login mode.

    cart_policy (a cart_guard.CartPolicy) installs the add-only CartGuard instead of the read-only Guard; it is
    refused together with login_mode, before Chrome starts. block_media applies to the CartGuard.

    Chrome is started as a normal browser (no --enable-automation, so no "controlled by automated
    test software" mode that bot filters like Swiggy's reject) and Playwright attaches over CDP.
    """
    if login_mode and cart_policy is not None:
        raise ValueError("cart mode cannot log in")
    cfg = SITES[site]
    profile = (profile_dir or PROFILES / cfg["profile"]).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    cart_mode = cart_policy is not None
    guard = (CartGuard(cart_policy, block_media, site=site) if cart_mode
             else Guard(allowlist, login_mode, block_media))
    capture = Capture()
    loc = effective_location(location)
    port = _free_port()
    chrome_args = [find_chrome(), f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
                   f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                   "--hide-crash-restore-bubble", "--window-size=1280,960"]
    if proxy_server:
        # <-loopback> clears Chrome's implicit loopback bypass so even 127.0.0.1 goes via the relay
        chrome_args += [f"--proxy-server={proxy_server}", "--proxy-bypass-list=<-loopback>",
                        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
    chrome_args += os.environ.get("CHROME_EXTRA_ARGS", "").split()  # e.g. --no-sandbox in a container
    proc = subprocess.Popen(
        chrome_args + ["about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=chrome_env())
    try:
        _wait_for_cdp(port, proc)
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0]
            cdp = browser.new_browser_cdp_session()
            # Read-only: no downloads, browser-wide and for the default context. Not wrapped in try -
            # if this fails we must not continue.
            cdp.send("Browser.setDownloadBehavior", {"behavior": "deny"})
            for ctx_id in {t["browserContextId"] for t in cdp.send("Target.getTargets")["targetInfos"]
                           if t.get("browserContextId")}:
                cdp.send("Browser.setDownloadBehavior", {"behavior": "deny", "browserContextId": ctx_id})
            if loc.get("lat") is not None and loc.get("lon") is not None:
                # "Detect my location" resolves to the saved address without a permission prompt
                context.grant_permissions(["geolocation"])
                context.set_geolocation({"latitude": loc["lat"], "longitude": loc["lon"]})
            guard.install(context)
            capture.install(context)
            on_page = None
            if cart_mode:
                # Service-worker fetches skip context.route: every page (and any page opened later) bypasses them.
                bypassed = _BypassRecord()
                for p in context.pages:
                    _bypass_service_workers(context, p, bypassed)  # raises: never run cart mode unguarded

                def on_page(p):
                    try:
                        _bypass_service_workers(context, p, bypassed)
                    except Exception:
                        log.warning("service-worker bypass failed on a new page; closing it")
                        with contextlib.suppress(Exception):
                            p.close()

                context.on("page", on_page)
            page = context.pages[0] if context.pages else context.new_page()
            if cart_mode:
                _bypass_service_workers(context, page, bypassed)
            try:
                yield context, page, guard, capture
            finally:
                try:  # detach handlers first so in-flight responses don't raise CancelledError on close
                    if cart_mode:
                        context.remove_listener("page", on_page)
                        context.remove_listener("response", guard._on_response)
                    context.remove_listener("response", capture._on_response)
                    context.unroute_all(behavior="ignoreErrors")
                    cdp.send("Browser.close")
                except Exception:
                    pass
    finally:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

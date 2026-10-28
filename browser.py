"""Persistent headful Chrome per site, with the read-only guard and JSON response capture."""
import contextlib
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


_FETCH_JS = """async ({url, method, headers, body}) => {
  const r = await fetch(url, {method, headers, body, credentials: 'include', signal: AbortSignal.timeout(60000)});
  return {status: r.status, url: r.url, text: await r.text(), retryAfter: r.headers.get('retry-after')};
}"""


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
            r = page.goto(url, wait_until="domcontentloaded")
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
    page.wait_for_timeout(2500)


def fetch_in_page(page, req: dict) -> dict:
    """Run a request from inside the page so the site's own cookies apply. Returns parsed JSON."""
    headers = {k: v for k, v in (req.get("headers") or {}).items() if not SECRET_HEADER.search(k)
               and not k.lower().startswith(("sec-", "content-length", "host", "accept-encoding"))}
    body = req.get("body")
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
        headers.setdefault("content-type", "application/json")
    r = page.evaluate(_FETCH_JS, {"url": req["url"], "method": req["method"], "headers": headers, "body": body})
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


@contextlib.contextmanager
def open_site(site: str, allowlist: list[dict] | None = None, login_mode: bool = False,
              profile_dir: Path | None = None, location: dict | None = None,
              proxy_server: str | None = None, block_media: bool = False):
    """Yield (context, page, guard, capture) for a site's persistent profile.

    profile_dir / location override the per-site profile folder and the saved delivery location (per-user runs);
    location None uses the saved one, {} means no location (no geolocation grant or override).
    block_media aborts image/media/font requests (user syncs, to save proxy bytes); the guard ignores it in
    login mode.

    Chrome is started as a normal browser (no --enable-automation, so no "controlled by automated
    test software" mode that bot filters like Swiggy's reject) and Playwright attaches over CDP.
    """
    cfg = SITES[site]
    profile = (profile_dir or PROFILES / cfg["profile"]).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    guard, capture = Guard(allowlist, login_mode, block_media), Capture()
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
            page = context.pages[0] if context.pages else context.new_page()
            try:
                yield context, page, guard, capture
            finally:
                try:  # detach handlers first so in-flight responses don't raise CancelledError on close
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

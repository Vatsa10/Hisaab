"""Read-only request guard. Pure decision logic + Playwright route wiring."""
import logging
import re
from urllib.parse import urlsplit

log = logging.getLogger("guard")

# ponytail: spec regex verbatim; "pay" also hits e.g. "/display", which is fine for navigations only.
BLOCKED_URL = re.compile(r"cart|checkout|payment|pay|address|place-?order|cancel", re.I)
MUTATION = re.compile(r"\bmutation\b", re.I)
# Bot-challenge POSTs (AWS WAF in front of Swiggy: token, captcha and edge SDK report/telemetry; Cloudflare).
# They go to the WAF vendor, not the user's account; blocking any of them fails the challenge and the site
# shows a blank page or "Max challenge attempts exceeded".
CHALLENGE_POST = re.compile(r"^https://[\w.-]+\.awswaf\.com/|/cdn-cgi/challenge-platform/", re.I)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def redact_url(url: str) -> str:
    """Drop query/fragment before logging - they can carry tokens."""
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def allowlisted(url: str, body: str | None, allowlist: list[dict]) -> bool:
    if body and MUTATION.search(body):
        return False
    return any(re.search(e["url_regex"], url) for e in allowlist)


def decide(method: str, url: str, is_nav: bool, body: str | None,
           login_mode: bool, allowlist: list[dict]) -> tuple[bool, str]:
    """Return (allowed, reason)."""
    method = method.upper()
    if is_nav and BLOCKED_URL.search(url):
        return False, "blocked navigation"
    if method in SAFE_METHODS:
        return True, "safe method"
    if method != "POST":
        return False, f"{method} never allowed"
    if BLOCKED_URL.search(urlsplit(url).path):
        return False, "POST to blocked path"
    if CHALLENGE_POST.search(url):
        return True, "bot-challenge token"
    if login_mode:
        return True, "login mode"
    if allowlisted(url, body, allowlist):
        return True, "read POST allowlist"
    return False, "POST not allowlisted"


class Guard:
    """Holds mutable state (login mode, allowlist) and records what it blocked."""

    def __init__(self, allowlist: list[dict] | None = None, login_mode: bool = False):
        self.allowlist = list(allowlist or [])
        self.login_mode = login_mode
        self.blocked: list[dict] = []

    def install(self, context) -> None:
        context.route("**/*", self._handle)

    def _handle(self, route, request) -> None:
        try:
            body = request.post_data
        except Exception:  # binary bodies
            body = None
        ok, reason = decide(request.method, request.url, request.is_navigation_request(),
                            body, self.login_mode, self.allowlist)
        if ok:
            route.continue_()
            return
        # Background POSTs (analytics beacons) are blocked by the dozen; only navigations/other methods are news.
        quiet = request.method == "POST" and not request.is_navigation_request()
        log.log(logging.DEBUG if quiet else logging.WARNING, "BLOCKED %s %s (%s)",
                request.method, redact_url(request.url), reason)
        self.blocked.append({"method": request.method, "url": request.url,
                             "body": (body or "")[:4000], "reason": reason})
        route.abort("blockedbyclient")

"""OTP auto-forward from a paired Android phone (the Hisaab OTP companion app).

Pairing: a signed-in user asks for a short single-use code (10 minutes), types it into the phone app, and the phone
trades it for a device bearer token, shown once. Only sha256 hashes of codes and tokens are stored.

Forwarding: the phone sends {sender, body, ts} for SMS whose sender is on the allowlist. The server checks the sender
again, pulls the code out of the text, and relays it over the same NOTIFY channel as POST /jobs/{id}/otp, and only to
that user's own running connect or fill job that is waiting for an OTP on the matching app. Every outcome answers
204 with no body. The SMS body, the code and the sender text are never stored and never logged: logs carry counts and
exception class names only.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

log = logging.getLogger("server.otp_devices")

router = APIRouter(prefix="/otp-devices")
_bearer = HTTPBearer(auto_error=False)

SENDERS_FILE = Path(__file__).parent / "data" / "otp_senders.json"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I/L
CODE_LEN = 8
PAIRING_TTL_S = 600
PAIRINGS_PER_HOUR = 5
PAIR_TRIES_PER_IP = 10          # per PAIR_WINDOW_S
PAIR_WINDOW_S = 3600
SMS_PER_DEVICE = 10             # per SMS_WINDOW_S
SMS_WINDOW_S = 60
TS_MAX_AGE_S = 120
TS_MAX_AHEAD_S = 60
PAIR_FAILS_GLOBAL = 100         # failed /pair tries per PAIR_WINDOW_S across all IPs
PAIR_FAIL_DELAY_S = 3.0
PROMPT_SLACK_S = 5
SEEN_TTL_S = 300
PURGE_AFTER = "1 day"
MAX_SENDER = 32
MAX_BODY = 1000

# Which jobs.site values each brand's OTP may go to (Instamart logs in through Swiggy).
BRAND_SITES = {"zomato": ("zomato",), "swiggy": ("swiggy", "instamart"), "zepto": ("zepto",),
               "blinkit": ("blinkit",)}

_KEYWORD = re.compile(r"\b(?:otp|code|verification|one[\s-]?time[\s-]?password)\b", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![\d,.])\d{4,8}(?![\d,])")
_CURRENCY_BEFORE = re.compile(r"(?:rs\.?|inr|₹)\s*$", re.IGNORECASE)


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------------------------------- filter (pure)

def load_senders(path: Path = SENDERS_FILE) -> dict[str, list[str]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: list(v) for k, v in raw.items() if not k.startswith("_")}


_compiled: list[tuple[str, re.Pattern]] | None = None


def _patterns() -> list[tuple[str, re.Pattern]]:
    global _compiled
    if _compiled is None:
        _compiled = [(brand, re.compile(p, re.IGNORECASE)) for brand, ps in load_senders().items() for p in ps]
    return _compiled


def brand_for_sender(sender: str) -> str | None:
    """The brand whose allowlisted sender-ID pattern matches the whole sender, or None."""
    s = (sender or "").strip()
    for brand, p in _patterns():
        if p.fullmatch(s):
            return brand
    return None


def extract_code(body: str) -> str | None:
    """A 4-8 digit code from an OTP text, or None. Needs an OTP keyword, skips amounts (Rs/INR/₹), and refuses a text
    with more than one candidate number rather than guess."""
    if not body or not _KEYWORD.search(body):
        return None
    found = {m.group(0) for m in _NUMBER.finditer(body) if not _CURRENCY_BEFORE.search(body[:m.start()])}
    return found.pop() if len(found) == 1 else None


# ---------------------------------------------------------------------------------------------------- rate limits

class Window:
    """In-process sliding-window counter (one API process per deployment; a restart resets it)."""

    def __init__(self, limit: int, seconds: int):
        self.limit, self.seconds = limit, seconds
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = _now()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= now - self.seconds:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


pair_limiter = Window(PAIR_TRIES_PER_IP, PAIR_WINDOW_S)
# Global ceiling on FAILED pair tries. Past it, a failure is answered after a delay, but every code is still checked,
# so a valid code always works and a flood from one (proxy) IP cannot lock real users out; it only gets slow.
pair_fail_limiter = Window(PAIR_FAILS_GLOBAL, PAIR_WINDOW_S)


class SeenCodes:
    """In-process TTL set of sha256(user, brand, code): the same OTP SMS is relayed once within SEEN_TTL_S."""

    def __init__(self, ttl: int):
        self.ttl = ttl
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def seen(self, key: str) -> bool:
        now = _now()
        with self._lock:
            self._seen = {k: t for k, t in self._seen.items() if t > now}
            return key in self._seen

    def add(self, key: str) -> None:
        with self._lock:
            self._seen[key] = _now() + self.ttl

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()


seen_codes = SeenCodes(SEEN_TTL_S)
sms_limiter = Window(SMS_PER_DEVICE, SMS_WINDOW_S)


# ---------------------------------------------------------------------------------------------------- helpers

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))


def _normalize_code(code: str) -> str:
    return re.sub(r"[\s-]", "", code or "").upper()


def _user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    from server import api
    return api.current_user(creds)


def _conn():
    from server import api
    yield from api.get_conn()


def _device(creds: HTTPAuthorizationCredentials | None = Depends(_bearer), conn=Depends(_conn)) -> tuple[str, str]:
    """(device id, user id) for a live device token. Revoked or unknown tokens get 401 at once."""
    if creds is None or not creds.credentials:
        raise HTTPException(401, "unauthorized")
    row = conn.execute("UPDATE otp_devices SET last_seen_at = now() WHERE token_hash = %s AND revoked_at IS NULL "
                       "RETURNING id, user_id, token_hash", (_sha(creds.credentials),)).fetchone()
    if row is None or not hmac.compare_digest(row[2], _sha(creds.credentials)):
        raise HTTPException(401, "unauthorized")
    return str(row[0]), str(row[1])


def _all_patterns() -> list[str]:
    return [p for ps in load_senders().values() for p in ps]


# ---------------------------------------------------------------------------------------------------- routes

@router.post("/pairing")
def create_pairing(user: str = Depends(_user), conn=Depends(_conn)):
    conn.execute("DELETE FROM otp_pairings WHERE expires_at < now() - %s::interval", (PURGE_AFTER,))
    recent = conn.execute("SELECT count(*) FROM otp_pairings WHERE user_id = %s "
                          "AND created_at > now() - interval '1 hour'", (user,)).fetchone()[0]
    if recent >= PAIRINGS_PER_HOUR:
        raise HTTPException(429, "Too many pairing codes. Try again in an hour.")
    code = new_code()
    conn.execute("INSERT INTO otp_pairings(code_hash, user_id, expires_at) "
                 "VALUES (%s, %s, now() + make_interval(secs => %s))", (_sha(code), user, PAIRING_TTL_S))
    return {"code": code, "expires_in": PAIRING_TTL_S}


async def _failed_pair():
    if not pair_fail_limiter.allow("all"):
        await asyncio.sleep(PAIR_FAIL_DELAY_S)
    raise HTTPException(400, "invalid or expired code")


@router.post("/pair")
async def pair(request: Request, conn=Depends(_conn)):
    ip = request.client.host if request.client else "unknown"
    if not pair_limiter.allow(ip):
        raise HTTPException(429, "Too many tries. Wait a while and try again.")
    try:
        data = await request.json()
    except Exception:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("code"), str):
        raise HTTPException(400, "invalid or expired code")
    code = _normalize_code(data["code"])
    name = data.get("name") if isinstance(data.get("name"), str) else None
    name = (name or "Android phone").strip()[:60] or "Android phone"
    if len(code) != CODE_LEN:
        await _failed_pair()
    want = _sha(code)
    row = conn.execute("UPDATE otp_pairings SET used_at = now() WHERE code_hash = %s AND used_at IS NULL "
                       "AND expires_at > now() RETURNING code_hash, user_id", (want,)).fetchone()
    if row is None or not hmac.compare_digest(row[0], want):
        await _failed_pair()
    token = secrets.token_urlsafe(32)
    device_id = conn.execute("INSERT INTO otp_devices(user_id, name, token_hash) VALUES (%s, %s, %s) RETURNING id",
                             (row[1], name, _sha(token))).fetchone()[0]
    log.info("otp device paired")
    return {"token": token, "device_id": str(device_id), "senders": _all_patterns()}


@router.get("/config")
def config(_device_ids: tuple[str, str] = Depends(_device)):
    return {"senders": _all_patterns()}


def _relay(conn, user_id: str, brand: str, code: str, ts_ms: float) -> int:
    """NOTIFY the code to this user's running connect/fill jobs that wait for an OTP on the brand's sites. Only an SMS
    sent at or after the wait began (progress.prompt_at minus PROMPT_SLACK_S) counts, so stale codes never match."""
    rows = conn.execute(
        "SELECT id FROM jobs WHERE user_id = %s AND status = 'running' AND type IN ('connect', 'fill_cart') "
        "AND progress->>'stage' = 'otp_needed' AND site = ANY(%s) "
        "AND (progress->>'prompt_at')::timestamptz - make_interval(secs => %s) <= to_timestamp(%s)",
        (user_id, list(BRAND_SITES[brand]), PROMPT_SLACK_S, ts_ms / 1000.0)).fetchall()
    for (jid,) in rows:
        conn.execute("SELECT pg_notify(%s, %s)", ("otp_" + str(jid).replace("-", ""), code))
    return len(rows)


@router.post("/sms", status_code=204)
async def sms(request: Request, device: tuple[str, str] = Depends(_device), conn=Depends(_conn)):
    device_id, user_id = device
    if not sms_limiter.allow(device_id):
        raise HTTPException(429, "slow down")
    relayed = 0
    try:
        data = await request.json()
        relayed = _handle_sms(conn, user_id, data)
    except Exception as e:      # never echo or log the message itself
        log.warning("otp sms dropped on error: %s", type(e).__name__)
    log.info("otp sms handled: relayed=%d", relayed)
    return Response(status_code=204)


def _handle_sms(conn, user_id: str, data) -> int:
    if not isinstance(data, dict):
        return 0
    sender, body, ts = data.get("sender"), data.get("body"), data.get("ts")
    if not isinstance(sender, str) or not isinstance(body, str) or len(sender) > MAX_SENDER or len(body) > MAX_BODY:
        return 0
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return 0
    age = _now() - ts / 1000.0          # ts is Unix epoch milliseconds, as Android reports it
    if age > TS_MAX_AGE_S or age < -TS_MAX_AHEAD_S:
        return 0
    brand = brand_for_sender(sender)
    if brand is None:
        return 0
    code = extract_code(body)
    if code is None:
        return 0
    key = _sha(f"{user_id}|{brand}|{code}")
    if seen_codes.seen(key):
        return 0
    n = _relay(conn, user_id, brand, code, ts)
    if n:
        seen_codes.add(key)
    return n


@router.get("")
def list_devices(user: str = Depends(_user), conn=Depends(_conn)):
    rows = conn.execute("SELECT id, name, created_at, last_seen_at FROM otp_devices WHERE user_id = %s "
                        "AND revoked_at IS NULL ORDER BY created_at DESC", (user,)).fetchall()
    iso = lambda d: d.astimezone(timezone.utc).isoformat() if isinstance(d, datetime) else None  # noqa: E731
    return {"devices": [{"id": str(r[0]), "name": r[1], "created_at": iso(r[2]), "last_seen_at": iso(r[3])}
                        for r in rows]}


@router.delete("/{device_id}", status_code=204)
def revoke(device_id: str, user: str = Depends(_user), conn=Depends(_conn)):
    import uuid
    try:
        did = str(uuid.UUID(device_id))
    except ValueError:
        raise HTTPException(404, "not found") from None
    row = conn.execute("UPDATE otp_devices SET revoked_at = now() WHERE id = %s AND user_id = %s "
                       "AND revoked_at IS NULL RETURNING id", (did, user)).fetchone()
    if row is None:
        raise HTTPException(404, "not found")
    return Response(status_code=204)

"""HTTP API: consent, connect, OTP and answer relay, refresh, delivery profile, Ask.
Never logs or stores request bodies that carry OTPs or answers; the delivery address is stored, never logged."""
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, StringConstraints

import browser
import sync
from browser import SITES
from server import ask, jobs
from server.auth import Unauthorized, user_id_from_token
from server.config import settings
from server.local_db import is_local_db

log = logging.getLogger("server.api")

DEFAULT_CORS_ORIGINS = "http://localhost:3000"


def ask_fake_enabled() -> bool:
    """ASK_FAKE=1 makes /ask answer deterministically without the LLM, for local end-to-end runs only."""
    if os.environ.get("ASK_FAKE", "") != "1":
        return False
    if not is_local_db(settings().database_url):
        raise RuntimeError("ASK_FAKE=1 is for local end-to-end tests only: DATABASE_URL must point at "
                           "localhost / 127.0.0.1 / ::1")
    return True


@asynccontextmanager
async def lifespan(_app):
    if ask_fake_enabled():  # raises on a non-local database, so the API refuses to start
        log.warning("ASK_FAKE=1: /ask answers from a fixed query without the LLM (local end-to-end runs only)")
    yield


class CorsFromEnv:
    """CORS for the PWA. Origins come from CORS_ORIGINS (comma-separated; default http://localhost:3000), read per
    request like every other setting."""

    def __init__(self, app):
        self.app = app
        self._by_origins: dict[tuple[str, ...], CORSMiddleware] = {}

    async def __call__(self, scope, receive, send):
        raw = os.environ.get("CORS_ORIGINS") or DEFAULT_CORS_ORIGINS
        origins = tuple(o.strip() for o in raw.split(",") if o.strip())
        mw = self._by_origins.get(origins)
        if mw is None:
            mw = self._by_origins[origins] = CORSMiddleware(
                self.app, allow_origins=list(origins), allow_methods=["GET", "POST", "PUT"],
                allow_headers=["Authorization", "Content-Type"])
        await mw(scope, receive, send)


app = FastAPI(title="expense-agent", lifespan=lifespan)
app.add_middleware(CorsFromEnv)
_bearer = HTTPBearer(auto_error=False)


class SiteBody(BaseModel):
    site: str


class PhoneBody(BaseModel):
    phone: str = Field(max_length=32)


class OtpBody(BaseModel):
    otp: str = Field(min_length=1, max_length=32)


class AnswerBody(BaseModel):
    answer: str = Field(min_length=1, max_length=500)


class ProfileBody(BaseModel):
    address: Annotated[str, StringConstraints(strip_whitespace=True, min_length=5, max_length=200)]
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None


class AskBody(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


def get_conn():
    conn = psycopg.connect(settings().database_url, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    if creds is None:
        raise HTTPException(401, "missing bearer token")
    try:
        return user_id_from_token(creds.credentials, settings())
    except Unauthorized:
        raise HTTPException(401, "invalid token") from None


def _known_site(site: str) -> None:
    if site not in SITES:
        raise HTTPException(400, "unknown site")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/consents", status_code=204)
def post_consent(body: SiteBody, user: str = Depends(current_user), conn=Depends(get_conn)):
    _known_site(body.site)
    conn.execute(
        "INSERT INTO consents (user_id, site, text_version) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
        (user, body.site, settings().consent_version),
    )
    return Response(status_code=204)


@app.post("/connections/{site}/connect", status_code=202)
def connect(site: str, body: PhoneBody, user: str = Depends(current_user), conn=Depends(get_conn)):
    _known_site(site)
    if site == "instamart":
        raise HTTPException(400, "Instamart orders arrive through the swiggy connection")
    try:
        phone = sync.normalize_phone(body.phone)
    except ValueError:
        raise HTTPException(422, "invalid phone number") from None
    consented = conn.execute(
        "SELECT 1 FROM consents WHERE user_id=%s AND site=%s AND text_version=%s",
        (user, site, settings().consent_version),
    ).fetchone()
    if consented is None:
        raise HTTPException(412, "consent required")
    try:
        job_id = jobs.enqueue(conn, user, "connect", site, {"phone": phone})
    except jobs.Conflict:
        raise HTTPException(409, "a connect job is already active") from None
    return {"job_id": job_id}


def _relay_to_running_connect(conn, job_id: str, user: str, text: str, stage: str | None = None) -> Response:
    """NOTIFY otp_<job id> with text, for the caller's own running connect job (and, when stage is given, only
    while the job's progress stage is that). The worker's prompt LISTENs on this one channel for OTPs and answers
    alike. The text is never stored or logged."""
    try:
        jid = str(uuid.UUID(job_id))
    except ValueError:
        raise HTTPException(404, "not found") from None
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE id=%s AND user_id=%s AND type='connect' AND status='running'"
        " AND (%s::text IS NULL OR progress->>'stage' = %s::text)",
        (jid, user, stage, stage),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "not found")
    conn.execute("SELECT pg_notify(%s, %s)", ("otp_" + jid.replace("-", ""), text))
    return Response(status_code=204)


@app.post("/jobs/{job_id}/otp", status_code=204)
def post_otp(job_id: str, body: OtpBody, user: str = Depends(current_user), conn=Depends(get_conn)):
    return _relay_to_running_connect(conn, job_id, user, body.otp)


@app.post("/jobs/{job_id}/answer", status_code=204)
def post_answer(job_id: str, body: AnswerBody, user: str = Depends(current_user), conn=Depends(get_conn)):
    return _relay_to_running_connect(conn, job_id, user, body.answer, stage="question_needed")


@app.post("/connections/{site}/refresh", status_code=202)
def refresh(site: str, user: str = Depends(current_user), conn=Depends(get_conn)):
    _known_site(site)
    row = conn.execute("SELECT 1 FROM connections WHERE user_id=%s AND site=%s", (user, site)).fetchone()
    if row is None:
        raise HTTPException(404, "no connection")
    connecting = conn.execute(
        "SELECT 1 FROM jobs WHERE user_id=%s AND site=%s AND type='connect' AND status IN ('queued','running')",
        (user, site),
    ).fetchone()
    if connecting is not None:  # the connect job syncs when it finishes; never two browsers on one profile
        raise HTTPException(409, "a connect job is active")
    try:
        job_id = jobs.enqueue(conn, user, "sync", site)
    except jobs.Conflict:
        raise HTTPException(409, "a sync job is already active") from None
    return {"job_id": job_id}


@app.put("/profile")
def put_profile(body: ProfileBody, user: str = Depends(current_user), conn=Depends(get_conn)):
    try:
        loc = browser.resolve_location(body.address)
    except ValueError:
        raise HTTPException(422, "address not found; add the area and city, or paste 'lat,lon'") from None
    except Exception as e:  # network or service trouble; the address itself is never logged
        log.warning("geocoding failed (%s)", type(e).__name__)
        raise HTTPException(503, "geocoding is unavailable, try again") from None
    lat, lon = float(loc["lat"]), float(loc["lon"])
    conn.execute(
        """INSERT INTO profiles (user_id, display_name, address, lat, lon) VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (user_id) DO UPDATE SET display_name=COALESCE(excluded.display_name, profiles.display_name),
             address=excluded.address, lat=excluded.lat, lon=excluded.lon""",
        (user, body.display_name or None, body.address, lat, lon),
    )
    return {"address": body.address, "lat": lat, "lon": lon}


@app.post("/ask")
def post_ask(body: AskBody, user: str = Depends(current_user), conn=Depends(get_conn)):
    if ask_fake_enabled():
        return {"answer": ask.fake_answer(conn, user)}
    try:
        text = ask.answer(conn, user, body.question)
    except Exception as e:  # LLM or database trouble; the question is not logged
        log.warning("ask failed (%s)", type(e).__name__)
        raise HTTPException(502, "the analyst is unavailable, try again") from None
    return {"answer": text}

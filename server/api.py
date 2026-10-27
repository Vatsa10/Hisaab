"""HTTP API: consent, connect, OTP relay, refresh. Never logs or stores request bodies (OTPs)."""
import uuid

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

import sync
from browser import SITES
from server import jobs
from server.auth import Unauthorized, user_id_from_token
from server.config import settings

app = FastAPI(title="expense-agent")
_bearer = HTTPBearer(auto_error=False)


class SiteBody(BaseModel):
    site: str


class PhoneBody(BaseModel):
    phone: str = Field(max_length=32)


class OtpBody(BaseModel):
    otp: str = Field(min_length=1, max_length=32)


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


@app.post("/jobs/{job_id}/otp", status_code=204)
def post_otp(job_id: str, body: OtpBody, user: str = Depends(current_user), conn=Depends(get_conn)):
    try:
        jid = str(uuid.UUID(job_id))
    except ValueError:
        raise HTTPException(404, "not found") from None
    row = conn.execute(
        "SELECT 1 FROM jobs WHERE id=%s AND user_id=%s AND type='connect' AND status='running'",
        (jid, user),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "not found")
    conn.execute("SELECT pg_notify(%s, %s)", ("otp_" + jid.replace("-", ""), body.otp))
    return Response(status_code=204)


@app.post("/connections/{site}/refresh", status_code=202)
def refresh(site: str, user: str = Depends(current_user), conn=Depends(get_conn)):
    _known_site(site)
    row = conn.execute("SELECT 1 FROM connections WHERE user_id=%s AND site=%s", (user, site)).fetchone()
    if row is None:
        raise HTTPException(404, "no connection")
    try:
        job_id = jobs.enqueue(conn, user, "sync", site)
    except jobs.Conflict:
        raise HTTPException(409, "a sync job is already active") from None
    return {"job_id": job_id}

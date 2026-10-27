"""Service settings, read from the environment at call time (never cached at import)."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    jwt_secret: str = ""
    jwks_url: str = ""
    consent_version: str = "2026-10-27"
    otp_timeout_s: int = 180
    stale_job_s: int = 2400


def settings() -> Settings:
    env = os.environ.get
    return Settings(
        database_url=env("DATABASE_URL", ""),
        jwt_secret=env("SUPABASE_JWT_SECRET", ""),
        jwks_url=env("SUPABASE_JWKS_URL", ""),
        stale_job_s=int(env("STALE_JOB_S", "2400")),
    )

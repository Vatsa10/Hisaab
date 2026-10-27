"""Service settings, read from the environment at call time (never cached at import)."""
import base64
import os
from dataclasses import dataclass, field

# Fixed key for local development only (file:// vault); never used unless ALLOW_DEV_PROFILE_KEY=1.
DEV_PROFILE_KEY = base64.b64encode(b"expense-agent-dev-profile-key-32"[:32]).decode()


@dataclass(frozen=True)
class Settings:
    # Secrets are left out of repr so a logged or printed Settings never carries them.
    database_url: str = field(default="", repr=False)
    jwt_secret: str = field(default="", repr=False)
    jwks_url: str = ""
    consent_version: str = "2026-10-27"
    otp_timeout_s: int = 180
    stale_job_s: int = 2400
    profile_store_url: str = "file://vault_local"
    profile_master_key: bytes | None = field(default=None, repr=False)
    proxy_url_template: str = field(default="", repr=False)
    heartbeat_s: int = 30


def _master_key(env, store_url: str, for_worker: bool) -> bytes | None:
    from server import vault
    raw = (env("PROFILE_MASTER_KEY", "") or "").strip()
    if raw:
        return vault.master_key_from_env(raw)
    if not for_worker:
        return None
    if not store_url.startswith("file://"):
        raise ValueError("PROFILE_MASTER_KEY is required when PROFILE_STORE_URL is not a file:// store")
    if env("ALLOW_DEV_PROFILE_KEY", "") == "1":
        return vault.master_key_from_env(DEV_PROFILE_KEY)
    raise ValueError("PROFILE_MASTER_KEY is not set; for a local file:// vault set ALLOW_DEV_PROFILE_KEY=1 "
                     "to use the fixed development key")


def settings(for_worker: bool = False) -> Settings:
    """for_worker=True also validates the vault configuration (the API never needs the profile key)."""
    env = os.environ.get
    store_url = env("PROFILE_STORE_URL", "") or "file://vault_local"
    return Settings(
        database_url=env("DATABASE_URL", ""),
        jwt_secret=env("SUPABASE_JWT_SECRET", ""),
        jwks_url=env("SUPABASE_JWKS_URL", ""),
        stale_job_s=int(env("STALE_JOB_S", "2400")),
        profile_store_url=store_url,
        profile_master_key=_master_key(env, store_url, for_worker),
        proxy_url_template=env("PROXY_URL_TEMPLATE", ""),
        heartbeat_s=int(env("HEARTBEAT_S", "30")),
    )

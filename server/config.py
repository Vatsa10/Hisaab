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


# Fixed text: the template carries the proxy password, so no part of it may reach an error or a log.
PROXY_TEMPLATE_INVALID = ("PROXY_URL_TEMPLATE is invalid: expected http://<user>:<password>@<host>:<port> with the "
                          "literal {session} placeholder and no other braces; percent-encode any / # ? @ : in the "
                          "user or password")


def _check_proxy_template(template: str) -> str:
    """Build one upstream URL from the template exactly as a job would, and parse it like the relay does."""
    if not template:
        return template
    from server.proxy_relay import _parse_upstream, proxy_url_for
    try:
        _parse_upstream(proxy_url_for(template, "0" * 12))
    except Exception:
        raise ValueError(PROXY_TEMPLATE_INVALID) from None
    return template


def settings(for_worker: bool = False) -> Settings:
    """for_worker=True also validates the vault configuration (the API never needs the profile key) and the
    proxy template, so a bad one stops the worker at start-up instead of failing every job."""
    env = os.environ.get
    store_url = env("PROFILE_STORE_URL", "") or "file://vault_local"
    proxy_template = env("PROXY_URL_TEMPLATE", "")
    if for_worker:
        _check_proxy_template(proxy_template)
    return Settings(
        database_url=env("DATABASE_URL", ""),
        jwt_secret=env("SUPABASE_JWT_SECRET", ""),
        jwks_url=env("SUPABASE_JWKS_URL", ""),
        stale_job_s=int(env("STALE_JOB_S", "2400")),
        profile_store_url=store_url,
        profile_master_key=_master_key(env, store_url, for_worker),
        proxy_url_template=proxy_template,
        heartbeat_s=int(env("HEARTBEAT_S", "30")),
    )

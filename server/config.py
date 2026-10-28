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
    stale_job_s: int = 300  # 10 heartbeat intervals without a beat: the worker is presumed dead
    profile_store_url: str = "file://vault_local"
    profile_master_key: bytes | None = field(default=None, repr=False)
    proxy_url_template: str = field(default="", repr=False)
    heartbeat_s: int = 30
    max_job_s: int = 1200  # a job running longer is presumed hung: the worker stops heart-beating and exits
    drain_s: int = 540  # after SIGTERM the current job may run this long (Azure grace period is 600 s)
    # Budget and unit costs (rupees). Only this module holds these numbers.
    monthly_budget_inr: float = 5000.0
    proxy_inr_per_gb: float = 400.0
    ai_inr_per_1k_tokens: float = 0.05
    worker_inr_per_minute: float = 0.20
    fixed_monthly_inr: float = 0.0
    budget_alert_pct: int = 80
    budget_pause_pct: int = 100
    jev_tokens_per_call: int = 1000
    # Daily limits and sync cadence
    refresh_per_app_per_day: int = 3
    cart_checks_per_day: int = 10
    sync_interval_h: int = 24
    paused_sync_interval_h: int = 72
    app_tz: str = "Asia/Kolkata"
    approval_timeout_s: int = 600
    max_learn_job_s: int = 2400
    sync_block_media: bool = True
    scheduler_tick_s: int = 900
    app_url: str = "http://localhost:3000"
    # Notifications
    vapid_public_key: str = ""
    vapid_private_key: str = field(default="", repr=False)
    vapid_subject: str = "mailto:admin@localhost"
    smtp_host: str = ""
    smtp_port: int = 1025
    smtp_user: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_starttls: bool = False
    email_from: str = "Expense Agent <no-reply@localhost>"


SITE_NAMES = {"zepto": "Zepto", "blinkit": "Blinkit", "swiggy": "Swiggy", "instamart": "Instamart",
              "zomato": "Zomato"}


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
        stale_job_s=int(env("STALE_JOB_S", "300")),
        profile_store_url=store_url,
        profile_master_key=_master_key(env, store_url, for_worker),
        proxy_url_template=proxy_template,
        heartbeat_s=int(env("HEARTBEAT_S", "30")),
        max_job_s=int(env("MAX_JOB_S", "1200")),
        drain_s=int(env("DRAIN_S", "540")),
        monthly_budget_inr=float(env("MONTHLY_BUDGET_INR", "5000")),
        proxy_inr_per_gb=float(env("PROXY_INR_PER_GB", "400")),
        ai_inr_per_1k_tokens=float(env("AI_INR_PER_1K_TOKENS", "0.05")),
        worker_inr_per_minute=float(env("WORKER_INR_PER_MINUTE", "0.20")),
        fixed_monthly_inr=float(env("FIXED_MONTHLY_INR", "0")),
        budget_alert_pct=int(env("BUDGET_ALERT_PCT", "80")),
        budget_pause_pct=int(env("BUDGET_PAUSE_PCT", "100")),
        jev_tokens_per_call=int(env("JEV_TOKENS_PER_CALL", "1000")),
        refresh_per_app_per_day=int(env("REFRESH_PER_APP_PER_DAY", "3")),
        cart_checks_per_day=int(env("CART_CHECKS_PER_DAY", "10")),
        sync_interval_h=int(env("SYNC_INTERVAL_H", "24")),
        paused_sync_interval_h=int(env("PAUSED_SYNC_INTERVAL_H", "72")),
        app_tz=env("APP_TZ", "") or "Asia/Kolkata",
        approval_timeout_s=int(env("APPROVAL_TIMEOUT_S", "600")),
        max_learn_job_s=int(env("MAX_LEARN_JOB_S", "2400")),
        sync_block_media=env("SYNC_BLOCK_MEDIA", "1") != "0",
        scheduler_tick_s=int(env("SCHEDULER_TICK_S", "900")),
        app_url=env("APP_URL", "") or "http://localhost:3000",
        vapid_public_key=env("VAPID_PUBLIC_KEY", ""),
        vapid_private_key=env("VAPID_PRIVATE_KEY", ""),
        vapid_subject=env("VAPID_SUBJECT", "") or "mailto:admin@localhost",
        smtp_host=env("SMTP_HOST", ""),
        smtp_port=int(env("SMTP_PORT", "1025")),
        smtp_user=env("SMTP_USER", ""),
        smtp_password=env("SMTP_PASSWORD", ""),
        smtp_starttls=env("SMTP_STARTTLS", "0") == "1",
        email_from=env("EMAIL_FROM", "") or "Expense Agent <no-reply@localhost>",
    )

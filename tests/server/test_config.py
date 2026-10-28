"""Service settings: secrets stay out of repr, worker-only validation, defaults, hosted checks."""
import base64

import pytest

from server.config import Settings, check_database_url, check_hosted, settings


def test_settings_repr_hides_secrets():
    s = Settings(database_url="postgresql://u:dbpass-XYZ@h/db", jwt_secret="jwt-secret-XYZ",
                 profile_master_key=b"master-key-bytes-XYZ-0123456789ab",
                 proxy_url_template="http://user-{session}:proxypass-XYZ@gw.example.com:7000")
    text = repr(s)
    for secret in ("dbpass-XYZ", "jwt-secret-XYZ", "master-key-bytes-XYZ", "proxypass-XYZ"):
        assert secret not in text
    assert "Settings(" in text and "otp_timeout_s=" in text


def test_settings_reads_budget_and_unit_costs_from_env(monkeypatch):
    from server import config
    for k, v in {"MONTHLY_BUDGET_INR": "1234", "PROXY_INR_PER_GB": "10", "AI_INR_PER_1K_TOKENS": "0.5",
                 "WORKER_INR_PER_MINUTE": "2", "FIXED_MONTHLY_INR": "99", "BUDGET_ALERT_PCT": "70",
                 "BUDGET_PAUSE_PCT": "90", "JEV_TOKENS_PER_CALL": "7", "REFRESH_PER_APP_PER_DAY": "5",
                 "CART_CHECKS_PER_DAY": "6", "SYNC_INTERVAL_H": "12", "PAUSED_SYNC_INTERVAL_H": "36",
                 "SYNC_BLOCK_MEDIA": "0", "SMTP_PASSWORD": "smtp-secret-XYZ",
                 "VAPID_PRIVATE_KEY": "vapid-secret-XYZ", "SMTP_STARTTLS": "1", "SMTP_PORT": "587"}.items():
        monkeypatch.setenv(k, v)
    s = config.settings()
    assert (s.monthly_budget_inr, s.proxy_inr_per_gb, s.ai_inr_per_1k_tokens, s.worker_inr_per_minute,
            s.fixed_monthly_inr) == (1234.0, 10.0, 0.5, 2.0, 99.0)
    assert (s.budget_alert_pct, s.budget_pause_pct, s.jev_tokens_per_call, s.refresh_per_app_per_day,
            s.cart_checks_per_day, s.sync_interval_h, s.paused_sync_interval_h) == (70, 90, 7, 5, 6, 12, 36)
    assert s.sync_block_media is False and s.smtp_starttls is True and s.smtp_port == 587
    assert "smtp-secret-XYZ" not in repr(s) and "vapid-secret-XYZ" not in repr(s)


def test_settings_defaults(monkeypatch):
    from server import config
    for k in ("MONTHLY_BUDGET_INR", "APP_TZ", "SYNC_BLOCK_MEDIA", "CART_CHECKS_PER_DAY", "APP_URL"):
        monkeypatch.delenv(k, raising=False)
    s = config.settings()
    assert s.monthly_budget_inr == 5000.0 and s.app_tz == "Asia/Kolkata" and s.sync_block_media is True
    assert s.cart_checks_per_day == 10 and s.approval_timeout_s == 600 and s.max_learn_job_s == 2400
    assert config.SITE_NAMES["instamart"] == "Instamart"


def test_worker_settings_validate_proxy_template(monkeypatch):
    import pytest

    from server import config
    monkeypatch.setenv("ALLOW_DEV_PROFILE_KEY", "1")
    monkeypatch.delenv("PROFILE_MASTER_KEY", raising=False)
    monkeypatch.setenv("PROXY_URL_TEMPLATE", "http://u-{session}:p%2Fw@gw.example.com:7000")
    assert config.settings(for_worker=True).proxy_url_template.startswith("http://u-{session}")
    for bad in ("http://u-{session}:pa/ss@gw.example.com:7000", "http://u-{session}:pw@gw.example.com",
                "http://u-{sess}:pw@gw.example.com:7000", "http://u-{session}:pw@gw.example.com:notaport"):
        monkeypatch.setenv("PROXY_URL_TEMPLATE", bad)
        with pytest.raises(ValueError) as e:
            config.settings(for_worker=True)
        assert str(e.value) == config.PROXY_TEMPLATE_INVALID and "pa/ss" not in str(e.value)
    monkeypatch.setenv("PROXY_URL_TEMPLATE", "")
    assert config.settings(for_worker=True).proxy_url_template == ""


# ---- Plan 7 Task 2: hardening tunables, API never loads the profile key, hosted checks ----

HOSTED_DB = "postgresql://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require"
HOSTED_ASK = "postgresql://ask_reader.ref:askpw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require"
LOCAL_DB = "postgresql://postgres:pw@127.0.0.1:54329/postgres"
TUNABLES = ("MAX_JOB_ATTEMPTS", "BACKOFF_BASE_S", "BACKOFF_MAX_S", "BACKOFF_RESET_H", "ERROR_BUDGET_WINDOW_H",
            "ERROR_BUDGET_MIN_JOBS", "ERROR_BUDGET_MAX_FAILURE_PCT", "NEEDS_LOGIN_REMIND_H")


def test_api_settings_ignore_master_key(monkeypatch):
    monkeypatch.setenv("PROFILE_MASTER_KEY", base64.b64encode(b"k" * 32).decode())
    assert settings().profile_master_key is None and settings(for_worker=True).profile_master_key == b"k" * 32


def test_api_settings_ignore_an_invalid_master_key(monkeypatch):
    monkeypatch.setenv("PROFILE_MASTER_KEY", "not-base64-!!")
    assert settings().profile_master_key is None  # the API never even parses it
    with pytest.raises(ValueError):
        settings(for_worker=True)


def test_hardening_tunables_from_env(monkeypatch):
    for k in TUNABLES:
        monkeypatch.delenv(k, raising=False)
    s = settings()
    assert (s.max_job_attempts, s.backoff_base_s, s.backoff_max_s, s.backoff_reset_h, s.error_budget_window_h,
            s.error_budget_min_jobs, s.error_budget_max_failure_pct, s.needs_login_remind_h) == \
        (3, 60, 3600, 24, 24, 10, 30, 72)
    assert Settings().max_job_attempts == 3 and Settings().needs_login_remind_h == 72
    for i, k in enumerate(TUNABLES):
        monkeypatch.setenv(k, str(i + 5))
    s = settings()
    assert s.max_job_attempts == 5
    assert [getattr(s, k.lower()) for k in TUNABLES] == list(range(5, 5 + len(TUNABLES)))


@pytest.mark.parametrize("url,ok", [
    ("postgresql://postgres:pw@127.0.0.1:54329/postgres", True),
    ("postgresql://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require", True),
    ("postgresql://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=verify-full", True),
    ("postgresql://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres", False),
    ("postgresql://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=prefer", False),
    ("postgresql://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?sslmode=require", False),
    ("postgresql://postgres.ref:pw@aws-0-ap-south-1.pooler.supabase.com/postgres?sslmode=require&port=6543", False),
    ("", False),
])
def test_check_database_url(url, ok):
    if ok:
        check_database_url(url, "worker")
    else:
        with pytest.raises(ValueError):
            check_database_url(url, "worker")


def test_check_database_url_rejects_unknown_role():
    with pytest.raises(ValueError):
        check_database_url(LOCAL_DB, "web")


def test_check_database_url_error_never_contains_password():
    for url in ("postgresql://postgres.ref:pw-SECRET@aws-0-ap-south-1.pooler.supabase.com:5432/postgres",
                "postgresql://postgres.ref:pw-SECRET@aws-0-ap-south-1.pooler.supabase.com:6543/postgres?sslmode=require",
                "postgresql://postgres.ref:pw-SECRET@[broken"):
        with pytest.raises(ValueError) as exc:
            check_database_url(url, "api")
        text = str(exc.value)
        assert "pw-SECRET" not in text and "pooler.supabase.com" not in text and "postgres.ref" not in text
    with pytest.raises(ValueError, match="sslmode"):
        check_database_url("postgresql://u:pw@db.example.com:5432/postgres", "scheduler")
    with pytest.raises(ValueError, match="6543"):
        check_database_url("postgresql://u:pw@db.example.com:6543/postgres?sslmode=require", "scaler")


def _hosted_api(**over) -> Settings:
    base = dict(database_url=HOSTED_DB, jwks_url="https://ref.supabase.co/auth/v1/.well-known/jwks.json",
                ask_database_url=HOSTED_ASK, cors_origins=("https://hisaab.example.in",),
                anthropic_api_key="sk-ant-x", anthropic_model="claude-haiku-5-5")
    base.update(over)
    return Settings(**base)


def test_hosted_api_requires_jwks():
    with pytest.raises(ValueError, match="SUPABASE_JWKS_URL"):
        check_hosted(_hosted_api(jwks_url=""), "api")
    check_hosted(_hosted_api(), "api")
    check_hosted(Settings(database_url=LOCAL_DB), "api")  # local: no hosted requirements


def test_hosted_runs_database_check_for_every_role():
    for role in ("api", "worker", "scheduler", "scaler"):
        with pytest.raises(ValueError, match="sslmode"):
            check_hosted(_hosted_api(database_url=HOSTED_DB.replace("?sslmode=require", "")), role)


def test_hosted_api_requires_a_safe_ask_database_url():
    with pytest.raises(ValueError, match="ASK_DATABASE_URL"):
        check_hosted(_hosted_api(ask_database_url=""), "api")
    with pytest.raises(ValueError, match="ASK_DATABASE_URL") as exc:
        check_hosted(_hosted_api(ask_database_url=HOSTED_ASK.replace("?sslmode=require", "")), "api")
    assert "askpw" not in str(exc.value)
    with pytest.raises(ValueError, match="ASK_DATABASE_URL"):
        check_hosted(_hosted_api(ask_database_url=HOSTED_ASK.replace(":5432", ":6543")), "api")
    check_hosted(_hosted_api(ask_database_url=""), "worker")  # only the API uses the Ask login


@pytest.mark.parametrize("origins", [(), ("http://localhost:3000",), ("*",), ("http://hisaab.example.in",),
                                     ("https://hisaab.example.in", "http://127.0.0.1:3000")])
def test_hosted_api_requires_https_public_cors_origins(origins):
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        check_hosted(_hosted_api(cors_origins=origins), "api")


def test_hosted_api_requires_anthropic_key_and_allowed_model():
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY") as exc:
        check_hosted(_hosted_api(anthropic_api_key=""), "api")
    with pytest.raises(ValueError, match="ANTHROPIC_MODEL"):
        check_hosted(_hosted_api(anthropic_model="claude-opus-5-5"), "api")
    with pytest.raises(ValueError, match="ANTHROPIC_MODEL"):
        check_hosted(_hosted_api(anthropic_model="claude-opus-5-5", anthropic_api_key=""), "worker")
    check_hosted(_hosted_api(anthropic_api_key=""), "worker")  # categorisation is optional on the worker
    with pytest.raises(ValueError) as exc:
        check_hosted(_hosted_api(anthropic_api_key=" ", jwks_url=""), "api")
    assert "sk-ant" not in str(exc.value)


def test_new_settings_read_from_env_and_hide_secrets(monkeypatch):
    monkeypatch.setenv("ASK_DATABASE_URL", "postgresql://ask_reader:askpw-XYZ@h/db")
    monkeypatch.setenv("CORS_ORIGINS", "https://a.example.in, https://b.example.in ,")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-XYZ")
    monkeypatch.setenv("ANTHROPIC_MODEL", " claude-haiku-5-5 ")
    s = settings()
    assert s.ask_database_url.endswith("@h/db") and s.anthropic_api_key == "sk-ant-XYZ"
    assert s.cors_origins == ("https://a.example.in", "https://b.example.in")
    assert s.anthropic_model == "claude-haiku-5-5"
    assert "askpw-XYZ" not in repr(s) and "sk-ant-XYZ" not in repr(s)
    for k in ("ASK_DATABASE_URL", "CORS_ORIGINS", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"):
        monkeypatch.delenv(k)
    s = settings()
    assert (s.ask_database_url, s.cors_origins, s.anthropic_api_key, s.anthropic_model) == \
        ("", ("http://localhost:3000",), "", "claude-haiku-5-5")


def test_settings_fill_tunables_from_env(monkeypatch):
    from server import config
    for k, v in {"CART_FILLS_PER_DAY": "3", "CART_FILL_MAX_AGE_S": "900", "CART_LOCATION_MAX_M": "250.5",
                 "FILL_CONFIRM_WINDOW_S": "300"}.items():
        monkeypatch.setenv(k, v)
    s = config.settings()
    assert (s.cart_fills_per_day, s.cart_fill_max_age_s, s.cart_location_max_m, s.fill_confirm_window_s) == \
        (3, 900, 250.5, 300)
    for k in ("CART_FILLS_PER_DAY", "CART_FILL_MAX_AGE_S", "CART_LOCATION_MAX_M", "FILL_CONFIRM_WINDOW_S"):
        monkeypatch.delenv(k)
    s = config.settings()
    assert (s.cart_fills_per_day, s.cart_fill_max_age_s, s.cart_location_max_m, s.fill_confirm_window_s) == \
        (5, 1800, 500.0, 600)
    assert config.OPEN_APP_URLS == {"zepto": "https://www.zepto.com/", "blinkit": "https://blinkit.com/",
                                    "instamart": "https://www.swiggy.com/instamart"}


def test_compose_sets_fill_tunables():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(encoding="utf-8")
    for line in ('CART_FILLS_PER_DAY: "${CART_FILLS_PER_DAY:-5}"', 'CART_FILL_MAX_AGE_S: "${CART_FILL_MAX_AGE_S:-1800}"',
                 'CART_LOCATION_MAX_M: "${CART_LOCATION_MAX_M:-500}"',
                 'FILL_CONFIRM_WINDOW_S: "${FILL_CONFIRM_WINDOW_S:-600}"'):
        assert line in text

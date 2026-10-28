"""Service settings: secrets stay out of repr, worker-only validation, defaults."""
from server.config import Settings


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

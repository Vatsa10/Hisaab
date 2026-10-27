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

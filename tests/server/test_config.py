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

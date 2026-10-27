"""Shared constants for server tests."""

A = "00000000-0000-0000-0000-00000000000a"
B = "00000000-0000-0000-0000-00000000000b"


def order(oid, total=100.0, status="delivered", items=None):
    return {"order_id": oid, "placed_at": "2025-01-05T10:00:00", "total_amount": total, "currency": "INR",
            "status": status, "merchant": "Store", "items": items or [], "raw": {"id": oid}}


def settings_for_tests():
    import os

    from server.config import Settings
    return Settings(database_url=os.environ["TEST_DATABASE_URL"], jwt_secret="test-secret",
                    jwks_url="", otp_timeout_s=5)


def make_token(sub, secret="test-secret", aud="authenticated", exp_in=3600):
    import time

    import jwt
    return jwt.encode({"sub": sub, "aud": aud, "exp": int(time.time()) + exp_in}, secret, algorithm="HS256")


def auth_header(sub):
    return {"Authorization": f"Bearer {make_token(sub)}"}

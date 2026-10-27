"""The verifier is chosen by the token's own `alg` header: HS256 -> shared secret, ES256/RS256 -> JWKS."""
import base64
import dataclasses
import json
import time
import types

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from server import auth
from server.auth import Unauthorized, user_id_from_token
from server.config import Settings
from helpers import TEST_JWT_SECRET, make_token

JWKS = "http://jwks.invalid/.well-known/jwks.json"
EC_KEY = ec.generate_private_key(ec.SECP256R1())


def settings(secret=TEST_JWT_SECRET, jwks=JWKS):
    return dataclasses.replace(Settings(database_url="x"), jwt_secret=secret, jwks_url=jwks)


@pytest.fixture(autouse=True)
def stub_jwks(monkeypatch):
    """Stand in for the network JWKS client; it hands back the test EC public key."""
    client = types.SimpleNamespace(
        get_signing_key_from_jwt=lambda token: types.SimpleNamespace(key=EC_KEY.public_key()))
    monkeypatch.setattr(auth, "_jwk_clients", {JWKS: client})


def es256_token(sub="u1", aud="authenticated", exp_in=3600):
    return jwt.encode({"sub": sub, "aud": aud, "exp": int(time.time()) + exp_in}, EC_KEY,
                      algorithm="ES256", headers={"kid": "k1"})


def b64(d):
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def test_es256_token_accepted_when_secret_also_set():
    assert user_id_from_token(es256_token("u-es"), settings()) == "u-es"


def test_hs256_token_still_accepted():
    assert user_id_from_token(make_token("u-hs"), settings()) == "u-hs"
    assert user_id_from_token(make_token("u-hs"), settings(jwks="")) == "u-hs"


def test_alg_none_rejected():
    claims = {"sub": "u", "aud": "authenticated", "exp": int(time.time()) + 99}
    tok = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}."
    with pytest.raises(Unauthorized):
        user_id_from_token(tok, settings())


def test_hs256_token_rejected_when_only_jwks_configured():
    with pytest.raises(Unauthorized):
        user_id_from_token(make_token("u"), settings(secret=""))


def test_es256_token_rejected_when_only_secret_configured():
    with pytest.raises(Unauthorized):
        user_id_from_token(es256_token(), settings(jwks=""))


def test_unsupported_alg_and_garbage_rejected():
    hs512 = jwt.encode({"sub": "u", "aud": "authenticated", "exp": int(time.time()) + 99},
                       TEST_JWT_SECRET * 2, algorithm="HS512")
    for tok in (hs512, "not-a-jwt", ""):
        with pytest.raises(Unauthorized):
            user_id_from_token(tok, settings())


def test_es256_wrong_audience_and_expired_rejected():
    for tok in (es256_token(aud="other"), es256_token(exp_in=-10)):
        with pytest.raises(Unauthorized):
            user_id_from_token(tok, settings())


def test_hs256_with_wrong_secret_rejected():
    with pytest.raises(Unauthorized):
        user_id_from_token(make_token("u", secret="x" * 40), settings())

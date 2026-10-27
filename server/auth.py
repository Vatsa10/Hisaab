"""Supabase JWT verification."""
import jwt

from server.config import Settings


class Unauthorized(Exception):
    pass


_jwk_clients: dict[str, jwt.PyJWKClient] = {}


def user_id_from_token(token: str, s: Settings) -> str:
    """Return the token's `sub`. HS256 with the shared secret when configured, else JWKS (RS256/ES256)."""
    try:
        if s.jwt_secret:
            key, algs = s.jwt_secret, ["HS256"]
        elif s.jwks_url:
            client = _jwk_clients.setdefault(s.jwks_url, jwt.PyJWKClient(s.jwks_url))
            key, algs = client.get_signing_key_from_jwt(token).key, ["RS256", "ES256"]
        else:
            raise Unauthorized("no verification key configured")
        claims = jwt.decode(token, key, algorithms=algs, audience="authenticated",
                            options={"require": ["exp", "sub"]})
    except jwt.PyJWTError as e:
        raise Unauthorized(str(e)) from e
    return str(claims["sub"])

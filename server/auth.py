"""Supabase JWT verification.

The verifier is chosen by the token's own (unverified) `alg` header, but only from a fixed allow-list, and
`jwt.decode` is then given exactly that one algorithm, so a token can never pick a weaker one for its key:
HS256 -> the shared secret; ES256/RS256 -> the JWKS. Anything else (including `none`) is refused.
Nothing from the token is ever logged or put in an error message."""
import jwt

from server.config import Settings


class Unauthorized(Exception):
    pass


_jwk_clients: dict[str, jwt.PyJWKClient] = {}
_JWKS_ALGS = ("ES256", "RS256")


def user_id_from_token(token: str, s: Settings) -> str:
    """Return the token's `sub`, verified with the key that matches the token's algorithm."""
    try:
        alg = jwt.get_unverified_header(token).get("alg")
        if alg == "HS256":
            if not s.jwt_secret:
                raise Unauthorized("HS256 token but no JWT secret configured")
            key = s.jwt_secret
        elif alg in _JWKS_ALGS:
            if not s.jwks_url:
                raise Unauthorized("asymmetric token but no JWKS URL configured")
            client = _jwk_clients.setdefault(s.jwks_url, jwt.PyJWKClient(s.jwks_url))
            key = client.get_signing_key_from_jwt(token).key
        else:
            raise Unauthorized("unsupported token algorithm")
        claims = jwt.decode(token, key, algorithms=[alg], audience="authenticated",
                            options={"require": ["exp", "sub"]})
    except jwt.PyJWTError as e:
        raise Unauthorized(type(e).__name__) from e
    return str(claims["sub"])

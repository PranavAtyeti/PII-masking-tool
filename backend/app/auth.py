"""
Auth0 JWT validation plus local Privy user/role resolution, with temporary guests.

Authentication order:
- If a Bearer token is supplied, validate it strictly as an Auth0 access token.
- If no Bearer token is supplied, allow a valid X-Guest-Session header.
- Never treat an invalid Bearer token as a guest request.
"""

import os
from functools import lru_cache
from typing import Any

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from . import mapping_store as store

AUTH0_DOMAIN = os.environ.get("AUTH0_DOMAIN", "").strip()
AUTH0_AUDIENCE = os.environ.get("AUTH0_AUDIENCE", "").strip()

if not AUTH0_DOMAIN or not AUTH0_AUDIENCE:
    raise RuntimeError(
        "Missing Auth0 backend configuration. Set AUTH0_DOMAIN and "
        "AUTH0_AUDIENCE in backend/.env."
    )

AUTH0_ISSUER = f"https://{AUTH0_DOMAIN}/"
JWKS_URL = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"
_bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    return PyJWKClient(JWKS_URL, cache_keys=True, lifespan=300)


def _unauthorized(detail: str = "Missing authentication") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_optional_user_claims(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, Any] | None:
    """Return verified Auth0 claims when a Bearer token is supplied.

    No token is a normal condition here because guest sessions are supported.
    A supplied token is always validated strictly; it is never downgraded to guest
    authentication if validation fails.
    """
    if credentials is None:
        return None

    if credentials.scheme.lower() != "bearer":
        raise _unauthorized("Invalid Authorization scheme")

    token = credentials.credentials.strip()
    if not token:
        raise _unauthorized("Missing Bearer access token")

    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(token).key
        return jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=AUTH0_AUDIENCE,
            issuer=AUTH0_ISSUER,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise _unauthorized("Auth0 access token has expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise _unauthorized("Auth0 access token has the wrong audience") from exc
    except jwt.InvalidIssuerError as exc:
        raise _unauthorized("Auth0 access token has the wrong issuer") from exc
    except jwt.PyJWTError as exc:
        raise _unauthorized("Invalid Auth0 access token") from exc
    except Exception as exc:
        raise _unauthorized("Unable to validate Auth0 access token") from exc


# Backward-compatible alias for any code that imports get_current_user.
# It now means "verified claims if authenticated, otherwise None".
def get_current_user(
    claims: dict[str, Any] | None = Depends(get_optional_user_claims),
) -> dict[str, Any] | None:
    return claims


def get_current_app_user(
    claims: dict[str, Any] | None = Depends(get_optional_user_claims),
    guest_session: str | None = Header(default=None, alias="X-Guest-Session"),
) -> dict:
    """Resolve either an Auth0 identity or a valid temporary guest session."""
    if claims is not None:
        auth0_sub = claims["sub"]
        email = claims.get("email")
        display_name = claims.get("name") or claims.get("nickname") or email or auth0_sub
        return store.get_or_create_user(auth0_sub, email=email, display_name=display_name)

    if guest_session:
        guest = store.get_guest_user(guest_session.strip())
        if guest:
            return guest

    raise _unauthorized("Missing authentication")


def require_admin(user: dict = Depends(get_current_app_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user

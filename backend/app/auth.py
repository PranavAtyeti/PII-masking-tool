"""
Auth0 JWT validation plus local Privy user/role resolution.
"""

import os
from functools import lru_cache
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
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


def _unauthorized(detail: str = "Invalid or missing Auth0 access token") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, Any]:
    """Validate the Auth0 access token and return its verified claims."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("Missing Bearer access token")

    try:
        signing_key = _jwks_client().get_signing_key_from_jwt(credentials.credentials).key
        return jwt.decode(
            credentials.credentials,
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
    # except jwt.PyJWTError as exc:
    #     print(f"Auth0 JWT validation error: {type(exc).__name__}: {exc}")
    #     raise _unauthorized("Invalid Auth0 access token") from exc
    except Exception as exc:
        raise _unauthorized("Unable to validate Auth0 access token") from exc


def get_current_app_user(claims: dict[str, Any] = Depends(get_current_user)) -> dict:
    """Resolve an Auth0 identity to a local Privy user and update last-login metadata."""
    auth0_sub = claims["sub"]
    email = claims.get("email")
    display_name = claims.get("name") or claims.get("nickname") or email or auth0_sub
    return store.get_or_create_user(auth0_sub, email=email, display_name=display_name)


def require_admin(user: dict = Depends(get_current_app_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user

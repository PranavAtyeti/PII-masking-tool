"""Privy local JWT authentication with temporary guest sessions.

Access tokens are short-lived signed JWTs. Refresh tokens are opaque random
values stored only as SHA-256 hashes in PostgreSQL and delivered through an
HttpOnly cookie by the auth router.
"""

from __future__ import annotations

import os
import time
from typing import Any

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import mapping_store as store


RUNTIME_ENVIRONMENT = os.getenv("PRIVY_ENVIRONMENT", "development").strip().lower()
JWT_SECRET = os.environ.get("PRIVY_JWT_SECRET", "").strip()
JWT_ALGORITHM = os.environ.get("PRIVY_JWT_ALGORITHM", "HS256").strip() or "HS256"
ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("PRIVY_ACCESS_TOKEN_TTL_SECONDS", "900"))

_ALLOWED_JWT_ALGORITHMS = {"HS256", "HS384", "HS512"}

if not JWT_SECRET:
    raise RuntimeError(
        "Missing local authentication configuration. Set PRIVY_JWT_SECRET "
        "in backend/.env."
    )

if JWT_ALGORITHM not in _ALLOWED_JWT_ALGORITHMS:
    raise RuntimeError(
        "Unsupported PRIVY_JWT_ALGORITHM. Use one of: "
        + ", ".join(sorted(_ALLOWED_JWT_ALGORITHMS))
    )

# Production access tokens should not be protected by a short or empty-ish
# development secret. A 256-bit-or-longer secret is recommended for HS256.
if RUNTIME_ENVIRONMENT in {"production", "prod"} and len(JWT_SECRET.encode("utf-8")) < 32:
    raise RuntimeError(
        "PRIVY_JWT_SECRET must be at least 32 bytes in production."
    )

if ACCESS_TOKEN_TTL_SECONDS < 60:
    ACCESS_TOKEN_TTL_SECONDS = 60

_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str = "Missing authentication") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def create_access_token(user: dict, ttl_seconds: int = ACCESS_TOKEN_TTL_SECONDS) -> str:
    now = int(time.time())
    payload = {
        "sub": user["auth0_sub"],
        "role": user["role"],
        "typ": "access",
        "iat": now,
        "exp": now + max(60, ttl_seconds),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    try:
        claims = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise _unauthorized("Access token has expired") from exc
    except jwt.PyJWTError as exc:
        raise _unauthorized("Invalid access token") from exc

    if claims.get("typ") != "access":
        raise _unauthorized("Invalid access token type")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise _unauthorized("Invalid access token subject")

    return claims


def get_optional_user_claims(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict[str, Any] | None:
    """Decode a local access token if supplied.

    No bearer token is allowed to fall back to guest authentication. An
    invalid bearer token always produces 401.
    """
    if credentials is None:
        return None

    if credentials.scheme.lower() != "bearer":
        raise _unauthorized("Invalid Authorization scheme")

    token = credentials.credentials.strip()
    if not token:
        raise _unauthorized("Missing Bearer access token")

    return decode_access_token(token)


def get_current_user(
    claims: dict[str, Any] | None = Depends(get_optional_user_claims),
) -> dict[str, Any] | None:
    return claims


def get_current_app_user(
    claims: dict[str, Any] | None = Depends(get_optional_user_claims),
    guest_session: str | None = Header(default=None, alias="X-Guest-Session"),
) -> dict:
    """Resolve either a local account or a valid temporary guest session."""
    if claims is not None:
        user_id = str(claims["sub"])
        user = store.get_user(user_id)
        if not user or user.get("role") == "guest":
            raise _unauthorized("User account is not available")
        return user

    if guest_session:
        guest = store.get_guest_user(guest_session.strip())
        if guest:
            return guest

    raise _unauthorized("Missing authentication")


def require_admin(user: dict = Depends(get_current_app_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user

"""Local Privy authentication endpoints."""

import os

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from .. import mapping_store as store
from ..auth import create_access_token, get_current_app_user
from ..llm import get_model_options
from ..schemas import (
    AuthResponse,
    LoginIn,
    RegisterIn,
    GuestSessionOut,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

_password_hasher = PasswordHasher()
REFRESH_COOKIE_NAME = os.getenv("PRIVY_REFRESH_COOKIE_NAME", "privy_refresh")
REFRESH_COOKIE_SECURE = os.getenv(
    "PRIVY_REFRESH_COOKIE_SECURE", "false"
).strip().lower() in {"1", "true", "yes"}
REFRESH_COOKIE_SAMESITE = os.getenv(
    "PRIVY_REFRESH_COOKIE_SAMESITE", "lax"
).strip().lower()
if REFRESH_COOKIE_SAMESITE not in {"lax", "strict", "none"}:
    REFRESH_COOKIE_SAMESITE = "lax"
REFRESH_COOKIE_PATH = "/api/auth"


def _set_refresh_cookie(response: Response, token: str, expires_at: float) -> None:
    max_age = max(0, int(expires_at - __import__("time").time()))
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=REFRESH_COOKIE_SECURE,
        samesite=REFRESH_COOKIE_SAMESITE,
        path=REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=REFRESH_COOKIE_PATH,
    )


def _public_user(user: dict) -> dict:
    return {
        "sub": user["auth0_sub"],
        "email": user["email"],
        "display_name": user["display_name"],
        "role": user["role"],
        "created_at": user["created_at"],
        "last_login_at": user["last_login_at"],
        "email_verified": user.get("email_verified", False),
    }


def _issue_login(response: Response, user: dict, request: Request) -> AuthResponse:
    access_token = create_access_token(user)
    refresh_token, expires_at = store.create_auth_session(
        user["auth0_sub"],
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    _set_refresh_cookie(response, refresh_token, expires_at)
    return AuthResponse(
        access_token=access_token,
        token_type="bearer",
        user=_public_user(user),
    )


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register(body: RegisterIn, request: Request, response: Response):
    try:
        password_hash = _password_hasher.hash(body.password)
        user = store.create_local_user(
            email=body.email,
            password_hash=password_hash,
            display_name=body.display_name,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    return _issue_login(response, user, request)


@router.post("/login", response_model=AuthResponse)
def login(body: LoginIn, request: Request, response: Response):
    user = store.get_local_user_by_email(body.email)

    if not user or not user.get("password_hash"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    try:
        _password_hasher.verify(user["password_hash"], body.password)
    except (VerifyMismatchError, VerificationError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        ) from None

    user = store.update_last_login(user["auth0_sub"]) or user
    return _issue_login(response, user, request)


@router.post("/refresh", response_model=AuthResponse)
def refresh(request: Request, response: Response):
    raw_token = request.cookies.get(REFRESH_COOKIE_NAME)
    result = store.rotate_auth_session(raw_token or "")

    if not result:
        _clear_refresh_cookie(response)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh session",
        )

    user, new_refresh_token, expires_at = result
    access_token = create_access_token(user)
    _set_refresh_cookie(response, new_refresh_token, expires_at)

    return AuthResponse(
        access_token=access_token,
        token_type="bearer",
        user=_public_user(user),
    )


@router.post("/logout")
def logout(request: Request, response: Response):
    store.revoke_auth_session(request.cookies.get(REFRESH_COOKIE_NAME))
    _clear_refresh_cookie(response)
    return {"ok": True}


@router.get("/me")
def me(user: dict = Depends(get_current_app_user)):
    return _public_user(user)


@router.get("/models")
def models(_user: dict = Depends(get_current_app_user)):
    options = get_model_options()
    configured_provider = os.environ.get("LLM_PROVIDER", "groq").strip().lower()
    configured_model = store.get_admin_config("llm_model", "")

    default_id = None
    for item in options:
        if item["model"] == configured_model:
            default_id = item["id"]
            break

    if default_id is None:
        for item in options:
            if item["provider"] == configured_provider:
                default_id = item["id"]
                break

    if default_id is None and options:
        default_id = options[0]["id"]

    return {"models": options, "default_model_id": default_id}


@router.post("/guest", response_model=GuestSessionOut)
def create_guest():
    session_id, user, expires_at = store.create_guest_session()
    return {
        "session_id": session_id,
        "expires_at": expires_at,
        "user": user,
    }

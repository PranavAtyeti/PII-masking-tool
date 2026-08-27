"""Authenticated-user endpoints."""

from fastapi import APIRouter, Depends

from ..auth import get_current_app_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/me")
def me(user: dict = Depends(get_current_app_user)):
    return {
        "sub": user["auth0_sub"],
        "email": user["email"],
        "display_name": user["display_name"],
        "role": user["role"],
        "created_at": user["created_at"],
        "last_login_at": user["last_login_at"],
    }

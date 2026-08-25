"""Authentication endpoints for the Auth0-backed Privy API."""

from fastapi import APIRouter, Depends

from ..auth import get_current_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/me")
def get_me(current_user: dict = Depends(get_current_user)):
    """Return the verified identity information contained in the access token."""
    return {
        "sub": current_user["sub"],
        "scope": current_user.get("scope", ""),
        "permissions": current_user.get("permissions", []),
        "azp": current_user.get("azp"),
    }

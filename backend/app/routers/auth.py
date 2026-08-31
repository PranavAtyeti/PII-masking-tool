"""Authenticated-user endpoints."""

from fastapi import APIRouter, Depends
import os

from .. import mapping_store as store
from ..schemas import GuestSessionOut

from ..auth import get_current_app_user
from ..llm import get_model_options

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


@router.get("/models")
def models(_user: dict = Depends(get_current_app_user)):
    options = get_model_options()
    configured_provider = os.environ.get("LLM_PROVIDER", "groq").strip().lower()
    default_id = None
    configured_model = ""
    from .. import mapping_store as store
    from ..llm import MODEL_CATALOG
    configured_model = store.get_admin_config("llm_model", "")
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
    return {"session_id": session_id, "expires_at": expires_at, "user": user}

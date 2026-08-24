"""
routers/admin.py
-----------------
LLM key/model config -- mirrors the Streamlit app's Settings -> Admin
panel. One change from that version: GET never returns the full raw key.

That wasn't very consequential in Streamlit (the value only round-tripped
into the same browser's own password-type input field, and if you have
access to the app you already effectively have the key -- also the reason
LLM_API_KEY ended up visible in that first screenshot early in this
project). It matters more now: this is a real HTTP endpoint, potentially
reachable from a browser dev console or a network capture, not just a
value living inside one Streamlit widget on one screen. A masked preview
(first/last few characters) is enough to visually confirm "yes, a key is
set, and it looks like the right one" without the endpoint being able to
hand the live secret back out on request.
"""

from fastapi import APIRouter

from .. import mapping_store as store
from ..llm import LLM_API_KEY_ENV, LLM_MODEL_DEFAULT, COMMON_MODELS
from ..schemas import AdminConfigOut, AdminConfigIn

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _preview_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


@router.get("/config", response_model=AdminConfigOut)
def get_config():
    api_key = store.get_admin_config("llm_api_key", LLM_API_KEY_ENV)
    model = store.get_admin_config("llm_model", LLM_MODEL_DEFAULT)
    return AdminConfigOut(
        model=model,
        api_key_set=bool(api_key),
        api_key_preview=_preview_key(api_key),
        common_models=COMMON_MODELS,
    )


@router.patch("/config", response_model=AdminConfigOut)
def update_config(body: AdminConfigIn):
    if body.api_key is not None and body.api_key.strip():
        store.set_admin_config("llm_api_key", body.api_key.strip())
    if body.model is not None and body.model.strip():
        store.set_admin_config("llm_model", body.model.strip())
    return get_config()

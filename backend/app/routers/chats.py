import re
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse

from .. import mapping_store as store
from ..auth import get_current_app_user
from ..schemas import ChatOut, ChatCreateIn, ChatRenameIn, MessageOut

router = APIRouter(prefix="/api/chats", tags=["chats"])


def _get_chat_or_404(chat_id: str, user_id: str) -> dict:
    chat = store.get_chat(chat_id, user_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


@router.post("", response_model=ChatOut)
def create_chat(body: ChatCreateIn, user: dict = Depends(get_current_app_user)):
    chat_id = store.create_chat(user["auth0_sub"], body.title)
    return store.get_chat(chat_id, user["auth0_sub"])


@router.get("", response_model=list[ChatOut])
def list_chats(user: dict = Depends(get_current_app_user)):
    return store.list_chats(user["auth0_sub"])


@router.get("/{chat_id}", response_model=ChatOut)
def get_chat(chat_id: str, user: dict = Depends(get_current_app_user)):
    return _get_chat_or_404(chat_id, user["auth0_sub"])


@router.get("/{chat_id}/messages", response_model=list[MessageOut])
def get_messages(chat_id: str, user: dict = Depends(get_current_app_user)):
    _get_chat_or_404(chat_id, user["auth0_sub"])
    return store.get_chat_messages(chat_id)


@router.patch("/{chat_id}", response_model=ChatOut)
def rename_chat(chat_id: str, body: ChatRenameIn, user: dict = Depends(get_current_app_user)):
    user_id = user["auth0_sub"]
    _get_chat_or_404(chat_id, user_id)
    store.rename_chat(chat_id, user_id, body.title.strip())
    return store.get_chat(chat_id, user_id)


@router.delete("/{chat_id}", status_code=204)
def delete_chat(chat_id: str, user: dict = Depends(get_current_app_user)):
    user_id = user["auth0_sub"]
    _get_chat_or_404(chat_id, user_id)
    store.delete_chat(chat_id, user_id)


@router.get("/{chat_id}/export", response_class=PlainTextResponse)
def export_chat(chat_id: str, user: dict = Depends(get_current_app_user)):
    chat = _get_chat_or_404(chat_id, user["auth0_sub"])
    messages = store.get_chat_messages(chat_id)
    title = chat["title"] or "New chat"
    lines = [title, "=" * len(title), ""]
    for m in messages:
        speaker = "You" if m["role"] == "user" else "Privy"
        lines.append(f"{speaker}: {m['content']}")
        lines.append("")
    text = "\n".join(lines)
    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")[:50] or "chat"
    return PlainTextResponse(
        text,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.txt"'},
    )

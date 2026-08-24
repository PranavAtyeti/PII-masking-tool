"""
routers/chats.py
-----------------
Chat CRUD -- direct 1:1 wrapping of mapping_store.py's chat functions, which
were already backend-agnostic (plain SQLite, no Streamlit import) from when
they were first built for the Streamlit app. This router is almost entirely
plumbing, on purpose: the logic already existed and was already correct.
"""

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from .. import mapping_store as store
from ..schemas import ChatOut, ChatCreateIn, ChatRenameIn, MessageOut

router = APIRouter(prefix="/api/chats", tags=["chats"])


def _get_chat_or_404(chat_id: str) -> dict:
    chat = store.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


@router.post("", response_model=ChatOut)
def create_chat(body: ChatCreateIn):
    chat_id = store.create_chat(body.title)
    return store.get_chat(chat_id)


@router.get("", response_model=list[ChatOut])
def list_chats():
    return store.list_chats()


@router.get("/{chat_id}", response_model=ChatOut)
def get_chat(chat_id: str):
    return _get_chat_or_404(chat_id)


@router.get("/{chat_id}/messages", response_model=list[MessageOut])
def get_messages(chat_id: str):
    _get_chat_or_404(chat_id)
    return store.get_chat_messages(chat_id)


@router.patch("/{chat_id}", response_model=ChatOut)
def rename_chat(chat_id: str, body: ChatRenameIn):
    _get_chat_or_404(chat_id)
    store.rename_chat(chat_id, body.title.strip())
    return store.get_chat(chat_id)


@router.delete("/{chat_id}", status_code=204)
def delete_chat(chat_id: str):
    _get_chat_or_404(chat_id)
    store.delete_chat(chat_id)


@router.get("/{chat_id}/export", response_class=PlainTextResponse)
def export_chat(chat_id: str):
    chat = _get_chat_or_404(chat_id)
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

"""Ask questions against masked files with bounded conversation memory."""

import json

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .. import mapping_store as store
from ..auth import get_current_app_user
from ..llm import call_llm, stream_llm, get_model_config
from ..prompts import build_privy_system_prompt
from ..context_limits import MAX_TOTAL_FILE_CONTEXT_TOKENS, limit_file_context
from ..masking import (
    mask_free_text_cell,
    _replace_known_values,
    unmask_text,
    stream_unmask,
    looks_unmasked,
    count_masked_tokens,
)
from ..schemas import MessageIn

router = APIRouter(prefix="/api/chats", tags=["messages"])

# Keep enough history for continuity without allowing prompt growth forever.
MAX_HISTORY_MESSAGES = 12


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _get_active_llm_config(model_id: str | None = None):
    """Resolve the model selected by the user."""
    if model_id:
        _provider, _base_url, api_key, model = get_model_config(model_id)
        return api_key, model

    configured_model = store.get_admin_config("llm_model", "")
    if configured_model:
        try:
            _provider, _base_url, api_key, model = get_model_config(configured_model)
            return api_key, model
        except RuntimeError:
            pass

    _provider, _base_url, api_key, model = get_model_config(None)
    return api_key, model


def _mask_history(messages: list[dict], known_values: dict) -> list[dict]:
    """Return recent chat history with known sensitive values tokenized."""
    masked: list[dict] = []
    for message in messages[-MAX_HISTORY_MESSAGES:]:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role not in {"user", "assistant"} or not content:
            continue
        masked.append({
            "role": role,
            "content": _replace_known_values(content, known_values),
        })
    return masked


def _format_history(history: list[dict]) -> str:
    if not history:
        return ""
    parts = [
        "RECENT CONVERSATION HISTORY (use this to maintain continuity; it is context, not a new user request):"
    ]
    for index, message in enumerate(history, start=1):
        role = "USER" if message["role"] == "user" else "PRIVY"
        parts.append(f"[{index}] {role}:\n{message['content']}")
    return "\n\n".join(parts)


def _build_prompt(
    chat_id: str,
    masked_question: str,
    concise: bool,
    history: list[dict] | None = None,
):
    length_instruction = (
        "Be concise: lead with the direct answer in 1-3 sentences, no preamble, no restating the question, and no unnecessary closing summary. Go longer when the user asks for detail or when more detail is necessary to answer correctly."
        if concise
        else
        "Give a complete, clearly explained answer. Use as much detail as genuinely helps the user, without padding."
    )

    chat_files = store.get_chat_files(chat_id)
    history_block = _format_history(history or [])

    if chat_files:
        sections: list[str] = []
        file_descriptions: list[str] = []
        total_rows = 0
        any_truncated = False
        context_was_limited = False
        remaining_tokens = MAX_TOTAL_FILE_CONTEXT_TOKENS

        for index, chat_file in enumerate(chat_files, start=1):
            total_rows += chat_file["row_count"]
            any_truncated = any_truncated or chat_file["truncated"]

            limited_csv, used_tokens, limited = limit_file_context(
                chat_file["filename"], chat_file["masked_csv"], remaining_tokens
            )
            if used_tokens <= 0:
                context_was_limited = True
                break

            remaining_tokens = max(0, remaining_tokens - used_tokens)
            context_was_limited = context_was_limited or limited
            columns = chat_file.get("columns") or []
            column_names = [
                str(c.get("name")) if isinstance(c, dict) else str(c)
                for c in columns
            ]
            file_descriptions.append(
                f"{chat_file['filename']} — {chat_file['row_count']:,} rows"
                + (f"; columns: {', '.join(column_names)}" if column_names else "")
            )
            sections.append(
                f"=== FILE {index}: {chat_file['filename']} ===\n{limited_csv}"
            )

            if remaining_tokens <= 0:
                context_was_limited = context_was_limited or index < len(chat_files)
                break

        file_context = "\n\n".join(sections)
        row_notes: list[str] = []
        if any_truncated:
            row_notes.append(
                "One or more files are truncated to their first 200 rows; do not imply you have every row from those files."
            )
        else:
            row_notes.append(
                f"You are seeing the stored rows across the attached files ({total_rows:,} rows total)."
            )
        if context_was_limited:
            row_notes.append(
                f"Privy may limit file context to about {MAX_TOTAL_FILE_CONTEXT_TOKENS:,} tokens across this request. Do not claim to have analyzed rows or file content that is not present in the supplied context."
            )
        row_note = " ".join(row_notes)

        system_prompt = build_privy_system_prompt(
            file_descriptions=file_descriptions,
            row_note=row_note,
            length_instruction=length_instruction,
        )
        prompt_parts: list[str] = []
        if history_block:
            prompt_parts.append(history_block)
        prompt_parts.append(f"MASKED DATA FROM ATTACHED FILES:\n{file_context}")
        prompt_parts.append(f"CURRENT USER QUESTION:\n{masked_question}")
        user_prompt = "\n\n".join(prompt_parts)
        payload_to_check = file_context + masked_question + history_block
    else:
        system_prompt = build_privy_system_prompt(length_instruction=length_instruction)
        prompt_parts = []
        if history_block:
            prompt_parts.append(history_block)
        prompt_parts.append(f"CURRENT USER QUESTION:\n{masked_question}")
        user_prompt = "\n\n".join(prompt_parts)
        payload_to_check = masked_question + history_block

    return system_prompt, user_prompt, payload_to_check


def _generate(chat_id: str, body: MessageIn, user_id: str):
    # Read prior turns before storing this turn so the current question is not duplicated.
    previous_messages = store.get_chat_messages(chat_id)
    is_first_message = len(previous_messages) == 0
    known_values_before_turn = store.get_known_values(chat_id)

    store.add_message(chat_id, "user", body.question)
    if is_first_message:
        fallback_title = " ".join(body.question.strip().split())
        if len(fallback_title) > 40:
            fallback_title = fallback_title[:40].rstrip() + "\u2026"
        store.rename_chat(chat_id, user_id, fallback_title or "New chat")

    counters = store.load_counters(chat_id)
    masked_question = _replace_known_values(body.question, known_values_before_turn)
    masked_question = mask_free_text_cell(
        masked_question, chat_id, counters, body.ner_confidence, body.use_ner
    )

    # Reuse mappings from the current chat so stored prior responses containing PII
    # are tokenized before being included in model context.
    history = _mask_history(previous_messages, store.get_known_values(chat_id))

    system_prompt, user_prompt, payload_to_check = _build_prompt(
        chat_id, masked_question, body.concise, history
    )
    max_tokens = 250 if body.concise else 800
    masked_count = count_masked_tokens(payload_to_check)

    if looks_unmasked(payload_to_check):
        answer = (
            "Request blocked: content still looks like it contains unmasked personal data. "
            "Nothing was sent to the model."
        )
        yield _sse({"delta": answer})
        store.add_message(chat_id, "assistant", answer, masked_count)
        yield _sse({"done": True, "masked_count": masked_count})
        return

    try:
        api_key, model = _get_active_llm_config(body.model_id)
    except RuntimeError as e:
        msg = f"{e}. Ask an admin to check the model configuration in Settings."
        yield _sse({"delta": msg})
        store.add_message(chat_id, "assistant", msg, masked_count)
        yield _sse({"done": True, "masked_count": masked_count})
        return

    if is_first_message:
        try:
            title_raw = call_llm(
                "Write a short title (3-6 words) summarizing the topic of the user's message below. Plain text only -- no quotes, no punctuation at the end, no preamble like 'Title:'.",
                masked_question,
                api_key,
                model,
                temperature=0.3,
                max_tokens=16,
            )
            title = unmask_text(" ".join(title_raw.strip().split()), chat_id)
            title = title.strip(" \"'.")[:60]
            if title:
                store.rename_chat(chat_id, user_id, title)
        except Exception:
            pass

    answer_parts: list[str] = []
    try:
        raw_chunks = stream_llm(
            system_prompt, user_prompt, api_key, model, max_tokens=max_tokens
        )
        for piece in stream_unmask(raw_chunks, chat_id):
            if piece:
                answer_parts.append(piece)
                yield _sse({"delta": piece})
    except RuntimeError as e:
        msg = f"{e}. Ask an admin to set it up in Settings."
        answer_parts = [msg]
        yield _sse({"delta": msg})
    except requests.exceptions.ConnectionError:
        msg = "Couldn't reach the AI service. Check your internet connection and try again."
        answer_parts = [msg]
        yield _sse({"delta": msg})
    except Exception as e:
        msg = f"Couldn't reach the model: {e}"
        answer_parts = [msg]
        yield _sse({"delta": msg})

    answer = "".join(answer_parts)
    store.add_message(chat_id, "assistant", answer, masked_count)
    yield _sse({"done": True, "masked_count": masked_count})


@router.post("/{chat_id}/messages")
def post_message(
    chat_id: str,
    body: MessageIn,
    user: dict = Depends(get_current_app_user),
):
    if not store.get_chat(chat_id, user["auth0_sub"]):
        raise HTTPException(status_code=404, detail="Chat not found")
    return StreamingResponse(
        _generate(chat_id, body, user["auth0_sub"]),
        media_type="text/event-stream",
    )

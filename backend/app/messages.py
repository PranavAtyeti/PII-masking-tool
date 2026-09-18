"""Ask questions against masked files with bounded conversation memory."""

import json
import os

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .. import mapping_store as store
from ..auth import get_current_app_user
from ..llm import stream_llm, get_model_config
from ..prompts import build_privy_system_prompt
from ..context_limits import MAX_TOTAL_FILE_CONTEXT_TOKENS, limit_file_context
from ..gemini_files import get_or_upload_gemini_file
from ..gemini_interactions import (
    get_interaction_id,
    save_interaction_id,
    delete_interaction,
)
from ..masking import (
    mask_free_text_cell,
    _replace_known_values,
    stream_unmask,
    looks_unmasked,
    count_masked_tokens,
)
from ..schemas import MessageIn

router = APIRouter(prefix="/api/chats", tags=["messages"])

# Keep enough history for continuity without allowing prompt growth forever.
MAX_HISTORY_MESSAGES = int(os.getenv("PRIVY_MAX_HISTORY_MESSAGES", "32"))
CONCISE_MAX_OUTPUT_TOKENS = int(os.getenv("PRIVY_CONCISE_MAX_OUTPUT_TOKENS", "2048"))
DETAILED_MAX_OUTPUT_TOKENS = int(os.getenv("PRIVY_DETAILED_MAX_OUTPUT_TOKENS", "16384"))


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _get_active_llm_config(model_id: str | None = None):
    """Resolve the selected model and return (provider, api_key, model)."""
    if model_id:
        provider, _base_url, api_key, model = get_model_config(model_id)
        return provider, api_key, model

    configured_model = store.get_admin_config("llm_model", "")
    if configured_model:
        try:
            provider, _base_url, api_key, model = get_model_config(configured_model)
            return provider, api_key, model
        except RuntimeError:
            pass

    provider, _base_url, api_key, model = get_model_config(None)
    return provider, api_key, model


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


def _parse_columns(chat_file: dict) -> list[str]:
    try:
        columns = json.loads(chat_file.get("columns_json") or "[]")
    except (TypeError, ValueError):
        columns = []
    return [
        str(item.get("name")) if isinstance(item, dict) else str(item)
        for item in columns
    ]


def _build_prompt(
    chat_id: str,
    masked_question: str,
    concise: bool,
    history: list[dict] | None = None,
    external_files: bool = False,
    server_state: bool = False,
):
    """Build the text prompt.

    With ``external_files=True`` the full masked CSV is deliberately omitted
    from the prompt because Gemini receives access through a persistent
    File Search store. Groq continues to use the existing inline-context path.
    """
    length_instruction = (
        "Be concise: lead with the direct answer in 1-3 sentences, no preamble, no restating the question, and no unnecessary closing summary. Go longer when the user asks for detail or when more detail is necessary to answer correctly."
        if concise
        else
        "Give a complete, clearly explained answer. Use as much detail as genuinely helps the user, without padding."
    )

    chat_files = store.get_chat_files(chat_id)
    history_block = _format_history(history or [])

    if chat_files:
        file_descriptions: list[str] = []
        total_rows = 0
        any_truncated = False

        for chat_file in chat_files:
            total_rows += chat_file["row_count"]
            any_truncated = any_truncated or bool(chat_file["truncated"])
            column_names = _parse_columns(chat_file)
            file_descriptions.append(
                f"{chat_file['filename']} — {chat_file['row_count']:,} rows"
                + (f"; columns: {', '.join(column_names)}" if column_names else "")
            )

        if external_files:
            if any_truncated:
                row_note = (
                    "One or more attached files were ingested with row truncation. "
                    "Do not imply access to rows that were not stored."
                )
            else:
                row_note = (
                    f"The attached files are indexed in persistent Gemini File Search stores. "
                    f"They contain {total_rows:,} stored rows in total. Use the File Search and "
                    "Code Execution tools as the authoritative source for file-specific questions."
                )

            system_prompt = build_privy_system_prompt(
                file_descriptions=file_descriptions,
                row_note=row_note,
                length_instruction=length_instruction,
            )
            prompt_parts: list[str] = [
                "ATTACHED FILES: The protected spreadsheet data is indexed in persistent Gemini "
                "File Search stores for this chat. Use the File Search and Code Execution tools "
                "as the source of truth for file-specific analysis."
            ]
            if history_block and not server_state:
                prompt_parts.append(history_block)
            prompt_parts.append(f"CURRENT USER QUESTION:\n{masked_question}")
            user_prompt = "\n\n".join(prompt_parts)

            # The outbound request now contains only the masked question and
            # conversation text. The dataset is represented by persistent
            # File Search store names rather than repeated file uploads.
            payload_to_check = (history_block if not server_state else "") + masked_question
            badge_attachment_count = sum(
                max(0, int(chat_file.get("masked_count") or 0))
                for chat_file in chat_files
            )
            badge_payload = masked_question
            return (
                system_prompt,
                user_prompt,
                payload_to_check,
                badge_payload,
                chat_files,
                badge_attachment_count,
            )

        # Inline-context path (Groq and ordinary Gemini).
        sections: list[str] = []
        context_was_limited = False
        remaining_tokens = MAX_TOTAL_FILE_CONTEXT_TOKENS

        for index, chat_file in enumerate(chat_files, start=1):
            limited_csv, used_tokens, limited = limit_file_context(
                chat_file["filename"], chat_file["masked_csv"], remaining_tokens
            )
            if used_tokens <= 0:
                context_was_limited = True
                break

            remaining_tokens = max(0, remaining_tokens - used_tokens)
            context_was_limited = context_was_limited or limited
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
                "One or more files were ingested with row truncation; do not imply you have rows that are not stored."
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
        # Keep the stable file dataset at the front so provider-side prompt
        # caching has the best chance to reuse the large static prefix.
        prompt_parts = [f"MASKED DATA FROM ATTACHED FILES:\n{file_context}"]
        if history_block:
            prompt_parts.append(history_block)
        prompt_parts.append(f"CURRENT USER QUESTION:\n{masked_question}")
        user_prompt = "\n\n".join(prompt_parts)
        payload_to_check = file_context + history_block + masked_question
        badge_payload = file_context + masked_question
        return (
            system_prompt,
            user_prompt,
            payload_to_check,
            badge_payload,
            chat_files,
            0,
        )

    system_prompt = build_privy_system_prompt(length_instruction=length_instruction)
    prompt_parts = []
    if history_block:
        prompt_parts.append(history_block)
    prompt_parts.append(f"CURRENT USER QUESTION:\n{masked_question}")
    user_prompt = "\n\n".join(prompt_parts)
    payload_to_check = masked_question + history_block
    badge_payload = masked_question
    return (
        system_prompt,
        user_prompt,
        payload_to_check,
        badge_payload,
        [],
        0,
    )


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

    try:
        provider, api_key, model = _get_active_llm_config(body.model_id)
    except RuntimeError as e:
        # The count can still be computed from the current question even when
        # provider configuration is missing.
        masked_count = count_masked_tokens(masked_question)
        msg = f"{e}. Ask an admin to check the model configuration in Settings."
        yield _sse({"delta": msg})
        store.add_message(chat_id, "assistant", msg, masked_count)
        yield _sse({"done": True, "masked_count": masked_count})
        return

    external_files = provider == "gemini"
    (
        system_prompt,
        user_prompt,
        payload_to_check,
        badge_payload,
        chat_files,
        badge_attachment_count,
    ) = _build_prompt(
        chat_id,
        masked_question,
        body.concise,
        history,
        external_files=external_files,
        server_state=False,
    )
    max_tokens = CONCISE_MAX_OUTPUT_TOKENS if body.concise else DETAILED_MAX_OUTPUT_TOKENS
    masked_count = badge_attachment_count + count_masked_tokens(badge_payload)

    if looks_unmasked(payload_to_check):
        answer = (
            "Request blocked: content still looks like it contains unmasked personal data. "
            "Nothing was sent to the model."
        )
        yield _sse({"delta": answer})
        store.add_message(chat_id, "assistant", answer, masked_count)
        yield _sse({"done": True, "masked_count": masked_count})
        return

    file_refs: list[dict] = []
    if external_files and chat_files:
        try:
            for chat_file in chat_files:
                cached_file = get_or_upload_gemini_file(
                    file_id=chat_file["file_id"],
                    filename=chat_file["filename"],
                    masked_csv=chat_file["masked_csv"],
                    api_key=api_key,
                )
                file_refs.append(
                    {
                        "file_search_store_name": cached_file["store_name"],
                        "content_sha256": cached_file.get("content_sha256", ""),
                    }
                )
        except RuntimeError as e:
            msg = f"Couldn't prepare the masked file for Gemini: {e}"
            yield _sse({"delta": msg})
            store.add_message(chat_id, "assistant", msg, masked_count)
            yield _sse({"done": True, "masked_count": masked_count})
            return

    interaction_id = None
    interaction_state: dict = {}
    if provider == "gemini":
        interaction_id = get_interaction_id(
            chat_id,
            model,
            file_refs,
            message_count=len(previous_messages),
        )

        # Rebuild local masked history only when Gemini does not already have a
        # valid stateful chain. This keeps later requests small while still
        # recovering continuity after expiry/restart/model changes.
        if interaction_id:
            (
                system_prompt,
                user_prompt,
                payload_to_check,
                badge_payload,
                chat_files,
                badge_attachment_count,
            ) = _build_prompt(
                chat_id,
                masked_question,
                body.concise,
                history,
                external_files=True,
                server_state=True,
            )
            masked_count = badge_attachment_count + count_masked_tokens(badge_payload)

    answer_parts: list[str] = []
    try:
        raw_chunks = stream_llm(
            system_prompt,
            user_prompt,
            api_key,
            model,
            max_tokens=max_tokens,
            provider=provider,
            file_refs=file_refs,
            interaction_id=interaction_id,
            interaction_state=interaction_state,
        )
        for piece in stream_unmask(raw_chunks, chat_id):
            if piece:
                answer_parts.append(piece)
                yield _sse({"delta": piece})
        if provider == "gemini":
            latest_interaction_id = interaction_state.get("latest_interaction_id")
            if latest_interaction_id:
                save_interaction_id(
                    chat_id,
                    model,
                    file_refs,
                    str(latest_interaction_id),
                    message_count=len(previous_messages) + 2,
                )
    except RuntimeError as e:
        if provider == "gemini" and interaction_state.get("reset_required"):
            # The provider-side interaction expired/vanished. Clear only the
            # local pointer here; the next request will reseed from Privy state.
            from ..gemini_interactions import clear_interaction_id
            clear_interaction_id(chat_id)
        msg = str(e)
        if provider != "gemini":
            msg = f"{msg}. Ask an admin to set it up in Settings."
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

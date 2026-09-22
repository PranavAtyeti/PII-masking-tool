"""Ask questions against masked files with bounded conversation memory."""

import json
import logging
import os
import time

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .. import mapping_store as store
from ..auth import get_current_app_user
from ..llm import LLMServiceError, stream_llm, get_model_config
from ..prompts import build_privy_system_prompt
from ..context_limits import MAX_TOTAL_FILE_CONTEXT_TOKENS, limit_file_context
from ..gemini_files import get_or_upload_gemini_file
from ..gemini_interactions import (
    get_interaction_id,
    get_interaction_tool_mode,
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
from ..logging_utils import elapsed_ms, log_event, new_request_id, reset_request_id, set_request_id

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/chats", tags=["messages"])

# Keep enough history for continuity without allowing prompt growth forever.
MAX_HISTORY_MESSAGES = int(os.getenv("PRIVY_MAX_HISTORY_MESSAGES", "32"))
CONCISE_MAX_OUTPUT_TOKENS = int(os.getenv("PRIVY_CONCISE_MAX_OUTPUT_TOKENS", "2048"))
DETAILED_MAX_OUTPUT_TOKENS = int(os.getenv("PRIVY_DETAILED_MAX_OUTPUT_TOKENS", "16384"))


# This is only a provider-tool router. Privy does not perform spreadsheet
# calculations locally; Gemini still performs the actual analysis.
_CODE_EXECUTION_PATTERNS = [
    r"\b(total|sum|summ?ation)\b",
    r"\b(average|avg|mean|median)\b",
    r"\b(minimum|minimum value|maximum|maximum value|min|max)\b",
    r"\b(highest|lowest|largest|smallest)\b",
    r"\b(count|counting)\b",
    r"\b(how many)\b",
    r"\b(percentage|percent|ratio|difference|variance|standard deviation|std dev)\b",
    r"\b(top\s+\d+|bottom\s+\d+)\b",
    r"\b(rank|ranking|ranked|compare|comparison)\b",
    r"\b(group(?:ed)? by|grouping by)\b",
    r"\b(filter|filtered|sort|sorted|order by)\b",
    r"\b(trend|monthly|weekly|daily|quarterly|yearly|year-over-year|yoy)\b",
    r"\b(calculate|calculation|compute|computed|analyze|analysis)\b",
]


def _requires_code_execution(question: str) -> bool:
    """Route deterministic/structured-data questions to Gemini Code Execution.

    This function only chooses the Gemini capability. It never calculates data
    itself, so Privy remains LLM-first rather than becoming an analytics engine.
    """
    import re

    text = " ".join(str(question or "").strip().lower().split())
    if not text:
        return False

    # Metadata questions are cheap and reliable through File Search/prompt data.
    if re.search(r"\b(how many|count|number of)\s+rows?\b", text):
        return False
    if re.search(r"\b(what|which)\s+(are\s+)?the\s+column|column names?\b", text):
        return False
    if re.search(r"\b(summary|summarize|overview)\b", text) and not re.search(
        r"\b(total|sum|average|count|highest|lowest|top\s+\d+|bottom\s+\d+)\b", text
    ):
        return False

    return any(re.search(pattern, text, re.IGNORECASE) for pattern in _CODE_EXECUTION_PATTERNS)


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
    tool_mode: str | None = None,
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
                tool_mode=tool_mode,
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
            tool_mode=tool_mode,
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


def _generate_impl(chat_id: str, body: MessageIn, user_id: str):
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
    masking_started = time.perf_counter()
    masked_question = _replace_known_values(body.question, known_values_before_turn)
    masked_question = mask_free_text_cell(
        masked_question, chat_id, counters, body.ner_confidence, body.use_ner
    )
    log_event(
        logger,
        "masking_complete",
        duration_ms=elapsed_ms(masking_started),
        masked_tokens=count_masked_tokens(masked_question),
    )

    # Reuse mappings from the current chat so stored prior responses containing PII
    # are tokenized before being included in model context.
    history = _mask_history(previous_messages, store.get_known_values(chat_id))

    provider_started = time.perf_counter()
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

    log_event(
        logger,
        "provider_selected",
        provider=provider,
        model=model,
        duration_ms=elapsed_ms(provider_started),
    )

    external_files = provider == "gemini"
    available_chat_files = store.get_chat_files(chat_id) if external_files else []
    gemini_tool_mode = (
        "code_execution"
        if external_files and available_chat_files and _requires_code_execution(masked_question)
        else "file_search"
        if external_files and available_chat_files
        else None
    )
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
        tool_mode=gemini_tool_mode,
    )
    max_tokens = CONCISE_MAX_OUTPUT_TOKENS if body.concise else DETAILED_MAX_OUTPUT_TOKENS
    masked_count = badge_attachment_count + count_masked_tokens(badge_payload)

    if looks_unmasked(payload_to_check):
        log_event(
            logger,
            "outbound_privacy_blocked",
            level=logging.WARNING,
            provider=provider,
            reason="prompt_contains_unmasked_sensitive_data",
        )
        answer = (
            "Request blocked: content still looks like it contains unmasked personal data. "
            "Nothing was sent to the model."
        )
        yield _sse({"delta": answer})
        store.add_message(chat_id, "assistant", answer, masked_count)
        yield _sse({"done": True, "masked_count": masked_count})
        return

    # Gemini receives the complete masked CSV through its provider-managed file
    # resources. Validate those exact bytes at the outbound boundary as well as
    # during upload, so a future code path cannot send an unsafe cached dataset.
    if external_files and chat_files:
        for chat_file in chat_files:
            if looks_unmasked(str(chat_file.get("masked_csv") or "")):
                log_event(
                    logger,
                    "outbound_privacy_blocked",
                    level=logging.ERROR,
                    provider=provider,
                    reason="gemini_file_contains_unmasked_sensitive_data",
                )
                answer = (
                    "Request blocked: the protected file failed the outbound privacy check. "
                    "Nothing was sent to the model."
                )
                yield _sse({"delta": answer})
                store.add_message(chat_id, "assistant", answer, masked_count)
                yield _sse({"done": True, "masked_count": masked_count})
                return

    file_refs: list[dict] = []
    if external_files and chat_files:
        file_prepare_started = time.perf_counter()
        log_event(
            logger,
            "gemini_file_prepare_start",
            file_count=len(chat_files),
            tool_mode=gemini_tool_mode,
        )
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
                        "include_document": False,
                        "file_uri": cached_file.get("file_uri", ""),
                        "mime_type": cached_file.get("mime_type", "text/csv"),
                    }
                )
            log_event(
                logger,
                "gemini_file_prepare_complete",
                file_count=len(file_refs),
                duration_ms=elapsed_ms(file_prepare_started),
            )
        except RuntimeError as e:
            log_event(
                logger,
                "gemini_file_prepare_error",
                level=logging.ERROR,
                file_count=len(file_refs),
                duration_ms=elapsed_ms(file_prepare_started),
                error_type=type(e).__name__,
            )
            msg = "We couldn't prepare the protected file for Gemini. Please try again."
            yield _sse({
                "delta": msg,
                "error": {"category": "file_preparation", "retryable": True},
            })
            store.add_message(chat_id, "assistant", msg, masked_count)
            yield _sse({"done": True, "masked_count": masked_count})
            return

    interaction_id = None
    interaction_state: dict = {}
    if provider == "gemini":
        interaction_lookup_started = time.perf_counter()
        interaction_id = get_interaction_id(
            chat_id,
            model,
            file_refs,
            message_count=len(previous_messages),
        )
        log_event(
            logger,
            "interaction_state_lookup_complete",
            reused=bool(interaction_id),
            duration_ms=elapsed_ms(interaction_lookup_started),
        )
        previous_tool_mode = (
            get_interaction_tool_mode(
                chat_id,
                model,
                file_refs,
                message_count=len(previous_messages),
            )
            if interaction_id
            else None
        )

        # The raw Gemini File is supplied as a document only when entering
        # Code Execution for the first time. Subsequent Code Execution turns
        # reuse the document carried in the prior stateful interaction.
        if gemini_tool_mode == "code_execution":
            attach_document = previous_tool_mode != "code_execution"
            for file_ref in file_refs:
                file_ref["include_document"] = attach_document

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
                tool_mode=gemini_tool_mode,
            )
            masked_count = badge_attachment_count + count_masked_tokens(badge_payload)

    # Final pre-send prompt gate. This intentionally runs after interaction-state
    # reconstruction because that is the exact prompt that will be sent.
    if looks_unmasked(payload_to_check):
        log_event(
            logger,
            "outbound_privacy_blocked",
            level=logging.WARNING,
            provider=provider,
            reason="final_prompt_contains_unmasked_sensitive_data",
        )
        answer = (
            "Request blocked: content still looks like it contains unmasked personal data. "
            "Nothing was sent to the model."
        )
        yield _sse({"delta": answer})
        store.add_message(chat_id, "assistant", answer, masked_count)
        yield _sse({"done": True, "masked_count": masked_count})
        return

    answer_parts: list[str] = []
    llm_started = time.perf_counter()
    log_event(
        logger,
        "llm_stream_start",
        provider=provider,
        model=model,
        tool_mode=gemini_tool_mode,
        reused_interaction=bool(interaction_id),
    )
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
        unmask_started = time.perf_counter()
        for piece in stream_unmask(raw_chunks, chat_id):
            if piece:
                answer_parts.append(piece)
                yield _sse({"delta": piece})
        log_event(
            logger,
            "unmask_complete",
            duration_ms=elapsed_ms(unmask_started),
            response_chars=len("".join(answer_parts)),
        )
        log_event(
            logger,
            "llm_stream_complete",
            provider=provider,
            model=model,
            duration_ms=elapsed_ms(llm_started),
            response_chars=len("".join(answer_parts)),
        )
        if provider == "gemini":
            latest_interaction_id = interaction_state.get("latest_interaction_id")
            if latest_interaction_id:
                save_interaction_id(
                    chat_id,
                    model,
                    file_refs,
                    str(latest_interaction_id),
                    message_count=len(previous_messages) + 2,
                    tool_mode=gemini_tool_mode,
                )
    except LLMServiceError as e:
        log_event(
            logger,
            "llm_service_error",
            level=logging.ERROR,
            provider=provider,
            model=model,
            duration_ms=elapsed_ms(llm_started),
            category=e.category,
            status_code=e.status_code,
            error_code=e.code,
            retryable=e.retryable,
        )
        if provider == "gemini" and interaction_state.get("reset_required"):
            from ..gemini_interactions import clear_interaction_id
            clear_interaction_id(chat_id)
        msg = e.user_message
        answer_parts = [msg]
        yield _sse({
            "delta": msg,
            "error": {
                "category": e.category,
                "retryable": e.retryable,
            },
        })
    except RuntimeError as e:
        log_event(
            logger,
            "llm_runtime_error",
            level=logging.ERROR,
            provider=provider,
            model=model,
            duration_ms=elapsed_ms(llm_started),
            error_type=type(e).__name__,
        )
        if provider == "gemini" and interaction_state.get("reset_required"):
            from ..gemini_interactions import clear_interaction_id
            clear_interaction_id(chat_id)
        msg = "The AI service could not complete this request. Please try again."
        answer_parts = [msg]
        yield _sse({
            "delta": msg,
            "error": {
                "category": "runtime",
                "retryable": True,
            },
        })
    except requests.exceptions.Timeout as e:
        log_event(
            logger,
            "llm_network_error",
            level=logging.ERROR,
            provider=provider,
            model=model,
            duration_ms=elapsed_ms(llm_started),
            category="timeout",
            error_type=type(e).__name__,
        )
        msg = "The AI service took too long to respond. Please try again."
        answer_parts = [msg]
        yield _sse({
            "delta": msg,
            "error": {"category": "timeout", "retryable": True},
        })
    except requests.exceptions.ConnectionError as e:
        log_event(
            logger,
            "llm_network_error",
            level=logging.ERROR,
            provider=provider,
            model=model,
            duration_ms=elapsed_ms(llm_started),
            category="network",
            error_type=type(e).__name__,
        )
        msg = "We couldn't connect to the AI service. Please try again."
        answer_parts = [msg]
        yield _sse({
            "delta": msg,
            "error": {"category": "network", "retryable": True},
        })
    except Exception as e:
        log_event(
            logger,
            "llm_unexpected_error",
            level=logging.ERROR,
            provider=provider,
            model=model,
            duration_ms=elapsed_ms(llm_started),
            error_type=type(e).__name__,
        )
        msg = "Something went wrong while processing your request. Please try again."
        answer_parts = [msg]
        yield _sse({
            "delta": msg,
            "error": {"category": "unexpected", "retryable": True},
        })

    answer = "".join(answer_parts)
    store.add_message(chat_id, "assistant", answer, masked_count)
    yield _sse({"done": True, "masked_count": masked_count})


def _generate(chat_id: str, body: MessageIn, user_id: str):
    request_id = new_request_id()
    token = set_request_id(request_id)
    started = time.perf_counter()
    log_event(
        logger,
        "request_start",
        chat_id=chat_id,
        question_chars=len(body.question or ""),
        requested_model=body.model_id or "default",
    )
    try:
        yield from _generate_impl(chat_id, body, user_id)
    finally:
        log_event(
            logger,
            "request_complete",
            chat_id=chat_id,
            duration_ms=elapsed_ms(started),
        )
        reset_request_id(token)


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

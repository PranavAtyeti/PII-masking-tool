"""Ask questions against all masked files attached to a chat."""

import json

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from .. import mapping_store as store
from ..auth import get_current_app_user
from ..context_limits import MAX_TOTAL_FILE_CONTEXT_TOKENS, limit_file_context
from ..llm import call_llm, get_model_config, stream_llm
from ..masking import (
    _replace_known_values,
    count_masked_tokens,
    mask_free_text_cell,
    stream_unmask,
    unmask_text,
)
from ..schemas import MessageIn
from ..security_scan import scan_for_unmasked_pii

router = APIRouter(prefix="/api/chats", tags=["messages"])
GUEST_MAX_QUESTIONS = int(__import__("os").getenv("PRIVY_GUEST_MAX_QUESTIONS", "5"))


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _get_active_llm_config(model_id: str | None = None):
    """Resolve (provider, api_key, model), preferring an explicit model ID."""
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


def _file_metadata_index(chat_files: list[dict]) -> str:
    """Build a lightweight index that is always included regardless of row budget."""
    lines = ["AVAILABLE FILES (metadata only; complete list):"]
    for index, chat_file in enumerate(chat_files, start=1):
        columns: list[str] = []
        try:
            columns_data = json.loads(chat_file.get("columns_json") or "[]")
            columns = [str(item.get("name")) for item in columns_data if item.get("name")]
        except (TypeError, ValueError):
            columns = []

        status = "truncated" if chat_file.get("truncated") else "complete"
        lines.append(
            f"{index}. {chat_file['filename']} | rows: {chat_file['row_count']} | "
            f"status: {status} | columns: {', '.join(columns) or '(unknown)'}"
        )
    return "\n".join(lines)


def _build_prompt(chat_id: str, masked_question: str, concise: bool):
    length_instruction = (
        "Be concise: lead with the direct answer in 1-3 sentences, no preamble, "
        "no restating the question, no closing summary. Only go longer if the "
        "question explicitly asks for detail or a breakdown."
        if concise
        else
        "Give a complete, clearly explained answer, using more than a few sentences "
        "where it genuinely helps understanding."
    )

    chat_files = store.get_chat_files(chat_id)
    if not chat_files:
        system_prompt = (
            "You are a helpful, general-purpose assistant. Any personal information the "
            "user typed may already have been replaced with placeholder tokens. Treat those "
            "tokens as stand-ins and use them exactly as written, rather than guessing what "
            "they contain. " + length_instruction
        )
        return system_prompt, masked_question, masked_question, {"files_used": [], "files_skipped": []}

    metadata_index = _file_metadata_index(chat_files)
    sections: list[str] = []
    files_used: list[str] = []
    files_skipped: list[str] = []
    total_rows = 0
    any_truncated = False
    context_was_limited = False
    remaining_tokens = MAX_TOTAL_FILE_CONTEXT_TOKENS

    for chat_file in chat_files:
        total_rows += chat_file["row_count"]
        any_truncated = any_truncated or chat_file["truncated"]

        limited_csv, used_tokens, limited = limit_file_context(
            chat_file["filename"],
            chat_file["masked_csv"],
            remaining_tokens,
        )

        if used_tokens <= 0:
            context_was_limited = True
            files_skipped.append(chat_file["filename"])
            continue

        remaining_tokens = max(0, remaining_tokens - used_tokens)
        context_was_limited = context_was_limited or limited
        files_used.append(chat_file["filename"])
        sections.append(f"[FILE: {chat_file['filename']}]\n{limited_csv}")

    row_context = "\n\n".join(sections)

    row_notes: list[str] = []
    if any_truncated:
        row_notes.append(
            "Some stored files are truncated to their first 200 rows; never imply that every row was analyzed."
        )
    else:
        row_notes.append(
            f"The stored files contain {total_rows} rows in total, but only the supplied row context is available to you."
        )
    if context_was_limited:
        row_notes.append(
            f"The combined row context is limited to about {MAX_TOTAL_FILE_CONTEXT_TOKENS:,} estimated tokens. "
            "Do not claim to have analyzed content that is not present."
        )

    system_prompt = (
        "You are analyzing one or more spreadsheets where sensitive values have been "
        "replaced with placeholder tokens like [PERSON_NAME_1], [EMAIL_EMAIL_1], "
        "[ID_AADHAAR_2]. Never claim to know the real values behind tokens. "
        "Use only the supplied file metadata and masked row data. Never guess or "
        "reconstruct masked values. Reproduce tokens exactly when referring to them. "
        "When asked about a specific person, use a PERSON_ token rather than an ID token. "
        "The file metadata index is complete even when row context is limited. "
        + " ".join(row_notes)
        + " "
        + length_instruction
    )
    user_prompt = f"{metadata_index}\n\nMASKED ROW DATA:\n{row_context or '(No row context available)'}\n\nQUESTION: {masked_question}"
    payload_to_check = f"{metadata_index}\n{row_context}\n{masked_question}"

    return (
        system_prompt,
        user_prompt,
        payload_to_check,
        {"files_used": files_used, "files_skipped": files_skipped},
    )


def _prepare_generation(chat_id: str, body: MessageIn):
    counters = store.load_counters(chat_id)
    known_values = store.get_known_values(chat_id)
    masked_question = _replace_known_values(body.question, known_values)
    masked_question = mask_free_text_cell(
        masked_question, chat_id, counters, body.ner_confidence, body.use_ner
    )

    system_prompt, user_prompt, payload_to_check, file_meta = _build_prompt(
        chat_id, masked_question, body.concise
    )
    masked_count = count_masked_tokens(payload_to_check)
    findings = scan_for_unmasked_pii(payload_to_check)

    return {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "masked_question": masked_question,
        "payload_to_check": payload_to_check,
        "masked_count": masked_count,
        "file_meta": file_meta,
        "findings": findings,
    }


def _risk_message(findings: list[dict]) -> str:
    labels = ", ".join(
        f"{finding['type']} ({finding['count']})" for finding in findings
    )
    return (
        "Privy detected possible unmasked sensitive data before sending this request to the AI. "
        f"Detected pattern(s): {labels}."
    )


def _message_metadata(provider: str, model: str, prepared: dict, allow_unmasked_risk: bool) -> dict:
    return {
        "provider": provider,
        "model": model,
        "files_used": prepared["file_meta"]["files_used"],
        "files_skipped": prepared["file_meta"]["files_skipped"],
        "masked_token_count": prepared["masked_count"],
        "security_override": bool(allow_unmasked_risk),
    }


def _generate(chat_id: str, body: MessageIn, user_id: str, prepared: dict, provider: str, api_key: str, model: str):
    is_first_message = len(store.get_chat_messages(chat_id)) == 0
    metadata = _message_metadata(provider, model, prepared, body.allow_unmasked_risk)

    store.add_message(chat_id, "user", body.question, metadata=metadata)
    if is_first_message:
        fallback_title = " ".join(body.question.strip().split())
        if len(fallback_title) > 40:
            fallback_title = fallback_title[:40].rstrip() + "\u2026"
        store.rename_chat(chat_id, user_id, fallback_title or "New chat")

    masked_count = prepared["masked_count"]
    masked_question = prepared["masked_question"]
    system_prompt = prepared["system_prompt"]
    user_prompt = prepared["user_prompt"]

    if is_first_message:
        try:
            title_raw = call_llm(
                "Write a short title (3-6 words) summarizing the topic of the user's "
                "message below. Plain text only -- no quotes, no punctuation at the end, "
                "no preamble like 'Title:'.",
                masked_question,
                api_key,
                model,
                temperature=0.3,
                max_tokens=16,
            )
            title = unmask_text(" ".join(title_raw.strip().split()), chat_id)
            title = title.strip(" \\\"'.")[:60]
            if title:
                store.rename_chat(chat_id, user_id, title)
        except Exception:
            pass

    answer_parts: list[str] = []
    try:
        raw_chunks = stream_llm(
            system_prompt, user_prompt, api_key, model, max_tokens=250 if body.concise else 800
        )
        for piece in stream_unmask(raw_chunks, chat_id):
            if piece:
                answer_parts.append(piece)
                yield _sse({"delta": piece})
    except requests.exceptions.ReadTimeout:
        msg = f"{provider.title()} took too long to respond. Try again or choose another model."
        answer_parts = [msg]
        yield _sse({"delta": msg})
    except requests.exceptions.ConnectionError:
        msg = f"Couldn't reach {provider.title()}. Check your internet connection or choose another model."
        answer_parts = [msg]
        yield _sse({"delta": msg})
    except requests.exceptions.Timeout:
        msg = f"The {provider.title()} request timed out. Try again or choose another model."
        answer_parts = [msg]
        yield _sse({"delta": msg})
    except RuntimeError as e:
        msg = str(e)
        answer_parts = [msg]
        yield _sse({"delta": msg})
    except Exception as e:
        msg = f"{provider.title()} returned an unexpected error: {e}"
        answer_parts = [msg]
        yield _sse({"delta": msg})

    answer = "".join(answer_parts).strip()
    if not answer:
        answer = "The model returned an empty response. Please try again or choose another model."
        yield _sse({"delta": answer})

    assistant_metadata = dict(metadata)
    store.add_message(chat_id, "assistant", answer, masked_count, assistant_metadata)
    yield _sse({"done": True, "masked_count": masked_count})


@router.post("/{chat_id}/messages")
def post_message(
    chat_id: str,
    body: MessageIn,
    user: dict = Depends(get_current_app_user),
):
    if not store.get_chat(chat_id, user["auth0_sub"]):
        raise HTTPException(status_code=404, detail="Chat not found")

    if user.get("role") == "guest" and store.count_user_questions(user["auth0_sub"]) >= GUEST_MAX_QUESTIONS:
        raise HTTPException(
            status_code=429,
            detail=f"Guest sessions are limited to {GUEST_MAX_QUESTIONS} questions. Sign in to continue.",
        )

    prepared = _prepare_generation(chat_id, body)
    if prepared["findings"] and not body.allow_unmasked_risk:
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": "UNMASKED_PII",
                    "message": _risk_message(prepared["findings"]),
                    "findings": prepared["findings"],
                }
            },
        )

    try:
        provider, api_key, model = _get_active_llm_config(body.model_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    return StreamingResponse(
        _generate(chat_id, body, user["auth0_sub"], prepared, provider, api_key, model),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

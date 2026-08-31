"""Ask a question against all masked files attached to a chat."""

import json

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .. import mapping_store as store
from ..auth import get_current_app_user
from ..llm import call_llm, stream_llm, LLM_API_KEY_ENV, LLM_MODEL_DEFAULT
from ..context_limits import (
    MAX_TOTAL_FILE_CONTEXT_TOKENS,
    limit_file_context,
)
from ..masking import (
    mask_free_text_cell, _replace_known_values, unmask_text, stream_unmask,
    looks_unmasked, count_masked_tokens,
)
from ..schemas import MessageIn

router = APIRouter(prefix="/api/chats", tags=["messages"])


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _get_active_llm_config():
    api_key = store.get_admin_config("llm_api_key", LLM_API_KEY_ENV)
    model = store.get_admin_config("llm_model", LLM_MODEL_DEFAULT)
    return api_key, model


def _build_prompt(chat_id: str, masked_question: str, concise: bool):
    length_instruction = (
        "Be concise: lead with the direct answer in 1-3 sentences, no preamble, "
        "no restating the question, no closing summary. Only go longer if the "
        "question explicitly asks for detail or a breakdown."
        if concise else
        "Give a complete, clearly explained answer, using more than a few sentences "
        "where it genuinely helps understanding."
    )

    chat_files = store.get_chat_files(chat_id)
    if chat_files:
        sections: list[str] = []
        total_rows = 0
        any_truncated = False
        context_was_limited = False
        remaining_tokens = MAX_TOTAL_FILE_CONTEXT_TOKENS

        for index, chat_file in enumerate(chat_files, start=1):
            total_rows += chat_file["row_count"]
            any_truncated = any_truncated or chat_file["truncated"]

            limited_csv, used_tokens, limited = limit_file_context(
                chat_file["filename"],
                chat_file["masked_csv"],
                remaining_tokens,
            )
            if used_tokens <= 0:
                context_was_limited = True
                break

            remaining_tokens = max(0, remaining_tokens - used_tokens)
            context_was_limited = context_was_limited or limited
            sections.append(
                f"=== FILE {index}: {chat_file['filename']} ===\n"
                f"{limited_csv}"
            )

            if remaining_tokens <= 0:
                context_was_limited = context_was_limited or index < len(chat_files)
                break

        file_context = "\n\n".join(sections)
        row_notes = []
        if any_truncated:
            row_notes.append(
                "One or more files are truncated to their first 200 rows; do not imply "
                "you have every row from those files."
            )
        else:
            row_notes.append(
                f"You are seeing the stored rows across the attached files ({total_rows} rows total)."
            )
        if context_was_limited:
            row_notes.append(
                f"Privy may limit file context to about {MAX_TOTAL_FILE_CONTEXT_TOKENS:,} "
                "tokens across this request. Do not claim to have analyzed rows or file "
                "content that is not present in the supplied context."
            )
        row_note = " ".join(row_notes)

        system_prompt = (
            "You are analyzing one or more spreadsheets where sensitive values have been "
            "replaced with placeholder tokens like [PERSON_NAME_1], [EMAIL_EMAIL_1], "
            "[ID_AADHAAR_2]. The part before the number hints at the data type and the "
            "column it came from. Never claim to know the real values behind tokens. "
            "Answer using only the masked data provided -- do not guess or fill in values "
            "that aren't there. " + row_note + " Keep file names in mind when combining "
            "information across files. When asked to identify a specific person, refer to "
            "them using the token from the column that actually represents their name "
            "(tokens starting with PERSON_), not an ID/account/customer-number token, even "
            "if both appear in the same row. Always reproduce tokens exactly as given, "
            "including the surrounding brackets. If a question requires exact arithmetic "
            "across many rows (sums, medians, averages, counts), work through the rows "
            "carefully and show the relevant reasoning briefly before the final answer, "
            "rather than estimating. " + length_instruction
        )
        user_prompt = f"MASKED DATA FROM ATTACHED FILES:\n{file_context}\n\nQUESTION: {masked_question}"
        payload_to_check = file_context + masked_question
    else:
        system_prompt = (
            "You are a helpful, general-purpose assistant. Any personal information the "
            "user typed may have already been replaced with placeholder tokens before "
            "reaching you. Treat those tokens as stand-ins and use them exactly as written, "
            "rather than guessing what they contain. " + length_instruction
        )
        user_prompt = masked_question
        payload_to_check = masked_question

    return system_prompt, user_prompt, payload_to_check


def _generate(chat_id: str, body: MessageIn, user_id: str):
    is_first_message = len(store.get_chat_messages(chat_id)) == 0

    store.add_message(chat_id, "user", body.question)
    if is_first_message:
        fallback_title = " ".join(body.question.strip().split())
        if len(fallback_title) > 40:
            fallback_title = fallback_title[:40].rstrip() + "\u2026"
        store.rename_chat(chat_id, user_id, fallback_title or "New chat")

    counters = store.load_counters(chat_id)
    known_values = store.get_known_values(chat_id)
    masked_question = _replace_known_values(body.question, known_values)
    masked_question = mask_free_text_cell(
        masked_question, chat_id, counters, body.ner_confidence, body.use_ner
    )

    system_prompt, user_prompt, payload_to_check = _build_prompt(
        chat_id, masked_question, body.concise
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

    api_key, model = _get_active_llm_config()

    if is_first_message:
        try:
            title_raw = call_llm(
                "Write a short title (3-6 words) summarizing the topic of the user's "
                "message below. Plain text only -- no quotes, no punctuation at the end, "
                "no preamble like 'Title:'.",
                masked_question, api_key, model,
                temperature=0.3, max_tokens=16,
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

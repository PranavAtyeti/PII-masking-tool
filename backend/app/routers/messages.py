"""
routers/messages.py
--------------------
Ask a question in a chat. Streams the answer back over Server-Sent Events
(SSE) as it arrives from the model, unmasking safely as it goes (see
masking.stream_unmask for why that's not just "unmask each chunk").

SSE event shapes sent to the client, one JSON object per `data:` line:
  {"delta": "..."}                      -- a piece of the answer, append it
  {"done": true, "masked_count": N}      -- stream finished normally
  {"error": "..."}                       -- something went wrong; the
                                             "error" text IS the message to
                                             show the user (already phrased
                                             for display, not a raw
                                             exception)

Whatever the outcome, the assistant's message is persisted to the chat
exactly once, after the stream ends -- same guarantee the original
Streamlit version had (one chat_history entry per question, even for a
blocked/errored answer).
"""

import json

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from .. import mapping_store as store
from ..llm import call_llm, stream_llm, LLM_API_KEY_ENV, LLM_MODEL_DEFAULT
from ..masking import (
    mask_free_text_cell, _replace_known_values, unmask_text, stream_unmask,
    looks_unmasked, count_masked_tokens,
)
from ..schemas import MessageIn

router = APIRouter(prefix="/api/chats", tags=["messages"])


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _get_active_llm_config():
    """Same precedence as the Streamlit version: admin-set DB config wins
    over .env, which wins over the hardcoded default. Not cached -- an
    admin's change should apply to the very next request, from anyone."""
    api_key = store.get_admin_config("llm_api_key", LLM_API_KEY_ENV)
    model = store.get_admin_config("llm_model", LLM_MODEL_DEFAULT)
    return api_key, model


def _build_prompt(chat_id: str, masked_question: str, concise: bool):
    """Returns (system_prompt, user_prompt, payload_to_check). Branches on
    whether this chat has an uploaded file, same as the original
    process_question(). The file's masked CSV is read back from
    mapping_store (persisted at upload time) rather than from any
    in-memory dataframe -- there isn't one; see masking.py's docstring."""
    length_instruction = (
        "Be concise: lead with the direct answer in 1-3 sentences, no preamble, "
        "no restating the question, no closing summary. Only go longer if the "
        "question explicitly asks for detail or a breakdown."
        if concise else
        "Give a complete, clearly explained answer, using more than a few "
        "sentences where it genuinely helps understanding."
    )

    chat_file = store.get_chat_file(chat_id)
    if chat_file:
        row_note = (
            f"You are seeing the first 200 of {chat_file['row_count']} total rows -- "
            "do not imply or assume you have the full dataset."
            if chat_file["truncated"] else
            f"You are seeing all {chat_file['row_count']} rows of the dataset."
        )
        system_prompt = (
            "You are analyzing a spreadsheet where sensitive values have been "
            "replaced with placeholder tokens like [PERSON_NAME_1], [EMAIL_EMAIL_1], "
            "[ID_AADHAAR_2]. The part before the number hints at both the data type "
            "and the column it came from. Never claim to know the real values behind "
            "tokens. Answer using only the masked data provided -- do not guess or "
            "fill in values that aren't there. " + row_note + " When asked to "
            "identify a specific person (e.g. 'who earns the most'), refer to them "
            "using the token from the column that actually represents their name "
            "(tokens starting with PERSON_), not an ID/account/customer-number token, "
            "even if both appear in the same row. Always reproduce tokens exactly as "
            "given, including the surrounding brackets (e.g. [PERSON_NAME_1]) -- never "
            "paraphrase, reformat, or drop the brackets, or the value cannot be "
            "restored for display. If a question requires exact arithmetic across many "
            "rows (sums, medians, averages, counts), work through the rows carefully "
            "and show your row-by-row reasoning briefly before giving the final answer, "
            "rather than estimating. " + length_instruction
        )
        user_prompt = f"MASKED DATA (CSV):\n{chat_file['masked_csv']}\n\nQUESTION: {masked_question}"
        payload_to_check = chat_file["masked_csv"] + masked_question
    else:
        system_prompt = (
            "You are a helpful, general-purpose assistant. Any personal information "
            "the user typed (names, emails, phone numbers, etc.) may have already "
            "been replaced with placeholder tokens like [PERSON_NAME_1] before "
            "reaching you, as a privacy safeguard -- if you see a token like that, "
            "treat it as a stand-in for that piece of information and use it "
            "naturally in your answer exactly as written, brackets included, rather "
            "than commenting on it or guessing what it says. " + length_instruction
        )
        user_prompt = masked_question
        payload_to_check = masked_question

    return system_prompt, user_prompt, payload_to_check


def _generate(chat_id: str, body: MessageIn):
    """The actual SSE event generator. All persistence + error handling
    lives in here so it happens regardless of how the stream ends."""
    is_first_message = len(store.get_chat_messages(chat_id)) == 0

    store.add_message(chat_id, "user", body.question)
    if is_first_message:
        fallback_title = " ".join(body.question.strip().split())
        if len(fallback_title) > 40:
            fallback_title = fallback_title[:40].rstrip() + "\u2026"
        store.rename_chat(chat_id, fallback_title or "New chat")

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
            "Request blocked: content still looks like it contains unmasked "
            "personal data. Nothing was sent to the model."
        )
        yield _sse({"delta": answer})
        store.add_message(chat_id, "assistant", answer, masked_count)
        yield _sse({"done": True, "masked_count": masked_count})
        return

    api_key, model = _get_active_llm_config()

    if is_first_message:
        # Best-effort title polish -- see the Streamlit version's comment
        # for why this is silent-fail and uses the already-masked question.
        try:
            title_raw = call_llm(
                "Write a short title (3-6 words) summarizing the topic of "
                "the user's message below. Plain text only -- no quotes, "
                "no punctuation at the end, no preamble like 'Title:'.",
                masked_question, api_key, model,
                temperature=0.3, max_tokens=16,
            )
            title = unmask_text(" ".join(title_raw.strip().split()), chat_id)
            title = title.strip(" \"'.")[:60]
            if title:
                store.rename_chat(chat_id, title)
        except Exception:
            pass

    answer_parts = []
    try:
        raw_chunks = stream_llm(system_prompt, user_prompt, api_key, model, max_tokens=max_tokens)
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
def post_message(chat_id: str, body: MessageIn):
    if not store.get_chat(chat_id):
        raise HTTPException(status_code=404, detail="Chat not found")
    return StreamingResponse(_generate(chat_id, body), media_type="text/event-stream")

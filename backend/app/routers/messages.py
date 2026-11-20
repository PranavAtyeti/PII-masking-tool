"""Ask questions against masked files with bounded conversation memory."""

import csv
import io
import json
import logging
import os
import re
import time

import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .. import mapping_store as store
from ..auth import get_current_app_user
from ..llm import LLMServiceError, stream_llm_events, get_model_config
from ..context_builder import build_prompt, PromptBudgetExceeded
from ..prompts import build_privy_system_prompt
from ..context_limits import MAX_TOTAL_FILE_CONTEXT_TOKENS, estimate_tokens, limit_file_context
from ..provider_registry import model_capabilities
from ..gemini_files import get_or_upload_gemini_file, upload_calculation_projection
from ..gemini_interactions import (
    get_interaction_id,
    get_interaction_tool_mode,
    save_interaction_id,
    delete_interaction,
)
from ..masking import (
    mask_free_text_cell,
    _replace_known_values,
    stream_unmask_events,
    looks_unmasked,
    count_masked_tokens,
)
from ..privacy_policy import build_outbound_csv, load_policy, validate_outbound_text
from ..workbook import deserialize_workbook, sample_csv_context, workbook_provider_context
from ..tools import (
    MAX_TOOL_CALLS_PER_REQUEST,
    MAX_TOOL_ROUNDS,
    ToolExecutionError,
    create_tool_context,
    execute_tool_safely,
    provider_tool_schemas,
)
from ..schemas import MessageIn
from ..logging_utils import elapsed_ms, log_event, new_request_id, set_request_id
logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/chats", tags=["messages"])

# Keep enough history for continuity without allowing prompt growth forever.
MAX_HISTORY_MESSAGES = int(os.getenv("PRIVY_MAX_HISTORY_MESSAGES", "32"))
CONCISE_MAX_OUTPUT_TOKENS = int(os.getenv("PRIVY_CONCISE_MAX_OUTPUT_TOKENS", "2048"))
DETAILED_MAX_OUTPUT_TOKENS = int(os.getenv("PRIVY_DETAILED_MAX_OUTPUT_TOKENS", "16384"))


def _safe_outbound_file_context(
    chat_file: dict,
    selected_columns: set[str] | None = None,
    question: str | None = None,
    sample_rows: bool = False,
) -> str:
    policy, legacy = load_policy(
        chat_file.get("privacy_policy_json"),
        chat_file.get("columns_json"),
    )
    workbook_json = chat_file.get("workbook_json")
    if workbook_json:
        if legacy:
            raise HTTPException(status_code=422, detail="A persisted workbook is missing its privacy policy.")
        workbook = deserialize_workbook(workbook_json)
        selected_keys = None
        if selected_columns is not None:
            selected_keys = {
                column["policy_key"]
                for sheet in workbook["worksheets"]
                for column in sheet.get("columns", [])
                if column.get("display_name") in selected_columns
                and policy.get(column.get("policy_key"), {}).get("action") != "exclude"
            }
        return workbook_provider_context(
            workbook,
            policy,
            selected_keys,
            query=question,
            sample_rows=sample_rows,
        )
    safe_csv = build_outbound_csv(
        str(chat_file.get("masked_csv") or ""),
        policy,
        legacy=legacy,
    )
    return sample_csv_context(safe_csv, question) if sample_rows and question is not None else safe_csv


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



def _normalize_column_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _resolve_calculation_columns(question: str, columns: list[str]) -> tuple[list[str], list[str]]:
    """Resolve explicit column references without calculating anything locally."""
    text = _normalize_column_name(question)
    normalized = {_normalize_column_name(column): column for column in columns}

    aliases = {
        "dept": "department",
        "departments": "department",
        "salary": "salary",
        "salaries": "salary",
        "wage": "salary",
        "wages": "salary",
        "pay": "salary",
        "debit": "debit",
        "debits": "debit",
        "credit": "credit",
        "credits": "credit",
        "amount": "amount",
        "amounts": "amount",
        "tax": "tax amount",
        "taxes": "tax amount",
    }

    requested: list[str] = []
    missing: list[str] = []

    # First use exact dataset column names.
    for norm_name, original in normalized.items():
        if norm_name and re.search(rf"(?<![a-z0-9]){re.escape(norm_name)}(?![a-z0-9])", text):
            requested.append(original)

    # Then recognize common natural-language aliases.
    for alias, canonical in aliases.items():
        if not re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text):
            continue
        if canonical in normalized:
            if normalized[canonical] not in requested:
                requested.append(normalized[canonical])
        elif alias not in {"dept", "departments", "salaries", "wage", "wages", "pay", "debits", "credits", "amounts", "tax", "taxes"}:
            missing.append(alias)

    # Grouping language commonly refers to a column without saying "by".
    if re.search(r"\bby\s+department\b", text) and "department" in normalized:
        if normalized["department"] not in requested:
            requested.append(normalized["department"])

    return requested, missing


def _build_calculation_projection(question: str, chat_files: list[dict]) -> tuple[str | None, list[str], str | None]:
    """Build a small masked CSV projection for Gemini Code Execution.

    Privy only selects columns and serializes already-masked values here. The
    actual arithmetic/aggregation remains entirely inside Gemini Code Execution.
    """
    if not chat_files:
        return None, [], None

    sections: list[str] = []
    selected_union: list[str] = []

    for chat_file in chat_files:
        if chat_file.get("workbook_json"):
            policy, legacy = load_policy(
                chat_file.get("privacy_policy_json"),
                chat_file.get("columns_json"),
            )
            if legacy:
                raise HTTPException(status_code=422, detail="A persisted workbook is missing its privacy policy.")
            workbook = deserialize_workbook(chat_file["workbook_json"])
            columns = list(dict.fromkeys(
                column["display_name"]
                for sheet in workbook["worksheets"]
                for column in sheet.get("columns", [])
                if policy.get(column.get("policy_key"), {}).get("action") != "exclude"
            ))
            selected, missing = _resolve_calculation_columns(question, columns)
            if missing:
                return None, [], missing[0]
            if not selected:
                return None, [], None
            for column in selected:
                if column not in selected_union:
                    selected_union.append(column)
            safe_context = _safe_outbound_file_context(chat_file, set(selected))
            sections.append(f"FILE: dataset_{str(chat_file['file_id'])[:8]}\n{safe_context}")
            continue
        try:
            metadata = json.loads(chat_file.get("columns_json") or "[]")
        except (TypeError, ValueError):
            metadata = []
        policy, legacy = load_policy(chat_file.get("privacy_policy_json"), chat_file.get("columns_json"))
        columns = [
            str(item.get("name")) if isinstance(item, dict) else str(item)
            for item in metadata
            if (str(item.get("name")) if isinstance(item, dict) else str(item)) in policy
            and policy[(str(item.get("name")) if isinstance(item, dict) else str(item))].get("action") != "exclude"
        ]
        selected, missing = _resolve_calculation_columns(question, columns)
        if missing:
            return None, [], missing[0]
        if not selected:
            return None, [], None

        for column in selected:
            if column not in selected_union:
                selected_union.append(column)

        safe_csv = _safe_outbound_file_context(
            chat_file,
            question=f"{masked_question}\n{history_block}",
            sample_rows=True,
        )
        reader = csv.DictReader(io.StringIO(safe_csv))
        rows: list[dict[str, str]] = []
        for row in reader:
            rows.append({column: str(row.get(column) or "") for column in selected})

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=selected, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        sections.append(f"FILE: dataset_{str(chat_file['file_id'])[:8]}\n{output.getvalue()}")

    return "\n\n".join(sections), selected_union, None

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


def _mask_history(
    messages: list[dict],
    known_values: dict,
    chat_id: str,
    ner_confidence: float,
    use_ner: bool,
) -> list[dict]:
    """Return recent chat history with known and newly detected values tokenized."""
    masked: list[dict] = []
    for message in messages[-MAX_HISTORY_MESSAGES:]:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role not in {"user", "assistant"} or not content:
            continue
        content = _replace_known_values(content, known_values)
        content = mask_free_text_cell(
            content,
            chat_id,
            store.load_counters(chat_id),
            ner_confidence,
            use_ner,
        )
        masked.append({
            "role": role,
            "content": content,
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
    model: str | None = None,
    requested_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    chat_files: list[dict] | None = None,
    tools_enabled: bool = False,
    workbook_schema: str = "",
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

    if chat_files is None:
        chat_files = store.get_chat_files(chat_id)
    history_block = _format_history(history or [])

    if chat_files:
        file_descriptions: list[str] = []
        total_rows = 0
        any_truncated = False

        for index, chat_file in enumerate(chat_files, start=1):
            total_rows += chat_file["row_count"]
            any_truncated = any_truncated or bool(chat_file["truncated"])
            file_policy, _legacy = load_policy(chat_file.get("privacy_policy_json"), chat_file.get("columns_json"))
            column_names = [name for name, choice in file_policy.items() if choice.get("action") != "exclude"]
            description = (
                f"Dataset {index} — {chat_file['row_count']:,} rows"
            )
            if not tools_enabled and column_names:
                description += f"; columns: {', '.join(column_names)}"
            file_descriptions.append(description)

        if tools_enabled:
            truncation_note = (
                "Some attached files were truncated during ingestion; exact calculations apply only to rows stored locally and must not be described as covering the original full files. "
                if any_truncated
                else ""
            )
            row_note = (
                f"The complete attached datasets contain {total_rows:,} stored rows. "
                f"{truncation_note}"
                "Use the available local workbook tools for schema discovery, filtering, and exact calculations. "
                "Do not infer data values from the schema."
            )
            system_prompt = build_privy_system_prompt(
                file_descriptions=file_descriptions,
                row_note=row_note,
                length_instruction=length_instruction,
                tool_mode="local_workbook_tools",
            )
            system_prompt += "\n\n" + workbook_schema
            system_prompt += (
                "\nUse a workbook tool for any question requiring workbook facts or calculations. "
                "Never claim exact results unless a tool returns exact=true. Do not request or infer excluded columns."
            )
            built_prompt = build_prompt(
                system_prompt=system_prompt,
                file_context="",
                history=history_block,
                question=masked_question,
                requested_output_tokens=requested_output_tokens,
                model_capabilities=model_capabilities(model or ""),
            )
            return (
                system_prompt,
                built_prompt.user_prompt,
                system_prompt + built_prompt.user_prompt,
                masked_question,
                chat_files,
                0,
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
            attached_files_note = (
                "The protected spreadsheet data is indexed in persistent provider resources. "
                "Use the available retrieval or analysis tools as the source of truth."
            )
            built_prompt = build_prompt(
                system_prompt=system_prompt,
                file_context=attached_files_note,
                history=history_block if not server_state else "",
                question=masked_question,
                requested_output_tokens=requested_output_tokens,
                model_capabilities=model_capabilities(model or "", "gemini"),
                context_label="ATTACHED FILES",
            )
            user_prompt = built_prompt.user_prompt

            # The outbound request now contains only the masked question and
            # conversation text. The dataset is represented by persistent
            # File Search store names rather than repeated file uploads.
            payload_to_check = user_prompt
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
            safe_csv = _safe_outbound_file_context(
                chat_file,
                question=masked_question,
                sample_rows=True,
            )
            limited_csv, used_tokens, limited = limit_file_context(
                f"dataset_{index}.csv", safe_csv, remaining_tokens
            )
            if used_tokens <= 0:
                context_was_limited = True
                break

            remaining_tokens = max(0, remaining_tokens - used_tokens)
            context_was_limited = context_was_limited or limited
            sections.append(
                f"=== DATASET {index} ===\n{limited_csv}"
            )

            if remaining_tokens <= 0:
                context_was_limited = context_was_limited or index < len(chat_files)
                break

        file_context = "\n\n".join(sections)
        row_notes: list[str] = [
            "Inline workbook content is a bounded, question-relevant sample, not the complete dataset. Do not present calculations over this sample as exact whole-dataset results."
        ]
        if any_truncated:
            row_notes.append(
                "One or more files were ingested with row truncation; do not imply you have rows that are not stored."
            )
        elif context_was_limited:
            row_notes.append(
                f"Only selected portions of the stored data ({total_rows:,} rows total) fit this request."
            )
        else:
            row_notes.append(
                f"The attached files contain {total_rows:,} stored rows. Use the supplied relevant context to answer."
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
        built_prompt = build_prompt(
            system_prompt=system_prompt,
            file_context=file_context,
            history=history_block,
            question=masked_question,
            requested_output_tokens=requested_output_tokens,
            model_capabilities=model_capabilities(model or ""),
        )
        user_prompt = built_prompt.user_prompt
        payload_to_check = built_prompt.file_context + built_prompt.history + masked_question
        badge_payload = built_prompt.file_context + masked_question
        return (
            system_prompt,
            user_prompt,
            payload_to_check,
            badge_payload,
            chat_files,
            0,
        )

    system_prompt = build_privy_system_prompt(length_instruction=length_instruction)
    built_prompt = build_prompt(
        system_prompt=system_prompt,
        file_context="",
        history=history_block,
        question=masked_question,
        requested_output_tokens=requested_output_tokens,
        model_capabilities=model_capabilities(model or ""),
    )
    user_prompt = built_prompt.user_prompt
    payload_to_check = user_prompt
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
    history = _mask_history(
        previous_messages,
        store.get_known_values(chat_id),
        chat_id,
        body.ner_confidence,
        body.use_ner,
    )

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

    requested_max_tokens = CONCISE_MAX_OUTPUT_TOKENS if body.concise else DETAILED_MAX_OUTPUT_TOKENS
    capabilities = model_capabilities(model or "", provider)
    configured_output_limit = capabilities.get("max_output_tokens")
    max_tokens = (
        min(requested_max_tokens, configured_output_limit)
        if isinstance(configured_output_limit, int)
        else requested_max_tokens
    )
    external_files = False
    gemini_tool_mode = None
    chat_files = store.get_chat_files(chat_id)
    tools_enabled = bool(chat_files)
    tool_context = None
    workbook_schema = ""
    if tools_enabled:
        if capabilities.get("tool_calling") is not True:
            msg = "The selected AI model does not support workbook tools. Choose a model with tool-calling support."
            yield _sse({"delta": msg, "error": {"category": "unsupported_capability", "retryable": False}})
            store.add_message(chat_id, "assistant", msg, count_masked_tokens(masked_question))
            yield _sse({"done": True, "masked_count": count_masked_tokens(masked_question)})
            return
        try:
            tool_context = create_tool_context(chat_id, user_id, chat_files)
        except (ToolExecutionError, HTTPException, RuntimeError) as exc:
            log_event(
                logger,
                "workbook_tool_context_error",
                level=logging.ERROR,
                error_type=type(exc).__name__,
            )
            msg = "The attached workbook could not be safely prepared for this request."
            yield _sse({"delta": msg, "error": {"category": "workbook_validation", "retryable": False}})
            store.add_message(chat_id, "assistant", msg, count_masked_tokens(masked_question))
            yield _sse({"done": True, "masked_count": count_masked_tokens(masked_question)})
            return
    try:
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
            external_files=False,
            server_state=False,
            tool_mode="local_workbook_tools" if tools_enabled else None,
            model=model,
            requested_output_tokens=max_tokens,
            chat_files=chat_files,
            tools_enabled=tools_enabled,
            workbook_schema=workbook_schema,
        )
    except PromptBudgetExceeded:
        msg = "This request exceeds the configured context limit. Shorten it or select a model with a larger configured limit."
        yield _sse({"delta": msg, "error": {"category": "context_window", "retryable": False}})
        store.add_message(chat_id, "assistant", msg, count_masked_tokens(masked_question))
        yield _sse({"done": True, "masked_count": count_masked_tokens(masked_question)})
        return
    masked_count = badge_attachment_count + count_masked_tokens(badge_payload)

    try:
        validate_outbound_text(payload_to_check)
    except HTTPException:
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

    answer_parts: list[str] = []
    usage_state: dict = {}
    estimated_input_tokens = estimate_tokens(system_prompt + user_prompt)
    llm_started = time.perf_counter()
    log_event(logger, "llm_stream_start", provider=provider, model=model, tools_enabled=tools_enabled)
    try:
        provider_events = stream_llm_events(
            system_prompt,
            user_prompt,
            api_key,
            model,
            max_tokens=max_tokens,
            provider=provider,
            tool_schemas=provider_tool_schemas() if tools_enabled else None,
            tool_executor=(
                lambda name, arguments: execute_tool_safely(tool_context, name, arguments)
                if tool_context is not None
                else {"ok": False, "error": {"code": "tool_context_unavailable", "message": "Workbook tools are unavailable."}}
            ),
            max_tool_rounds=MAX_TOOL_ROUNDS if tools_enabled else 0,
            max_tool_calls=MAX_TOOL_CALLS_PER_REQUEST if tools_enabled else 0,
            usage_state=usage_state,
        )
        unmask_started = time.perf_counter()
        operation_labels = {
            "get_workbook_schema": "Reviewing workbook structure",
            "get_column_profile": "Profiling a workbook column",
            "filter_rows": "Filtering workbook rows",
            "aggregate_data": "Calculating a workbook result",
            "group_and_aggregate": "Comparing workbook groups",
            "compare_groups": "Comparing workbook groups",
        }
        for event in stream_unmask_events(provider_events, chat_id):
            if event.get("type") == "delta":
                piece = str(event.get("text") or "")
                if piece:
                    answer_parts.append(piece)
                    yield _sse({"delta": piece})
            elif event.get("type") in {"tool_start", "tool_complete"}:
                tool_name = str(event.get("name") or "")
                yield _sse({
                    "tool_status": {
                        "label": operation_labels.get(tool_name, "Working with workbook data"),
                        "status": "running" if event["type"] == "tool_start" else "complete",
                    }
                })
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
        msg = e.user_message
        answer_parts = [msg]
        yield _sse({
            "delta": msg,
            "error": {
                "category": e.category,
                "retryable": e.retryable,
                "retry_after_seconds": e.retry_after_seconds,
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
    usage_state.setdefault("provider", provider)
    usage_state.setdefault("model", model)
    usage_state.setdefault("source", "estimated")
    usage_state["estimated_input_tokens"] = estimated_input_tokens
    usage_state["estimated_output_tokens"] = estimate_tokens(answer)
    store.add_message(
        chat_id,
        "assistant",
        answer,
        masked_count,
        metadata={"provider": provider, "model": model, "usage": usage_state},
    )
    yield _sse({"done": True, "masked_count": masked_count, "usage": usage_state})


def _generate(chat_id: str, body: MessageIn, user_id: str):
    # The StreamingResponse body is consumed after the endpoint function has
    # returned. Resetting a ContextVar token in this generator can therefore
    # happen in a different execution context and raise ValueError after an
    # otherwise successful response. Set the correlation id for this generator
    # and let its short-lived streaming task discard the context naturally.
    request_id = new_request_id()
    set_request_id(request_id)
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

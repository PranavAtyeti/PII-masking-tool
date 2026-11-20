"""Provider-aware LLM client and streaming tool-call adapter for Privy."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, replace
from email.utils import parsedate_to_datetime
from typing import Callable, Iterator, Protocol

import requests

from .logging_utils import elapsed_ms, log_event
from .context_builder import DEFAULT_PROVIDER_OVERHEAD_TOKENS
from .context_limits import MAX_TOTAL_FILE_CONTEXT_TOKENS, estimate_tokens
from .provider_registry import (
    MODEL_CATALOG,
    PROVIDERS,
    find_model,
    model_capabilities,
    model_options,
    provider_is_disabled,
)

logger = logging.getLogger(__name__)

DEFAULT_TEMPERATURE = float(os.environ.get("MODEL_TEMPERATURE", "0.1"))
DEFAULT_MAX_OUTPUT_TOKENS = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "16384"))
LLM_CONNECT_TIMEOUT = float(os.environ.get("LLM_CONNECT_TIMEOUT", "10"))
LLM_READ_TIMEOUT = float(os.environ.get("LLM_READ_TIMEOUT", "180"))

COMMON_MODELS = [item["model"] for item in MODEL_CATALOG]

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "groq").strip().lower()
_default_provider = PROVIDERS.get(LLM_PROVIDER, PROVIDERS["groq"])
LLM_API_KEY_ENV = _default_provider["api_key_env"]
_default_model_id = os.environ.get(
    "GEMINI_MODEL" if LLM_PROVIDER == "gemini" else "GROQ_MODEL",
    "gemini-3.7-flash" if LLM_PROVIDER == "gemini" else "openai/gpt-oss-20b",
).strip()
LLM_MODEL_DEFAULT = _default_model_id


def _clean_base_url(base_url: str) -> str:
    base_url = base_url.strip().rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"


def get_model_options() -> list[dict]:
    """Return enabled, configured models and their explicit capabilities."""
    return model_options()


def get_model_config(model_id: str | None = None) -> tuple[str, str, str, str]:
    """Resolve (provider, base_url, api_key, provider_model)."""
    if model_id:
        item = find_model(model_id)
        if item is None:
            raise RuntimeError("Unsupported model selection")
        provider_name = item["provider"]
        if provider_is_disabled(provider_name):
            raise RuntimeError(f"Provider '{provider_name}' is disabled by configuration")
        provider = PROVIDERS[provider_name]
        api_key = os.environ.get(provider["api_key_env"], "").strip()
        base_url = os.environ.get(provider["base_url_env"], provider["default_base_url"])
        if not api_key:
            raise RuntimeError(f"No API key is configured for provider '{provider_name}'")
        return provider_name, _clean_base_url(base_url), api_key, item["model"]

    provider_name = os.environ.get("LLM_PROVIDER", "groq").strip().lower()
    provider = PROVIDERS.get(provider_name)
    if provider is None:
        raise RuntimeError(f"Unsupported LLM_PROVIDER '{provider_name}'")
    if provider_is_disabled(provider_name):
        raise RuntimeError(f"Provider '{provider_name}' is disabled by configuration")

    api_key = os.environ.get(provider["api_key_env"], "").strip()
    model_env = "GEMINI_MODEL" if provider_name == "gemini" else "GROQ_MODEL"
    default_model = "gemini-3.7-flash" if provider_name == "gemini" else "openai/gpt-oss-20b"
    model = os.environ.get(model_env, default_model).strip()
    base_url = os.environ.get(provider["base_url_env"], provider["default_base_url"])

    if not api_key:
        raise RuntimeError(f"No API key is configured for provider '{provider_name}'")
    return provider_name, _clean_base_url(base_url), api_key, model


def get_provider_config(api_key: str | None = None, model: str | None = None):
    """Backward-compatible config resolver for older callers."""
    _provider, base_url, resolved_key, resolved_model = get_model_config(None)
    return base_url, api_key or resolved_key, model or resolved_model


def _post_payload(
    system_prompt: str,
    user_prompt: str,
    model: str,
    temperature: float,
    max_tokens: int,
    stream: bool,
    provider: str,
    *,
    messages: list[dict] | None = None,
    tools: list[dict] | None = None,
) -> dict:
    payload = {
        "model": model,
        "messages": messages or [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if provider == "groq":
        payload["max_completion_tokens"] = max_tokens
        payload["temperature"] = temperature
    else:
        payload["max_tokens"] = max_tokens
        payload["reasoning_effort"] = os.environ.get("GEMINI_REASONING_EFFORT", "low")
    if stream:
        payload["stream"] = True
        if os.environ.get("PRIVY_STREAM_USAGE_REPORTING", "true").strip().lower() in {"1", "true", "yes", "on"}:
            payload["stream_options"] = {"include_usage": True}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def _legacy_call_llm(
    system_prompt: str,
    user_prompt: str,
    api_key: str | None,
    model: str | None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> str:
    provider, base_url, resolved_key, resolved_model = get_model_config(model if model else None)
    if api_key:
        resolved_key = api_key

    request_started = time.perf_counter()
    log_event(logger, "llm_request_start", provider=provider, model=resolved_model)
    response = requests.post(
        base_url,
        headers={"Authorization": f"Bearer {resolved_key}", "Content-Type": "application/json"},
        json=_post_payload(
            system_prompt,
            user_prompt,
            resolved_model,
            temperature,
            max_tokens,
            False,
            provider,
        ),
        timeout=(LLM_CONNECT_TIMEOUT, LLM_READ_TIMEOUT),
    )
    log_event(logger, "llm_request_complete", provider=provider, model=resolved_model, duration_ms=elapsed_ms(request_started), status_code=response.status_code)
    response.raise_for_status()
    data = response.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("LLM returned an unexpected response format") from exc


def _gemini_native_headers(api_key: str) -> dict[str, str]:
    return {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
        "Api-Revision": os.environ.get("GEMINI_API_REVISION", "2026-05-20"),
    }


class LLMServiceError(RuntimeError):
    """Provider failure with a safe message intended for the UI."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        user_message: str,
        code: str | None = None,
        status_code: int | None = None,
        retryable: bool = False,
        retry_after_seconds: float | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.user_message = user_message
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class ProviderRequest:
    system_prompt: str
    user_prompt: str
    api_key: str
    model: str
    provider: str
    temperature: float
    max_tokens: int
    file_refs: list[dict]
    interaction_id: str | None = None
    interaction_state: dict | None = None
    tool_mode: str | None = None
    tool_schemas: list[dict] = field(default_factory=list)
    tool_executor: Callable[[str, str], dict] | None = None
    max_tool_rounds: int = 0
    max_tool_calls: int = 0


class ProviderAdapter(Protocol):
    """Provider-independent streaming and completion contract."""

    def stream(
        self,
        request: ProviderRequest,
        usage_state: dict | None = None,
    ) -> Iterator[str]: ...

    def complete(self, request: ProviderRequest) -> str: ...

    def stream_events(
        self,
        request: ProviderRequest,
        usage_state: dict | None = None,
    ) -> Iterator[dict]: ...


class GeminiInteractionError(LLMServiceError):
    pass


def _retry_after_seconds(headers: dict | None) -> float | None:
    values = headers or {}
    value = values.get("Retry-After") or values.get("retry-after")
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0.0, parsedate_to_datetime(str(value)).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def _provider_error_category(status_code: int, body: str = "") -> str:
    normalized = str(body or "").lower()
    if status_code in {401, 403}:
        return "authentication"
    if status_code == 429:
        return "quota"
    if status_code in {408, 504}:
        return "timeout"
    if any(term in normalized for term in ("context length", "context_length", "maximum context", "token limit")):
        return "context_window"
    if status_code in {400, 413, 422}:
        return "invalid_request"
    if status_code >= 500:
        return "provider_unavailable"
    return "provider_error"


def _safe_provider_user_message(
    provider: str,
    status_code: int | None,
    body: str = "",
) -> tuple[str, bool]:
    """Return (user-facing message, retryable) without exposing provider internals."""
    if status_code == 401 or status_code == 403:
        return (
            f"{provider.title()} is not authorized for this request. Please check the server configuration.",
            False,
        )
    if status_code == 408:
        return (
            f"{provider.title()} took too long to start the request. Please try again.",
            True,
        )
    if status_code == 429:
        return (
            f"{provider.title()} rate limit or quota was reached. Please wait and try again.",
            True,
        )
    if status_code in {413, 422} or _provider_error_category(status_code or 0, body) == "context_window":
        return (
            "This request exceeds the AI service's context limit. Shorten the request or choose another model.",
            False,
        )
    if status_code == 400:
        return (
            f"{provider.title()} could not accept this request. Please review the request and try again.",
            False,
        )
    if status_code is not None and status_code >= 500:
        return (
            f"{provider.title()} is temporarily unavailable. Please try again.",
            True,
        )
    return (
        f"{provider.title()} could not complete the request. Please try again.",
        True,
    )


def _gemini_interactions_url() -> str:
    base_url = os.environ.get(
        "GEMINI_API_ROOT",
        "https://generativelanguage.googleapis.com/v1beta",
    ).strip().rstrip("/")
    return f"{base_url}/interactions?alt=sse"


def _gemini_interaction_payload(
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_tokens: int,
    file_refs: list[dict],
    previous_interaction_id: str | None,
    tool_mode: str | None,
) -> tuple[dict, str]:
    calculation_file_refs = [
        ref
        for ref in file_refs
        if ref.get("include_document") and ref.get("file_uri")
    ]
    store_names = [
        str(ref.get("file_search_store_name") or "")
        for ref in file_refs
        if ref.get("file_search_store_name")
    ]

    # The intended Gemini tool mode comes from messages.py. Do not infer it
    # from include_document: on a stateful Code Execution follow-up, the raw
    # document is intentionally omitted because it is already part of the
    # previous interaction.
    effective_tool_mode = tool_mode
    if effective_tool_mode is None:
        # Backward-compatible fallback for callers that do not provide an
        # explicit mode.
        effective_tool_mode = (
            "code_execution"
            if calculation_file_refs
            else "file_search"
            if store_names
            else "plain"
        )

    if effective_tool_mode == "code_execution":
        # Exact calculations use the reusable Files API object as an actual
        # document input on the first Code Execution turn. On subsequent
        # stateful turns the document is already carried by Gemini, so the
        # request only needs the Code Execution tool and the new question.
        calculation_instruction = (
            "You are answering a deterministic question about the attached complete dataset. "
            "Use Code Execution with pandas to calculate the requested result over the entire dataset. "
            "Load the dataset once, perform only the requested calculation, and do not repeat or verify "
            "the calculation unnecessarily. Do not perform exploratory analysis or calculate unrelated "
            "statistics. Use all rows, not a sample. Once the result is computed, stop Code Execution "
            "and return the result with a brief explanation."
        )
        input_parts: list[dict] = [{
            "type": "text",
            "text": calculation_instruction + "\n\n" + user_prompt,
        }]
        for ref in calculation_file_refs:
            input_parts.append(
                {
                    "type": "document",
                    "uri": ref["file_uri"],
                    "mime_type": ref.get("mime_type") or "text/csv",
                }
            )
        tools = [{"type": "code_execution"}]
        mode = "code_execution"
    elif effective_tool_mode == "file_search":
        input_parts = [{"type": "text", "text": user_prompt}]
        tools = [
            {
                "type": "file_search",
                "file_search_store_names": store_names,
            }
        ]
        mode = "file_search"
    else:
        input_parts = [{"type": "text", "text": user_prompt}]
        tools = []
        mode = "plain"

    payload: dict = {
        "model": model,
        "input": input_parts,
        "system_instruction": system_prompt,
        "stream": True,
        "store": True,
        "tools": tools,
        "generation_config": {
            "max_output_tokens": max_tokens,
            "thinking_level": os.environ.get(
                "GEMINI_THINKING_LEVEL",
                os.environ.get("GEMINI_REASONING_EFFORT", "low"),
            ).strip().lower(),
        },
    }
    if previous_interaction_id:
        payload["previous_interaction_id"] = previous_interaction_id
    return payload, mode


def _format_gemini_api_error(
    status_code: int,
    body: str,
    headers: dict | None = None,
) -> GeminiInteractionError:
    """Convert a Gemini HTTP error into a safe, typed application error."""
    message = body[:1000]
    code: str | None = None
    try:
        data = json.loads(body)
        err = data.get("error") or {}
        code_value = err.get("status") or err.get("code")
        code = str(code_value) if code_value is not None else None
        message = str(err.get("message") or message)
    except (ValueError, TypeError):
        pass

    normalized = f"{code or ''} {message}".lower()
    if "deadline_exceeded" in normalized or "timed out" in normalized or status_code == 504:
        category = "timeout"
        user_message = "Gemini took too long to complete this request. Please try again."
        retryable = True
    elif any(term in normalized for term in ("context length", "context_length", "maximum context", "token limit")):
        category = "context_window"
        user_message = "This request exceeds Gemini's context limit. Shorten it or choose another model."
        retryable = False
    elif status_code == 429:
        category = "quota"
        user_message = "Gemini rate limit or quota was reached. Please wait and try again."
        retryable = True
    elif status_code in {401, 403}:
        category = "authentication"
        user_message = "Gemini is not authorized for this request. Please check the server configuration."
        retryable = False
    elif status_code == 404:
        category = "not_found"
        user_message = "The Gemini conversation state is no longer available. Please retry the request."
        retryable = True
    elif status_code >= 500:
        category = "provider_unavailable"
        user_message = "Gemini is temporarily unavailable. Please try again."
        retryable = True
    else:
        category = "provider_error"
        user_message = "Gemini could not complete this request. Please try again."
        retryable = True

    return GeminiInteractionError(
        message,
        category=category,
        user_message=user_message,
        code=code,
        status_code=status_code,
        retryable=retryable,
        retry_after_seconds=_retry_after_seconds(headers),
    )

def _stream_gemini_interaction(
    system_prompt: str,
    user_prompt: str,
    api_key: str,
    model: str,
    max_tokens: int,
    file_refs: list[dict],
    previous_interaction_id: str | None,
    interaction_state: dict | None,
    tool_mode: str | None,
    usage_state: dict | None = None,
) -> Iterator[str]:
    started = time.perf_counter()
    payload, mode = _gemini_interaction_payload(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
        max_tokens=max_tokens,
        file_refs=file_refs,
        previous_interaction_id=previous_interaction_id,
        tool_mode=tool_mode,
    )

    log_event(
        logger,
        "gemini_interaction_start",
        mode=mode,
        model=model,
        file_search_stores=sum(1 for ref in file_refs if ref.get("file_search_store_name")),
        document_refs=sum(1 for ref in file_refs if ref.get("include_document")),
        document_inputs=sum(1 for ref in file_refs if ref.get("include_document") and ref.get("file_uri")),
        has_previous_interaction=bool(previous_interaction_id),
    )

    http_started = time.perf_counter()
    try:
        response = requests.post(
            _gemini_interactions_url(),
            headers=_gemini_native_headers(api_key),
            json=payload,
            timeout=(LLM_CONNECT_TIMEOUT, LLM_READ_TIMEOUT),
            stream=True,
        )
    except requests.exceptions.Timeout as exc:
        log_event(
            logger,
            "gemini_interaction_http_error",
            level=logging.ERROR,
            duration_ms=elapsed_ms(http_started),
            error_type=type(exc).__name__,
            category="timeout",
        )
        raise LLMServiceError(
            str(exc),
            category="timeout",
            user_message="The AI service took too long to respond. Please try again.",
            retryable=True,
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        log_event(
            logger,
            "gemini_interaction_http_error",
            level=logging.ERROR,
            duration_ms=elapsed_ms(http_started),
            error_type=type(exc).__name__,
            category="network",
        )
        raise LLMServiceError(
            str(exc),
            category="network",
            user_message="We couldn't connect to Gemini. Please try again.",
            retryable=True,
        ) from exc
    except requests.RequestException as exc:
        log_event(
            logger,
            "gemini_interaction_http_error",
            level=logging.ERROR,
            duration_ms=elapsed_ms(http_started),
            error_type=type(exc).__name__,
            category="network",
        )
        raise LLMServiceError(
            str(exc),
            category="network",
            user_message="We couldn't reach the AI service. Please try again.",
            retryable=True,
        ) from exc

    log_event(
        logger,
        "gemini_interaction_http_response",
        duration_ms=elapsed_ms(http_started),
        status_code=response.status_code,
    )
    if response.status_code >= 400:
        log_event(
            logger,
            "gemini_interaction_api_error",
            level=logging.ERROR,
            status_code=response.status_code,
        )
        raise _format_gemini_api_error(response.status_code, response.text, response.headers)

    response.encoding = "utf-8"
    completed_id = None
    emitted_text = False

    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line or not raw_line.startswith("data:"):
            continue
        raw_payload = raw_line[len("data:"):].strip()
        if not raw_payload:
            continue
        try:
            event = json.loads(raw_payload)
        except json.JSONDecodeError:
            continue

        event_type = event.get("event_type")

        if event_type == "interaction.created":
            interaction = event.get("interaction") or {}
            interaction_id = interaction.get("id")
            if interaction_id and interaction_state is not None:
                interaction_state["latest_interaction_id"] = str(interaction_id)
            continue

        if event_type == "step.start":
            # The current Interactions API can include model_output content
            # directly on step.start. Most responses stream the same content
            # through step.delta, but accepting both forms prevents a valid
            # response from being silently reduced to zero characters.
            step = event.get("step") or {}
            step_type = step.get("type")
            if step_type == "model_output":
                content = step.get("content") or []
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text = block.get("text")
                            if text:
                                emitted_text = True
                            yield str(text)
            continue

        if event_type == "step.delta":
            delta = event.get("delta") or {}
            delta_type = delta.get("type")

            if delta_type == "file_search_call":
                if interaction_state is not None:
                    interaction_state.setdefault("tool_started_at", time.perf_counter())
                log_event(logger, "gemini_tool_start", tool="file_search")

            elif delta_type == "file_search_result":
                tool_started_at = (interaction_state or {}).get("tool_started_at")
                log_event(
                    logger,
                    "gemini_tool_complete",
                    tool="file_search",
                    result_chars=len(str(delta.get("result") or "")),
                    duration_ms=elapsed_ms(tool_started_at) if tool_started_at else None,
                )
                if interaction_state is not None:
                    interaction_state.pop("tool_started_at", None)

            elif delta_type == "text":
                text = delta.get("text")
                if text:
                    emitted_text = True
                    yield text

            elif delta_type == "code_execution_call":
                code = str((delta.get("arguments") or {}).get("code") or "")
                if interaction_state is not None:
                    interaction_state.setdefault("tool_started_at", time.perf_counter())
                log_event(logger, "gemini_tool_start", tool="code_execution", code_chars=len(code))

            elif delta_type == "code_execution_result":
                result = str(delta.get("result") or "")
                tool_started_at = (interaction_state or {}).get("tool_started_at")
                log_event(
                    logger,
                    "gemini_tool_complete",
                    tool="code_execution",
                    is_error=delta.get("is_error"),
                    result_chars=len(result),
                    duration_ms=elapsed_ms(tool_started_at) if tool_started_at else None,
                )
                if interaction_state is not None:
                    interaction_state.pop("tool_started_at", None)
            continue

        if event_type == "interaction.completed":
            interaction = event.get("interaction") or {}
            completed_id = (
                interaction.get("id")
                or (interaction_state or {}).get("latest_interaction_id")
            )
            interaction_status = str(interaction.get("status") or "").lower()
            if interaction_state is not None:
                interaction_state["completed_status"] = interaction_status
            if completed_id and interaction_state is not None:
                interaction_state["latest_interaction_id"] = str(completed_id)

            usage = interaction.get("usage") or {}
            if usage_state is not None and isinstance(usage, dict):
                usage_state.update(_normalize_usage("gemini", model, usage))
            logger.info(
                "gemini_usage interaction_id=%s previous_interaction_id=%s "
                "status=%s input_tokens=%s cached_tokens=%s output_tokens=%s "
                "thought_tokens=%s tool_use_tokens=%s total_tokens=%s",
                completed_id,
                previous_interaction_id,
                interaction_status,
                usage.get("total_input_tokens"),
                usage.get("total_cached_tokens"),
                usage.get("total_output_tokens"),
                usage.get("total_thought_tokens"),
                usage.get("total_tool_use_tokens"),
                usage.get("total_tokens"),
            )
            if interaction_status in {"failed", "cancelled", "incomplete", "budget_exceeded"}:
                raise GeminiInteractionError(
                    f"Gemini interaction ended with status {interaction_status}",
                    category="provider_error",
                    user_message="Gemini could not complete this request. Please try again.",
                    code=interaction_status,
                    retryable=interaction_status != "cancelled",
                )
            if interaction_status == "completed" and not emitted_text:
                raise GeminiInteractionError(
                    "Gemini completed the interaction without model output",
                    category="provider_error",
                    user_message="Gemini returned an empty response. Please try again.",
                    code="empty_response",
                    retryable=True,
                )
            continue

        if event_type == "error":
            error = event.get("error") or {}
            message = str(error.get("message") or "Gemini interaction failed")
            code = str(error.get("code") or error.get("status") or "") or None
            normalized = f"{code or ''} {message}".lower()
            if "too_many_requests" in normalized or "rate limit" in normalized or "quota" in normalized:
                category = "quota"
                user_message = "Gemini rate limit or quota was reached. Please wait and try again."
                retryable = True
            elif "deadline" in normalized or "timeout" in normalized:
                category = "timeout"
                user_message = "Gemini took too long to complete this request. Please try again."
                retryable = True
            elif any(term in normalized for term in ("context length", "context_length", "maximum context", "token limit")):
                category = "context_window"
                user_message = "This request exceeds Gemini's context limit. Shorten it or choose another model."
                retryable = False
            else:
                category = "provider_error"
                user_message = "Gemini could not complete this request. Please try again."
                retryable = True
            log_event(
                logger,
                "gemini_interaction_error",
                level=logging.ERROR,
                code=code,
                duration_ms=elapsed_ms(started),
                error_type="provider_event",
            )
            raise GeminiInteractionError(
                message,
                category=category,
                user_message=user_message,
                code=code,
                retryable=retryable,
            )


def _stream_gemini_with_state(
    system_prompt: str,
    user_prompt: str,
    api_key: str,
    model: str,
    max_tokens: int,
    file_refs: list[dict],
    interaction_id: str,
    interaction_state: dict,
    tool_mode: str | None,
    usage_state: dict | None = None,
) -> Iterator[str]:
    try:
        yield from _stream_gemini_interaction(
            system_prompt,
            user_prompt,
            api_key,
            model,
            max_tokens,
            file_refs,
            interaction_id,
            interaction_state,
            tool_mode,
            usage_state,
        )
    except GeminiInteractionError as exc:
        if exc.status_code == 404 or exc.code in {"not_found", "NOT_FOUND"}:
            interaction_state["reset_required"] = True
        raise


def _stream_provider_implementation(
    system_prompt: str,
    user_prompt: str,
    api_key: str | None,
    model: str | None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    provider: str | None = None,
    file_refs: list[dict] | None = None,
    interaction_id: str | None = None,
    interaction_state: dict | None = None,
    tool_mode: str | None = None,
    usage_state: dict | None = None,
) -> Iterator[str]:
    resolved_provider, base_url, resolved_key, resolved_model = get_model_config(
        model if model else None
    )
    if api_key:
        resolved_key = api_key
    provider = provider or resolved_provider
    file_refs = file_refs or []

    if provider == "gemini":
        state = interaction_state if interaction_state is not None else {}
        state.setdefault("latest_interaction_id", interaction_id)

        if interaction_id:
            try:
                yield from _stream_gemini_with_state(
                    system_prompt,
                    user_prompt,
                    resolved_key,
                    resolved_model,
                    max_tokens,
                    file_refs,
                    interaction_id,
                    state,
                    tool_mode,
                    usage_state,
                )
                return
            except GeminiInteractionError:
                if not state.get("reset_required"):
                    raise
                state.pop("reset_required", None)
                state["latest_interaction_id"] = None

        yield from _stream_gemini_interaction(
            system_prompt,
            user_prompt,
            resolved_key,
            resolved_model,
            max_tokens,
            file_refs,
            None,
            state,
            tool_mode,
            usage_state,
        )
        return

    try:
        response = requests.post(
            base_url,
            headers={"Authorization": f"Bearer {resolved_key}", "Content-Type": "application/json"},
            json=_post_payload(
                system_prompt,
                user_prompt,
                resolved_model,
                temperature,
                max_tokens,
                True,
                provider,
            ),
            timeout=(LLM_CONNECT_TIMEOUT, LLM_READ_TIMEOUT),
            stream=True,
        )
    except requests.exceptions.Timeout as exc:
        raise LLMServiceError(
            str(exc),
            category="timeout",
            user_message="The AI service took too long to respond. Please try again.",
            retryable=True,
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise LLMServiceError(
            str(exc),
            category="network",
            user_message="We couldn't connect to the AI service. Please try again.",
            retryable=True,
        ) from exc
    except requests.RequestException as exc:
        raise LLMServiceError(
            str(exc),
            category="network",
            user_message="We couldn't reach the AI service. Please try again.",
            retryable=True,
        ) from exc

    if response.status_code >= 400:
        body = response.text
        user_message, retryable = _safe_provider_user_message(provider, response.status_code, body)
        raise LLMServiceError(
            f"{provider} HTTP {response.status_code}",
            category=_provider_error_category(response.status_code, body),
            user_message=user_message,
            status_code=response.status_code,
            retryable=retryable,
            retry_after_seconds=_retry_after_seconds(response.headers),
        )
    response.encoding = "utf-8"
    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line or not raw_line.startswith("data:"):
            continue
        raw_payload = raw_line[len("data:"):].strip()
        if raw_payload == "[DONE]":
            break
        try:
            chunk = json.loads(raw_payload)
        except json.JSONDecodeError:
            continue
        if usage_state is not None and isinstance(chunk.get("usage"), dict):
            usage_state.update(_normalize_usage(provider, resolved_model, chunk["usage"]))
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content:
            yield content


def _normalize_usage(provider: str, model: str, usage: dict) -> dict:
    if provider == "gemini":
        input_tokens = usage.get("total_input_tokens")
        output_tokens = usage.get("total_output_tokens")
        cached_tokens = usage.get("total_cached_tokens")
        reasoning_tokens = usage.get("total_thought_tokens")
        tool_tokens = usage.get("total_tool_use_tokens")
    else:
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        cached_tokens = prompt_details.get("cached_tokens")
        reasoning_tokens = completion_details.get("reasoning_tokens")
        tool_tokens = usage.get("tool_tokens")
    return {
        "provider": provider,
        "model": model,
        "source": "provider_reported",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
        "tool_tokens": tool_tokens,
        "total_tokens": usage.get("total_tokens"),
        "estimated_input_tokens": None,
        "estimated_output_tokens": None,
        "estimated_cost": None,
    }


def _merge_usage_totals(target: dict, current: dict) -> None:
    for field in (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
        "tool_tokens",
        "total_tokens",
    ):
        value = current.get(field)
        if isinstance(value, int):
            target[field] = int(target.get(field) or 0) + value
    target.update({
        "provider": current.get("provider"),
        "model": current.get("model"),
        "source": "provider_reported",
        "estimated_input_tokens": None,
        "estimated_output_tokens": None,
        "estimated_cost": None,
    })


def _stream_openai_compatible_tools(
    request: ProviderRequest,
    usage_state: dict | None,
) -> Iterator[dict]:
    if not request.tool_executor or not request.tool_schemas:
        raise LLMServiceError(
            "Tool calling was requested without a registered executor.",
            category="tool_configuration",
            user_message="Workbook tools are temporarily unavailable.",
            retryable=False,
        )
    provider, base_url, _resolved_key, _resolved_model = get_model_config(request.model)
    messages = [
        {"role": "system", "content": request.system_prompt},
        {"role": "user", "content": request.user_prompt},
    ]
    calls_used = 0
    usage_totals: dict = {}
    capabilities = model_capabilities(request.model, provider)
    configured_context = capabilities.get("context_window_tokens")
    context_limit = (
        configured_context
        if isinstance(configured_context, int)
        else MAX_TOTAL_FILE_CONTEXT_TOKENS + request.max_tokens + DEFAULT_PROVIDER_OVERHEAD_TOKENS
    )

    for round_index in range(request.max_tool_rounds + 1):
        messages_tokens = estimate_tokens(json.dumps(messages, ensure_ascii=True, separators=(",", ":")))
        tool_tokens = estimate_tokens(json.dumps(request.tool_schemas, ensure_ascii=True, separators=(",", ":")))
        if messages_tokens + tool_tokens + request.max_tokens + DEFAULT_PROVIDER_OVERHEAD_TOKENS > context_limit:
            raise LLMServiceError(
                "The workbook tool interaction exceeded the configured prompt budget.",
                category="context_window",
                user_message="The workbook results exceeded the selected model's context limit. Narrow the question and try again.",
                retryable=False,
            )
        try:
            response = requests.post(
                base_url,
                headers={
                    "Authorization": f"Bearer {request.api_key}",
                    "Content-Type": "application/json",
                },
                json=_post_payload(
                    request.system_prompt,
                    request.user_prompt,
                    request.model,
                    request.temperature,
                    request.max_tokens,
                    True,
                    provider,
                    messages=messages,
                    tools=request.tool_schemas,
                ),
                timeout=(LLM_CONNECT_TIMEOUT, LLM_READ_TIMEOUT),
                stream=True,
            )
        except requests.exceptions.Timeout as exc:
            raise LLMServiceError(
                str(exc),
                category="timeout",
                user_message="The AI service took too long to respond. Please try again.",
                retryable=True,
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise LLMServiceError(
                str(exc),
                category="network",
                user_message="We couldn't connect to the AI service. Please try again.",
                retryable=True,
            ) from exc
        except requests.RequestException as exc:
            raise LLMServiceError(
                str(exc),
                category="network",
                user_message="We couldn't reach the AI service. Please try again.",
                retryable=True,
            ) from exc

        calls_by_index: dict[int, dict] = {}
        round_usage: dict = {}
        finish_reason: str | None = None
        if response.status_code >= 400:
            try:
                body = response.text
                user_message, retryable = _safe_provider_user_message(provider, response.status_code, body)
                raise LLMServiceError(
                    f"{provider} HTTP {response.status_code}",
                    category=_provider_error_category(response.status_code, body),
                    user_message=user_message,
                    status_code=response.status_code,
                    retryable=retryable,
                    retry_after_seconds=_retry_after_seconds(response.headers),
                )
            finally:
                response.close()
        response.encoding = "utf-8"
        try:
            for raw_line in response.iter_lines(decode_unicode=True):
                if not raw_line or not raw_line.startswith("data:"):
                    continue
                raw_payload = raw_line[len("data:"):].strip()
                if raw_payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(raw_payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(chunk.get("usage"), dict):
                    round_usage.update(_normalize_usage(provider, request.model, chunk["usage"]))
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield {"type": "delta", "text": content}
                for tool_call in delta.get("tool_calls") or []:
                    if not isinstance(tool_call, dict):
                        continue
                    index = tool_call.get("index")
                    if not isinstance(index, int) or index < 0:
                        continue
                    current = calls_by_index.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tool_call.get("id"):
                        current["id"] = str(tool_call["id"])
                    function = tool_call.get("function") or {}
                    if function.get("name"):
                        current["function"]["name"] += str(function["name"])
                    if function.get("arguments"):
                        current["function"]["arguments"] += str(function["arguments"])
        finally:
            response.close()

        if round_usage:
            _merge_usage_totals(usage_totals, round_usage)
        tool_calls = [calls_by_index[key] for key in sorted(calls_by_index)]
        if not tool_calls:
            if finish_reason == "tool_calls":
                raise LLMServiceError(
                    "Provider reported tool calls without valid call data.",
                    category="invalid_tool_call",
                    user_message="The AI service returned an invalid workbook operation. Please try again.",
                    retryable=False,
                )
            if usage_state is not None and usage_totals:
                usage_state.update(usage_totals)
            return
        if round_index >= request.max_tool_rounds:
            raise LLMServiceError(
                "The provider exceeded the configured tool round limit.",
                category="tool_round_limit",
                user_message="This request required too many workbook operations. Please narrow the question and try again.",
                retryable=False,
            )
        if calls_used + len(tool_calls) > request.max_tool_calls:
            raise LLMServiceError(
                "The provider exceeded the configured tool call limit.",
                category="tool_call_limit",
                user_message="This request reached its workbook operation limit. Please narrow the question and try again.",
                retryable=False,
            )
        calls_used += len(tool_calls)
        assistant_message = {
            "role": "assistant",
            "content": None,
            "tool_calls": tool_calls,
        }
        messages.append(assistant_message)
        for tool_call in tool_calls:
            call_id = str(tool_call.get("id") or "")
            function = tool_call.get("function") or {}
            name = str(function.get("name") or "")
            arguments = str(function.get("arguments") or "")
            if not call_id or not name:
                raise LLMServiceError(
                    "The provider returned an incomplete tool call.",
                    category="invalid_tool_call",
                    user_message="The AI service returned an invalid workbook operation. Please try again.",
                    retryable=False,
                )
            yield {"type": "tool_start", "name": name}
            try:
                result = request.tool_executor(name, arguments)
            except Exception:
                result = {
                    "ok": False,
                    "error": {
                        "code": "tool_execution_failed",
                        "message": "The workbook operation could not be completed safely.",
                    },
                }
            encoded_result = json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": encoded_result,
            })
            yield {"type": "tool_complete", "name": name}
    if usage_state is not None and usage_totals:
        usage_state.update(usage_totals)


class _ProviderAdapter:
    def __init__(self, provider: str):
        self.provider = provider

    def stream(self, request: ProviderRequest, usage_state: dict | None = None) -> Iterator[str]:
        yield from _stream_provider_implementation(
            request.system_prompt,
            request.user_prompt,
            request.api_key,
            request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            provider=self.provider,
            file_refs=request.file_refs,
            interaction_id=request.interaction_id,
            interaction_state=request.interaction_state,
            tool_mode=request.tool_mode,
            usage_state=usage_state,
        )

    def complete(self, request: ProviderRequest) -> str:
        return _complete_provider_implementation(request)

    def stream_events(
        self,
        request: ProviderRequest,
        usage_state: dict | None = None,
    ) -> Iterator[dict]:
        if request.tool_schemas:
            compatible_request = replace(
                request,
                tool_schemas=self._translate_tool_schemas(request.tool_schemas),
            )
            yield from _stream_openai_compatible_tools(compatible_request, usage_state)
            return
        for text in self.stream(request, usage_state):
            yield {"type": "delta", "text": text}

    def _translate_tool_schemas(self, schemas: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": item["name"],
                    "description": item["description"],
                    "parameters": item["parameters"],
                },
            }
            for item in schemas
        ]


class _GroqAdapter(_ProviderAdapter):
    pass


class _GeminiAdapter(_ProviderAdapter):
    pass


def _complete_provider_implementation(request: ProviderRequest) -> str:
    provider, base_url, _, _ = get_model_config(request.model)
    started = time.perf_counter()
    log_event(logger, "llm_request_start", provider=provider, model=request.model)
    try:
        response = requests.post(
            base_url,
            headers={"Authorization": f"Bearer {request.api_key}", "Content-Type": "application/json"},
            json=_post_payload(
                request.system_prompt,
                request.user_prompt,
                request.model,
                request.temperature,
                request.max_tokens,
                False,
                provider,
            ),
            timeout=(LLM_CONNECT_TIMEOUT, LLM_READ_TIMEOUT),
        )
    except requests.exceptions.Timeout as exc:
        raise LLMServiceError(
            str(exc),
            category="timeout",
            user_message="The AI service took too long to respond. Please try again.",
            retryable=True,
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise LLMServiceError(
            str(exc),
            category="network",
            user_message="We couldn't connect to the AI service. Please try again.",
            retryable=True,
        ) from exc
    except requests.RequestException as exc:
        raise LLMServiceError(
            str(exc),
            category="network",
            user_message="We couldn't reach the AI service. Please try again.",
            retryable=True,
        ) from exc
    log_event(
        logger,
        "llm_request_complete",
        provider=provider,
        model=request.model,
        duration_ms=elapsed_ms(started),
        status_code=response.status_code,
    )
    if response.status_code >= 400:
        user_message, retryable = _safe_provider_user_message(provider, response.status_code, response.text)
        raise LLMServiceError(
            f"{provider} HTTP {response.status_code}",
            category=_provider_error_category(response.status_code, response.text),
            user_message=user_message,
            status_code=response.status_code,
            retryable=retryable,
            retry_after_seconds=_retry_after_seconds(response.headers),
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise LLMServiceError(
            "The provider returned invalid JSON.",
            category="invalid_response",
            user_message="The AI service returned an unexpected response. Please try again.",
            retryable=True,
        ) from exc
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LLMServiceError(
            "The provider returned an unexpected response format.",
            category="invalid_response",
            user_message="The AI service returned an unexpected response. Please try again.",
            retryable=True,
        ) from exc


_PROVIDER_ADAPTERS: dict[str, ProviderAdapter] = {
    "groq": _GroqAdapter("groq"),
    "gemini": _GeminiAdapter("gemini"),
}


def get_provider_adapter(provider: str) -> ProviderAdapter:
    adapter = _PROVIDER_ADAPTERS.get(provider)
    if adapter is None:
        raise LLMServiceError(
            "No adapter is registered for the selected provider.",
            category="unsupported_provider",
            user_message="The selected AI provider is not supported.",
            retryable=False,
        )
    return adapter


def stream_llm(
    system_prompt: str,
    user_prompt: str,
    api_key: str | None,
    model: str | None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    provider: str | None = None,
    file_refs: list[dict] | None = None,
    interaction_id: str | None = None,
    interaction_state: dict | None = None,
    tool_mode: str | None = None,
    usage_state: dict | None = None,
) -> Iterator[str]:
    resolved_provider, _base_url, resolved_key, resolved_model = get_model_config(
        model if model else None
    )
    selected_provider = provider or resolved_provider
    request = ProviderRequest(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        api_key=api_key or resolved_key,
        model=resolved_model,
        provider=selected_provider,
        temperature=temperature,
        max_tokens=max_tokens,
        file_refs=file_refs or [],
        interaction_id=interaction_id,
        interaction_state=interaction_state,
        tool_mode=tool_mode,
    )
    yield from get_provider_adapter(selected_provider).stream(request, usage_state)


def stream_llm_events(
    system_prompt: str,
    user_prompt: str,
    api_key: str | None,
    model: str | None,
    *,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    provider: str | None = None,
    tool_schemas: list[dict] | None = None,
    tool_executor: Callable[[str, str], dict] | None = None,
    max_tool_rounds: int = 0,
    max_tool_calls: int = 0,
    usage_state: dict | None = None,
) -> Iterator[dict]:
    resolved_provider, _base_url, resolved_key, resolved_model = get_model_config(
        model if model else None
    )
    selected_provider = provider or resolved_provider
    request = ProviderRequest(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        api_key=api_key or resolved_key,
        model=resolved_model,
        provider=selected_provider,
        temperature=temperature,
        max_tokens=max_tokens,
        file_refs=[],
        tool_schemas=tool_schemas or [],
        tool_executor=tool_executor,
        max_tool_rounds=max_tool_rounds,
        max_tool_calls=max_tool_calls,
    )
    yield from get_provider_adapter(selected_provider).stream_events(request, usage_state)


def call_llm(
    system_prompt: str,
    user_prompt: str,
    api_key: str | None,
    model: str | None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
) -> str:
    provider, _base_url, resolved_key, resolved_model = get_model_config(model or None)
    request = ProviderRequest(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        api_key=api_key or resolved_key,
        model=resolved_model,
        provider=provider,
        temperature=temperature,
        max_tokens=max_tokens,
        file_refs=[],
    )
    return get_provider_adapter(provider).complete(request)

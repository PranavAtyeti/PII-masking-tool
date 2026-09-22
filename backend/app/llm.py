"""Provider-aware LLM client for Privy.

Groq continues to use the OpenAI-compatible chat-completions endpoint.
Gemini uses the native Interactions API.

Gemini file handling is deliberately split by workload:

* retrieval questions use a persistent File Search store only;
* exact calculations/data analysis use Code Execution plus the already-uploaded
  Gemini Files API URI for the masked CSV.

The masked CSV bytes are uploaded once and reused. The raw Files API object is
only recreated after its provider-side expiry or when the masked content changes.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Iterator

import requests

from .logging_utils import elapsed_ms, log_event

logger = logging.getLogger(__name__)

DEFAULT_TEMPERATURE = float(os.environ.get("MODEL_TEMPERATURE", "0.1"))
DEFAULT_MAX_OUTPUT_TOKENS = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "16384"))
LLM_CONNECT_TIMEOUT = float(os.environ.get("LLM_CONNECT_TIMEOUT", "10"))
LLM_READ_TIMEOUT = float(os.environ.get("LLM_READ_TIMEOUT", "180"))

PROVIDERS = {
    "groq": {
        "api_key_env": "GROQ_API_KEY",
        "base_url_env": "GROQ_BASE_URL",
        "default_base_url": "https://api.groq.com/openai/v1",
    },
    "gemini": {
        "api_key_env": "GEMINI_API_KEY",
        "base_url_env": "GEMINI_BASE_URL",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
}

MODEL_CATALOG = [
    {
        "id": "gemini-3.7-flash",
        "provider": "gemini",
        "model": "gemini-3.7-flash",
        "label": "Gemini 3.7 Flash",
        "description": "Latest Gemini Flash workhorse model",
    },
    {
        "id": "gemini-3.6-flash",
        "provider": "gemini",
        "model": "gemini-3.6-flash",
        "label": "Gemini 3.6 Flash",
        "description": "Fast general-purpose Gemini model",
    },
    {
        "id": "gemini-3.5-flash-lite",
        "provider": "gemini",
        "model": "gemini-3.5-flash-lite",
        "label": "Gemini 3.5 Flash-Lite",
        "description": "Fast, cost-sensitive Gemini model",
    },
    {
        "id": "groq-gpt-oss-20b",
        "provider": "groq",
        "model": "openai/gpt-oss-20b",
        "label": "GPT-OSS 20B",
        "description": "Fast open-weight model via Groq",
    },
    {
        "id": "groq-gpt-oss-120b",
        "provider": "groq",
        "model": "openai/gpt-oss-120b",
        "label": "GPT-OSS 120B",
        "description": "Larger open-weight model via Groq",
    },
]

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
    """Return only models whose provider has a configured API key."""
    options: list[dict] = []
    for item in MODEL_CATALOG:
        provider = PROVIDERS[item["provider"]]
        if os.environ.get(provider["api_key_env"], "").strip():
            options.append(item.copy())
    return options


def get_model_config(model_id: str | None = None) -> tuple[str, str, str, str]:
    """Resolve (provider, base_url, api_key, provider_model)."""
    if model_id:
        item = next(
            (m for m in MODEL_CATALOG if m["id"] == model_id or m["model"] == model_id),
            None,
        )
        if item is None:
            raise RuntimeError("Unsupported model selection")
        provider_name = item["provider"]
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
) -> dict:
    payload = {
        "model": model,
        "messages": [
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
    return payload


def call_llm(
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
    ):
        super().__init__(message)
        self.category = category
        self.user_message = user_message
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class GeminiInteractionError(LLMServiceError):
    pass


def _safe_provider_user_message(provider: str, status_code: int | None) -> tuple[str, bool]:
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

    if calculation_file_refs:
        # Exact calculations use the reusable Files API object as an actual
        # document input to the Code Execution-capable interaction. The file
        # bytes are not uploaded here; only the existing provider URI is sent.
        input_parts: list[dict] = [{"type": "text", "text": user_prompt}]
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
    elif store_names:
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


def _format_gemini_api_error(status_code: int, body: str) -> GeminiInteractionError:
    """Convert a Gemini HTTP error into a typed error without retaining provider text."""
    code: str | None = None
    provider_text = ""
    try:
        data = json.loads(body)
        err = data.get("error") or {}
        code_value = err.get("status") or err.get("code")
        code = str(code_value) if code_value is not None else None
        provider_text = str(err.get("message") or "")
    except (ValueError, TypeError):
        provider_text = ""

    normalized = f"{code or ''} {provider_text}".lower()
    if "deadline_exceeded" in normalized or "timed out" in normalized or status_code == 504:
        category = "timeout"
        user_message = "Gemini took too long to complete this request. Please try again."
        retryable = True
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
        f"Gemini HTTP {status_code}",
        category=category,
        user_message=user_message,
        code=code,
        status_code=status_code,
        retryable=retryable,
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
) -> Iterator[str]:
    started = time.perf_counter()
    payload, mode = _gemini_interaction_payload(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
        max_tokens=max_tokens,
        file_refs=file_refs,
        previous_interaction_id=previous_interaction_id,
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
            "Gemini request timed out",
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
            "Gemini connection failed",
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
            "Gemini request failed",
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
        raise _format_gemini_api_error(response.status_code, response.text)

    response.encoding = "utf-8"
    completed_id = None

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
            if completed_id and interaction_state is not None:
                interaction_state["latest_interaction_id"] = str(completed_id)

            usage = interaction.get("usage") or {}
            logger.info(
                "gemini_usage interaction_id=%s previous_interaction_id=%s "
                "input_tokens=%s cached_tokens=%s output_tokens=%s "
                "thought_tokens=%s tool_use_tokens=%s total_tokens=%s",
                completed_id,
                previous_interaction_id,
                usage.get("total_input_tokens"),
                usage.get("total_cached_tokens"),
                usage.get("total_output_tokens"),
                usage.get("total_thought_tokens"),
                usage.get("total_tool_use_tokens"),
                usage.get("total_tokens"),
            )
            continue

        if event_type == "error":
            error = event.get("error") or {}
            code = str(error.get("code") or error.get("status") or "") or None
            log_event(
                logger,
                "gemini_interaction_error",
                level=logging.ERROR,
                code=code,
                duration_ms=elapsed_ms(started),
                error_type="provider_event",
            )
            raise GeminiInteractionError(
                "Gemini provider event failed",
                category="provider_error",
                user_message="Gemini could not complete this request. Please try again.",
                code=code,
                retryable=True,
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
        )
    except GeminiInteractionError as exc:
        if exc.status_code == 404 or exc.code in {"not_found", "NOT_FOUND"}:
            interaction_state["reset_required"] = True
        raise


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
            "LLM request timed out",
            category="timeout",
            user_message="The AI service took too long to respond. Please try again.",
            retryable=True,
        ) from exc
    except requests.exceptions.ConnectionError as exc:
        raise LLMServiceError(
            "LLM connection failed",
            category="network",
            user_message="We couldn't connect to the AI service. Please try again.",
            retryable=True,
        ) from exc
    except requests.RequestException as exc:
        raise LLMServiceError(
            "LLM request failed",
            category="network",
            user_message="We couldn't reach the AI service. Please try again.",
            retryable=True,
        ) from exc

    if response.status_code >= 400:
        user_message, retryable = _safe_provider_user_message(provider, response.status_code)
        raise LLMServiceError(
            f"{provider} HTTP {response.status_code}",
            category="provider_error" if response.status_code < 500 else "provider_unavailable",
            user_message=user_message,
            status_code=response.status_code,
            retryable=retryable,
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
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content:
            yield content

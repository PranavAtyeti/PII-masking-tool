"""Provider-aware LLM client for Privy.

Groq continues to use the OpenAI-compatible chat-completions endpoint.
Gemini uses that endpoint for ordinary text chats, but switches to Gemini's
native GenerateContent streaming endpoint when reusable Files API references
are supplied. That lets Privy upload a masked dataset once and send only a
small file URI + conversation/question on subsequent requests.
"""

import json
import logging
import os
from typing import Iterator


import requests

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

# Backward-compatible defaults used by the existing admin settings route.
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
        configured = bool(os.environ.get(provider["api_key_env"], "").strip())
        if configured:
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
    provider, base_url, resolved_key, resolved_model = get_model_config(None)
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
    if provider == "gemini":
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


class GeminiInteractionError(RuntimeError):
    def __init__(self, message: str, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _gemini_interactions_url() -> str:
    base_url = os.environ.get(
        "GEMINI_API_ROOT",
        "https://generativelanguage.googleapis.com/v1beta",
    ).strip().rstrip("/")
    return f"{base_url}/interactions?alt=sse"


def _gemini_interaction_payload(
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_tokens: int,
    file_refs: list[dict],
    previous_interaction_id: str | None,
    include_files: bool,
) -> dict:
    input_parts: list[dict] = [{"type": "text", "text": user_prompt}]
    if include_files:
        for file_ref in file_refs:
            input_parts.append(
                {
                    "type": "document",
                    "uri": file_ref["file_uri"],
                    "mime_type": file_ref["mime_type"],
                }
            )

    payload: dict = {
        "model": model,
        "input": input_parts,
        "system_instruction": system_prompt,
        "stream": True,
        "store": True,
        "tools": [{"type": "code_execution"}],
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
    return payload


def _format_gemini_api_error(status_code: int, body: str) -> GeminiInteractionError:
    message = body[:1000]
    code = None
    retry_after = None
    try:
        data = json.loads(body)
        err = data.get("error") or {}
        code = err.get("status") or err.get("code")
        message = err.get("message") or message
        if status_code == 429:
            import re
            match = re.search(r"retry after (\d+(?:\.\d+)?)s", str(message), re.I)
            if match:
                retry_after = float(match.group(1))
    except (ValueError, TypeError):
        pass

    if status_code == 429:
        suffix = f" Retry after about {retry_after:.0f} seconds." if retry_after is not None else " Please wait and retry."
        return GeminiInteractionError(
            f"Gemini rate limit/quota reached.{suffix} OpenAI/Groq or another configured model can be used while the Gemini limit resets.",
            code=str(code) if code else None,
            status_code=status_code,
        )

    return GeminiInteractionError(
        f"Gemini interaction request failed ({status_code}): {message}",
        code=str(code) if code else None,
        status_code=status_code,
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
    include_files = previous_interaction_id is None
    logger.info(
        "gemini_interaction_request include_files=%s has_previous_interaction=%s",
        include_files,
        bool(previous_interaction_id),
    )
    payload = _gemini_interaction_payload(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
        max_tokens=max_tokens,
        file_refs=file_refs,
        previous_interaction_id=previous_interaction_id,
        include_files=include_files,
    )

    response = requests.post(
        _gemini_interactions_url(),
        headers=_gemini_native_headers(api_key),
        json=payload,
        timeout=(LLM_CONNECT_TIMEOUT, LLM_READ_TIMEOUT),
        stream=True,
    )
    if response.status_code >= 400:
        raise _format_gemini_api_error(response.status_code, response.text)

    response.encoding = "utf-8"
    completed_id = None
    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line:
            continue
        if not raw_line.startswith("data:"):
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
            if delta.get("type") == "text":
                text = delta.get("text")
                if text:
                    yield text
            continue

        if event_type == "interaction.completed":
            interaction = event.get("interaction") or {}
            completed_id = interaction.get("id") or (interaction_state or {}).get("latest_interaction_id")
            if completed_id and interaction_state is not None:
                interaction_state["latest_interaction_id"] = str(completed_id)
            continue

        if event_type == "error":
            error = event.get("error") or {}
            message = str(error.get("message") or "Gemini interaction failed")
            code = str(error.get("code") or error.get("status") or "") or None
            raise GeminiInteractionError(message, code=code)


def _stream_gemini_with_state(
    system_prompt: str,
    user_prompt: str,
    api_key: str,
    model: str,
    max_tokens: int,
    file_refs: list[dict],
    interaction_id: str | None,
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
            # The free-tier interaction retention window is finite. Let the
            # caller start a fresh chain on the same still-valid Gemini File.
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
        previous_id = interaction_id
        if previous_id:
            try:
                yield from _stream_gemini_with_state(
                    system_prompt,
                    user_prompt,
                    resolved_key,
                    resolved_model,
                    max_tokens,
                    file_refs,
                    previous_id,
                    state,
                )
            except GeminiInteractionError as exc:
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
        else:
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
    response.raise_for_status()
    response.encoding = "utf-8"
    for raw_line in response.iter_lines(decode_unicode=True):
        if not raw_line or not raw_line.startswith("data:"):
            continue
        payload = raw_line[len("data:"):].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content:
            yield content


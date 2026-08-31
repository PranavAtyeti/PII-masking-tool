"""Provider-aware OpenAI-compatible LLM client for Privy."""

import json
import os
from typing import Iterator

import requests

DEFAULT_TEMPERATURE = float(os.environ.get("MODEL_TEMPERATURE", "0.1"))

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
    """Resolve (provider, base_url, api_key, provider_model).

    When model_id is omitted, the configured LLM_PROVIDER and provider default
    model are used. Explicit model IDs are validated against the server catalog.
    """
    if model_id:
        item = next((m for m in MODEL_CATALOG if m["id"] == model_id or m["model"] == model_id), None)
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


def _post_payload(system_prompt: str, user_prompt: str, model: str, temperature: float, max_tokens: int, stream: bool, provider: str) -> dict:
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    # Gemini 3.x models have deprecated legacy sampling parameters.
    if provider == "groq":
        payload["temperature"] = temperature
    if provider == "gemini":
        payload["reasoning_effort"] = "low"
    if stream:
        payload["stream"] = True
    return payload


def call_llm(system_prompt: str, user_prompt: str, api_key: str | None, model: str | None, temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = 400) -> str:
    provider, base_url, resolved_key, resolved_model = get_model_config(model if model else None)
    # Preserve legacy explicit keys when a caller supplies one.
    if api_key:
        resolved_key = api_key

    response = requests.post(
        base_url,
        headers={"Authorization": f"Bearer {resolved_key}", "Content-Type": "application/json"},
        json=_post_payload(system_prompt, user_prompt, resolved_model, temperature, max_tokens, False, provider),
        timeout=(10, 30),
    )
    response.raise_for_status()
    data = response.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("LLM returned an unexpected response format") from exc


def _extract_stream_text(chunk: dict) -> str:
    """Extract textual content from common OpenAI-compatible stream shapes."""
    choices = chunk.get("choices") or []
    if not choices:
        return ""

    choice = choices[0] or {}
    delta = choice.get("delta") or {}
    content = delta.get("content")

    # Standard OpenAI-compatible shape: delta.content = string.
    if isinstance(content, str):
        return content

    # Some compatible APIs return structured content parts.
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    # Tolerate providers that place text directly under delta.
    text = delta.get("text")
    if isinstance(text, str):
        return text

    return ""


def stream_llm(
    system_prompt: str,
    user_prompt: str,
    api_key: str | None,
    model: str | None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = 400,
) -> Iterator[str]:
    """Streaming OpenAI-compatible call yielding correctly decoded UTF-8 text.

    We explicitly decode SSE lines as UTF-8 instead of allowing ``requests`` to
    guess the response encoding. This prevents mojibake such as ``â``/``�`` in
    assistant responses containing emoji or other non-ASCII characters.
    """
    provider, base_url, resolved_key, resolved_model = get_model_config(model if model else None)
    if api_key:
        resolved_key = api_key

    response = requests.post(
        base_url,
        headers={
            "Authorization": f"Bearer {resolved_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        json=_post_payload(
            system_prompt, user_prompt, resolved_model, temperature, max_tokens, True, provider
        ),
        timeout=(10, 30),
        stream=True,
    )
    response.raise_for_status()

    yielded_any = False

    for raw_line in response.iter_lines(decode_unicode=False):
        if not raw_line:
            continue

        try:
            line = raw_line.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeError("The model returned invalid UTF-8 text") from exc

        if not line.startswith("data:"):
            continue

        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            break

        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue

        content = _extract_stream_text(chunk)
        if content:
            yielded_any = True
            yield content

    if not yielded_any:
        raise RuntimeError("The model returned an empty response")

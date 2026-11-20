"""Provider and model metadata, keeping unknown limits explicitly unknown."""

from __future__ import annotations

import json
import os
from typing import Any


PROVIDERS = {
    "groq": {
        "api_key_env": "GROQ_API_KEY",
        "base_url_env": "GROQ_BASE_URL",
        "default_base_url": "https://api.groq.com/openai/v1",
        "adapter": "openai_compatible",
        "capabilities": {
            "streaming": True,
            "file_input": False,
            "persistent_resources": False,
            "cached_context": None,
            "tool_calling": True,
            "structured_output": None,
            "token_usage_reporting": True,
            "modalities": ["text"],
        },
    },
    "gemini": {
        "api_key_env": "GEMINI_API_KEY",
        "base_url_env": "GEMINI_BASE_URL",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "adapter": "gemini_interactions",
        "capabilities": {
            "streaming": True,
            "file_input": True,
            "persistent_resources": True,
            "cached_context": None,
            "tool_calling": True,
            "structured_output": None,
            "token_usage_reporting": True,
            "modalities": ["text", "spreadsheet"],
        },
    },
}

MODEL_CATALOG = [
    {"id": "gemini-3.7-flash", "provider": "gemini", "model": "gemini-3.7-flash",
     "label": "Gemini 3.7 Flash", "description": "Latest Gemini Flash workhorse model"},
    {"id": "gemini-3.6-flash", "provider": "gemini", "model": "gemini-3.6-flash",
     "label": "Gemini 3.6 Flash", "description": "Fast general-purpose Gemini model"},
    {"id": "gemini-3.5-flash-lite", "provider": "gemini", "model": "gemini-3.5-flash-lite",
     "label": "Gemini 3.5 Flash-Lite", "description": "Fast, cost-sensitive Gemini model"},
    {"id": "groq-gpt-oss-20b", "provider": "groq", "model": "openai/gpt-oss-20b",
     "label": "GPT-OSS 20B", "description": "Fast open-weight model via Groq"},
    {"id": "groq-gpt-oss-120b", "provider": "groq", "model": "openai/gpt-oss-120b",
     "label": "GPT-OSS 120B", "description": "Larger open-weight model via Groq"},
]

MODEL_METADATA_ENV = "PRIVY_MODEL_METADATA"
_MODEL_LIMIT_FIELDS = {"context_window_tokens", "max_output_tokens"}
_MODEL_CAPABILITY_FIELDS = {
    "streaming",
    "file_input",
    "persistent_resources",
    "cached_context",
    "tool_calling",
    "structured_output",
    "token_usage_reporting",
}


def _configured_metadata() -> dict[str, dict[str, Any]]:
    raw = os.environ.get(MODEL_METADATA_ENV, "").strip()
    if not raw:
        return {}
    try:
        configured = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"{MODEL_METADATA_ENV} must contain a JSON object.") from exc
    if not isinstance(configured, dict):
        raise RuntimeError(f"{MODEL_METADATA_ENV} must contain a JSON object.")
    valid_ids = {item["id"] for item in MODEL_CATALOG}
    result: dict[str, dict[str, Any]] = {}
    for model_id, metadata in configured.items():
        if model_id not in valid_ids or not isinstance(metadata, dict):
            raise RuntimeError(f"{MODEL_METADATA_ENV} contains an unknown model or invalid metadata.")
        values: dict[str, Any] = {}
        for field in _MODEL_LIMIT_FIELDS:
            if field in metadata:
                value = metadata[field]
                if value is not None and (not isinstance(value, int) or value <= 0):
                    raise RuntimeError(f"{MODEL_METADATA_ENV}.{model_id}.{field} must be a positive integer.")
                values[field] = value
        capabilities = metadata.get("capabilities", {})
        if not isinstance(capabilities, dict) or set(capabilities) - _MODEL_CAPABILITY_FIELDS:
            raise RuntimeError(f"{MODEL_METADATA_ENV}.{model_id}.capabilities contains unsupported fields.")
        for field, value in capabilities.items():
            if value is not None and not isinstance(value, bool):
                raise RuntimeError(f"{MODEL_METADATA_ENV}.{model_id}.capabilities.{field} must be boolean or null.")
        values["capabilities"] = capabilities
        result[model_id] = values
    return result


def find_model(model_id: str | None) -> dict[str, Any] | None:
    if not model_id:
        return None
    return next(
        (item for item in MODEL_CATALOG if model_id in {item["id"], item["model"]}),
        None,
    )


def provider_is_disabled(provider: str) -> bool:
    return provider.strip().lower() in {
        value.strip().lower()
        for value in os.environ.get("PRIVY_DISABLED_PROVIDERS", "").split(",")
        if value.strip()
    }


def model_capabilities(model_id: str, provider: str | None = None) -> dict[str, Any]:
    model = find_model(model_id)
    resolved_provider = provider or (model["provider"] if model else None)
    provider_info = PROVIDERS.get(resolved_provider or "")
    if provider_info is None:
        return {
            "context_window_tokens": None,
            "max_output_tokens": None,
            "streaming": None,
            "file_input": None,
            "persistent_resources": None,
            "cached_context": None,
            "tool_calling": None,
            "structured_output": None,
            "token_usage_reporting": None,
            "modalities": [],
        }
    configured = _configured_metadata().get(model["id"], {}) if model else {}
    capabilities = dict(provider_info["capabilities"])
    capabilities.update(configured.get("capabilities", {}))
    return {
        "context_window_tokens": configured.get("context_window_tokens"),
        "max_output_tokens": configured.get("max_output_tokens"),
        **capabilities,
    }


def model_options() -> list[dict[str, Any]]:
    options = []
    for item in MODEL_CATALOG:
        provider = PROVIDERS[item["provider"]]
        if provider_is_disabled(item["provider"]) or not os.environ.get(provider["api_key_env"], "").strip():
            continue
        options.append({**item, "capabilities": model_capabilities(item["id"])})
    return options

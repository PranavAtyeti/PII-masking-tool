"""
llm.py
------
LLM provider client. Provider-agnostic, same as the original app.py: works
with any OpenAI-compatible /chat/completions endpoint (Groq, Together,
Fireworks, OpenRouter, self-hosted vLLM...) -- swapping providers is
changing env vars, not code.

call_llm() is the original non-streaming call (used for the title-generation
side call). stream_llm() is new for this session: same request, but with
stream=True, yielding text deltas as they arrive from the provider instead
of waiting for the full response.
"""

import json
import os

import requests

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1/chat/completions")
LLM_MODEL_DEFAULT = os.environ.get("LLM_MODEL", "openai/gpt-oss-20b")
LLM_API_KEY_ENV = os.environ.get("LLM_API_KEY", "")
DEFAULT_TEMPERATURE = float(os.environ.get("MODEL_TEMPERATURE", "0.1"))

COMMON_MODELS = [
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
]


def call_llm(system_prompt: str, user_prompt: str, api_key: str, model: str,
             temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = 400) -> str:
    """Non-streaming call. Only the masked/tokenized text passed in ever
    leaves this machine -- raw values are swapped back in locally by the
    caller after the response comes back."""
    if not api_key:
        raise RuntimeError("No API key is set")

    response = requests.post(
        LLM_BASE_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def stream_llm(system_prompt: str, user_prompt: str, api_key: str, model: str,
               temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = 400):
    """Streaming call. Yields text deltas (str) as they arrive.

    Groq's streaming format (like OpenAI's) is Server-Sent Events: each line
    is either `data: {json chunk}` or the literal `data: [DONE]` sentinel.
    Each json chunk looks like {"choices": [{"delta": {"content": "..."}}]}
    -- delta.content is missing/empty on some chunks (e.g. the very first,
    which only carries a role field), so those are skipped rather than
    yielding an empty string.
    """
    if not api_key:
        raise RuntimeError("No API key is set")

    response = requests.post(
        LLM_BASE_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=60,
        stream=True,
    )
    response.raise_for_status()

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

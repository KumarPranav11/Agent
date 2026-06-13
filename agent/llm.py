"""Thin wrapper around the Groq chat-completions API via the OpenAI SDK.

Groq exposes an OpenAI-compatible endpoint, so we point the OpenAI client at a
custom base_url. The only logic that lives here beyond a pass-through is
exponential backoff for transient errors (429, 5xx).
"""
from __future__ import annotations

import os
import time
from typing import Any

from openai import OpenAI
from openai import APIStatusError, APIConnectionError, RateLimitError, InternalServerError


GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_TEMPERATURE = 0.2
MAX_RETRIES = 4


def _default_model() -> str:
    # Read lazily — .env is loaded by main.py after this module is imported.
    return os.getenv("AGENT_MODEL", "llama-3.3-70b-versatile")


def _client() -> OpenAI:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return OpenAI(api_key=key, base_url=GROQ_BASE_URL)


def chat(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str = "auto",
    model: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
):
    """Send a chat-completion request with retries on transient failures."""
    client = _client()
    model = model or _default_model()

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice

    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return client.chat.completions.create(**kwargs)
        except (RateLimitError, InternalServerError, APIConnectionError) as e:
            last_exc = e
            time.sleep(delay)
            delay *= 2
        except APIStatusError as e:
            # Retry only on 5xx; bubble up 4xx (including tool_use_failed 400)
            if 500 <= getattr(e, "status_code", 0) < 600:
                last_exc = e
                time.sleep(delay)
                delay *= 2
                continue
            raise
    assert last_exc is not None
    raise last_exc

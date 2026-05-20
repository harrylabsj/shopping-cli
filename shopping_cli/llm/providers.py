"""Provider abstraction for optional OpenAI-compatible LLM runtimes."""

from __future__ import annotations

import json
import math
import os
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


Transport = Callable[[str, dict[str, str], dict[str, Any], int], dict[str, Any]]
MAX_PROVIDER_TIMEOUT_SECONDS = 300
MAX_PROVIDER_MAX_TOKENS = 32768


@dataclass(frozen=True)
class LLMResponse:
    content: str
    raw: dict[str, Any]


def _assistant_message_from_raw(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("LLM provider returned a non-object response")
    choices = raw.get("choices") or []
    if not isinstance(choices, list):
        raise ValueError("LLM provider choices must be a list")
    if not choices:
        return {}
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError("LLM provider choice must be an object")
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        raise ValueError("LLM provider message must be an object")
    return message


def _safe_positive_int(value: Any, default: int, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    try:
        number = int(value)
    except (OverflowError, TypeError, ValueError):
        return default
    if number <= 0:
        return default
    if maximum is not None:
        return min(number, maximum)
    return number


def _safe_optional_positive_int(value: Any, maximum: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        number = int(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if maximum is not None:
        return min(number, maximum)
    return number


def _default_transport(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # pragma: no cover - network path
        try:
            return json.loads(response.read().decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("LLM provider returned invalid JSON") from exc


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 30,
        max_tokens: int | None = None,
        transport: Transport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = _safe_positive_int(timeout, 30, maximum=MAX_PROVIDER_TIMEOUT_SECONDS)
        self.max_tokens = _safe_optional_positive_int(max_tokens, maximum=MAX_PROVIDER_MAX_TOKENS)
        self.transport = transport or _default_transport

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if tools:
            payload["tools"] = tools
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self.api_key}",
        }
        raw = self.transport(f"{self.base_url}/chat/completions", headers, payload, self.timeout)
        message = _assistant_message_from_raw(raw)
        return LLMResponse(content=str(message.get("content") or ""), raw=raw)


def provider_from_env(transport: Transport | None = None) -> OpenAICompatibleProvider:
    timeout_raw = os.environ.get("SHOPPING_LLM_TIMEOUT_SECONDS") or "30"
    try:
        timeout = int(timeout_raw)
    except ValueError:
        timeout = 30
    max_tokens_raw = os.environ.get("SHOPPING_LLM_MAX_TOKENS") or ""
    try:
        max_tokens = int(max_tokens_raw) if max_tokens_raw else None
    except ValueError:
        max_tokens = None
    return OpenAICompatibleProvider(
        base_url=os.environ.get("SHOPPING_LLM_BASE_URL") or "https://api.openai.com/v1",
        api_key=os.environ.get("SHOPPING_LLM_API_KEY") or "",
        model=os.environ.get("SHOPPING_LLM_MODEL") or "gpt-4.1-mini",
        timeout=timeout,
        max_tokens=max_tokens,
        transport=transport,
    )

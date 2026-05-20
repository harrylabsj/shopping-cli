"""Deterministic LLM tool-call loop for marketplace tools."""

from __future__ import annotations

import json
import math
import time
from typing import Any

from shopping_cli.llm.dispatcher import MarketplaceToolDispatcher
from shopping_cli.llm.providers import OpenAICompatibleProvider, LLMResponse
from shopping_cli.llm.tools import marketplace_tool_schemas

FALLBACK_CONTENT = "I could not safely complete this consultation tool loop. A human should review before replying."
MAX_LLM_TOOL_LOOP_STEPS = 16
MAX_LLM_TOOL_CALL_BUDGET = 64
MAX_LLM_PROVIDER_RETRIES = 5
MAX_LLM_PROVIDER_RETRY_DELAY_SECONDS = 60.0


def _assistant_message(response: LLMResponse) -> dict[str, Any]:
    choices = response.raw.get("choices") or []
    message = choices[0].get("message", {}) if choices else {}
    if not isinstance(message, dict):
        return {"role": "assistant", "content": response.content}
    return dict(message)


def _fallback(messages: list[dict[str, Any]], tool_results: list[dict[str, Any]], error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "content": FALLBACK_CONTENT,
        "messages": messages,
        "tool_results": tool_results,
        "error": error,
    }


def _safe_non_negative_int(value: Any, default: int, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    try:
        number = int(value)
    except (OverflowError, TypeError, ValueError):
        return default
    number = max(number, 0)
    if maximum is not None:
        return min(number, maximum)
    return number


def _safe_positive_int(value: Any, default: int, maximum: int | None = None) -> int:
    return max(_safe_non_negative_int(value, default, maximum=maximum), 1)


def _safe_optional_non_negative_int(value: Any, maximum: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        number = int(value)
    except (OverflowError, TypeError, ValueError):
        return None
    number = max(number, 0)
    if maximum is not None:
        return min(number, maximum)
    return number


def _safe_non_negative_float(value: Any, default: float, maximum: float | None = None) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    number = max(number, 0.0)
    if maximum is not None:
        return min(number, maximum)
    return number


def _tool_call_name_and_arguments(tool_call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    function = tool_call.get("function") or {}
    name = str(function.get("name") or "")
    if not name:
        raise ValueError("tool call missing function name")
    raw_arguments = function.get("arguments") or "{}"
    if isinstance(raw_arguments, dict):
        arguments = raw_arguments
    else:
        arguments = json.loads(str(raw_arguments or "{}"))
    if not isinstance(arguments, dict):
        raise ValueError(f"tool call {name} arguments must be a JSON object")
    return name, arguments


def run_marketplace_tool_loop(
    provider: OpenAICompatibleProvider,
    dispatcher: MarketplaceToolDispatcher,
    messages: list[dict[str, Any]],
    max_steps: int = 4,
    max_tool_calls: int | None = None,
    provider_retries: int = 0,
    provider_retry_delay_seconds: float = 0.0,
) -> dict[str, Any]:
    conversation_messages = [dict(message) for message in messages]
    tool_results: list[dict[str, Any]] = []
    tools = marketplace_tool_schemas()
    retries = _safe_non_negative_int(provider_retries, 0, maximum=MAX_LLM_PROVIDER_RETRIES)
    tool_call_budget = _safe_optional_non_negative_int(max_tool_calls, maximum=MAX_LLM_TOOL_CALL_BUDGET)
    steps = _safe_positive_int(max_steps, 4, maximum=MAX_LLM_TOOL_LOOP_STEPS)
    retry_delay = _safe_non_negative_float(
        provider_retry_delay_seconds,
        0.0,
        maximum=MAX_LLM_PROVIDER_RETRY_DELAY_SECONDS,
    )

    for _step in range(steps):
        response: LLMResponse | None = None
        for attempt in range(retries + 1):
            try:
                response = provider.complete(conversation_messages, tools=tools)
                break
            except SystemExit as exc:
                return _fallback(conversation_messages, tool_results, f"{type(exc).__name__}: {exc}")
            except Exception as exc:
                if attempt >= retries:
                    return _fallback(conversation_messages, tool_results, f"{type(exc).__name__}: {exc}")
                if retry_delay:
                    time.sleep(retry_delay)
        if response is None:  # pragma: no cover - defensive guard
            return _fallback(conversation_messages, tool_results, "provider returned no response")

        assistant = _assistant_message(response)
        tool_calls = assistant.get("tool_calls") or []
        if not tool_calls:
            return {
                "ok": True,
                "content": str(assistant.get("content") or response.content or ""),
                "messages": conversation_messages + [assistant],
                "tool_results": tool_results,
                "error": "",
            }

        conversation_messages.append(assistant)
        for tool_call in tool_calls:
            if tool_call_budget is not None and len(tool_results) >= tool_call_budget:
                return _fallback(
                    conversation_messages,
                    tool_results,
                    f"LLM tool call budget exceeded: {tool_call_budget}",
                )
            try:
                name, arguments = _tool_call_name_and_arguments(tool_call)
                dispatched = dispatcher.dispatch(name, arguments)
            except (Exception, SystemExit) as exc:
                return _fallback(conversation_messages, tool_results, f"{type(exc).__name__}: {exc}")
            tool_results.append(dispatched)
            conversation_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(tool_call.get("id") or ""),
                    "name": name,
                    "content": json.dumps(dispatched, ensure_ascii=False, sort_keys=True),
                }
            )

    return _fallback(conversation_messages, tool_results, "LLM tool loop exceeded max_steps")

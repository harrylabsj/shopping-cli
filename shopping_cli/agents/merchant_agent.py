"""Resident deterministic merchant agent."""

from __future__ import annotations

import math
import os
import sqlite3
from typing import Any

from shopping_cli import VERSION
from shopping_cli.agents.buyer_cli import MVP_WARNINGS
from shopping_cli.agents.tools import DEFAULT_CAPABILITIES, MerchantAgentTools, SQLiteMerchantAgentTools, record_heartbeat
from shopping_cli.core.harness import MAX_STALE_TTL_SECONDS, message_idempotency_key
from shopping_cli.core.risk import human_review_reason


def _safe_non_negative_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number < 0:
        return 0.0
    return number


def _safe_non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    try:
        number = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(number, 0)


def _positive_message_id(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("buyer message id must be a positive integer")
    try:
        message_id = int(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("buyer message id must be a positive integer") from exc
    if message_id <= 0:
        raise ValueError("buyer message id must be a positive integer")
    return message_id


def _claim_ttl_seconds_from_env() -> int:
    try:
        seconds = int(os.environ.get("SHOPPING_AGENT_CLAIM_TTL_SECONDS") or "300")
    except (OverflowError, TypeError, ValueError):
        return 300
    return seconds if 0 < seconds <= MAX_STALE_TTL_SECONDS else 300


def heartbeat(
    conn: sqlite3.Connection,
    merchant_id: str,
    status: str = "online",
    capabilities: list[str] | None = None,
    pid: int = 0,
    version: str = VERSION,
    last_error: str = "",
    checked_count: int = 0,
    replied_count: int = 0,
) -> dict[str, Any]:
    return record_heartbeat(
        conn,
        merchant_id,
        status=status,
        capabilities=capabilities,
        pid=pid,
        version=version,
        last_error=last_error,
        checked_count=checked_count,
        replied_count=replied_count,
    )


def latest_buyer_message(conversation: dict[str, Any]) -> dict[str, Any] | None:
    for message in reversed(conversation.get("messages", [])):
        if message["sender"] == "buyer":
            return message
    return None


def has_agent_reply_after(conversation: dict[str, Any], buyer_message_id: int) -> bool:
    for message in conversation.get("messages", []):
        try:
            message_id = _positive_message_id(message.get("id"))
        except ValueError:
            continue
        if message_id <= buyer_message_id:
            continue
        if message["sender"] in {"merchant_agent", "merchant"}:
            return True
    return False


def generate_reply(
    tools: MerchantAgentTools,
    conversation: dict[str, Any],
    buyer_message: dict[str, Any],
) -> tuple[str, bool, str]:
    product = None
    if conversation.get("sku"):
        try:
            product = tools.product_summary(conversation["sku"])
        except SystemExit:
            product = None
    reason = human_review_reason(buyer_message["text"], product_found=product is not None)
    disclaimer = " ".join(MVP_WARNINGS)
    if product is None:
        return f"I need a merchant human to confirm which product this consultation refers to. {disclaimer}", True, reason
    delivery = product["delivery"]
    price = _safe_non_negative_float(product.get("price"))
    stock = _safe_non_negative_int(product.get("stock"))
    if not reason and stock <= 2:
        reason = "low_stock"
    if not reason and buyer_message["intent"] == "ask_delivery" and not delivery.get("service_area"):
        reason = "unclear_delivery"
    if reason:
        return (
            f"{product['title']} is listed at {price:.2f} {product['currency']} with "
            f"{stock} in stock. This request needs merchant human review because: {reason}. {disclaimer}",
            True,
            reason,
        )
    delivery_text = "delivery rule is missing"
    if delivery.get("service_area"):
        delivery_text = (
            f"delivery area {delivery['service_area']}, ETA {_safe_non_negative_int(delivery.get('eta_minutes'))} minutes, "
            f"fee {_safe_non_negative_float(delivery.get('fee')):.2f} {delivery['currency']}"
        )
    return (
        f"{product['title']} has stock {stock} and current price "
        f"{price:.2f} {product['currency']}; {delivery_text}. {disclaimer}",
        False,
        "",
    )


def process_once_with_tools(tools: MerchantAgentTools, merchant_id: str) -> dict[str, Any]:
    agent = tools.heartbeat(merchant_id)
    abandoned: list[dict[str, Any]] = []
    if hasattr(tools, "abandon_stale_messages"):
        abandoned = tools.abandon_stale_messages(agent["id"], stale_after_seconds=_claim_ttl_seconds_from_env())
    conversations = tools.waiting_merchant_conversations(merchant_id)
    replied: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for conversation in conversations:
        buyer_message = latest_buyer_message(conversation)
        if buyer_message is None:
            continue
        try:
            buyer_message_id = _positive_message_id(buyer_message.get("id"))
        except ValueError as exc:
            error = f"{type(exc).__name__}: {exc}"
            tools.heartbeat(
                merchant_id,
                status="online",
                last_error=error,
                checked_count=len(conversations),
                replied_count=len(replied),
            )
            failed.append({"conversation_id": conversation["id"], "message_id": 0, "error": error})
            continue
        if has_agent_reply_after(conversation, buyer_message_id):
            continue
        idempotency_key = message_idempotency_key(agent["id"], buyer_message_id)
        claim = tools.claim_message(agent["id"], conversation["id"], buyer_message_id, idempotency_key)
        if not claim.get("claimed"):
            continue
        try:
            reply, needs_human, reason = generate_reply(tools, conversation, buyer_message)
            status = "human_required" if needs_human else "waiting_buyer"
            message = tools.append_message(
                conversation["id"],
                "merchant_agent",
                buyer_message["intent"],
                reply,
                structured_payload={
                    "human_required": needs_human,
                    "reason": reason,
                    "source_id": agent["id"],
                    "processed_message_id": buyer_message_id,
                    "idempotency_key": idempotency_key,
                },
                status=status,
            )
            tools.complete_message(agent["id"], buyer_message_id)
            flags = []
            if needs_human:
                flags.append(tools.add_flag(conversation["id"], reason or "human_required", sku=conversation.get("sku", "")))
                tools.heartbeat(merchant_id, status="human_required")
            replied.append(
                {
                    "conversation_id": conversation["id"],
                    "message_id": message["id"],
                    "human_required": needs_human,
                    "reason": reason,
                    "flags": flags,
                }
            )
        except Exception as exc:  # pragma: no cover - exercised through fake tools
            error = f"{type(exc).__name__}: {exc}"
            tools.fail_message(agent["id"], buyer_message_id, error)
            tools.heartbeat(
                merchant_id,
                status="online",
                last_error=error,
                checked_count=len(conversations),
                replied_count=len(replied),
            )
            failed.append({"conversation_id": conversation["id"], "message_id": buyer_message_id, "error": error})
    return {
        "ok": True,
        "merchant_id": merchant_id,
        "agent": agent,
        "checked": len(conversations),
        "replied": replied,
        "failed": failed,
        "abandoned": abandoned,
    }


def process_once(conn: sqlite3.Connection, merchant_id: str) -> dict[str, Any]:
    return process_once_with_tools(SQLiteMerchantAgentTools(conn), merchant_id)

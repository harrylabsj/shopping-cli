"""Pure negotiation policy-result data and contract payload builder.

This module owns the GateOutcome gate result value, its accepted /
rejected-retryable / human-required constructors, and the policy-result
contract payload builder. It never touches SQLite, claims, turn/state
transitions, or negotiation writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shopping_cli.core import negotiation as protocol


@dataclass(frozen=True)
class GateOutcome:
    result: str  # "accepted" | "rejected_retryable" | "human_required"
    reason_codes: tuple[str, ...]
    public_reason: str


ACCEPTED_OUTCOME = GateOutcome("accepted", (), "")


def rejected(code: str, public_reason: str) -> GateOutcome:
    return GateOutcome("rejected_retryable", (code,), public_reason)


def human_required(code: str, public_reason: str) -> GateOutcome:
    return GateOutcome("human_required", (code,), public_reason)


def build_policy_result(
    conversation_id: str,
    result: str,
    next_actor: str,
    reason_codes: list[str],
    public_reason: str,
    retries_remaining: int,
    message_id: int | None = None,
) -> dict[str, Any]:
    """Build and contract-validate a frozen policy-result payload."""
    payload: dict[str, Any] = {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "result": result,
        "conversation_id": conversation_id,
        "next_actor": next_actor,
        "reason_codes": reason_codes[:32],
        "public_reason": protocol.truncate_text(public_reason, 1000),
        "retries_remaining": max(0, retries_remaining),
    }
    if message_id is not None:
        payload["message_id"] = message_id
    protocol.validate_contract("policy-result", payload)
    return payload

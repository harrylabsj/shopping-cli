"""Pure negotiation snapshot message projection.

This module owns the message-level projection used to build a negotiation
snapshot: id conversion, buyer/merchant sender mapping, timestamp
normalization, text truncation, protocol-version gating, valid decision
action handling, proposal handling, and None behavior. It never touches
SQLite, claims, turn/state transitions, or negotiation writes.
"""

from __future__ import annotations

from typing import Any

from shopping_cli.core import negotiation as protocol


def snapshot_message(message: dict[str, Any]) -> dict[str, Any]:
    """Project one DB conversation message into a public snapshot message entry.

    The projection is intentionally lossless for public fields: ids are coerced
    to int, ``sender_role`` is derived from the sender constant set, ``created_at``
    is normalized to explicit-offset RFC 3339, and the text is truncated to the
    schema's 2000-char ``public_message`` limit. Structured decisions are only
    surfaced when they match the current protocol version; proposal values that
    are not objects are coerced to ``None``.
    """
    entry: dict[str, Any] = {
        "id": int(message["id"]),
        "sender_role": "buyer" if message["sender"] in protocol.BUYER_SENDERS else "merchant",
        # DB rows store naive local time; emit explicit-offset RFC 3339 so the
        # frozen schema (and Kiwi's strict Ajv date-time check) accepts it.
        "created_at": protocol.normalize_db_timestamp(message["created_at"]),
        "public_message": protocol.truncate_text(message["text"], 2000),
    }
    payload = message.get("structured_payload") or {}
    decision = payload.get("decision") if payload.get("protocol_version") == protocol.PROTOCOL_VERSION else None
    if isinstance(decision, dict):
        action = decision.get("action")
        if action in protocol.DECISION_ACTIONS:
            entry["action"] = action
        proposal = decision.get("proposal")
        entry["proposal"] = proposal if isinstance(proposal, dict) else None
    else:
        entry["proposal"] = None
    return entry

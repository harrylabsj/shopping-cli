"""Human review service helpers shared by transports."""

from __future__ import annotations

from typing import Any

from shopping_cli.core.errors import NotFoundError, ValidationError

HUMAN_REVIEW_ACTIONS = {"reply", "approve_public_answer", "reject", "close"}
HUMAN_REVIEW_SENDERS = {"merchant", "merchant_agent", "operator"}


def human_review_sender(payload: dict[str, Any]) -> str:
    sender = str(payload.get("sender") or "merchant").strip() or "merchant"
    if sender not in HUMAN_REVIEW_SENDERS:
        raise ValidationError(f"Unknown human-review sender: {sender}")
    return sender


def validate_action(action: str) -> str:
    normalized = str(action or "reply")
    if normalized not in HUMAN_REVIEW_ACTIONS:
        raise ValidationError(f"Unknown human-review action: {normalized}")
    return normalized


def human_review_row(conn: Any, review_id: str | int, positive_whole_int: Any) -> Any:
    normalized_review_id = positive_whole_int(review_id, "review_id")
    row = conn.execute("select * from moderation_flags where id = ?", (normalized_review_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown human review: {review_id}")
    return row

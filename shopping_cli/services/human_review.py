"""Human review service helpers shared by transports."""

from __future__ import annotations

from typing import Any

from shopping_cli.core.errors import ConflictError, NotFoundError, ValidationError
from shopping_cli.db.session import now_iso

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


def list_unresolved_reviews(
    conn: Any,
    merchant_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[Any]:
    """Return unresolved moderation flag rows for a merchant, newest first."""
    return conn.execute(
        """
        select f.*, c.merchant_id as merchant_id, c.buyer_id as buyer_id
        from moderation_flags f
        join conversations c on c.id = f.conversation_id
        where f.resolved_at = '' and c.merchant_id = ?
        order by f.created_at desc, f.id desc limit ? offset ?
        """,
        (merchant_id, limit, offset),
    ).fetchall()


def list_conversation_reviews(conn: Any, conversation_id: str) -> list[Any]:
    """Return all moderation flag rows for a conversation, ordered by id."""
    return conn.execute(
        "select * from moderation_flags where conversation_id = ? order by id",
        (conversation_id,),
    ).fetchall()


def remaining_unresolved_reviews(conn: Any, conversation_id: str) -> list[str]:
    """Return reasons for still-unresolved reviews in priority order."""
    rows = conn.execute(
        """
        select reason from moderation_flags
        where conversation_id = ? and resolved_at = ''
        order by case when reason = 'suspicious_content' then 0 else 1 end, id
        """,
        (conversation_id,),
    ).fetchall()
    return [str(row["reason"] or "") for row in rows]


def update_conversation_status(
    conn: Any,
    conversation_id: str,
    *,
    status: str,
    next_actor: str,
    sender: str,
    now: str | None = None,
) -> None:
    """Update a conversation's status, next_actor, and last_sender."""
    conn.execute(
        "update conversations set status = ?, next_actor = ?, updated_at = ?, last_sender = ? where id = ?",
        (status, next_actor, now or now_iso(), sender, conversation_id),
    )


def resolve_review(
    conn: Any,
    review_id: int,
    *,
    action: str,
    sender: str,
    now: str | None = None,
) -> int:
    """Resolve a single moderation flag. Returns the rowcount (1 on success)."""
    resolved = conn.execute(
        """
        update moderation_flags
        set resolved_at = ?, resolution = ?, resolved_by = ?
        where id = ? and resolved_at = ''
        """,
        (now or now_iso(), action, sender, review_id),
    )
    return resolved.rowcount


def resolve_all_conversation_reviews(
    conn: Any,
    conversation_id: str,
    *,
    action: str,
    sender: str,
    now: str | None = None,
) -> int:
    """Resolve all unresolved moderation flags for a conversation. Returns rowcount."""
    resolved = conn.execute(
        """
        update moderation_flags
        set resolved_at = ?, resolution = ?, resolved_by = ?
        where conversation_id = ? and resolved_at = ''
        """,
        (now or now_iso(), action, sender, conversation_id),
    )
    return resolved.rowcount

"""Conversation write use cases shared by transports."""

from __future__ import annotations

from typing import Any

from shopping_cli.core.conversations import add_flag, append_message, conversation_summary, ensure_conversation
from shopping_cli.core.errors import ConflictError, ValidationError
from shopping_cli.core.harness import append_audit_event, next_actor_for_status
from shopping_cli.db.session import now_iso


def create_conversation(
    conn: Any,
    *,
    buyer_id: str,
    merchant_id: str,
    sku: str = "",
    text: str = "",
    intent: str = "ask_product",
    source_id: str = "",
    reuse_open: bool = False,
) -> dict[str, Any]:
    conversation = ensure_conversation(
        conn,
        buyer_id=buyer_id,
        merchant_id=merchant_id,
        sku=sku,
        reuse_open=reuse_open,
    )
    if text:
        append_message(
            conn,
            conversation["id"],
            "buyer",
            intent,
            text,
            structured_payload={"source_id": source_id},
        )
        conversation = conversation_summary(conn, conversation["id"])
    return conversation


def append_conversation_message(
    conn: Any,
    conversation: dict[str, Any],
    conversation_id: str,
    *,
    sender: str,
    intent: str,
    text: str,
    structured_payload: dict[str, Any],
    status: Any = None,
) -> dict[str, Any]:
    if str(status or "").strip() == "closed":
        raise ValidationError("conversation messages cannot close conversations; use the close endpoint")
    message = append_message(
        conn,
        conversation_id,
        sender=sender,
        intent=intent,
        text=text,
        structured_payload=structured_payload,
        status=status,
    )
    if str(status or "").strip() == "human_required":
        add_flag(
            conn,
            conversation_id,
            reason=str(message["structured_payload"].get("reason") or "human_required"),
            severity=str(message["structured_payload"].get("severity") or "review"),
            sku=conversation.get("sku") or "",
        )
    return {"message": message, "conversation": conversation_summary(conn, conversation_id)}


def close_conversation(
    conn: Any,
    conversation: dict[str, Any],
    conversation_id: str,
    *,
    sender: str,
    intent: str = "support",
    text: str = "",
    source_id: str = "",
) -> dict[str, Any]:
    if conversation["status"] == "closed":
        raise ConflictError(f"Conversation {conversation_id} is closed")
    next_actor = next_actor_for_status("closed")
    if text:
        append_message(
            conn,
            conversation_id,
            sender=sender,
            intent=intent,
            text=text,
            structured_payload={"source_id": source_id},
            status="closed",
        )
    else:
        conn.execute(
            "update conversations set status = 'closed', next_actor = ?, updated_at = ?, last_sender = ? where id = ?",
            (next_actor, now_iso(), sender, conversation_id),
        )
    append_conversation_closed_audit(conn, conversation_id, sender, next_actor)
    return conversation_summary(conn, conversation_id)


def append_conversation_closed_audit(
    conn: Any,
    conversation_id: str,
    actor: str,
    next_actor: str,
    details: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"next_actor": next_actor}
    if details:
        payload.update(details)
    append_audit_event(conn, conversation_id, actor, "conversation_closed", payload)

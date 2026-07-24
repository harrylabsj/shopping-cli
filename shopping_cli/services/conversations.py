"""Conversation write use cases shared by transports."""

from __future__ import annotations

from typing import Any

from shopping_cli.core.conversations import (
    add_flag,
    append_message,
    conversation_details_batch,
    conversation_list_summary_from_row,
    conversation_summary,
    ensure_conversation,
)
from shopping_cli.core.errors import ConflictError, ValidationError
from shopping_cli.core.harness import append_audit_event, next_actor_for_status
from shopping_cli.db.session import now_iso
from shopping_cli.services import tokens as token_service


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
    new_flag: dict[str, Any] | None = None
    if str(status or "").strip() == "human_required":
        new_flag = add_flag(
            conn,
            conversation_id,
            reason=str(message["structured_payload"].get("reason") or "human_required"),
            severity=str(message["structured_payload"].get("severity") or "review"),
            sku=conversation.get("sku") or "",
        )
    result: dict[str, Any] = {"message": message, "conversation": conversation_summary(conn, conversation_id)}
    if new_flag is not None:
        result["new_flag"] = new_flag
    return result


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
    unresolved = conn.execute(
        "select 1 from moderation_flags where conversation_id = ? and resolved_at = '' limit 1",
        (conversation_id,),
    ).fetchone()
    if unresolved is not None:
        raise ConflictError(f"Conversation {conversation_id} has unresolved human review")
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
        updated = conn.execute(
            """
            update conversations
            set status = 'closed', next_actor = ?, updated_at = ?, last_sender = ?
            where id = ? and status = ? and status != 'closed'
              and not exists (
                  select 1 from moderation_flags
                  where conversation_id = ? and resolved_at = ''
              )
            """,
            (next_actor, now_iso(), sender, conversation_id, conversation["status"], conversation_id),
        )
        if updated.rowcount != 1:
            raise ConflictError(f"Conversation {conversation_id} changed concurrently")
    append_conversation_closed_audit(conn, conversation_id, sender, next_actor)
    return conversation_summary(conn, conversation_id)


def append_conversation_closed_audit(
    conn: Any,
    conversation_id: str,
    actor: str,
    next_actor: str,
    details: dict[str, Any] | None = None,
) -> None:
    token_service.revoke_buyer_tokens_for_conversation(conn, conversation_id)
    payload: dict[str, Any] = {"next_actor": next_actor}
    if details:
        payload.update(details)
    append_audit_event(conn, conversation_id, actor, "conversation_closed", payload)


def list_conversation_ids(
    conn: Any,
    *,
    clauses: list[str] | None = None,
    values: list[Any] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[str]:
    """Return conversation IDs matching the given SQL clauses, ordered by most recently updated."""
    sql = "select id from conversations"
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by updated_at desc limit ? offset ?"
    params = list(values or [])
    params.extend([limit, offset])
    return [str(row["id"]) for row in conn.execute(sql, params).fetchall()]


def list_conversation_summaries(
    conn: Any,
    *,
    clauses: list[str] | None = None,
    values: list[Any] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    sql = """select c.*,
        (select count(*) from messages m where m.conversation_id = c.id) as message_count,
        (select count(*) from moderation_flags f where f.conversation_id = c.id and f.resolved_at = '') as unresolved_flag_count,
        (select count(*) from audit_events e where e.conversation_id = c.id) as audit_event_count
        from conversations c"""
    if clauses:
        sql += " where " + " and ".join(f"c.{clause}" for clause in clauses)
    sql += " order by c.updated_at desc limit ? offset ?"
    params = list(values or [])
    params.extend([limit, offset])
    return [conversation_list_summary_from_row(conn, row) for row in conn.execute(sql, params).fetchall()]


def list_conversation_details(
    conn: Any,
    *,
    clauses: list[str] | None = None,
    values: list[Any] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return conversation details (summary fields plus messages) with O(1) queries."""
    sql = """select c.*,
        (select count(*) from messages m where m.conversation_id = c.id) as message_count,
        (select count(*) from moderation_flags f where f.conversation_id = c.id and f.resolved_at = '') as unresolved_flag_count,
        (select count(*) from audit_events e where e.conversation_id = c.id) as audit_event_count
        from conversations c"""
    if clauses:
        sql += " where " + " and ".join(f"c.{clause}" for clause in clauses)
    sql += " order by c.updated_at desc limit ? offset ?"
    params = list(values or [])
    params.extend([limit, offset])
    return conversation_details_batch(conn, conn.execute(sql, params).fetchall())

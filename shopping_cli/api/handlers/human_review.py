"""Human review API handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shopping_cli.api import auth as api_auth
from shopping_cli.api.handlers.common import DEFAULT_RESULT_LIMIT, positive_whole_int, result_limit, result_offset
from shopping_cli.core.conversations import add_flag, append_message, conversation_summary
from shopping_cli.core.errors import AuthError, ConflictError, NotFoundError
from shopping_cli.core.harness import append_audit_event, next_actor_for_status
from shopping_cli.db.session import db_session, now_iso
from shopping_cli.services import conversations as conversation_service
from shopping_cli.services import human_review as human_review_service
from shopping_cli.services import tokens as token_service


def _payload_token(payload: dict[str, Any]) -> str:
    return api_auth.payload_token(payload)


def human_review_sender(payload: dict[str, Any]) -> str:
    return human_review_service.human_review_sender(payload)


def review_summary(conn: Any, flag_row: Any) -> dict[str, Any]:
    row_keys = set(flag_row.keys()) if hasattr(flag_row, "keys") else set()
    if {"merchant_id", "buyer_id"}.issubset(row_keys):
        merchant_id = flag_row["merchant_id"]
        buyer_id = flag_row["buyer_id"]
    else:
        conversation = conversation_summary(conn, flag_row["conversation_id"])
        merchant_id = conversation["merchant_id"]
        buyer_id = conversation["buyer_id"]
    return {
        "id": flag_row["id"],
        "conversation_id": flag_row["conversation_id"],
        "merchant_id": merchant_id,
        "buyer_id": buyer_id,
        "sku": flag_row["sku"],
        "reason": flag_row["reason"],
        "severity": flag_row["severity"],
        "created_at": flag_row["created_at"],
        "resolved_at": flag_row["resolved_at"] or None,
        "resolution": flag_row["resolution"],
        "resolved_by": flag_row["resolved_by"],
    }


def human_review_queue(
    db_path: str | Path,
    payload: dict[str, Any],
    merchant_id: str = "",
    limit: Any = DEFAULT_RESULT_LIMIT,
    offset: Any = 0,
) -> dict[str, Any]:
    if not merchant_id:
        raise AuthError("merchant_id is required for human-review queue")
    sql = """
        select f.*, c.merchant_id as merchant_id, c.buyer_id as buyer_id
        from moderation_flags f
        join conversations c on c.id = f.conversation_id
        where f.resolved_at = ''
    """
    values: list[Any] = []
    sql += " and c.merchant_id = ?"
    values.append(merchant_id)
    sql += " order by f.created_at desc, f.id desc limit ? offset ?"
    values.extend([result_limit(limit), result_offset(offset)])
    with db_session(db_path) as conn:
        token_service.require_merchant_read_token(conn, merchant_id, _payload_token(payload))
        rows = conn.execute(sql, values).fetchall()
        return {"ok": True, "reviews": [review_summary(conn, row) for row in rows]}


def human_review_row(conn: Any, review_id: str | int) -> Any:
    return human_review_service.human_review_row(conn, review_id, positive_whole_int)


def get_human_review(db_path: str | Path, review_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        row = human_review_row(conn, review_id)
        review = review_summary(conn, row)
        token_service.require_merchant_read_token(conn, review["merchant_id"], _payload_token(payload))
        return {"ok": True, "review": review, "conversation": conversation_summary(conn, review["conversation_id"])}


def create_human_review(db_path: str | Path, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        conversation = conversation_summary(conn, conversation_id)
        actor = str(payload.get("source_id") or token_service.default_merchant_agent_id(conversation["merchant_id"]))
        if actor.startswith("shopping-cli-merchant-agent:"):
            token_service.require_agent_or_merchant_token(conn, conversation["merchant_id"], actor, _payload_token(payload))
        else:
            token_service.require_merchant_token(conn, conversation["merchant_id"], _payload_token(payload))
        review = add_flag(
            conn,
            conversation_id,
            reason=str(payload.get("reason") or "human_required"),
            severity=str(payload.get("severity") or "review"),
            sku=conversation.get("sku") or "",
        )
        next_actor = next_actor_for_status("human_required", review["reason"])
        conn.execute(
            "update conversations set status = 'human_required', next_actor = ?, updated_at = ?, last_sender = ? where id = ?",
            (next_actor, now_iso(), actor, conversation_id),
        )
        append_audit_event(
            conn,
            conversation_id,
            actor,
            "conversation_routed",
            {"status": "human_required", "next_actor": next_actor, "reason": review["reason"]},
        )
        row = conn.execute("select * from moderation_flags where id = ?", (review["id"],)).fetchone()
        return {
            "ok": True,
            "review": review_summary(conn, row),
            "conversation": conversation_summary(conn, conversation_id),
        }


def resolve_human_review_item(db_path: str | Path, review_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
    action = human_review_service.validate_action(str(payload.get("action") or "reply"))
    sender = human_review_sender(payload)
    with db_session(db_path) as conn:
        row = human_review_row(conn, review_id)
        if row["resolved_at"]:
            raise ConflictError(f"Human review already resolved: {review_id}")
        conversation_id = row["conversation_id"]
        conversation = conversation_summary(conn, conversation_id)
        token_service.require_merchant_token(conn, conversation["merchant_id"], _payload_token(payload))
        if conversation["status"] == "closed":
            raise ConflictError(f"Conversation {conversation_id} is closed")
        now = now_iso()
        resolved = conn.execute(
            """
            update moderation_flags
            set resolved_at = ?, resolution = ?, resolved_by = ?
            where id = ? and resolved_at = ''
            """,
            (now, action, sender, int(review_id)),
        )
        if resolved.rowcount != 1:
            raise ConflictError(f"Human review already resolved: {review_id}")
        remaining_rows = conn.execute(
            """
            select reason from moderation_flags
            where conversation_id = ? and resolved_at = ''
            order by case when reason = 'suspicious_content' then 0 else 1 end, id
            """,
            (conversation_id,),
        ).fetchall()
        remaining = len(remaining_rows)
        remaining_reason = str(remaining_rows[0]["reason"] or "") if remaining_rows else ""
        status = "human_required" if remaining else ("closed" if action == "close" else "waiting_buyer")
        status_reason = remaining_reason if status == "human_required" else str(row["reason"] or "")
        next_actor = next_actor_for_status(status, status_reason if status == "human_required" else "")
        if payload.get("text"):
            append_message(
                conn,
                conversation_id,
                sender=sender,
                intent=str(payload.get("intent") or "support"),
                text=str(payload["text"]),
                structured_payload={
                    "resolution": action,
                    "source_id": payload.get("source_id") or sender,
                    "review_id": int(review_id),
                    "reason": status_reason,
                    "resolved_reason": row["reason"],
                },
                status=status,
            )
        else:
            conn.execute(
                "update conversations set status = ?, next_actor = ?, updated_at = ?, last_sender = ? where id = ?",
                (status, next_actor, now, sender, conversation_id),
            )
        append_audit_event(
            conn,
            conversation_id,
            payload.get("source_id") or sender,
            "human_review_resolved",
            {
                "review_id": int(review_id),
                "resolution": action,
                "status": status,
                "next_actor": next_actor,
                "remaining_unresolved_reviews": int(remaining or 0),
            },
        )
        if status == "closed":
            conversation_service.append_conversation_closed_audit(
                conn,
                conversation_id,
                payload.get("source_id") or sender,
                next_actor,
                {"resolution": action, "review_id": int(review_id), "source": "human_review"},
            )
        review = review_summary(conn, human_review_row(conn, review_id))
        rows = conn.execute(
            "select * from moderation_flags where conversation_id = ? order by id",
            (conversation_id,),
        ).fetchall()
        return {
            "ok": True,
            "review": review,
            "reviews": [review_summary(conn, row) for row in rows],
            "conversation": conversation_summary(conn, conversation_id),
        }


def resolve_human_review(db_path: str | Path, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    action = human_review_service.validate_action(str(payload.get("action") or "reply"))
    sender = human_review_sender(payload)
    status = "closed" if action == "close" else "waiting_buyer"
    with db_session(db_path) as conn:
        conversation = conversation_summary(conn, conversation_id)
        token_service.require_merchant_token(conn, conversation["merchant_id"], _payload_token(payload))
        if conversation["status"] == "closed":
            raise ConflictError(f"Conversation {conversation_id} is closed")
        now = now_iso()
        resolved = conn.execute(
            """
            update moderation_flags
            set resolved_at = ?, resolution = ?, resolved_by = ?
            where conversation_id = ? and resolved_at = ''
            """,
            (now, action, sender, conversation_id),
        )
        if resolved.rowcount == 0:
            raise NotFoundError(f"No unresolved human reviews for conversation: {conversation_id}")
        next_actor = next_actor_for_status(status)
        if payload.get("text"):
            append_message(
                conn,
                conversation_id,
                sender=sender,
                intent=str(payload.get("intent") or "support"),
                text=str(payload["text"]),
                structured_payload={"resolution": action, "source_id": payload.get("source_id") or ""},
                status=status,
            )
        else:
            conn.execute(
                "update conversations set status = ?, next_actor = ?, updated_at = ?, last_sender = ? where id = ?",
                (status, next_actor, now, sender, conversation_id),
            )
        append_audit_event(
            conn,
            conversation_id,
            payload.get("source_id") or sender,
            "human_review_resolved",
            {"resolution": action, "status": status, "next_actor": next_actor},
        )
        if status == "closed":
            conversation_service.append_conversation_closed_audit(
                conn,
                conversation_id,
                payload.get("source_id") or sender,
                next_actor,
                {"resolution": action, "source": "human_review"},
            )
        rows = conn.execute(
            "select * from moderation_flags where conversation_id = ? order by id",
            (conversation_id,),
        ).fetchall()
        return {
            "ok": True,
            "reviews": [review_summary(conn, row) for row in rows],
            "conversation": conversation_summary(conn, conversation_id),
        }

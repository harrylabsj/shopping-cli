"""Human review API handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shopping_cli.api import auth as api_auth
from shopping_cli.api.handlers.common import DEFAULT_RESULT_LIMIT, positive_whole_int, result_limit, result_offset
from shopping_cli.core.conversations import add_flag, append_message, conversation_summary
from shopping_cli.core.errors import AuthError, ConflictError, NotFoundError
from shopping_cli.core.harness import append_audit_event, next_actor_for_review_reason, next_actor_for_status
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
    with db_session(db_path) as conn:
        token_service.require_merchant_read_token(conn, merchant_id, _payload_token(payload))
        rows = human_review_service.list_unresolved_reviews(
            conn,
            merchant_id,
            limit=result_limit(limit),
            offset=result_offset(offset),
        )
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
        token_row = token_service.require_api_token(conn, _payload_token(payload), "merchant or agent token required")
        if token_row["merchant_id"] != conversation["merchant_id"] or token_row["role"] not in {"merchant", "agent"}:
            raise AuthError("invalid merchant or agent token")
        actor = str(token_row["agent_id"] or token_row["merchant_id"])
        review = add_flag(
            conn,
            conversation_id,
            reason=str(payload.get("reason") or "human_required"),
            severity=str(payload.get("severity") or "review"),
            sku=conversation.get("sku") or "",
        )
        # add_flag already transitions the conversation to human_required with
        # a rowcount-guarded UPDATE — no need for a separate status update here.
        append_audit_event(
            conn,
            conversation_id,
            actor,
            "conversation_routed",
            {"status": "human_required", "next_actor": next_actor_for_status("human_required", review["reason"]), "reason": review["reason"]},
        )
        row = human_review_service.human_review_row(conn, review["id"], positive_whole_int)
        return {
            "ok": True,
            "review": review_summary(conn, row),
            "conversation": conversation_summary(conn, conversation_id),
        }


def _require_resolver_identity(
    conn: Any,
    conversation: dict[str, Any],
    reasons: list[str],
    payload: dict[str, Any],
) -> str:
    """H7: operator 路由的 flag（suspicious_content）只能由 admin 身份销案。

    merchant 是"被审查方"——用自家 merchant token 自销 operator 仲裁的
    flag 会完全绕过人工审查控制。普通 flag 仍走 merchant token。
    返回解析者 actor 标识。
    """
    if any(
        next_actor_for_review_reason(str(reason or "")) == "operator" for reason in reasons
    ):
        api_auth.require_admin_token(payload)
        return "admin"
    token_row = token_service.require_merchant_token(conn, conversation["merchant_id"], _payload_token(payload))
    return str(token_row["agent_id"] or token_row["merchant_id"]) if token_row is not None else conversation["merchant_id"]


def resolve_human_review_item(db_path: str | Path, review_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
    action = human_review_service.validate_action(str(payload.get("action") or "reply"))
    sender = human_review_sender(payload)
    with db_session(db_path) as conn:
        row = human_review_row(conn, review_id)
        if row["resolved_at"]:
            raise ConflictError(f"Human review already resolved: {review_id}")
        conversation_id = row["conversation_id"]
        conversation = conversation_summary(conn, conversation_id)
        actor = _require_resolver_identity(conn, conversation, [str(row["reason"] or "")], payload)
        if conversation["status"] == "closed":
            raise ConflictError(f"Conversation {conversation_id} is closed")
        now = now_iso()
        rowcount = human_review_service.resolve_review(
            conn,
            int(review_id),
            action=action,
            sender=actor,
            now=now,
        )
        if rowcount != 1:
            raise ConflictError(f"Human review already resolved: {review_id}")
        remaining_reasons = human_review_service.remaining_unresolved_reviews(conn, conversation_id)
        remaining = len(remaining_reasons)
        remaining_reason = remaining_reasons[0] if remaining_reasons else ""
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
                    "source_id": actor,
                    "review_id": int(review_id),
                    "reason": status_reason,
                    "resolved_reason": row["reason"],
                },
                status=status,
            )
        else:
            human_review_service.update_conversation_status(
                conn,
                conversation_id,
                status=status,
                next_actor=next_actor,
                sender=actor,
                expected_status="human_required",
                # 非 human_required 目标态必须无未决 flag（防并发 add_flag
                # 竞态把会话带 flag 推进到 waiting_buyer/closed）；human_required
                # 时剩余未决 flag 是正常状态，不能 reject。
                reject_if_unresolved=(status != "human_required"),
                now=now,
            )
        append_audit_event(
            conn,
            conversation_id,
            actor,
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
                actor,
                next_actor,
                {"resolution": action, "review_id": int(review_id), "source": "human_review"},
            )
        review = review_summary(conn, human_review_row(conn, review_id))
        rows = human_review_service.list_conversation_reviews(conn, conversation_id)
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
        unresolved_reasons = [
            str(r["reason"] or "")
            for r in human_review_service.list_conversation_reviews(conn, conversation_id)
            if not str(r["resolved_at"] or "")
        ]
        actor = _require_resolver_identity(conn, conversation, unresolved_reasons, payload)
        if conversation["status"] == "closed":
            raise ConflictError(f"Conversation {conversation_id} is closed")
        now = now_iso()
        rowcount = human_review_service.resolve_all_conversation_reviews(
            conn,
            conversation_id,
            action=action,
            sender=actor,
            now=now,
        )
        if rowcount == 0:
            raise NotFoundError(f"No unresolved human reviews for conversation: {conversation_id}")
        next_actor = next_actor_for_status(status)
        if payload.get("text"):
            append_message(
                conn,
                conversation_id,
                sender=sender,
                intent=str(payload.get("intent") or "support"),
                text=str(payload["text"]),
                structured_payload={"resolution": action, "source_id": actor},
                status=status,
            )
        else:
            human_review_service.update_conversation_status(
                conn,
                conversation_id,
                status=status,
                next_actor=next_actor,
                sender=actor,
                expected_status="human_required",
                # 非 human_required 目标态必须无未决 flag（防并发 add_flag
                # 竞态把会话带 flag 推进到 waiting_buyer/closed）；human_required
                # 时剩余未决 flag 是正常状态，不能 reject。
                reject_if_unresolved=(status != "human_required"),
                now=now,
            )
        append_audit_event(
            conn,
            conversation_id,
            actor,
            "human_review_resolved",
            {"resolution": action, "status": status, "next_actor": next_actor},
        )
        if status == "closed":
            conversation_service.append_conversation_closed_audit(
                conn,
                conversation_id,
                actor,
                next_actor,
                {"resolution": action, "source": "human_review"},
            )
        rows = human_review_service.list_conversation_reviews(conn, conversation_id)
        return {
            "ok": True,
            "reviews": [review_summary(conn, row) for row in rows],
            "conversation": conversation_summary(conn, conversation_id),
        }

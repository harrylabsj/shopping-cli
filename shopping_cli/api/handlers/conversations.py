"""Conversation API handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shopping_cli.api import auth as api_auth
from shopping_cli.api.handlers.common import DEFAULT_RESULT_LIMIT, require_field, result_limit, result_offset
from shopping_cli.core.conversations import (
    conversation_summary,
    merchant_conversations as merchant_conversation_summaries,
    normalize_structured_payload,
)
from shopping_cli.core.errors import AuthError, ValidationError
from shopping_cli.db.session import db_session
from shopping_cli.services import conversations as conversation_service
from shopping_cli.services import tokens as token_service


def _payload_token(payload: dict[str, Any]) -> str:
    return api_auth.payload_token(payload)


def get_conversation(db_path: str | Path, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        conversation = conversation_summary(conn, conversation_id)
        token_service.require_conversation_read_token(conn, conversation, _payload_token(payload))
        return {"ok": True, "conversation": conversation}


def append_conversation_message(db_path: str | Path, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        conversation = conversation_summary(conn, conversation_id)
        sender = str(require_field(payload, "sender"))
        structured_payload = normalize_structured_payload(payload.get("structured_payload"))
        if payload.get("source_id"):
            structured_payload["source_id"] = payload.get("source_id")
        status = payload.get("status")
        if sender in {"buyer", "buyer_cli"}:
            token_service.require_buyer_conversation_token(conn, conversation, _payload_token(payload))
            if status not in (None, ""):
                raise ValidationError("buyer messages cannot set conversation status")
        elif sender == "merchant":
            token_service.require_merchant_token(conn, conversation["merchant_id"], _payload_token(payload))
        elif sender == "merchant_agent":
            agent_id = str(
                structured_payload.get("source_id")
                or token_service.default_merchant_agent_id(conversation["merchant_id"])
            )
            token_service.require_agent_or_merchant_token(
                conn,
                conversation["merchant_id"],
                agent_id,
                _payload_token(payload),
            )
        elif sender == "operator":
            token_service.require_merchant_token(conn, conversation["merchant_id"], _payload_token(payload))
        else:
            raise ValidationError(f"Unknown conversation sender: {sender}")
        result = conversation_service.append_conversation_message(
            conn,
            conversation,
            conversation_id,
            sender=sender,
            intent=str(require_field(payload, "intent")),
            text=str(require_field(payload, "text")),
            structured_payload=structured_payload,
            status=status,
        )
        return {"ok": True, **result}


def close_conversation(db_path: str | Path, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        conversation = conversation_summary(conn, conversation_id)
        sender = str(payload.get("sender") or "operator")
        if sender in {"buyer", "buyer_cli"}:
            token_service.require_buyer_conversation_token(conn, conversation, _payload_token(payload))
        elif sender == "merchant":
            token_service.require_merchant_token(conn, conversation["merchant_id"], _payload_token(payload))
        elif sender == "merchant_agent":
            agent_id = str(payload.get("source_id") or token_service.default_merchant_agent_id(conversation["merchant_id"]))
            token_service.require_agent_or_merchant_token(conn, conversation["merchant_id"], agent_id, _payload_token(payload))
        elif sender == "operator":
            token_service.require_merchant_token(conn, conversation["merchant_id"], _payload_token(payload))
        else:
            raise ValidationError(f"Unknown conversation sender: {sender}")
        conversation = conversation_service.close_conversation(
            conn,
            conversation,
            conversation_id,
            sender=sender,
            intent=str(payload.get("intent") or "support"),
            text=str(payload.get("text") or ""),
            source_id=str(payload.get("source_id") or ""),
        )
        return {"ok": True, "conversation": conversation}


def append_conversation_closed_audit(
    conn: Any,
    conversation_id: str,
    actor: str,
    next_actor: str,
    details: dict[str, Any] | None = None,
) -> None:
    conversation_service.append_conversation_closed_audit(conn, conversation_id, actor, next_actor, details)


def conversation_list(
    db_path: str | Path,
    filters: dict[str, Any],
    payload: dict[str, Any],
    owner_kind: str,
    owner_id: str,
) -> dict[str, Any]:
    clauses: list[str] = []
    values: list[Any] = []
    for column in ("status", "merchant_id", "buyer_id", "sku"):
        if filters.get(column):
            clauses.append(f"{column} = ?")
            values.append(str(filters[column]))
    if filters.get("updated_since"):
        clauses.append("updated_at >= ?")
        values.append(str(filters["updated_since"]))
    with db_session(db_path) as conn:
        if owner_kind == "buyer":
            token_row = token_service.require_buyer_read_token(conn, owner_id, _payload_token(payload))
            if token_row["conversation_id"]:
                clauses.append("id = ?")
                values.append(str(token_row["conversation_id"]))
        elif owner_kind == "merchant":
            token_service.require_merchant_read_token(conn, owner_id, _payload_token(payload))
        else:
            raise AuthError("conversation list owner is required")
        limit = result_limit(filters.get("limit"))
        offset = result_offset(filters.get("offset"))
        if str(filters.get("include") or "").lower() == "details":
            results = conversation_service.list_conversation_details(
                conn, clauses=clauses, values=values, limit=limit, offset=offset
            )
        else:
            results = conversation_service.list_conversation_summaries(
                conn, clauses=clauses, values=values, limit=limit, offset=offset
            )
        return {"ok": True, "conversations": results}


def merchant_conversations(
    db_path: str | Path,
    merchant_id: str,
    payload: dict[str, Any],
    status: str = "",
    limit: Any = DEFAULT_RESULT_LIMIT,
    offset: Any = 0,
    include: Any = "",
) -> dict[str, Any]:
    with db_session(db_path) as conn:
        token_service.require_merchant_read_token(conn, merchant_id, _payload_token(payload))
        return {
            "ok": True,
            "merchant_id": merchant_id,
            "conversations": merchant_conversation_summaries(
                conn,
                merchant_id,
                status,
                limit=result_limit(limit),
                offset=result_offset(offset),
                summary_only=str(include or "").lower() != "details",
            ),
        }

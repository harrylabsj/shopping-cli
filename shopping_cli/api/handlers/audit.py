"""Audit API handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shopping_cli.api import auth as api_auth
from shopping_cli.api.handlers.common import positive_whole_int, result_offset
from shopping_cli.core.conversations import conversation_summary
from shopping_cli.core.errors import AuthError
from shopping_cli.core.harness import append_audit_event
from shopping_cli.db.session import db_session
from shopping_cli.services import audit as audit_service
from shopping_cli.services import tokens as token_service


MAX_AUDIT_EVENT_LIMIT = 200


def _payload_token(payload: dict[str, Any]) -> str:
    return api_auth.payload_token(payload)


def audit_event_limit(value: Any) -> int:
    if value in (None, ""):
        return 50
    return min(positive_whole_int(value, "limit"), MAX_AUDIT_EVENT_LIMIT)


def record_tool_call_audit(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    conversation_id = str(payload.get("conversation_id") or "")
    with db_session(db_path) as conn:
        if conversation_id:
            conversation = conversation_summary(conn, conversation_id)
            row = token_service.require_api_token(conn, _payload_token(payload), "merchant or agent audit token required")
            if row["role"] == "merchant" and row["merchant_id"] == conversation["merchant_id"]:
                actor = row["merchant_id"]
                token_scope = "merchant"
            elif row["role"] == "agent" and row["merchant_id"] == conversation["merchant_id"]:
                actor = row["agent_id"] or token_service.default_merchant_agent_id(conversation["merchant_id"])
                token_scope = "merchant_agent"
            else:
                raise AuthError("merchant or agent audit token required")
        else:
            row = token_service.require_api_token(conn, _payload_token(payload), "merchant or agent audit token required")
            if row["role"] == "merchant":
                actor = row["merchant_id"]
                token_scope = "merchant"
            elif row["role"] == "agent":
                actor = row["agent_id"] or token_service.default_merchant_agent_id(row["merchant_id"])
                token_scope = "merchant_agent"
            else:
                raise AuthError("merchant or agent audit token required")
        event = append_audit_event(
            conn,
            conversation_id,
            actor,
            "llm_tool_call",
            {
                "tool": str(payload.get("tool") or ""),
                "status": str(payload.get("status") or ""),
                "host": str(payload.get("host") or ""),
                "session_id": str(payload.get("session_id") or ""),
                "actor": actor,
                "source_id": actor,
                "token_scope": token_scope,
                "error": str(payload.get("error") or ""),
            },
        )
        return {"ok": True, "event": event}


def audit_events(
    db_path: str | Path,
    payload: dict[str, Any],
    merchant_id: str = "",
    event: str = "",
    limit: Any = 50,
    offset: Any = 0,
) -> dict[str, Any]:
    with db_session(db_path) as conn:
        resolved_merchant_id = str(merchant_id or payload.get("merchant_id") or "")
        if not resolved_merchant_id:
            raise AuthError("merchant_id is required for audit events")
        token_service.require_merchant_token(conn, resolved_merchant_id, _payload_token(payload))
        return {
            "ok": True,
            "merchant_id": resolved_merchant_id,
            "events": audit_service.merchant_audit_events(
                conn,
                resolved_merchant_id,
                event=event,
                limit=audit_event_limit(limit),
                offset=result_offset(offset),
            ),
        }

"""Agent API handlers."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from shopping_cli.agents import merchant_agent
from shopping_cli.api import auth as api_auth
from shopping_cli.api.handlers.common import (
    DEFAULT_RESULT_LIMIT,
    non_negative_whole_int,
    positive_whole_int,
    require_field,
    result_limit,
    result_offset,
)
from shopping_cli.config import agent_stale_ttl_seconds_from
from shopping_cli.core.conversations import conversation_summary
from shopping_cli.core.errors import AuthError, ValidationError
from shopping_cli.core.harness import (
    abandon_agent_message,
    abandon_stale_agent_messages,
    agent_message_process_summary,
    claim_agent_message,
    complete_agent_message,
    fail_agent_message,
)
from shopping_cli.db.session import db_session
from shopping_cli.services import agents as agent_service
from shopping_cli.services import tokens as token_service


def _payload_token(payload: dict[str, Any]) -> str:
    return api_auth.payload_token(payload)


def _positive_whole_seconds(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{field_name} must be a whole number")
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValidationError(f"{field_name} must be a whole number")
        seconds = int(value)
    else:
        try:
            seconds = int(str(value).strip())
        except ValueError as exc:
            raise ValidationError(f"{field_name} must be a whole number") from exc
    if seconds <= 0:
        raise ValidationError(f"{field_name} must be greater than 0")
    return seconds


def default_merchant_agent_id(merchant_id: str) -> str:
    return token_service.default_merchant_agent_id(merchant_id)


def agent_heartbeat(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id = str(require_field(payload, "merchant_id"))
        agent_id = default_merchant_agent_id(merchant_id)
        token_service.require_agent_or_merchant_token(conn, merchant_id, agent_id, _payload_token(payload))
        agent = merchant_agent.heartbeat(
            conn,
            merchant_id=merchant_id,
            status=str(payload.get("status") or "online"),
            capabilities=payload.get("capabilities"),
            pid=non_negative_whole_int(payload.get("pid"), "pid"),
            version=str(payload.get("version") or ""),
            last_error=str(payload.get("last_error") or ""),
            checked_count=non_negative_whole_int(payload.get("checked_count"), "checked_count"),
            replied_count=non_negative_whole_int(payload.get("replied_count"), "replied_count"),
        )
        return {"ok": True, "agent": agent}


def create_agent_token(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id = str(require_field(payload, "merchant_id"))
        token_service.require_merchant_token(conn, merchant_id, _payload_token(payload))
        return agent_service.issue_agent_token_for_merchant(
            conn,
            merchant_id,
            agent_id=str(payload.get("agent_id") or ""),
            ttl_seconds=payload.get("ttl_seconds"),
            positive_whole_seconds=_positive_whole_seconds,
        )


def list_agent_tokens(
    db_path: str | Path,
    payload: dict[str, Any],
    merchant_id: str = "",
    limit: Any = DEFAULT_RESULT_LIMIT,
    offset: Any = 0,
) -> dict[str, Any]:
    with db_session(db_path) as conn:
        resolved_merchant_id = str(merchant_id or payload.get("merchant_id") or "")
        if not resolved_merchant_id:
            raise ValidationError("merchant_id is required")
        token_service.require_merchant_token(conn, resolved_merchant_id, _payload_token(payload))
        return agent_service.list_agent_tokens(
            conn,
            resolved_merchant_id,
            limit=result_limit(limit),
            offset=result_offset(offset),
        )


def revoke_agent_token(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id = str(require_field(payload, "merchant_id"))
        token_service.require_merchant_token(conn, merchant_id, _payload_token(payload))
        return agent_service.revoke_agent_token(
            conn,
            merchant_id,
            token=payload.get("token"),
            token_prefix=payload.get("token_prefix"),
        )


def rotate_agent_token(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id = str(require_field(payload, "merchant_id"))
        token_service.require_merchant_token(conn, merchant_id, _payload_token(payload))
        return agent_service.rotate_agent_token(
            conn,
            merchant_id,
            token=payload.get("token"),
            token_prefix=payload.get("token_prefix"),
            ttl_seconds=payload.get("ttl_seconds"),
            positive_whole_seconds=_positive_whole_seconds,
        )


def require_agent_payload(conn: Any, payload: dict[str, Any]) -> tuple[str, str]:
    merchant_id = str(require_field(payload, "merchant_id"))
    agent_id = str(payload.get("agent_id") or default_merchant_agent_id(merchant_id))
    token_service.require_agent_or_merchant_token(conn, merchant_id, agent_id, _payload_token(payload))
    return merchant_id, agent_id


def require_agent_conversation(conn: Any, merchant_id: str, conversation_id: str) -> dict[str, Any]:
    conversation = conversation_summary(conn, conversation_id)
    if conversation["merchant_id"] != merchant_id:
        raise AuthError(f"Merchant {merchant_id} cannot access conversation {conversation_id}")
    return conversation


def require_message_in_conversation(conn: Any, conversation_id: str, message_id: int) -> None:
    agent_service.validate_buyer_message_for_claim(conn, conversation_id, message_id)


def require_agent_process_scope(conn: Any, merchant_id: str, agent_id: str, message_id: int) -> dict[str, Any]:
    process = agent_message_process_summary(conn, agent_id, message_id)
    require_agent_conversation(conn, merchant_id, process["conversation_id"])
    return process


def claim_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id, agent_id = require_agent_payload(conn, payload)
        conversation_id = str(require_field(payload, "conversation_id"))
        require_agent_conversation(conn, merchant_id, conversation_id)
        message_id = positive_whole_int(require_field(payload, "message_id"), "message_id")
        require_message_in_conversation(conn, conversation_id, message_id)
        # 回合/乱序防护（与 negotiation claim 一致）：只能 claim 处于
        # waiting_merchant 且是**最新**买家消息——否则回答会落在旧问题上，
        # 更新的买家消息还会被再次派发（跨消息跳答）。
        conversation = conversation_summary(conn, conversation_id)
        if str(conversation["status"] or "") != "waiting_merchant":
            raise ConflictError(
                f"Conversation {conversation_id} is not waiting for the merchant agent "
                f"(status={conversation['status']!r})"
            )
        latest_row = conn.execute(
            "select id from messages where conversation_id = ? and sender = 'buyer' "
            "order by id desc limit 1",
            (conversation_id,),
        ).fetchone()
        if latest_row is None or int(latest_row["id"]) != message_id:
            raise ConflictError(f"Message {message_id} is not the latest buyer message")
        claim = claim_agent_message(
            conn,
            agent_id,
            conversation_id,
            message_id,
            str(require_field(payload, "idempotency_key")),
        )
        return {"ok": True, "claim": claim}


def complete_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id, agent_id = require_agent_payload(conn, payload)
        message_id = positive_whole_int(require_field(payload, "message_id"), "message_id")
        require_agent_process_scope(conn, merchant_id, agent_id, message_id)
        process = complete_agent_message(conn, agent_id, message_id)
        return {"ok": True, "process": process}


def fail_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id, agent_id = require_agent_payload(conn, payload)
        message_id = positive_whole_int(require_field(payload, "message_id"), "message_id")
        require_agent_process_scope(conn, merchant_id, agent_id, message_id)
        process = fail_agent_message(conn, agent_id, message_id, str(payload.get("error") or "agent failure"))
        return {"ok": True, "process": process}


def abandon_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id, agent_id = require_agent_payload(conn, payload)
        message_id = positive_whole_int(require_field(payload, "message_id"), "message_id")
        require_agent_process_scope(conn, merchant_id, agent_id, message_id)
        process = abandon_agent_message(conn, agent_id, message_id, str(payload.get("error") or "agent abandoned claim"))
        return {"ok": True, "process": process}


def abandon_stale_messages(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        _merchant_id, agent_id = require_agent_payload(conn, payload)
        abandoned = abandon_stale_agent_messages(
            conn,
            agent_id,
            stale_after_seconds=(
                _positive_whole_seconds(payload.get("stale_after_seconds", 300), "stale_after_seconds") or 300
            ),
        )
        return {"ok": True, "abandoned": abandoned}


def list_agents(
    db_path: str | Path,
    payload: dict[str, Any],
    owner_id: str = "",
    limit: Any = DEFAULT_RESULT_LIMIT,
    offset: Any = 0,
) -> dict[str, Any]:
    with db_session(db_path) as conn:
        scoped_owner_id = str(owner_id or "")
        if scoped_owner_id:
            token_service.require_merchant_read_token(conn, scoped_owner_id, _payload_token(payload))
        else:
            token_row = token_service.require_api_token(conn, _payload_token(payload), "agent read token required")
            if token_row["role"] not in {"merchant", "agent"} or not token_row["merchant_id"]:
                raise AuthError("invalid agent read token")
            scoped_owner_id = str(token_row["merchant_id"])
        return {
            "ok": True,
            "agents": agent_service.list_agent_summaries(
                conn,
                owner_id=scoped_owner_id,
                limit=result_limit(limit),
                offset=result_offset(offset),
                include_stale=True,
                stale_ttl_seconds=agent_stale_ttl_seconds_from(),
            ),
        }


def get_agent(db_path: str | Path, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        summary = agent_service.get_agent_summary(
            conn,
            agent_id,
            include_stale=True,
            stale_ttl_seconds=agent_stale_ttl_seconds_from(),
        )
        token_service.require_merchant_read_token(conn, summary["owner_id"], _payload_token(payload))
        return {"ok": True, "agent": summary}

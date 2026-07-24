"""Agent management services shared by API and CLI transports."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from shopping_cli.core.errors import AuthError, ConflictError, NotFoundError, ValidationError
from shopping_cli.db.session import decode_json, now_iso
from shopping_cli.services import tokens as token_service


def default_merchant_agent_id(merchant_id: str) -> str:
    return token_service.default_merchant_agent_id(merchant_id)


def safe_non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    try:
        number = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(number, 0)


def agent_summary(
    row: Any,
    *,
    include_stale: bool = False,
    stale_ttl_seconds: int | None = None,
) -> dict[str, Any]:
    summary = {
        "id": row["id"],
        "type": row["type"],
        "owner_id": row["owner_id"],
        "status": row["status"],
        "capabilities": decode_json(row["capabilities_json"], []),
        "last_seen_at": row["last_seen_at"],
        "pid": safe_non_negative_int(row["pid"]),
        "version": row["version"],
        "last_error": row["last_error"],
        "checked_count": safe_non_negative_int(row["checked_count"]),
        "replied_count": safe_non_negative_int(row["replied_count"]),
    }
    if include_stale:
        ttl = timedelta(seconds=safe_non_negative_int(stale_ttl_seconds))
        last_seen_at = row["last_seen_at"]
        try:
            stale = datetime.now() - datetime.fromisoformat(last_seen_at) > ttl
        except (TypeError, ValueError):
            stale = True
        summary["stale"] = stale
        summary["stale_ttl_seconds"] = int(ttl.total_seconds())
    return summary


def list_agent_summaries(
    conn: Any,
    *,
    owner_id: str = "",
    limit: int = 50,
    offset: int = 0,
    include_stale: bool = False,
    stale_ttl_seconds: int | None = None,
) -> list[dict[str, Any]]:
    if owner_id:
        rows = conn.execute(
            "select * from agents where owner_id = ? order by id limit ? offset ?",
            (owner_id, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "select * from agents order by id limit ? offset ?",
            (limit, offset),
        ).fetchall()
    return [
        agent_summary(row, include_stale=include_stale, stale_ttl_seconds=stale_ttl_seconds)
        for row in rows
    ]


def get_agent_summary(
    conn: Any,
    agent_id: str,
    *,
    include_stale: bool = False,
    stale_ttl_seconds: int | None = None,
) -> dict[str, Any]:
    row = conn.execute("select * from agents where id = ?", (agent_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown agent: {agent_id}")
    return agent_summary(row, include_stale=include_stale, stale_ttl_seconds=stale_ttl_seconds)


def validate_buyer_message_for_claim(conn: Any, conversation_id: str, message_id: int) -> None:
    """Verify a message exists, belongs to the conversation, and was sent by the buyer."""
    row = conn.execute("select conversation_id, sender from messages where id = ?", (message_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown message: {message_id}")
    if row["conversation_id"] != conversation_id:
        raise ValidationError(f"Message {message_id} does not belong to conversation {conversation_id}")
    if row["sender"] != "buyer":
        raise ValidationError(f"Agent can only claim buyer messages, got {row['sender']}")


def issue_agent_token_for_merchant(
    conn: Any,
    merchant_id: str,
    *,
    agent_id: str = "",
    ttl_seconds: Any = None,
    positive_whole_seconds: Any,
) -> dict[str, Any]:
    resolved_agent_id = str(agent_id or default_merchant_agent_id(merchant_id))
    if resolved_agent_id != default_merchant_agent_id(merchant_id):
        raise AuthError(f"Agent {resolved_agent_id} cannot act for merchant {merchant_id}")
    token, expires_at = token_service.issue_agent_token(
        conn,
        merchant_id,
        resolved_agent_id,
        ttl_seconds,
        positive_whole_seconds=positive_whole_seconds,
    )
    issued = token_service.agent_token_row(conn, token)
    token_service.append_agent_token_audit(
        conn,
        merchant_id,
        "agent_token_issued",
        {"agent_id": resolved_agent_id, "token": token_service.agent_token_summary(issued)},
    )
    return {
        "ok": True,
        "merchant_id": merchant_id,
        "agent_id": resolved_agent_id,
        "agent_token": token,
        "expires_at": expires_at,
    }


def list_agent_tokens(conn: Any, merchant_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    rows = conn.execute(
        """
        select token, token_hash, token_prefix, token_suffix, role, merchant_id, agent_id, created_at, expires_at, revoked_at
        from api_tokens
        where merchant_id = ? and role = 'agent'
        order by created_at desc, token desc
        limit ? offset ?
        """,
        (merchant_id, limit, offset),
    ).fetchall()
    return {"ok": True, "merchant_id": merchant_id, "tokens": [token_service.agent_token_summary(row) for row in rows]}


def revoke_agent_token(conn: Any, merchant_id: str, *, token: Any = "", token_prefix: Any = "") -> dict[str, Any]:
    try:
        resolved_token = token_service.resolve_agent_token(conn, merchant_id, token, token_prefix)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    row = token_service.agent_token_row(conn, resolved_token)
    if row is None or row["role"] != "agent" or row["merchant_id"] != merchant_id:
        raise AuthError("invalid agent token")
    if not row["revoked_at"]:
        revoked_at = now_iso()
        updated = conn.execute(
            "update api_tokens set revoked_at = ? where token = ? and revoked_at = ''",
            (revoked_at, resolved_token),
        )
        if updated.rowcount == 1:
            revoked = token_service.agent_token_row(conn, resolved_token)
            token_service.append_agent_token_audit(
                conn,
                merchant_id,
                "agent_token_revoked",
                {"agent_id": row["agent_id"], "revoked_at": revoked_at, "token": token_service.agent_token_summary(revoked)},
            )
            return {
                "ok": True,
                "revoked": True,
                "merchant_id": merchant_id,
                "agent_id": row["agent_id"],
                "token_role": row["role"],
                "revoked_at": revoked_at,
            }
        # A concurrent transaction revoked the same token first. Fall through to
        # the idempotent response without writing a duplicate revoke audit event.
        row = token_service.agent_token_row(conn, resolved_token)
    return {
        "ok": True,
        "revoked": True,
        "merchant_id": merchant_id,
        "agent_id": row["agent_id"],
        "token_role": row["role"],
        "revoked_at": row["revoked_at"],
    }


def rotate_agent_token(
    conn: Any,
    merchant_id: str,
    *,
    token: Any = "",
    token_prefix: Any = "",
    ttl_seconds: Any = None,
    positive_whole_seconds: Any,
) -> dict[str, Any]:
    try:
        old_token = token_service.resolve_agent_token(conn, merchant_id, token, token_prefix)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    row = token_service.agent_token_row(conn, old_token)
    if row is None or row["role"] != "agent" or row["merchant_id"] != merchant_id:
        raise AuthError("invalid agent token")
    if row["revoked_at"]:
        raise ConflictError("agent token is already revoked or rotated")
    revoked_at = now_iso()
    revoked = conn.execute(
        "update api_tokens set revoked_at = ? where token = ? and revoked_at = ''",
        (revoked_at, old_token),
    )
    if revoked.rowcount != 1:
        raise ConflictError("agent token was rotated or revoked concurrently")
    new_token, expires_at = token_service.issue_agent_token(
        conn,
        merchant_id,
        row["agent_id"],
        ttl_seconds,
        positive_whole_seconds=positive_whole_seconds,
    )
    previous = token_service.agent_token_row(conn, old_token)
    replacement = token_service.agent_token_row(conn, new_token)
    token_service.append_agent_token_audit(
        conn,
        merchant_id,
        "agent_token_rotated",
        {
            "agent_id": row["agent_id"],
            "revoked_at": revoked_at,
            "previous_token": token_service.agent_token_summary(previous),
            "new_token": token_service.agent_token_summary(replacement),
        },
    )
    return {
        "ok": True,
        "rotated": True,
        "merchant_id": merchant_id,
        "agent_id": row["agent_id"],
        "agent_token": new_token,
        "expires_at": expires_at,
        "revoked_at": revoked_at,
        "previous_token": token_service.agent_token_summary(previous),
    }

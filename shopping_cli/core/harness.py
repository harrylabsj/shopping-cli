"""MVP orchestration helpers for routing, idempotency, and audit events."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from shopping_cli.db.session import decode_json, encode_json, now_iso


def next_actor_for_review_reason(reason: str) -> str:
    if reason == "suspicious_content":
        return "operator"
    return "merchant_human"


def next_actor_for_status(status: str, reason: str = "") -> str:
    if status == "human_required":
        return next_actor_for_review_reason(reason)
    return {
        "open": "buyer",
        "waiting_merchant": "merchant_agent",
        "waiting_buyer": "buyer",
        "closed": "",
    }.get(status, "")


def append_audit_event(
    conn: sqlite3.Connection,
    conversation_id: str,
    actor: str,
    event: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cursor = conn.execute(
        """
        insert into audit_events(conversation_id, actor, event, details_json, created_at)
        values (?, ?, ?, ?, ?)
        """,
        (conversation_id, actor, event, encode_json(details or {}), now_iso()),
    )
    return audit_event_summary(conn, int(cursor.lastrowid))


def audit_event_summary_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "actor": row["actor"],
        "event": row["event"],
        "details": decode_json(row["details_json"], {}),
        "created_at": row["created_at"],
    }


def audit_event_summary(conn: sqlite3.Connection, event_id: int) -> dict[str, Any]:
    row = conn.execute("select * from audit_events where id = ?", (event_id,)).fetchone()
    if row is None:
        raise SystemExit(f"Unknown audit event: {event_id}")
    return audit_event_summary_from_row(row)


def conversation_audit_events(conn: sqlite3.Connection, conversation_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select id, conversation_id, actor, event, details_json, created_at
        from audit_events
        where conversation_id = ?
        order by id
        """,
        (conversation_id,),
    ).fetchall()
    return [audit_event_summary_from_row(row) for row in rows]


def message_idempotency_key(agent_id: str, message_id: int) -> str:
    return f"{agent_id}:{message_id}"


PROCESSING_STATUS = "processing"
PROCESSED_STATUS = "processed"
FAILED_STATUS = "failed"
ABANDONED_STATUS = "abandoned"
RETRYABLE_PROCESS_STATUSES = {FAILED_STATUS, ABANDONED_STATUS}
MAX_STALE_TTL_SECONDS = 9_999_999_999


def _safe_non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    try:
        number = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(number, 0)


def _safe_positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return max(default, 1)
    if isinstance(value, float) and not math.isfinite(value):
        return max(default, 1)
    try:
        number = int(value or default)
    except (OverflowError, TypeError, ValueError):
        number = default
    return max(number, 1)


def claim_agent_message(
    conn: sqlite3.Connection,
    agent_id: str,
    conversation_id: str,
    message_id: int,
    idempotency_key: str,
) -> dict[str, Any]:
    now = now_iso()
    row = conn.execute(
        "select * from agent_message_processes where agent_id = ? and message_id = ?",
        (agent_id, message_id),
    ).fetchone()
    if row is not None and row["status"] not in RETRYABLE_PROCESS_STATUSES:
        return {
            "claimed": False,
            "status": row["status"],
            "attempts": _safe_non_negative_int(row["attempts"]),
            "idempotency_key": row["idempotency_key"],
        }

    if row is None:
        attempts = 1
        try:
            conn.execute(
                """
                insert into agent_message_processes(
                    agent_id, message_id, conversation_id, idempotency_key, status,
                    attempts, last_error, created_at, updated_at, processed_at
                )
                values (?, ?, ?, ?, ?, ?, '', ?, ?, '')
                """,
                (agent_id, message_id, conversation_id, idempotency_key, PROCESSING_STATUS, attempts, now, now),
            )
        except sqlite3.IntegrityError:
            current = agent_message_process_summary(conn, agent_id, message_id)
            return {
                "claimed": False,
                "status": current["status"],
                "attempts": current["attempts"],
                "idempotency_key": current["idempotency_key"],
            }
    else:
        attempts = _safe_non_negative_int(row["attempts"]) + 1
        cursor = conn.execute(
            """
            update agent_message_processes
            set conversation_id = ?,
                idempotency_key = ?,
                status = ?,
                attempts = attempts + 1,
                last_error = '',
                updated_at = ?,
                processed_at = ''
            where agent_id = ? and message_id = ? and status in (?, ?)
            """,
            (
                conversation_id,
                idempotency_key,
                PROCESSING_STATUS,
                now,
                agent_id,
                message_id,
                FAILED_STATUS,
                ABANDONED_STATUS,
            ),
        )
        if cursor.rowcount != 1:
            current = agent_message_process_summary(conn, agent_id, message_id)
            return {
                "claimed": False,
                "status": current["status"],
                "attempts": current["attempts"],
                "idempotency_key": current["idempotency_key"],
            }
    append_audit_event(
        conn,
        conversation_id,
        agent_id,
        "agent_message_claimed",
        {"message_id": message_id, "idempotency_key": idempotency_key, "attempts": attempts},
    )
    return {"claimed": True, "status": PROCESSING_STATUS, "attempts": attempts, "idempotency_key": idempotency_key}


def complete_agent_message(conn: sqlite3.Connection, agent_id: str, message_id: int) -> dict[str, Any]:
    now = now_iso()
    cursor = conn.execute(
        """
        update agent_message_processes
        set status = ?, last_error = '', updated_at = ?, processed_at = ?
        where agent_id = ? and message_id = ? and status = ?
        """,
        (PROCESSED_STATUS, now, now, agent_id, message_id, PROCESSING_STATUS),
    )
    process = agent_message_process_summary(conn, agent_id, message_id)
    if cursor.rowcount == 1:
        append_audit_event(
            conn,
            process["conversation_id"],
            agent_id,
            "agent_message_processed",
            {"message_id": message_id, "idempotency_key": process["idempotency_key"], "attempts": process["attempts"]},
        )
    return process


def abandon_agent_message(
    conn: sqlite3.Connection,
    agent_id: str,
    message_id: int,
    error: str,
    reason: str = "explicit_abandon",
) -> dict[str, Any]:
    now = now_iso()
    cursor = conn.execute(
        """
        update agent_message_processes
        set status = ?, last_error = ?, updated_at = ?
        where agent_id = ? and message_id = ? and status = ?
        """,
        (ABANDONED_STATUS, error, now, agent_id, message_id, PROCESSING_STATUS),
    )
    process = agent_message_process_summary(conn, agent_id, message_id)
    if cursor.rowcount == 1:
        append_audit_event(
            conn,
            process["conversation_id"],
            agent_id,
            "agent_message_abandoned",
            {"message_id": message_id, "idempotency_key": process["idempotency_key"], "error": error, "reason": reason},
        )
    return process


def abandon_stale_agent_messages(
    conn: sqlite3.Connection,
    agent_id: str,
    stale_after_seconds: Any = 300,
    now: str | datetime | None = None,
) -> list[dict[str, Any]]:
    current = datetime.fromisoformat(now) if isinstance(now, str) else now or datetime.fromisoformat(now_iso())
    ttl_seconds = _safe_positive_int(stale_after_seconds, 300)
    if ttl_seconds > MAX_STALE_TTL_SECONDS:
        ttl_seconds = 300
    cutoff = current - timedelta(seconds=ttl_seconds)
    rows = conn.execute(
        """
        select message_id from agent_message_processes
        where agent_id = ? and status = ? and updated_at < ?
        order by updated_at, message_id
        """,
        (agent_id, PROCESSING_STATUS, cutoff.isoformat(timespec="seconds")),
    ).fetchall()
    abandoned: list[dict[str, Any]] = []
    for row in rows:
        message_id = int(row["message_id"])
        error = f"stale processing claim abandoned after {ttl_seconds} seconds"
        abandoned_process = abandon_agent_message(conn, agent_id, message_id, error, reason="stale_processing_claim")
        abandoned.append(abandoned_process)
    return abandoned


def fail_agent_message(conn: sqlite3.Connection, agent_id: str, message_id: int, error: str) -> dict[str, Any]:
    now = now_iso()
    cursor = conn.execute(
        """
        update agent_message_processes
        set status = ?, last_error = ?, updated_at = ?
        where agent_id = ? and message_id = ? and status = ?
        """,
        (FAILED_STATUS, error, now, agent_id, message_id, PROCESSING_STATUS),
    )
    process = agent_message_process_summary(conn, agent_id, message_id)
    if cursor.rowcount == 1:
        append_audit_event(
            conn,
            process["conversation_id"],
            agent_id,
            "agent_message_failed",
            {"message_id": message_id, "idempotency_key": process["idempotency_key"], "attempts": process["attempts"], "error": error},
        )
    return process


def agent_message_process_summary(conn: sqlite3.Connection, agent_id: str, message_id: int) -> dict[str, Any]:
    row = conn.execute(
        "select * from agent_message_processes where agent_id = ? and message_id = ?",
        (agent_id, message_id),
    ).fetchone()
    if row is None:
        raise SystemExit(f"Unknown agent message process: {agent_id} {message_id}")
    return {
        "agent_id": row["agent_id"],
        "message_id": row["message_id"],
        "conversation_id": row["conversation_id"],
        "idempotency_key": row["idempotency_key"],
        "status": row["status"],
        "attempts": _safe_non_negative_int(row["attempts"]),
        "last_error": row["last_error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "processed_at": row["processed_at"],
    }

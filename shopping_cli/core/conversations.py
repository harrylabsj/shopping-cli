"""Conversation and message state transitions."""

from __future__ import annotations

import sqlite3
from typing import Any

from shopping_cli.core.catalog import product_summary, require_merchant, require_product
from shopping_cli.core.harness import append_audit_event, conversation_audit_events, next_actor_for_status
from shopping_cli.db.session import decode_json, encode_json, now_iso

CONVERSATION_STATUSES = {"open", "waiting_merchant", "waiting_buyer", "human_required", "closed"}


def next_conversation_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """
        select cast(substr(id, 6) as integer) as max_id
        from conversations
        where id like 'CONV-%'
          and substr(id, 6) <> ''
          and substr(id, 6) not glob '*[^0-9]*'
        order by max_id desc
        limit 1
        """
    ).fetchone()
    max_id = int(row["max_id"]) if row is not None and row["max_id"] is not None else 0
    return f"CONV-{max_id + 1:04d}"


def ensure_conversation(
    conn: sqlite3.Connection,
    buyer_id: str,
    merchant_id: str,
    sku: str = "",
    reuse_open: bool = True,
) -> dict[str, Any]:
    buyer_id = str(buyer_id or "").strip()
    merchant_id = str(merchant_id or "").strip()
    sku = str(sku or "").strip()
    if not buyer_id:
        raise SystemExit("buyer id is required")
    if not merchant_id:
        raise SystemExit("merchant id is required")
    require_merchant(conn, merchant_id)
    if sku:
        product = require_product(conn, sku)
        if product["merchant_id"] != merchant_id:
            raise SystemExit(f"Product {sku} does not belong to merchant {merchant_id}")
    if reuse_open:
        row = conn.execute(
            """
            select * from conversations
            where buyer_id = ? and merchant_id = ? and sku = ? and status != 'closed'
            order by created_at desc
            limit 1
            """,
            (buyer_id, merchant_id, sku),
        ).fetchone()
        if row is not None:
            return conversation_summary(conn, row["id"])
    last_insert_error: sqlite3.IntegrityError | None = None
    conversation_id = ""
    for _attempt in range(3):
        now = now_iso()
        conversation_id = next_conversation_id(conn)
        try:
            conn.execute(
                """
                insert into conversations(
                    id, buyer_id, merchant_id, sku, status, next_actor,
                    created_at, updated_at, last_sender
                )
                values (?, ?, ?, ?, 'open', 'buyer', ?, ?, '')
                """,
                (conversation_id, buyer_id, merchant_id, sku, now, now),
            )
            break
        except sqlite3.IntegrityError as exc:
            if "conversations.id" not in str(exc):
                raise
            last_insert_error = exc
    else:
        raise last_insert_error or SystemExit("Could not create conversation")
    append_audit_event(
        conn,
        conversation_id,
        "system",
        "conversation_created",
        {"buyer_id": buyer_id, "merchant_id": merchant_id, "sku": sku, "next_actor": "buyer"},
    )
    return conversation_summary(conn, conversation_id)


def require_conversation(conn: sqlite3.Connection, conversation_id: str) -> sqlite3.Row:
    row = conn.execute("select * from conversations where id = ?", (conversation_id,)).fetchone()
    if row is None:
        raise SystemExit(f"Unknown conversation: {conversation_id}")
    return row


def require_open_conversation(conn: sqlite3.Connection, conversation_id: str) -> sqlite3.Row:
    row = require_conversation(conn, conversation_id)
    if row["status"] == "closed":
        raise SystemExit(f"Conversation {conversation_id} is closed")
    return row


def _normalize_review_text(value: Any, default: str) -> str:
    return str(value or "").strip() or default


def _normalize_conversation_status(value: Any) -> str:
    status = str(value or "").strip()
    if status not in CONVERSATION_STATUSES:
        raise SystemExit(f"Unknown conversation status: {status or '-'}")
    return status


def normalize_structured_payload(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise SystemExit("structured_payload must be an object")
    return dict(value)


def _safe_non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(number, 0)


def append_message(
    conn: sqlite3.Connection,
    conversation_id: str,
    sender: str,
    intent: str,
    text: str,
    structured_payload: dict[str, Any] | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    conversation = require_open_conversation(conn, conversation_id)
    if not text.strip():
        raise SystemExit("message text is required")
    now = now_iso()
    payload = normalize_structured_payload(structured_payload)
    if status is None:
        if sender == "buyer":
            status = "waiting_merchant"
        elif sender in {"merchant_agent", "merchant"}:
            status = "waiting_buyer"
        else:
            status = conversation["status"]
    status = _normalize_conversation_status(status)
    if status == "human_required":
        payload["reason"] = _normalize_review_text(payload.get("reason"), "human_required")
    next_actor = next_actor_for_status(status, str(payload.get("reason") or ""))
    cursor = conn.execute(
        """
        insert into messages(conversation_id, sender, intent, text, structured_payload_json, created_at)
        values (?, ?, ?, ?, ?, ?)
        """,
        (
            conversation_id,
            sender,
            intent,
            text,
            encode_json(payload),
            now,
        ),
    )
    conn.execute(
        "update conversations set status = ?, next_actor = ?, updated_at = ?, last_sender = ? where id = ?",
        (status, next_actor, now, sender, conversation_id),
    )
    append_audit_event(
        conn,
        conversation_id,
        sender,
        "message_appended",
        {
            "message_id": int(cursor.lastrowid),
            "intent": intent,
            "status": status,
            "next_actor": next_actor,
            "source_id": payload.get("source_id", ""),
        },
    )
    return message_summary(conn, int(cursor.lastrowid))


def add_flag(
    conn: sqlite3.Connection,
    conversation_id: str,
    reason: str,
    severity: str = "review",
    sku: str = "",
) -> dict[str, Any]:
    require_open_conversation(conn, conversation_id)
    reason = _normalize_review_text(reason, "human_required")
    severity = _normalize_review_text(severity, "review")
    sku = str(sku or "").strip()
    now = now_iso()
    cursor = conn.execute(
        """
        insert into moderation_flags(conversation_id, sku, reason, severity, created_at)
        values (?, ?, ?, ?, ?)
        """,
        (conversation_id, sku, reason, severity, now),
    )
    append_audit_event(
        conn,
        conversation_id,
        "system",
        "human_review_flagged",
        {"reason": reason, "severity": severity, "sku": sku, "next_actor": next_actor_for_status("human_required", reason)},
    )
    return flag_summary(conn, int(cursor.lastrowid))


def message_summary(conn: sqlite3.Connection, message_id: int) -> dict[str, Any]:
    row = conn.execute("select * from messages where id = ?", (message_id,)).fetchone()
    if row is None:
        raise SystemExit(f"Unknown message: {message_id}")
    return message_summary_from_row(row)


def message_summary_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "sender": row["sender"],
        "intent": row["intent"],
        "text": row["text"],
        "structured_payload": decode_json(row["structured_payload_json"], {}),
        "created_at": row["created_at"],
    }


def flag_summary(conn: sqlite3.Connection, flag_id: int) -> dict[str, Any]:
    row = conn.execute("select * from moderation_flags where id = ?", (flag_id,)).fetchone()
    if row is None:
        raise SystemExit(f"Unknown moderation flag: {flag_id}")
    return flag_summary_from_row(row)


def flag_summary_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "sku": row["sku"],
        "reason": row["reason"],
        "severity": row["severity"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"] or None,
        "resolution": row["resolution"],
        "resolved_by": row["resolved_by"],
    }


def conversation_messages(conn: sqlite3.Connection, conversation_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "select * from messages where conversation_id = ? order by id",
        (conversation_id,),
    ).fetchall()
    return [message_summary_from_row(row) for row in rows]


def conversation_flags(conn: sqlite3.Connection, conversation_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "select * from moderation_flags where conversation_id = ? order by id",
        (conversation_id,),
    ).fetchall()
    return [flag_summary_from_row(row) for row in rows]


def conversation_summary(conn: sqlite3.Connection, conversation_id: str) -> dict[str, Any]:
    row = require_conversation(conn, conversation_id)
    summary: dict[str, Any] = {
        "id": row["id"],
        "buyer_id": row["buyer_id"],
        "merchant_id": row["merchant_id"],
        "sku": row["sku"],
        "status": row["status"],
        "next_actor": row["next_actor"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_sender": row["last_sender"],
        "messages": conversation_messages(conn, conversation_id),
        "flags": conversation_flags(conn, conversation_id),
        "audit_events": conversation_audit_events(conn, conversation_id),
    }
    if row["sku"]:
        try:
            summary["product"] = product_summary(conn, row["sku"])
        except SystemExit:
            pass
    return summary


def merchant_conversations(
    conn: sqlite3.Connection,
    merchant_id: str,
    status: str = "",
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    require_merchant(conn, merchant_id)
    values: list[Any] = [merchant_id]
    if status:
        sql = "select id from conversations where merchant_id = ? and status = ? order by updated_at desc"
        values.append(status)
    else:
        sql = "select id from conversations where merchant_id = ? order by updated_at desc"
    if limit is not None:
        sql += " limit ? offset ?"
        values.extend([_safe_non_negative_int(limit), _safe_non_negative_int(offset)])
    rows = conn.execute(sql, values).fetchall()
    return [conversation_summary(conn, row["id"]) for row in rows]


def waiting_merchant_conversations(conn: sqlite3.Connection, merchant_id: str) -> list[dict[str, Any]]:
    return merchant_conversations(conn, merchant_id, "waiting_merchant")

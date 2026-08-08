"""Conversation and message state transitions."""

from __future__ import annotations

import sqlite3
from typing import Any

from shopping_cli.core.catalog import product_summary, public_product_summary, require_merchant, require_product
from shopping_cli.core.errors import ConflictError, NotFoundError, ValidationError
from shopping_cli.core.harness import append_audit_event, conversation_audit_events, next_actor_for_status
from shopping_cli.db.session import decode_json, encode_json, now_iso
from shopping_cli.core.tokens import token_digest
from shopping_cli.core.limits import MAX_SHORT_TEXT_CHARS, bounded_text, safe_non_negative_int as _safe_non_negative_int

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
        raise ValidationError("buyer id is required")
    if not merchant_id:
        raise ValidationError("merchant id is required")
    require_merchant(conn, merchant_id)
    if sku:
        product = require_product(conn, sku)
        if product["merchant_id"] != merchant_id:
            raise ValidationError(f"Product {sku} does not belong to merchant {merchant_id}")
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
    reuse_key = token_digest(f"{buyer_id}\n{merchant_id}\n{sku}") if reuse_open else ""
    last_insert_error: sqlite3.IntegrityError | None = None
    conversation_id = ""
    for _attempt in range(3):
        now = now_iso()
        conversation_id = next_conversation_id(conn)
        try:
            conn.execute(
                """
                insert into conversations(
                    id, buyer_id, merchant_id, sku, reuse_key, status, next_actor,
                    created_at, updated_at, last_sender
                )
                values (?, ?, ?, ?, ?, 'open', 'buyer', ?, ?, '')
                """,
                (conversation_id, buyer_id, merchant_id, sku, reuse_key, now, now),
            )
            break
        except sqlite3.IntegrityError as exc:
            existing = conn.execute(
                """
                select id from conversations
                where buyer_id = ? and merchant_id = ? and sku = ? and status != 'closed'
                order by updated_at desc, id desc limit 1
                """,
                (buyer_id, merchant_id, sku),
            ).fetchone()
            if existing is not None:
                if reuse_open:
                    return conversation_summary(conn, existing["id"])
                raise ConflictError(
                    f"Open conversation already exists for buyer {buyer_id}, merchant {merchant_id}, sku {sku or '-'}"
                ) from exc
            if "conversations.id" not in str(exc):
                raise
            last_insert_error = exc
    else:
        if last_insert_error is not None:
            raise last_insert_error
        raise ConflictError("Could not create conversation")
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
        raise NotFoundError(f"Unknown conversation: {conversation_id}")
    return row


def require_open_conversation(conn: sqlite3.Connection, conversation_id: str) -> sqlite3.Row:
    row = require_conversation(conn, conversation_id)
    if row["status"] == "closed":
        raise ConflictError(f"Conversation {conversation_id} is closed")
    return row


def _normalize_review_text(value: Any, default: str) -> str:
    return str(value or "").strip() or default


def _normalize_conversation_status(value: Any) -> str:
    status = str(value or "").strip()
    if status not in CONVERSATION_STATUSES:
        raise ValidationError(f"Unknown conversation status: {status or '-'}")
    return status


def normalize_structured_payload(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ValidationError("structured_payload must be an object")
    return dict(value)



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
    sender = bounded_text(sender, "message sender", MAX_SHORT_TEXT_CHARS).strip()
    intent = bounded_text(intent, "message intent", MAX_SHORT_TEXT_CHARS).strip()
    text = bounded_text(text, "message text")
    if not text.strip():
        raise ValidationError("message text is required")
    now = now_iso()
    payload = normalize_structured_payload(structured_payload)
    unresolved_review = conn.execute(
        "select 1 from moderation_flags where conversation_id = ? and resolved_at = '' limit 1",
        (conversation_id,),
    ).fetchone()
    if unresolved_review is not None:
        if status == "closed":
            raise ConflictError(f"Conversation {conversation_id} has unresolved human review")
        status = "human_required"
    elif status is None:
        if sender == "buyer":
            status = "waiting_merchant"
        elif sender in {"merchant_agent", "merchant"}:
            status = "waiting_buyer"
        else:
            status = conversation["status"]
    status = _normalize_conversation_status(status)
    # 状态转移表：merchant 侧发送者只能推进到 waiting_buyer / human_required /
    # closed。waiting_merchant 会重新武装 resident 队列（feedback 循环），
    # open 会把会话卡死在无队列状态——两者都破坏路由/谈判完整性（H4 系）。
    # closed 走显式 close 路径（含 unresolved-review 检查）；buyer 侧仍不
    # 允许显式设置状态（handler 层拦截）。
    if sender in {"merchant", "merchant_agent"}:
        if status not in {"waiting_buyer", "human_required", "closed"}:
            raise ValidationError(
                f"merchant senders may only set status to waiting_buyer, human_required "
                f"or closed (got {status!r})"
            )
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
    if cursor.lastrowid is None:
        raise RuntimeError("message insert did not return an id")
    message_id = cursor.lastrowid
    # When closing, atomically verify no unresolved reviews remain so a
    # concurrent add_flag cannot slip a flag between the Python-level
    # check and this UPDATE (TOCTOU).
    if status == "closed":
        updated = conn.execute(
            """
            update conversations
            set status = ?, next_actor = ?, updated_at = ?, last_sender = ?
            where id = ? and status = ? and status != 'closed'
              and not exists (
                  select 1 from moderation_flags
                  where conversation_id = ? and resolved_at = ''
              )
            """,
            (status, next_actor, now, sender, conversation_id, conversation["status"], conversation_id),
        )
    else:
        updated = conn.execute(
            """
            update conversations
            set status = ?, next_actor = ?, updated_at = ?, last_sender = ?
            where id = ? and status = ? and status != 'closed'
            """,
            (status, next_actor, now, sender, conversation_id, conversation["status"]),
        )
    if updated.rowcount != 1:
        raise ConflictError(f"Conversation {conversation_id} changed concurrently")
    append_audit_event(
        conn,
        conversation_id,
        sender,
        "message_appended",
        {
            "message_id": message_id,
            "intent": intent,
            "status": status,
            "next_actor": next_actor,
            "source_id": payload.get("source_id", ""),
        },
    )
    return message_summary(conn, message_id)


def add_flag(
    conn: sqlite3.Connection,
    conversation_id: str,
    reason: str,
    severity: str = "review",
    sku: str = "",
) -> dict[str, Any]:
    conversation = require_open_conversation(conn, conversation_id)
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
    if cursor.lastrowid is None:
        raise RuntimeError("moderation flag insert did not return an id")
    flag_id = cursor.lastrowid
    next_actor = next_actor_for_status("human_required", reason)
    updated = conn.execute(
        """
        update conversations
        set status = 'human_required', next_actor = ?, updated_at = ?, last_sender = 'system'
        where id = ? and status = ? and status != 'closed'
        """,
        (next_actor, now, conversation_id, conversation["status"]),
    )
    if updated.rowcount != 1:
        raise ConflictError(f"Conversation {conversation_id} changed concurrently")
    append_audit_event(
        conn,
        conversation_id,
        "system",
        "human_review_flagged",
        {"reason": reason, "severity": severity, "sku": sku, "next_actor": next_actor},
    )
    return flag_summary(conn, flag_id)


def message_summary(conn: sqlite3.Connection, message_id: int) -> dict[str, Any]:
    row = conn.execute("select * from messages where id = ?", (message_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown message: {message_id}")
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
        raise NotFoundError(f"Unknown moderation flag: {flag_id}")
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
            # 公开投影：product_summary 内嵌完整 merchant_summary（含
            # contact / automation_boundaries 底价）——买家可见路径必须剥离。
            summary["product"] = public_product_summary(product_summary(conn, row["sku"]))
        except NotFoundError:
            pass
    return summary


def conversation_list_summary_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    conversation_id = str(row["id"])
    row_keys = set(row.keys())
    counts = row if {"message_count", "unresolved_flag_count", "audit_event_count"} <= row_keys else conn.execute(
        """
        select
            count(distinct m.id) as message_count,
            count(distinct case when f.resolved_at = '' then f.id end) as unresolved_flag_count,
            count(distinct e.id) as audit_event_count
        from conversations c
        left join messages m on m.conversation_id = c.id
        left join moderation_flags f on f.conversation_id = c.id
        left join audit_events e on e.conversation_id = c.id
        where c.id = ?
        group by c.id
        """,
        (conversation_id,),
    ).fetchone()
    return {
        "id": row["id"],
        "buyer_id": row["buyer_id"],
        "merchant_id": row["merchant_id"],
        "sku": row["sku"],
        "status": row["status"],
        "next_actor": row["next_actor"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_sender": row["last_sender"],
        "message_count": _safe_non_negative_int(counts["message_count"] if counts else 0),
        "unresolved_flag_count": _safe_non_negative_int(counts["unresolved_flag_count"] if counts else 0),
        "audit_event_count": _safe_non_negative_int(counts["audit_event_count"] if counts else 0),
    }


def conversation_list_summary(conn: sqlite3.Connection, conversation_id: str) -> dict[str, Any]:
    return conversation_list_summary_from_row(conn, require_conversation(conn, conversation_id))


def conversation_details_batch(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    include_flags: bool = False,
) -> list[dict[str, Any]]:
    """Build detail dicts (summary fields plus messages) with a constant number of queries.

    Unlike conversation_summary(), this never loads audit events, and only loads
    flags when include_flags is set (one batched SELECT), so a bounded page of
    conversations costs O(1) queries instead of per-conversation lookups.
    """
    rows = list(rows)
    ids = [str(row["id"]) for row in rows]
    placeholders = ", ".join("?" for _ in ids)
    messages_by_id: dict[str, list[dict[str, Any]]] = {conversation_id: [] for conversation_id in ids}
    flags_by_id: dict[str, list[dict[str, Any]]] = {conversation_id: [] for conversation_id in ids}
    if ids:
        message_rows = conn.execute(
            f"select * from messages where conversation_id in ({placeholders}) order by id",
            ids,
        ).fetchall()
        for message_row in message_rows:
            messages_by_id[str(message_row["conversation_id"])].append(message_summary_from_row(message_row))
        if include_flags:
            flag_rows = conn.execute(
                f"select * from moderation_flags where conversation_id in ({placeholders}) order by id",
                ids,
            ).fetchall()
            for flag_row in flag_rows:
                flags_by_id[str(flag_row["conversation_id"])].append(flag_summary_from_row(flag_row))
    details: list[dict[str, Any]] = []
    for row in rows:
        detail = conversation_list_summary_from_row(conn, row)
        detail["messages"] = messages_by_id[str(row["id"])]
        if include_flags:
            detail["flags"] = flags_by_id[str(row["id"])]
        details.append(detail)
    return details


def merchant_conversations(
    conn: sqlite3.Connection,
    merchant_id: str,
    status: str = "",
    limit: int | None = None,
    offset: int = 0,
    summary_only: bool = True,
    include_flags: bool = False,
) -> list[dict[str, Any]]:
    require_merchant(conn, merchant_id)
    values: list[Any] = [merchant_id]
    projection = """select c.*,
        (select count(*) from messages m where m.conversation_id = c.id) as message_count,
        (select count(*) from moderation_flags f where f.conversation_id = c.id and f.resolved_at = '') as unresolved_flag_count,
        (select count(*) from audit_events e where e.conversation_id = c.id) as audit_event_count
        from conversations c"""
    if status:
        sql = projection + " where c.merchant_id = ? and c.status = ? order by c.updated_at desc"
        values.append(status)
    else:
        sql = projection + " where c.merchant_id = ? order by c.updated_at desc"
    if limit is not None:
        sql += " limit ? offset ?"
        values.extend([_safe_non_negative_int(limit), _safe_non_negative_int(offset)])
    rows = conn.execute(sql, values).fetchall()
    if summary_only:
        return [conversation_list_summary_from_row(conn, row) for row in rows]
    return conversation_details_batch(conn, rows, include_flags=include_flags)


MAX_WAITING_MERCHANT_LIMIT = 100


def waiting_merchant_conversations(conn: sqlite3.Connection, merchant_id: str, limit: int = MAX_WAITING_MERCHANT_LIMIT) -> list[dict[str, Any]]:
    bounded_limit = min(_safe_non_negative_int(limit), MAX_WAITING_MERCHANT_LIMIT)
    return merchant_conversations(conn, merchant_id, "waiting_merchant", limit=bounded_limit, summary_only=False)

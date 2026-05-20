"""Channel ingress helpers for external buyer messages."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from shopping_cli.core.catalog import search_products
from shopping_cli.core.conversations import append_message, conversation_summary, ensure_conversation, message_summary
from shopping_cli.core.harness import append_audit_event
from shopping_cli.core.risk import infer_intent
from shopping_cli.db.session import now_iso

MVP_WARNINGS = [
    "MVP records consultation only; no order is created.",
    "No stock is reserved by shopping-cli.",
    "Payment, refund, escrow, and delivery-success handling are outside this version.",
]

PROCESSING_STATUS = "processing"
PROCESSED_STATUS = "processed"
DEFAULT_PROCESSING_STALE_SECONDS = 300


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


def normalize_channel(channel: str) -> str:
    return str(channel or "").strip().lower()


def channel_buyer_id(channel: str, external_user_id: str) -> str:
    channel = normalize_channel(channel)
    external_user_id = str(external_user_id or "").strip()
    if not channel:
        raise SystemExit("channel is required")
    if not external_user_id:
        raise SystemExit("external_user_id is required")
    return f"{channel}:{external_user_id}"


def _channel_payload(
    channel: str,
    external_user_id: str,
    source_id: str,
    city: str = "",
    area: str = "",
    selected_sku: str = "",
    external_message_id: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "source_id": source_id,
        "channel": channel,
        "external_user_id": external_user_id,
    }
    if city:
        payload["city"] = city
    if area:
        payload["area"] = area
    if selected_sku:
        payload["selected_sku"] = selected_sku
    if external_message_id:
        payload["external_message_id"] = external_message_id
    return payload


def _record_ingress_replay(conn: sqlite3.Connection, row: sqlite3.Row, channel: str) -> None:
    conversation_id = str(row["conversation_id"] or "")
    message_id = _safe_non_negative_int(row["message_id"])
    if not conversation_id or message_id <= 0:
        return
    conn.execute(
        """
        update channel_message_ingresses
        set updated_at = ?
        where channel = ? and external_user_id = ? and external_message_id = ?
        """,
        (now_iso(), channel, row["external_user_id"], row["external_message_id"]),
    )
    append_audit_event(
        conn,
        conversation_id,
        f"channel:{channel}",
        "channel_message_replayed",
        {
            "channel": channel,
            "external_user_id": row["external_user_id"],
            "external_message_id": row["external_message_id"],
            "message_id": message_id,
        },
    )


def _existing_ingress_response(conn: sqlite3.Connection, row: sqlite3.Row, buyer_id: str, channel: str) -> dict[str, Any]:
    if row["status"] != PROCESSED_STATUS:
        raise SystemExit(
            f"Channel message {row['external_message_id']} is already being processed for {channel}:{row['external_user_id']}"
        )
    message_id = _safe_non_negative_int(row["message_id"])
    if message_id <= 0:
        return {
            "ok": True,
            "idempotent": True,
            "buyer_id": buyer_id,
            "channel": channel,
            "candidates": [],
            "conversation": None,
            "warnings": ["No matching merchant or product found.", *MVP_WARNINGS],
            "missing_facts": ["merchant", "product"],
        }
    conversation_id = str(row["conversation_id"])
    message = message_summary(conn, message_id)
    _record_ingress_replay(conn, row, channel)
    return {
        "ok": True,
        "idempotent": True,
        "buyer_id": buyer_id,
        "channel": channel,
        "conversation": conversation_summary(conn, conversation_id),
        "message": message,
        "warnings": MVP_WARNINGS,
    }


def _processing_ingress_is_stale(row: sqlite3.Row, stale_after_seconds: int = DEFAULT_PROCESSING_STALE_SECONDS) -> bool:
    try:
        updated_at = datetime.fromisoformat(str(row["updated_at"] or ""))
    except (TypeError, ValueError):
        return True
    try:
        current = datetime.now(tz=updated_at.tzinfo) if updated_at.tzinfo is not None else datetime.now()
    except TypeError:
        return True
    return current - updated_at > timedelta(seconds=max(int(stale_after_seconds), 1))


def _begin_channel_ingress(
    conn: sqlite3.Connection,
    channel: str,
    external_user_id: str,
    external_message_id: str,
    buyer_id: str,
) -> dict[str, Any] | None:
    if not external_message_id:
        return None
    row = conn.execute(
        """
        select * from channel_message_ingresses
        where channel = ? and external_user_id = ? and external_message_id = ?
        """,
        (channel, external_user_id, external_message_id),
    ).fetchone()
    if row is not None:
        if row["status"] == PROCESSING_STATUS and _processing_ingress_is_stale(row):
            conn.execute(
                """
                delete from channel_message_ingresses
                where channel = ? and external_user_id = ? and external_message_id = ?
                """,
                (channel, external_user_id, external_message_id),
            )
        else:
            return _existing_ingress_response(conn, row, buyer_id, channel)
    now = now_iso()
    try:
        conn.execute(
            """
            insert into channel_message_ingresses(
                channel, external_user_id, external_message_id, status,
                conversation_id, message_id, created_at, updated_at
            )
            values (?, ?, ?, ?, '', 0, ?, ?)
            """,
            (channel, external_user_id, external_message_id, PROCESSING_STATUS, now, now),
        )
    except sqlite3.IntegrityError:
        row = conn.execute(
            """
            select * from channel_message_ingresses
            where channel = ? and external_user_id = ? and external_message_id = ?
            """,
            (channel, external_user_id, external_message_id),
        ).fetchone()
        if row is not None:
            return _existing_ingress_response(conn, row, buyer_id, channel)
        raise
    return None


def _complete_channel_ingress(
    conn: sqlite3.Connection,
    channel: str,
    external_user_id: str,
    external_message_id: str,
    conversation_id: str,
    message_id: int,
) -> None:
    if not external_message_id:
        return
    conn.execute(
        """
        update channel_message_ingresses
        set status = ?, conversation_id = ?, message_id = ?, updated_at = ?
        where channel = ? and external_user_id = ? and external_message_id = ?
        """,
        (PROCESSED_STATUS, conversation_id, int(message_id), now_iso(), channel, external_user_id, external_message_id),
    )


def ingest_buyer_message(
    conn: sqlite3.Connection,
    channel: str,
    external_user_id: str,
    text: str,
    city: str = "",
    area: str = "",
    conversation_id: str = "",
    external_message_id: str = "",
    limit: int = 3,
) -> dict[str, Any]:
    channel = normalize_channel(channel)
    external_user_id = str(external_user_id or "").strip()
    text = str(text or "")
    if not text.strip():
        raise SystemExit("text is required")

    resolved_buyer_id = channel_buyer_id(channel, external_user_id)
    source_id = f"channel:{channel}"
    external_message_id = str(external_message_id or "").strip()
    if conversation_id:
        existing = _begin_channel_ingress(conn, channel, external_user_id, external_message_id, resolved_buyer_id)
        if existing is not None:
            return existing
        conversation = conversation_summary(conn, conversation_id)
        if conversation["buyer_id"] != resolved_buyer_id:
            raise SystemExit(f"Channel buyer {resolved_buyer_id} cannot write to conversation {conversation_id}")
        message = append_message(
            conn,
            conversation_id,
            "buyer",
            infer_intent(text),
            text,
            structured_payload=_channel_payload(channel, external_user_id, source_id, external_message_id=external_message_id),
        )
        _complete_channel_ingress(conn, channel, external_user_id, external_message_id, conversation_id, int(message["id"]))
        append_audit_event(
            conn,
            conversation_id,
            source_id,
            "channel_message_ingested",
            {"channel": channel, "external_user_id": external_user_id, "message_id": message["id"]},
        )
        return {
            "ok": True,
            "idempotent": False,
            "buyer_id": resolved_buyer_id,
            "channel": channel,
            "conversation": conversation_summary(conn, conversation_id),
            "message": message,
            "warnings": MVP_WARNINGS,
        }

    existing = _begin_channel_ingress(conn, channel, external_user_id, external_message_id, resolved_buyer_id)
    if existing is not None:
        return existing

    candidates = search_products(conn, query=text, city=city, area=area, limit=limit)
    if not candidates:
        _complete_channel_ingress(conn, channel, external_user_id, external_message_id, "", 0)
        return {
            "ok": True,
            "idempotent": False,
            "buyer_id": resolved_buyer_id,
            "channel": channel,
            "candidates": [],
            "conversation": None,
            "warnings": ["No matching merchant or product found.", *MVP_WARNINGS],
            "missing_facts": ["merchant", "product"],
        }

    selected = candidates[0]
    conversation = ensure_conversation(conn, resolved_buyer_id, selected["merchant_id"], selected["sku"])
    message = append_message(
        conn,
        conversation["id"],
        "buyer",
        infer_intent(text),
        text,
        structured_payload=_channel_payload(
            channel,
            external_user_id,
            source_id,
            city=city,
            area=area,
            selected_sku=selected["sku"],
            external_message_id=external_message_id,
        ),
    )
    _complete_channel_ingress(conn, channel, external_user_id, external_message_id, conversation["id"], int(message["id"]))
    append_audit_event(
        conn,
        conversation["id"],
        source_id,
        "channel_message_ingested",
        {"channel": channel, "external_user_id": external_user_id, "message_id": message["id"]},
    )
    return {
        "ok": True,
        "idempotent": False,
        "buyer_id": resolved_buyer_id,
        "channel": channel,
        "candidates": candidates,
        "selected": selected,
        "conversation": conversation_summary(conn, conversation["id"]),
        "message": message,
        "warnings": MVP_WARNINGS,
    }

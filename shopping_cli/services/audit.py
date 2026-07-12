"""Audit event query services."""

from __future__ import annotations

from typing import Any

from shopping_cli.core.harness import audit_event_summary_from_row


def merchant_audit_events(
    conn: Any,
    merchant_id: str,
    *,
    event: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    sql = "select id, conversation_id, actor, event, details_json, created_at from audit_events where actor = ?"
    values: list[Any] = [merchant_id]
    if event:
        sql += " and event = ?"
        values.append(event)
    sql += " order by id desc limit ? offset ?"
    values.extend([limit, offset])
    rows = conn.execute(sql, values).fetchall()
    return [audit_event_summary_from_row(row) for row in rows]

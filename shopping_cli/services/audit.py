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
    # actor 过滤只覆盖 token 生命周期事件（actor=merchant_id）；会话事件
    # 的 actor 是 sender/agent id——按该商家的会话归属一并纳入，否则查询
    # 近乎为空且误导。
    sql = """
        select id, conversation_id, actor, event, details_json, created_at
        from audit_events
        where (actor = ?
           or conversation_id in (select id from conversations where merchant_id = ?))
    """
    values: list[Any] = [merchant_id, merchant_id]
    if event:
        sql += " and event = ?"
        values.append(event)
    sql += " order by id desc limit ? offset ?"
    values.extend([limit, offset])
    rows = conn.execute(sql, values).fetchall()
    return [audit_event_summary_from_row(row) for row in rows]

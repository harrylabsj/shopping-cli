"""shopping.negotiation/0.1 API handlers.

Thin adapters over services.negotiation; both the FastAPI app and the
fallback ASGI router dispatch here, so no business logic is duplicated.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from shopping_cli.api import auth as api_auth
from shopping_cli.api.handlers.common import positive_whole_int, require_field
from shopping_cli.core import negotiation as protocol
from shopping_cli.core.errors import RateLimitError
from shopping_cli.db.session import db_session
from shopping_cli.services import negotiation as negotiation_service

# 审查 S-M2：merchant/agent/negotiation/human-review 端点此前均无限流——已泄露/
# 低熵 token 的滥用无节流。对磋商决策提交（写路径，追加磋商消息）加固定窗口
# 限流：进程内（单进程部署前提，与 ERP sync 限流同款）。env 可覆盖（调用时
# 读取，便于测试注入）。
# 审查 S-M2 补强：限流键从 per-owner（buyer 的 owner_id 客户端声明，轮换
# buyer_id 即绕过）改为 per-token（token_hash，服务端派生）——同凭证无论声明
# 什么 buyer_id 都共享一个桶；轮换新 token 受 S-M1 bootstrap 全局桶（铸造速率）
# 封顶。merchant/agent 单 token 部署下语义不变。
_NEGOTIATION_DECISION_WINDOW_SECONDS = 60
_DECISION_RATE_LIMIT_DEFAULT_PER_MINUTE = 600
_decision_buckets: dict[str, tuple[float, int]] = {}
_decision_buckets_lock = threading.Lock()


def _decision_rate_limit_per_minute() -> int:
    raw = os.environ.get("SHOPPING_NEGOTIATION_DECISION_RATE_LIMIT_PER_MINUTE", "")
    try:
        value = int(raw)
    except ValueError:
        return _DECISION_RATE_LIMIT_DEFAULT_PER_MINUTE
    return value if value > 0 else _DECISION_RATE_LIMIT_DEFAULT_PER_MINUTE


def _enforce_decision_rate_limit(credential_key: str) -> None:
    limit = _decision_rate_limit_per_minute()
    now = time.monotonic()
    with _decision_buckets_lock:
        window_start, count = _decision_buckets.get(credential_key, (0.0, 0))
        if now - window_start >= _NEGOTIATION_DECISION_WINDOW_SECONDS:
            window_start, count = now, 0
        if count >= limit:
            raise RateLimitError(
                f"negotiation decision rate limit exceeded ({limit}/minute)"
            )
        _decision_buckets[credential_key] = (window_start, count + 1)


def _actor(conn: Any, payload: dict[str, Any]) -> negotiation_service.NegotiationActor:
    return negotiation_service.require_negotiation_actor(conn, api_auth.payload_token(payload))


def capabilities(db_path: str | Path) -> dict[str, Any]:
    return {"ok": True, "capabilities": protocol.capabilities_report()}


def pending_messages(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        actor = _actor(conn, payload)
        pending = negotiation_service.list_pending_messages(conn, actor)
        return {"ok": True, "role": actor.role, "owner_id": actor.owner_id, "pending": pending}


def claim_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        actor = _actor(conn, payload)
        claim = negotiation_service.claim_message(
            conn,
            actor,
            str(require_field(payload, "conversation_id")),
            positive_whole_int(require_field(payload, "message_id"), "message_id"),
            str(require_field(payload, "idempotency_key")),
        )
        return {"ok": True, "claim": claim}


def get_snapshot(db_path: str | Path, payload: dict[str, Any], query: dict[str, Any] | None = None) -> dict[str, Any]:
    query = query or {}
    with db_session(db_path) as conn:
        actor = _actor(conn, payload)
        snapshot = negotiation_service.build_snapshot(
            conn,
            actor,
            str(query.get("conversation_id") or require_field(payload, "conversation_id")),
            positive_whole_int(query.get("message_id") or require_field(payload, "message_id"), "message_id"),
        )
        return {"ok": True, "snapshot": snapshot}


def submit_decision(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        actor = _actor(conn, payload)
        # 审查 S-M2：固定窗口限流（决策提交=磋商消息追加，滥用最直接）。键用
        # 服务端派生的 token_hash（per-token），空值兜底 owner_id。
        _enforce_decision_rate_limit(actor.token_hash or actor.owner_id)
        policy_result = negotiation_service.submit_decision(
            conn,
            actor,
            require_field(payload, "decision"),
            str(require_field(payload, "idempotency_key")),
        )
        return {"ok": True, "policy_result": policy_result}


def complete_claim(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        actor = _actor(conn, payload)
        process = negotiation_service.complete_claim(
            conn, actor, positive_whole_int(require_field(payload, "message_id"), "message_id")
        )
        return {"ok": True, "process": process}


def fail_claim(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        actor = _actor(conn, payload)
        process = negotiation_service.fail_claim(
            conn,
            actor,
            positive_whole_int(require_field(payload, "message_id"), "message_id"),
            str(payload.get("error") or "agent failure"),
        )
        return {"ok": True, "process": process}


def abandon_claim(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        actor = _actor(conn, payload)
        process = negotiation_service.abandon_claim(
            conn,
            actor,
            positive_whole_int(require_field(payload, "message_id"), "message_id"),
            str(payload.get("error") or "agent abandoned claim"),
        )
        return {"ok": True, "process": process}


def heartbeat_claims(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        actor = _actor(conn, payload)
        message_id = payload.get("message_id")
        heartbeat = negotiation_service.heartbeat_claims(
            conn,
            actor,
            None if message_id is None else positive_whole_int(message_id, "message_id"),
        )
        return {"ok": True, "heartbeat": heartbeat}


def abandon_stale_claims(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        actor = _actor(conn, payload)
        stale = negotiation_service.abandon_stale_claims(conn, actor, payload.get("ttl_seconds"))
        return {"ok": True, "stale": stale}

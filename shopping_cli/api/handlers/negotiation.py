"""shopping.negotiation/0.1 API handlers.

Thin adapters over services.negotiation; both the FastAPI app and the
fallback ASGI router dispatch here, so no business logic is duplicated.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shopping_cli.api import auth as api_auth
from shopping_cli.api.handlers.common import positive_whole_int, require_field
from shopping_cli.core import negotiation as protocol
from shopping_cli.db.session import db_session
from shopping_cli.services import negotiation as negotiation_service


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

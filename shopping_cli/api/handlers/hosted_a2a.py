"""Hosted A2A JSON-RPC API handler (v2.4-W3) — thin transport adapter.

Authenticates the caller with the existing buyer-token facility and hands the
parsed JSON-RPC body to ``shopping_cli.a2a.hosted_server.process_jsonrpc_request``.
All protocol logic lives in the framework-agnostic core; this module only
resolves the buyer identity from the token (never from the envelope) and maps
authentication failures to the JSON-RPC auth error shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shopping_cli.a2a.hosted_server import auth_error_response, process_jsonrpc_request
from shopping_cli.api import auth as api_auth
from shopping_cli.core.errors import AuthError
from shopping_cli.db.session import db_session
from shopping_cli.services import negotiation as negotiation_service


def a2a_message_send(
    db_path: str | Path,
    catalog_agent_id: str,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """POST /a2a/agents/{catalog_agent_id} — one JSON-RPC message/send.

    Returns ``(http_status, jsonrpc_body)``.  Authentication failures return
    401/403 JSON-RPC auth errors (missing vs invalid); the hosted-agent 404
    gate and the JSON-RPC protocol handling are delegated to the core.
    """
    token = api_auth.payload_token(payload)
    if not token:
        return auth_error_response(None, "authentication_required")

    with db_session(db_path) as conn:
        try:
            actor = negotiation_service.require_negotiation_actor(conn, token)
        except AuthError:
            return auth_error_response(None, "authorization_failed")
        if actor.role != "buyer":
            # This shared-host endpoint serves buyer agents only.
            return auth_error_response(None, "authorization_failed")
        return process_jsonrpc_request(
            conn,
            payload,
            sender_identity=actor.owner_id,
            actor=actor,
            catalog_agent_id=str(catalog_agent_id or "").strip(),
        )


__all__ = ["a2a_message_send"]

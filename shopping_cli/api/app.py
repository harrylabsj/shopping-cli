"""Marketplace API app factory.

FastAPI is used when installed. The lightweight fallback keeps route metadata
available for local tests in environments where optional API dependencies have
not been installed yet.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from shopping_cli import VERSION
from shopping_cli.api import auth as api_auth
from shopping_cli.api.fallback_asgi import MarketplaceASGIApp
from shopping_cli.api.handlers import agents as agent_handlers
from shopping_cli.api.handlers import audit as audit_handlers
from shopping_cli.api.handlers import buyer as buyer_handlers
from shopping_cli.api.handlers import catalog as catalog_handlers
from shopping_cli.api.handlers import conversations as conversation_handlers
from shopping_cli.api.handlers import erp as erp_handlers
from shopping_cli.api.handlers import listings_projection as listings_projection_handlers
from shopping_cli.api.handlers import human_review as human_review_handlers
from shopping_cli.api.handlers import negotiation as negotiation_handlers
from shopping_cli.api.handlers.common import DEFAULT_RESULT_LIMIT
from shopping_cli.api.limits import max_request_body_bytes, validate_payload
from shopping_cli.api.error_response import build_error_response
from shopping_cli.api.route_matching import match_path as _match_path
from shopping_cli.core.errors import (
    AuthError,
    ConflictError,
    IdempotencyConflict,
    MethodNotAllowedError,
    NotFoundError,
    PermissionDenied,
    RateLimitError,
    PayloadTooLargeError,
    ShoppingCliError,
    ValidationError,
)
from shopping_cli.services import tokens as token_service

logger = logging.getLogger("shopping-cli")

try:  # pragma: no cover - exercised when optional dependency is installed
    from fastapi import FastAPI, Header, Request, Response
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException
except ModuleNotFoundError:  # pragma: no cover - local CI currently has no fastapi
    FastAPI = None  # type: ignore[misc,assignment]
    Header = None  # type: ignore[assignment]
    Response = None  # type: ignore[misc,assignment]
    JSONResponse = None  # type: ignore[misc,assignment]
    RequestValidationError = None  # type: ignore[misc,assignment]
    Request = None  # type: ignore[misc,assignment]
    StarletteHTTPException = None  # type: ignore[misc,assignment]


# Route handlers return the JSON response body (a dict).
Handler = Callable[..., Any]


@dataclass(frozen=True)
class RouteEntry:
    methods: set[str]
    path_template: str
    handler: Handler


class _RequestBodyLimitMiddleware:
    """Reject oversized HTTP bodies before FastAPI attempts to parse them."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        maximum = max_request_body_bytes()
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        try:
            content_length = int(headers.get(b"content-length", b"0") or b"0")
        except ValueError:
            content_length = 0
        if content_length > maximum:
            await self._send_too_large(send)
            return

        messages: list[dict[str, Any]] = []
        received = 0
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") != "http.request":
                break
            received += len(message.get("body", b""))
            if received > maximum:
                await self._send_too_large(send)
                return
            if not message.get("more_body", False):
                break

        message_index = 0

        async def replay_receive() -> dict[str, Any]:
            nonlocal message_index
            if message_index < len(messages):
                message = messages[message_index]
                message_index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _send_too_large(send: Any) -> None:
        body = json.dumps({"ok": False, "error": "request body is too large"}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _json_error_response(status_code: int, error: str) -> Any:
    return build_error_response(status_code, error, JSONResponse)


def _auth_header_default() -> Any:
    if Header is None:
        return ""
    return Header(default="")


AUTHORIZATION_HEADER = _auth_header_default()


def _idempotency_key_header_default() -> Any:
    if Header is None:
        return ""
    return Header(default="", alias="Idempotency-Key")


IDEMPOTENCY_KEY_HEADER = _idempotency_key_header_default()


def _require_merchant_token(conn: Any, merchant_id: str, payload: dict[str, Any]) -> None:
    token_service.require_merchant_token(conn, merchant_id, api_auth.payload_token(payload))


def _resolve_agent_token(conn: Any, merchant_id: str, token: Any = "", token_prefix: Any = "") -> str:
    return token_service.resolve_agent_token(conn, merchant_id, token, token_prefix)


def _health(db_path: str | Path) -> dict[str, Any]:
    return catalog_handlers.health(db_path)


def _create_merchant(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return catalog_handlers.create_merchant(db_path, payload, api_auth.require_admin_token)


def _update_merchant(db_path: str | Path, merchant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return catalog_handlers.update_merchant(db_path, merchant_id, payload, _require_merchant_token)


def _rotate_merchant_token(db_path: str | Path, merchant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return catalog_handlers.rotate_merchant_token(db_path, merchant_id, payload, api_auth.require_admin_token)


def _revoke_merchant_tokens(db_path: str | Path, merchant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return catalog_handlers.revoke_merchant_tokens(db_path, merchant_id, payload, api_auth.require_admin_token)


def _recover_merchant_token(db_path: str | Path, merchant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return catalog_handlers.recover_merchant_token(db_path, merchant_id, payload, api_auth.require_admin_token)


def _get_merchant(db_path: str | Path, merchant_id: str) -> dict[str, Any]:
    return catalog_handlers.get_merchant(db_path, merchant_id)


def _get_merchant_private_config(
    db_path: str | Path,
    merchant_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return catalog_handlers.get_merchant_private_config(db_path, merchant_id, payload)


def _list_merchants(db_path: str | Path, query: dict[str, Any] | None = None) -> dict[str, Any]:
    return catalog_handlers.list_merchants(db_path, query)


def _create_product(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return catalog_handlers.create_product(db_path, payload, _require_merchant_token)


def _update_product(db_path: str | Path, sku: str, payload: dict[str, Any]) -> dict[str, Any]:
    return catalog_handlers.update_product(db_path, sku, payload, _require_merchant_token)


def _get_product(db_path: str | Path, sku: str) -> dict[str, Any]:
    return catalog_handlers.get_product(db_path, sku)


def _list_listing_projections(db_path: str | Path, query: dict[str, Any]) -> dict[str, Any]:
    return listings_projection_handlers.list_listing_projections(db_path, query)


def _sync_erp(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return erp_handlers.sync_erp(db_path, payload)


def _get_listing_projection(db_path: str | Path, sku: str) -> dict[str, Any]:
    return listings_projection_handlers.get_listing_projection(db_path, sku)


def _search_products(db_path: str | Path, query: dict[str, Any]) -> dict[str, Any]:
    return catalog_handlers.search_products(db_path, query)


def _search_merchants(db_path: str | Path, query: dict[str, Any]) -> dict[str, Any]:
    return catalog_handlers.search_merchants(db_path, query)


def _buyer_ask(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return buyer_handlers.buyer_ask(db_path, payload)


def _ingest_channel_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return buyer_handlers.ingest_channel_message(db_path, payload)


def _get_conversation(db_path: str | Path, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return conversation_handlers.get_conversation(db_path, conversation_id, payload)


def _create_conversation(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return buyer_handlers.create_conversation(db_path, payload)


def _append_conversation_message(db_path: str | Path, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return conversation_handlers.append_conversation_message(db_path, conversation_id, payload)


def _close_conversation(db_path: str | Path, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return conversation_handlers.close_conversation(db_path, conversation_id, payload)


def _agent_heartbeat(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return agent_handlers.agent_heartbeat(db_path, payload)


def _create_agent_token(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return agent_handlers.create_agent_token(db_path, payload)


def _list_agent_tokens(
    db_path: str | Path,
    payload: dict[str, Any],
    merchant_id: str = "",
    limit: Any = DEFAULT_RESULT_LIMIT,
    offset: Any = 0,
) -> dict[str, Any]:
    return agent_handlers.list_agent_tokens(db_path, payload, merchant_id=merchant_id, limit=limit, offset=offset)


def _revoke_agent_token(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return agent_handlers.revoke_agent_token(db_path, payload)


def _rotate_agent_token(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return agent_handlers.rotate_agent_token(db_path, payload)


def _claim_agent_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return agent_handlers.claim_message(db_path, payload)


def _complete_agent_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return agent_handlers.complete_message(db_path, payload)


def _fail_agent_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return agent_handlers.fail_message(db_path, payload)


def _abandon_agent_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return agent_handlers.abandon_message(db_path, payload)


def _abandon_stale_agent_messages(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return agent_handlers.abandon_stale_messages(db_path, payload)


def _list_agents(
    db_path: str | Path,
    payload: dict[str, Any],
    owner_id: str = "",
    limit: Any = DEFAULT_RESULT_LIMIT,
    offset: Any = 0,
) -> dict[str, Any]:
    return agent_handlers.list_agents(db_path, payload, owner_id=owner_id, limit=limit, offset=offset)


def _get_agent(db_path: str | Path, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return agent_handlers.get_agent(db_path, agent_id, payload)


def _conversation_list(
    db_path: str | Path,
    filters: dict[str, Any],
    payload: dict[str, Any],
    owner_kind: str,
    owner_id: str,
) -> dict[str, Any]:
    return conversation_handlers.conversation_list(db_path, filters, payload, owner_kind, owner_id)


def _merchant_conversations(
    db_path: str | Path,
    merchant_id: str,
    payload: dict[str, Any],
    status: str = "",
    limit: Any = DEFAULT_RESULT_LIMIT,
    offset: Any = 0,
    include: Any = "",
) -> dict[str, Any]:
    return conversation_handlers.merchant_conversations(
        db_path,
        merchant_id,
        payload,
        status=status,
        limit=limit,
        offset=offset,
        include=include,
    )


def _human_review_queue(
    db_path: str | Path,
    payload: dict[str, Any],
    merchant_id: str = "",
    limit: Any = DEFAULT_RESULT_LIMIT,
    offset: Any = 0,
) -> dict[str, Any]:
    return human_review_handlers.human_review_queue(
        db_path,
        payload,
        merchant_id=merchant_id,
        limit=limit,
        offset=offset,
    )


def _record_tool_call_audit(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return audit_handlers.record_tool_call_audit(db_path, payload)


def _audit_events(
    db_path: str | Path,
    payload: dict[str, Any],
    merchant_id: str = "",
    event: str = "",
    limit: Any = 50,
    offset: Any = 0,
) -> dict[str, Any]:
    return audit_handlers.audit_events(
        db_path,
        payload,
        merchant_id=merchant_id,
        event=event,
        limit=limit,
        offset=offset,
    )


def _get_human_review(db_path: str | Path, review_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
    return human_review_handlers.get_human_review(db_path, review_id, payload)


def _create_human_review(db_path: str | Path, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return human_review_handlers.create_human_review(db_path, conversation_id, payload)


def _resolve_human_review_item(db_path: str | Path, review_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
    return human_review_handlers.resolve_human_review_item(db_path, review_id, payload)


def _resolve_human_review(db_path: str | Path, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return human_review_handlers.resolve_human_review(db_path, conversation_id, payload)


def _capabilities(db_path: str | Path) -> dict[str, Any]:
    return negotiation_handlers.capabilities(db_path)


def _negotiation_pending_messages(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return negotiation_handlers.pending_messages(db_path, payload)


def _negotiation_claim(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return negotiation_handlers.claim_message(db_path, payload)


def _negotiation_complete_claim(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return negotiation_handlers.complete_claim(db_path, payload)


def _negotiation_fail_claim(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return negotiation_handlers.fail_claim(db_path, payload)


def _negotiation_abandon_claim(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return negotiation_handlers.abandon_claim(db_path, payload)


def _negotiation_heartbeat_claims(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return negotiation_handlers.heartbeat_claims(db_path, payload)


def _negotiation_abandon_stale_claims(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return negotiation_handlers.abandon_stale_claims(db_path, payload)


def _negotiation_snapshot(db_path: str | Path, payload: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    return negotiation_handlers.get_snapshot(db_path, payload, query)


def _negotiation_submit_decision(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return negotiation_handlers.submit_decision(db_path, payload)


def _buyer_conversation_list(
    db_path: str | Path, payload: dict[str, Any], query: dict[str, Any], buyer_id: str
) -> dict[str, Any]:
    filters = dict(query)
    filters["buyer_id"] = buyer_id
    return _conversation_list(db_path, filters, payload, owner_kind="buyer", owner_id=buyer_id)


def _merchant_conversation_list(
    db_path: str | Path, payload: dict[str, Any], query: dict[str, Any], merchant_id: str
) -> dict[str, Any]:
    filters = dict(query)
    filters["merchant_id"] = merchant_id
    return _conversation_list(db_path, filters, payload, owner_kind="merchant", owner_id=merchant_id)


def _merchant_human_review_list(
    db_path: str | Path, payload: dict[str, Any], query: dict[str, Any], merchant_id: str
) -> dict[str, Any]:
    return _merchant_conversations(
        db_path,
        merchant_id,
        payload,
        status="human_required",
        limit=query.get("limit"),
        offset=query.get("offset"),
        include=query.get("include"),
    )


_ROUTE_TABLE: tuple[RouteEntry, ...] = (
    RouteEntry({"GET"}, "/health", lambda db_path, payload, query, **kw: _health(db_path)),
    RouteEntry({"GET"}, "/merchants", lambda db_path, payload, query, **kw: _list_merchants(db_path, query)),
    RouteEntry({"POST"}, "/merchants", lambda db_path, payload, query, **kw: _create_merchant(db_path, payload)),
    RouteEntry(
        {"GET"},
        "/merchants/{merchant_id}",
        lambda db_path, payload, query, merchant_id: _get_merchant(db_path, merchant_id),
    ),
    RouteEntry(
        {"GET"},
        "/merchants/{merchant_id}/private-config",
        lambda db_path, payload, query, merchant_id: _get_merchant_private_config(db_path, merchant_id, payload),
    ),
    RouteEntry(
        {"POST"},
        "/merchants/{merchant_id}/token/rotate",
        lambda db_path, payload, query, merchant_id: _rotate_merchant_token(db_path, merchant_id, payload),
    ),
    RouteEntry(
        {"POST"},
        "/merchants/{merchant_id}/token/revoke",
        lambda db_path, payload, query, merchant_id: _revoke_merchant_tokens(db_path, merchant_id, payload),
    ),
    RouteEntry(
        {"POST"},
        "/merchants/{merchant_id}/token/recover",
        lambda db_path, payload, query, merchant_id: _recover_merchant_token(db_path, merchant_id, payload),
    ),
    RouteEntry(
        {"PATCH"},
        "/merchants/{merchant_id}",
        lambda db_path, payload, query, merchant_id: _update_merchant(db_path, merchant_id, payload),
    ),
    RouteEntry({"GET"}, "/merchants/{merchant_id}/conversations", _merchant_conversation_list),
    RouteEntry({"GET"}, "/merchants/{merchant_id}/human-review", _merchant_human_review_list),
    RouteEntry(
        {"GET"},
        "/merchants/{merchant_id}/agents",
        lambda db_path, payload, query, merchant_id: _list_agents(
            db_path,
            payload,
            owner_id=merchant_id,
            limit=query.get("limit"),
            offset=query.get("offset"),
        ),
    ),
    RouteEntry({"POST"}, "/products", lambda db_path, payload, query, **kw: _create_product(db_path, payload)),
    RouteEntry({"GET"}, "/products/{sku}", lambda db_path, payload, query, sku: _get_product(db_path, sku)),
    RouteEntry(
        {"PATCH"}, "/products/{sku}", lambda db_path, payload, query, sku: _update_product(db_path, sku, payload)
    ),
    RouteEntry({"GET"}, "/search/products", lambda db_path, payload, query, **kw: _search_products(db_path, query)),
    RouteEntry(
        {"GET"},
        "/v1/merchant/listings/projections",
        lambda db_path, payload, query, **kw: _list_listing_projections(db_path, query or {}),
    ),
    RouteEntry(
        {"GET"},
        "/v1/merchant/listings/{sku}/projection",
        lambda db_path, payload, query, sku: _get_listing_projection(db_path, sku),
    ),
    RouteEntry(
        {"POST"},
        "/v1/merchant/erp/sync",
        lambda db_path, payload, query, **kw: _sync_erp(db_path, payload),
    ),
    RouteEntry({"GET"}, "/search/merchants", lambda db_path, payload, query, **kw: _search_merchants(db_path, query)),
    RouteEntry(
        {"POST"}, "/channels/messages", lambda db_path, payload, query, **kw: _ingest_channel_message(db_path, payload)
    ),
    RouteEntry({"POST"}, "/buyer/ask", lambda db_path, payload, query, **kw: _buyer_ask(db_path, payload)),
    RouteEntry(
        {"POST"}, "/conversations", lambda db_path, payload, query, **kw: _create_conversation(db_path, payload)
    ),
    RouteEntry(
        {"GET"},
        "/conversations/{conversation_id}",
        lambda db_path, payload, query, conversation_id: _get_conversation(db_path, conversation_id, payload),
    ),
    RouteEntry(
        {"POST"},
        "/conversations/{conversation_id}/messages",
        lambda db_path, payload, query, conversation_id: _append_conversation_message(
            db_path, conversation_id, payload
        ),
    ),
    RouteEntry(
        {"POST"},
        "/conversations/{conversation_id}/close",
        lambda db_path, payload, query, conversation_id: _close_conversation(db_path, conversation_id, payload),
    ),
    RouteEntry(
        {"POST"},
        "/conversations/{conversation_id}/human-review",
        lambda db_path, payload, query, conversation_id: _create_human_review(db_path, conversation_id, payload),
    ),
    RouteEntry(
        {"POST"},
        "/conversations/{conversation_id}/human-review/resolve",
        lambda db_path, payload, query, conversation_id: _resolve_human_review(db_path, conversation_id, payload),
    ),
    RouteEntry({"GET"}, "/buyers/{buyer_id}/conversations", _buyer_conversation_list),
    RouteEntry({"POST"}, "/agents/heartbeat", lambda db_path, payload, query, **kw: _agent_heartbeat(db_path, payload)),
    RouteEntry(
        {"GET"},
        "/agents/tokens",
        lambda db_path, payload, query, **kw: _list_agent_tokens(
            db_path,
            payload,
            merchant_id=str(query.get("merchant_id") or ""),
            limit=query.get("limit"),
            offset=query.get("offset"),
        ),
    ),
    RouteEntry({"POST"}, "/agents/tokens", lambda db_path, payload, query, **kw: _create_agent_token(db_path, payload)),
    RouteEntry(
        {"POST"}, "/agents/tokens/revoke", lambda db_path, payload, query, **kw: _revoke_agent_token(db_path, payload)
    ),
    RouteEntry(
        {"POST"}, "/agents/tokens/rotate", lambda db_path, payload, query, **kw: _rotate_agent_token(db_path, payload)
    ),
    RouteEntry(
        {"POST"}, "/agents/messages/claim", lambda db_path, payload, query, **kw: _claim_agent_message(db_path, payload)
    ),
    RouteEntry(
        {"POST"},
        "/agents/messages/complete",
        lambda db_path, payload, query, **kw: _complete_agent_message(db_path, payload),
    ),
    RouteEntry(
        {"POST"}, "/agents/messages/fail", lambda db_path, payload, query, **kw: _fail_agent_message(db_path, payload)
    ),
    RouteEntry(
        {"POST"},
        "/agents/messages/abandon",
        lambda db_path, payload, query, **kw: _abandon_agent_message(db_path, payload),
    ),
    RouteEntry(
        {"POST"},
        "/agents/messages/abandon-stale",
        lambda db_path, payload, query, **kw: _abandon_stale_agent_messages(db_path, payload),
    ),
    RouteEntry(
        {"GET"},
        "/agents",
        lambda db_path, payload, query, **kw: _list_agents(
            db_path,
            payload,
            limit=query.get("limit"),
            offset=query.get("offset"),
        ),
    ),
    RouteEntry(
        {"GET"}, "/agents/{agent_id}", lambda db_path, payload, query, agent_id: _get_agent(db_path, agent_id, payload)
    ),
    RouteEntry(
        {"POST"}, "/audit/tool-calls", lambda db_path, payload, query, **kw: _record_tool_call_audit(db_path, payload)
    ),
    RouteEntry(
        {"GET"},
        "/audit/events",
        lambda db_path, payload, query, **kw: _audit_events(
            db_path,
            payload,
            merchant_id=str(query.get("merchant_id") or ""),
            event=str(query.get("event") or ""),
            limit=query.get("limit") or 50,
            offset=query.get("offset"),
        ),
    ),
    RouteEntry(
        {"GET"},
        "/human-review/queue",
        lambda db_path, payload, query, **kw: _human_review_queue(
            db_path,
            payload,
            merchant_id=str(query.get("merchant_id") or ""),
            limit=query.get("limit"),
            offset=query.get("offset"),
        ),
    ),
    RouteEntry(
        {"GET"},
        "/human-review/{review_id}",
        lambda db_path, payload, query, review_id: _get_human_review(db_path, review_id, payload),
    ),
    RouteEntry(
        {"POST"},
        "/human-review/{review_id}/resolve",
        lambda db_path, payload, query, review_id: _resolve_human_review_item(db_path, review_id, payload),
    ),
    RouteEntry({"GET"}, "/capabilities", lambda db_path, payload, query, **kw: _capabilities(db_path)),
    RouteEntry(
        {"GET"},
        "/negotiation/pending-messages",
        lambda db_path, payload, query, **kw: _negotiation_pending_messages(db_path, payload),
    ),
    RouteEntry(
        {"POST"}, "/negotiation/claims", lambda db_path, payload, query, **kw: _negotiation_claim(db_path, payload)
    ),
    RouteEntry(
        {"POST"},
        "/negotiation/claims/complete",
        lambda db_path, payload, query, **kw: _negotiation_complete_claim(db_path, payload),
    ),
    RouteEntry(
        {"POST"},
        "/negotiation/claims/fail",
        lambda db_path, payload, query, **kw: _negotiation_fail_claim(db_path, payload),
    ),
    RouteEntry(
        {"POST"},
        "/negotiation/claims/abandon",
        lambda db_path, payload, query, **kw: _negotiation_abandon_claim(db_path, payload),
    ),
    RouteEntry(
        {"POST"},
        "/negotiation/claims/heartbeat",
        lambda db_path, payload, query, **kw: _negotiation_heartbeat_claims(db_path, payload),
    ),
    RouteEntry(
        {"POST"},
        "/negotiation/claims/abandon-stale",
        lambda db_path, payload, query, **kw: _negotiation_abandon_stale_claims(db_path, payload),
    ),
    RouteEntry(
        {"GET"},
        "/negotiation/snapshot",
        lambda db_path, payload, query, **kw: _negotiation_snapshot(db_path, payload, query),
    ),
    RouteEntry(
        {"POST"},
        "/negotiation/decisions",
        lambda db_path, payload, query, **kw: _negotiation_submit_decision(db_path, payload),
    ),
)


def resolve_route(
    method: str, path: str, routes: tuple[RouteEntry, ...] | list[Any] | None = None
) -> tuple[bool, bool]:
    """Return (path_known, method_allowed) without parsing the request body.

    *routes* defaults to the full ``_ROUTE_TABLE``; the parameter is kept for
    API compatibility (previously used by the kiwi-catalog standalone service).
    """
    table = _ROUTE_TABLE if routes is None else tuple(routes)
    path_known = False
    for route in table:
        template = getattr(route, "path_template", None) or getattr(route, "path", "")
        if _match_path(template, path) is None:
            continue
        path_known = True
        if method.upper() in route.methods:
            return True, True
    return path_known, False


def handle_request(
    db_path: str | Path,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    payload = payload or {}
    query = query or {}
    try:
        validate_payload(payload)
        path_matched = False
        for route in _ROUTE_TABLE:
            path_params = _match_path(route.path_template, path)
            if path_params is None:
                continue
            path_matched = True
            if method.upper() in route.methods:
                result = route.handler(db_path, payload, query, **path_params)
                return 200, result
        if path_matched:
            raise MethodNotAllowedError(f"Method not allowed for {method} {path}")
        raise NotFoundError(f"No route for {method} {path}")
    except AuthError as exc:
        return 403, {"ok": False, "error": str(exc)}
    except PermissionDenied as exc:
        return 403, {"ok": False, "error": str(exc)}
    except IdempotencyConflict as exc:
        return 409, {"ok": False, "error": str(exc)}
    except ConflictError as exc:
        return 409, {"ok": False, "error": str(exc)}
    except NotFoundError as exc:
        return 404, {"ok": False, "error": str(exc)}
    except RateLimitError as exc:
        return 429, {"ok": False, "error": str(exc)}
    except PayloadTooLargeError as exc:
        return 413, {"ok": False, "error": str(exc)}
    except MethodNotAllowedError as exc:
        return 405, {"ok": False, "error": str(exc)}
    except ValidationError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except ShoppingCliError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except Exception:
        logger.exception("unhandled error handling %s %s", method, path)
        return 500, {"ok": False, "error": "internal server error"}


def create_app(db_path: str | Path = "shopping-cli.sqlite") -> Any:
    from shopping_cli.api.route_registry import route_info

    routes = route_info()
    if FastAPI is None:
        return MarketplaceASGIApp(
            db_path,
            route_provider=lambda: routes,
            route_resolver=lambda method, path: resolve_route(method, path, routes=routes),
        )

    app = FastAPI(
        title="shopping-cli Marketplace API",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.db_path = str(db_path)
    app.state.fastapi_available = True

    if hasattr(app, "add_middleware"):
        app.add_middleware(_RequestBodyLimitMiddleware)

    @app.exception_handler(AuthError)
    def auth_error_handler(_request: Any, exc: AuthError) -> Any:
        return _json_error_response(403, str(exc))

    @app.exception_handler(PermissionDenied)
    def permission_denied_handler(_request: Any, exc: PermissionDenied) -> Any:
        return _json_error_response(403, str(exc))

    @app.exception_handler(IdempotencyConflict)
    def idempotency_conflict_handler(_request: Any, exc: IdempotencyConflict) -> Any:
        return _json_error_response(409, str(exc))

    @app.exception_handler(ConflictError)
    def conflict_error_handler(_request: Any, exc: ConflictError) -> Any:
        return _json_error_response(409, str(exc))

    @app.exception_handler(NotFoundError)
    def not_found_error_handler(_request: Any, exc: NotFoundError) -> Any:
        return _json_error_response(404, str(exc))

    @app.exception_handler(RateLimitError)
    def rate_limit_error_handler(_request: Any, exc: RateLimitError) -> Any:
        return _json_error_response(429, str(exc))

    @app.exception_handler(PayloadTooLargeError)
    def payload_too_large_handler(_request: Any, exc: PayloadTooLargeError) -> Any:
        return _json_error_response(413, str(exc))

    @app.exception_handler(MethodNotAllowedError)
    def method_not_allowed_handler(_request: Any, exc: MethodNotAllowedError) -> Any:
        return _json_error_response(405, str(exc))

    @app.exception_handler(ValidationError)
    def validation_error_handler(_request: Any, exc: ValidationError) -> Any:
        return _json_error_response(400, str(exc))

    @app.exception_handler(ShoppingCliError)
    def shopping_cli_error_handler(_request: Any, exc: ShoppingCliError) -> Any:
        return _json_error_response(400, str(exc))

    if RequestValidationError is not None:  # pragma: no cover - exercised with fastapi installed

        @app.exception_handler(RequestValidationError)
        def request_validation_error_handler(request: Any, exc: Exception) -> Any:
            # 不回显 str(exc)：包含 schema 内部结构并回显调用方输入。
            logger.warning("request validation failed on %s: %s", getattr(request, "url", "?"), exc)
            return _json_error_response(400, "invalid request body")

    if StarletteHTTPException is not None:  # pragma: no cover - exercised with fastapi installed

        @app.exception_handler(StarletteHTTPException)
        def http_exception_handler(_request: Any, exc: Any) -> Any:
            status = int(exc.status_code)
            message = "not found" if status == 404 else "method not allowed" if status == 405 else str(exc.detail)
            return _json_error_response(status, message)

    @app.exception_handler(Exception)
    def unexpected_error_handler(request: Any, exc: Exception) -> Any:
        logger.exception("unhandled error on %s %s", getattr(request, "method", "?"), getattr(request, "url", "?"))
        return _json_error_response(500, "internal server error")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return _health(db_path)

    @app.get("/merchants")
    def list_merchants(limit: str = "", offset: str = "") -> dict[str, Any]:
        return _list_merchants(db_path, {"limit": limit, "offset": offset})

    @app.post("/merchants")
    def create_merchant(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
        idempotency_key: str = IDEMPOTENCY_KEY_HEADER,
    ) -> dict[str, Any]:
        return _create_merchant(
            db_path,
            api_auth.payload_with_auth(payload, authorization, idempotency_key),
        )

    @app.get("/merchants/{merchant_id}")
    def get_merchant(merchant_id: str) -> dict[str, Any]:
        return _get_merchant(db_path, merchant_id)

    @app.get("/merchants/{merchant_id}/private-config")
    def get_merchant_private_config(
        merchant_id: str,
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _get_merchant_private_config(
            db_path,
            merchant_id,
            api_auth.payload_with_auth({}, authorization),
        )

    @app.post("/merchants/{merchant_id}/token/rotate")
    def rotate_merchant_token(
        merchant_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _rotate_merchant_token(
            db_path,
            merchant_id,
            api_auth.payload_with_auth(payload, authorization),
        )

    @app.post("/merchants/{merchant_id}/token/revoke")
    def revoke_merchant_tokens(
        merchant_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _revoke_merchant_tokens(
            db_path,
            merchant_id,
            api_auth.payload_with_auth(payload, authorization),
        )

    @app.post("/merchants/{merchant_id}/token/recover")
    def recover_merchant_token(
        merchant_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _recover_merchant_token(
            db_path,
            merchant_id,
            api_auth.payload_with_auth(payload, authorization),
        )

    @app.patch("/merchants/{merchant_id}")
    def update_merchant(
        merchant_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _update_merchant(db_path, merchant_id, api_auth.payload_with_auth(payload, authorization))

    @app.post("/products")
    def create_product(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _create_product(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.get("/products/{sku}")
    def get_product(sku: str) -> dict[str, Any]:
        return _get_product(db_path, sku)

    @app.patch("/products/{sku}")
    def update_product(
        sku: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _update_product(db_path, sku, api_auth.payload_with_auth(payload, authorization))

    @app.get("/v1/merchant/listings/projections")
    def merchant_listing_projections(merchant_id: str = "") -> dict[str, Any]:
        return _list_listing_projections(db_path, {"merchant_id": merchant_id})

    @app.get("/v1/merchant/listings/{sku}/projection")
    def merchant_listing_projection(sku: str) -> dict[str, Any]:
        return _get_listing_projection(db_path, sku)

    @app.post("/v1/merchant/erp/sync")
    def merchant_erp_sync(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _sync_erp(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.get("/search/products")
    def search_products(
        query: str = "",
        city: str = "",
        area: str = "",
        max_price: str = "",
        include_out_of_stock: str = "",
        limit: str = "",
        offset: str = "",
    ) -> dict[str, Any]:
        return _search_products(
            db_path,
            {
                "query": query,
                "city": city,
                "area": area,
                "max_price": max_price,
                "include_out_of_stock": include_out_of_stock,
                "limit": limit,
                "offset": offset,
            },
        )

    @app.get("/search/merchants")
    def search_merchants(query: str = "", city: str = "", limit: str = "", offset: str = "") -> dict[str, Any]:
        return _search_merchants(db_path, {"query": query, "city": city, "limit": limit, "offset": offset})

    @app.post("/channels/messages")
    def ingest_channel_message(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _ingest_channel_message(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/buyer/ask")
    def buyer_ask(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
        idempotency_key: str = IDEMPOTENCY_KEY_HEADER,
    ) -> dict[str, Any]:
        return _buyer_ask(db_path, api_auth.payload_with_auth(payload, authorization, idempotency_key))

    @app.post("/conversations")
    def create_conversation(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
        idempotency_key: str = IDEMPOTENCY_KEY_HEADER,
    ) -> dict[str, Any]:
        return _create_conversation(db_path, api_auth.payload_with_auth(payload, authorization, idempotency_key))

    @app.get("/buyers/{buyer_id}/conversations")
    def get_buyer_conversations(
        buyer_id: str,
        status: str = "",
        merchant_id: str = "",
        sku: str = "",
        updated_since: str = "",
        authorization: str = AUTHORIZATION_HEADER,
        limit: str = "",
        offset: str = "",
        include: str = "",
    ) -> dict[str, Any]:
        return _conversation_list(
            db_path,
            {
                "buyer_id": buyer_id,
                "status": status,
                "merchant_id": merchant_id,
                "sku": sku,
                "updated_since": updated_since,
                "limit": limit,
                "offset": offset,
                "include": include,
            },
            api_auth.payload_with_auth({}, authorization),
            owner_kind="buyer",
            owner_id=buyer_id,
        )

    @app.get("/conversations/{conversation_id}")
    def get_conversation(conversation_id: str, authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _get_conversation(db_path, conversation_id, api_auth.payload_with_auth({}, authorization))

    @app.post("/conversations/{conversation_id}/messages")
    def add_message(
        conversation_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _append_conversation_message(
            db_path, conversation_id, api_auth.payload_with_auth(payload, authorization)
        )

    @app.post("/conversations/{conversation_id}/close")
    def close_conversation(
        conversation_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _close_conversation(db_path, conversation_id, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/heartbeat")
    def agent_heartbeat(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _agent_heartbeat(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/tokens")
    def create_agent_token(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _create_agent_token(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.get("/agents/tokens")
    def list_agent_tokens(
        merchant_id: str = "",
        limit: str = "",
        offset: str = "",
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _list_agent_tokens(
            db_path,
            api_auth.payload_with_auth({}, authorization),
            merchant_id=merchant_id,
            limit=limit,
            offset=offset,
        )

    @app.post("/agents/tokens/revoke")
    def revoke_agent_token(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _revoke_agent_token(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/tokens/rotate")
    def rotate_agent_token(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _rotate_agent_token(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/messages/claim")
    def claim_agent_message_route(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _claim_agent_message(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/messages/complete")
    def complete_agent_message_route(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _complete_agent_message(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/messages/fail")
    def fail_agent_message_route(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _fail_agent_message(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/messages/abandon")
    def abandon_agent_message_route(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _abandon_agent_message(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/agents/messages/abandon-stale")
    def abandon_stale_agent_messages_route(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _abandon_stale_agent_messages(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.get("/agents")
    def list_agents(
        authorization: str = AUTHORIZATION_HEADER,
        limit: str = "",
        offset: str = "",
    ) -> dict[str, Any]:
        return _list_agents(db_path, api_auth.payload_with_auth({}, authorization), limit=limit, offset=offset)

    @app.get("/agents/{agent_id}")
    def get_agent(agent_id: str, authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _get_agent(db_path, agent_id, api_auth.payload_with_auth({}, authorization))

    @app.get("/merchants/{merchant_id}/agents")
    def get_merchant_agents(
        merchant_id: str,
        authorization: str = AUTHORIZATION_HEADER,
        limit: str = "",
        offset: str = "",
    ) -> dict[str, Any]:
        return _list_agents(
            db_path,
            api_auth.payload_with_auth({}, authorization),
            owner_id=merchant_id,
            limit=limit,
            offset=offset,
        )

    @app.post("/audit/tool-calls")
    def record_tool_call_audit(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _record_tool_call_audit(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.get("/audit/events")
    def get_audit_events(
        merchant_id: str = "",
        event: str = "",
        limit: str = "",
        offset: str = "",
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _audit_events(
            db_path,
            api_auth.payload_with_auth({}, authorization),
            merchant_id=merchant_id,
            event=event,
            limit=limit,
            offset=offset,
        )

    @app.get("/human-review/queue")
    def human_review_queue(
        merchant_id: str = "",
        limit: str = "",
        offset: str = "",
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _human_review_queue(
            db_path,
            api_auth.payload_with_auth({}, authorization),
            merchant_id=merchant_id,
            limit=limit,
            offset=offset,
        )

    @app.get("/human-review/{review_id}")
    def get_human_review(review_id: str, authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _get_human_review(db_path, review_id, api_auth.payload_with_auth({}, authorization))

    @app.post("/human-review/{review_id}/resolve")
    def resolve_human_review_item(
        review_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _resolve_human_review_item(db_path, review_id, api_auth.payload_with_auth(payload, authorization))

    @app.get("/merchants/{merchant_id}/conversations")
    def get_merchant_conversations(
        merchant_id: str,
        status: str = "",
        buyer_id: str = "",
        sku: str = "",
        updated_since: str = "",
        authorization: str = AUTHORIZATION_HEADER,
        limit: str = "",
        offset: str = "",
        include: str = "",
    ) -> dict[str, Any]:
        return _conversation_list(
            db_path,
            {
                "merchant_id": merchant_id,
                "status": status,
                "buyer_id": buyer_id,
                "sku": sku,
                "updated_since": updated_since,
                "limit": limit,
                "offset": offset,
                "include": include,
            },
            api_auth.payload_with_auth({}, authorization),
            owner_kind="merchant",
            owner_id=merchant_id,
        )

    @app.get("/merchants/{merchant_id}/human-review")
    def human_review(
        merchant_id: str,
        limit: str = "",
        offset: str = "",
        authorization: str = AUTHORIZATION_HEADER,
        include: str = "",
    ) -> dict[str, Any]:
        return _merchant_conversations(
            db_path,
            merchant_id,
            api_auth.payload_with_auth({}, authorization),
            status="human_required",
            limit=limit,
            offset=offset,
            include=include,
        )

    @app.post("/conversations/{conversation_id}/human-review")
    def create_human_review(
        conversation_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _create_human_review(db_path, conversation_id, api_auth.payload_with_auth(payload, authorization))

    @app.post("/conversations/{conversation_id}/human-review/resolve")
    def resolve_human_review(
        conversation_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _resolve_human_review(db_path, conversation_id, api_auth.payload_with_auth(payload, authorization))

    @app.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        return _capabilities(db_path)

    @app.get("/negotiation/pending-messages")
    def negotiation_pending_messages(authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _negotiation_pending_messages(db_path, api_auth.payload_with_auth({}, authorization))

    @app.post("/negotiation/claims")
    def negotiation_claim(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _negotiation_claim(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/negotiation/claims/complete")
    def negotiation_complete_claim(
        payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER
    ) -> dict[str, Any]:
        return _negotiation_complete_claim(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/negotiation/claims/fail")
    def negotiation_fail_claim(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _negotiation_fail_claim(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/negotiation/claims/abandon")
    def negotiation_abandon_claim(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _negotiation_abandon_claim(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/negotiation/claims/heartbeat")
    def negotiation_heartbeat_claims(
        payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER
    ) -> dict[str, Any]:
        return _negotiation_heartbeat_claims(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.post("/negotiation/claims/abandon-stale")
    def negotiation_abandon_stale_claims(
        payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER
    ) -> dict[str, Any]:
        return _negotiation_abandon_stale_claims(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.get("/negotiation/snapshot")
    def negotiation_snapshot(
        conversation_id: str = "",
        message_id: str = "",
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _negotiation_snapshot(
            db_path,
            api_auth.payload_with_auth({}, authorization),
            {"conversation_id": conversation_id, "message_id": message_id},
        )

    @app.post("/negotiation/decisions")
    def negotiation_submit_decision(
        payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER
    ) -> dict[str, Any]:
        return _negotiation_submit_decision(db_path, api_auth.payload_with_auth(payload, authorization))

    return app

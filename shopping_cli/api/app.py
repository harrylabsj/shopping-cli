"""Marketplace API app factory.

FastAPI is used when installed. The lightweight fallback keeps route metadata
available for local tests in environments where optional API dependencies have
not been installed yet.
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from shopping_cli import VERSION
from shopping_cli.api import auth as api_auth
from shopping_cli.api import idempotency as api_idempotency
from shopping_cli.api.fallback_asgi import MarketplaceASGIApp
from shopping_cli.api.handlers import agents as agent_handlers
from shopping_cli.api.handlers import audit as audit_handlers
from shopping_cli.api.handlers import catalog as catalog_handlers
from shopping_cli.api.handlers import conversations as conversation_handlers
from shopping_cli.api.handlers import human_review as human_review_handlers
from shopping_cli.agents import buyer_cli
from shopping_cli.core.channels import ingest_buyer_message
from shopping_cli.core.errors import (
    AuthError,
    ConflictError,
    IdempotencyConflict,
    NotFoundError,
    PermissionDenied,
    RateLimitError,
    ShoppingCliError,
    ValidationError,
)
from shopping_cli.db.session import db_session
from shopping_cli.core.tokens import token_digest
from shopping_cli.services import buyer_bootstrap as buyer_bootstrap_service
from shopping_cli.services import conversations as conversation_service
from shopping_cli.services import tokens as token_service

try:  # pragma: no cover - exercised when optional dependency is installed
    from fastapi import FastAPI, Header
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
except ModuleNotFoundError:  # pragma: no cover - local CI currently has no fastapi
    FastAPI = None  # type: ignore[assignment]
    Header = None  # type: ignore[assignment]
    JSONResponse = None  # type: ignore[assignment]
    RequestValidationError = None  # type: ignore[assignment]


MAX_SQLITE_INTEGER = 2**63 - 1
DEFAULT_RESULT_LIMIT = 50
DEFAULT_BUYER_BOOTSTRAP_RATE_LIMIT_PER_MINUTE = api_idempotency.DEFAULT_BUYER_BOOTSTRAP_RATE_LIMIT_PER_MINUTE
BUYER_BOOTSTRAP_RATE_LIMIT_WINDOW_SECONDS = api_idempotency.BUYER_BOOTSTRAP_RATE_LIMIT_WINDOW_SECONDS
MAX_IDEMPOTENCY_KEY_LENGTH = api_idempotency.MAX_IDEMPOTENCY_KEY_LENGTH


Handler = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class RouteEntry:
    methods: set[str]
    path_template: str
    handler: Handler


def _json_error_response(status_code: int, error: str) -> Any:
    payload = {"ok": False, "error": error}
    if JSONResponse is not None:  # pragma: no cover - exercised with fastapi installed
        return JSONResponse(status_code=status_code, content=payload)
    return SimpleNamespace(status_code=status_code, body=json.dumps(payload, ensure_ascii=False).encode("utf-8"))


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


def _non_negative_whole_int(value: Any, field_name: str, default: int = 0) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValidationError(f"{field_name} must be a whole number")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValidationError(f"{field_name} must be a whole number")
        number = int(value)
    else:
        try:
            number = int(str(value).strip())
        except ValueError as exc:
            raise ValidationError(f"{field_name} must be a whole number") from exc
    if number < 0:
        raise ValidationError(f"{field_name} must be non-negative")
    if number > MAX_SQLITE_INTEGER:
        raise ValidationError(f"{field_name} must be <= {MAX_SQLITE_INTEGER}")
    return number


def _require_merchant_token(conn: Any, merchant_id: str, payload: dict[str, Any]) -> None:
    token_service.require_merchant_token(conn, merchant_id, api_auth.payload_token(payload))


def _resolve_agent_token(conn: Any, merchant_id: str, token: Any = "", token_prefix: Any = "") -> str:
    return token_service.resolve_agent_token(conn, merchant_id, token, token_prefix)


def _issue_buyer_token(conn: Any, buyer_id: str, conversation_id: str) -> str:
    return token_service.issue_buyer_token(conn, buyer_id, conversation_id)


def _ensure_buyer_token(conn: Any, buyer_id: str, conversation_id: str, token: str) -> str:
    return token_service.ensure_buyer_token(conn, buyer_id, conversation_id, token)


def _buyer_bootstrap_rate_limit_per_minute() -> int:
    return buyer_bootstrap_service.rate_limit_per_minute(
        os.environ.get("SHOPPING_BUYER_BOOTSTRAP_RATE_LIMIT_PER_MINUTE"),
        default=DEFAULT_BUYER_BOOTSTRAP_RATE_LIMIT_PER_MINUTE,
        maximum=MAX_SQLITE_INTEGER,
    )


def _enforce_buyer_bootstrap_rate_limit(conn: Any, bootstrap_token_hash: str) -> None:
    api_idempotency.enforce_buyer_bootstrap_rate_limit(
        conn,
        bootstrap_token_hash,
        _buyer_bootstrap_rate_limit_per_minute(),
    )


def _replay_buyer_idempotency(
    conn: Any,
    payload: dict[str, Any],
    endpoint: str,
    bootstrap_token_hash: str,
    idempotency_key: str,
    request_hash: str,
) -> dict[str, Any] | None:
    return api_idempotency.replay_buyer_idempotency(
        conn,
        payload,
        endpoint,
        bootstrap_token_hash,
        idempotency_key,
        request_hash,
        _ensure_buyer_token,
    )


def _claim_buyer_idempotency(
    conn: Any,
    payload: dict[str, Any],
    endpoint: str,
    bootstrap_token_hash: str,
    idempotency_key: str,
    request_hash: str,
) -> dict[str, Any] | None:
    return api_idempotency.claim_buyer_idempotency(
        conn,
        payload,
        endpoint,
        bootstrap_token_hash,
        idempotency_key,
        request_hash,
        _ensure_buyer_token,
    )


def _complete_buyer_idempotency(
    conn: Any,
    endpoint: str,
    bootstrap_token_hash: str,
    idempotency_key: str,
    request_hash: str,
    response: dict[str, Any],
) -> None:
    api_idempotency.complete_buyer_idempotency(
        conn,
        endpoint,
        bootstrap_token_hash,
        idempotency_key,
        request_hash,
        response,
        _non_negative_whole_int,
    )


def _clear_buyer_idempotency_claim(
    conn: Any,
    endpoint: str,
    bootstrap_token_hash: str,
    idempotency_key: str,
    request_hash: str,
) -> None:
    api_idempotency.clear_buyer_idempotency_claim(
        conn,
        endpoint,
        bootstrap_token_hash,
        idempotency_key,
        request_hash,
    )


def _health(db_path: str | Path) -> dict[str, Any]:
    return catalog_handlers.health(db_path)


def _create_merchant(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return catalog_handlers.create_merchant(db_path, payload, api_auth.require_admin_token)


def _update_merchant(db_path: str | Path, merchant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return catalog_handlers.update_merchant(db_path, merchant_id, payload, _require_merchant_token)


def _get_merchant(db_path: str | Path, merchant_id: str) -> dict[str, Any]:
    return catalog_handlers.get_merchant(db_path, merchant_id)


def _list_merchants(db_path: str | Path, query: dict[str, Any] | None = None) -> dict[str, Any]:
    return catalog_handlers.list_merchants(db_path, query)


def _create_product(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    return catalog_handlers.create_product(db_path, payload, _require_merchant_token)


def _update_product(db_path: str | Path, sku: str, payload: dict[str, Any]) -> dict[str, Any]:
    return catalog_handlers.update_product(db_path, sku, payload, _require_merchant_token)


def _get_product(db_path: str | Path, sku: str) -> dict[str, Any]:
    return catalog_handlers.get_product(db_path, sku)


def _search_products(db_path: str | Path, query: dict[str, Any]) -> dict[str, Any]:
    return catalog_handlers.search_products(db_path, query)


def _search_merchants(db_path: str | Path, query: dict[str, Any]) -> dict[str, Any]:
    return catalog_handlers.search_merchants(db_path, query)


def _buyer_ask(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    bootstrap_token_hash = api_auth.require_buyer_bootstrap_token(payload, token_digest)
    idempotency_key = api_idempotency.idempotency_key_from_payload(payload)
    request_hash = api_idempotency.buyer_ask_request_hash(payload)
    endpoint = "/buyer/ask"
    with db_session(db_path) as conn:
        replayed = _replay_buyer_idempotency(
            conn,
            payload,
            endpoint,
            bootstrap_token_hash,
            idempotency_key,
            request_hash,
        )
        if replayed is not None:
            return replayed
        _enforce_buyer_bootstrap_rate_limit(conn, bootstrap_token_hash)
        replayed = _claim_buyer_idempotency(
            conn,
            payload,
            endpoint,
            bootstrap_token_hash,
            idempotency_key,
            request_hash,
        )
        if replayed is not None:
            return replayed
        try:
            buyer_id = str(payload["buyer_id"])
            result = buyer_cli.ask(
                conn,
                buyer_id=buyer_id,
                text=str(payload["text"]),
                city=str(payload.get("city") or ""),
                area=str(payload.get("area") or ""),
                source_id=str(payload.get("source_id") or "buyer-cli"),
                host=str(payload.get("host") or ""),
                session_id=str(payload.get("session_id") or ""),
                reuse_open=False,
            )
            if result.get("conversation"):
                if idempotency_key:
                    token = api_idempotency.deterministic_buyer_token(
                        payload,
                        endpoint,
                        idempotency_key,
                        result["buyer_id"],
                        result["conversation"]["id"],
                    )
                    result["buyer_token"] = _ensure_buyer_token(
                        conn,
                        result["buyer_id"],
                        result["conversation"]["id"],
                        token,
                    )
                else:
                    result["buyer_token"] = _issue_buyer_token(conn, result["buyer_id"], result["conversation"]["id"])
            result["idempotent"] = False
            _complete_buyer_idempotency(conn, endpoint, bootstrap_token_hash, idempotency_key, request_hash, result)
            return result
        except KeyError as exc:
            raise ValidationError(f"missing required field: {exc.args[0]}") from exc
        except Exception:
            _clear_buyer_idempotency_claim(conn, endpoint, bootstrap_token_hash, idempotency_key, request_hash)
            raise


def _ingest_channel_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("buyer_id"):
        raise ValidationError("buyer_id override is not allowed for channel ingress")
    api_auth.require_channel_token(str(payload.get("channel") or ""), payload)
    with db_session(db_path) as conn:
        return ingest_buyer_message(
            conn,
            channel=str(payload["channel"]),
            external_user_id=str(payload["external_user_id"]),
            text=str(payload["text"]),
            city=str(payload.get("city") or ""),
            area=str(payload.get("area") or ""),
            conversation_id=str(payload.get("conversation_id") or ""),
            external_message_id=str(payload.get("external_message_id") or ""),
        )


def _get_conversation(db_path: str | Path, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return conversation_handlers.get_conversation(db_path, conversation_id, payload)


def _create_conversation(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    bootstrap_token_hash = api_auth.require_buyer_bootstrap_token(payload, token_digest)
    idempotency_key = api_idempotency.idempotency_key_from_payload(payload)
    request_hash = api_idempotency.conversation_create_request_hash(payload)
    endpoint = "/conversations"
    with db_session(db_path) as conn:
        replayed = _replay_buyer_idempotency(
            conn,
            payload,
            endpoint,
            bootstrap_token_hash,
            idempotency_key,
            request_hash,
        )
        if replayed is not None:
            return replayed
        _enforce_buyer_bootstrap_rate_limit(conn, bootstrap_token_hash)
        replayed = _claim_buyer_idempotency(
            conn,
            payload,
            endpoint,
            bootstrap_token_hash,
            idempotency_key,
            request_hash,
        )
        if replayed is not None:
            return replayed
        try:
            conversation = conversation_service.create_conversation(
                conn,
                buyer_id=str(payload["buyer_id"]),
                merchant_id=str(payload["merchant_id"]),
                sku=str(payload.get("sku") or ""),
                text=str(payload.get("text") or ""),
                intent=str(payload.get("intent") or "ask_product"),
                source_id=str(payload.get("source_id") or ""),
                reuse_open=False,
            )
            if idempotency_key:
                token = api_idempotency.deterministic_buyer_token(
                    payload,
                    endpoint,
                    idempotency_key,
                    conversation["buyer_id"],
                    conversation["id"],
                )
                buyer_token = _ensure_buyer_token(conn, conversation["buyer_id"], conversation["id"], token)
            else:
                buyer_token = _issue_buyer_token(conn, conversation["buyer_id"], conversation["id"])
            result = {
                "ok": True,
                "conversation": conversation,
                "buyer_token": buyer_token,
                "idempotent": False,
            }
            _complete_buyer_idempotency(conn, endpoint, bootstrap_token_hash, idempotency_key, request_hash, result)
            return result
        except KeyError as exc:
            raise ValidationError(f"missing required field: {exc.args[0]}") from exc
        except Exception:
            _clear_buyer_idempotency_claim(conn, endpoint, bootstrap_token_hash, idempotency_key, request_hash)
            raise


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


def _match_path(template: str, path: str) -> dict[str, str] | None:
    parts = template.split("/")
    regex_parts = []
    for part in parts:
        if part.startswith("{") and part.endswith("}"):
            param_name = part[1:-1]
            regex_parts.append(f"(?P<{param_name}>[^/]+)")
        else:
            regex_parts.append(re.escape(part))
    match = re.match("^" + "/".join(regex_parts) + "$", path)
    return match.groupdict() if match else None


def _buyer_conversation_list(db_path: str | Path, payload: dict[str, Any], query: dict[str, Any], buyer_id: str) -> dict[str, Any]:
    filters = dict(query)
    filters["buyer_id"] = buyer_id
    return _conversation_list(db_path, filters, payload, owner_kind="buyer", owner_id=buyer_id)


def _merchant_conversation_list(db_path: str | Path, payload: dict[str, Any], query: dict[str, Any], merchant_id: str) -> dict[str, Any]:
    filters = dict(query)
    filters["merchant_id"] = merchant_id
    return _conversation_list(db_path, filters, payload, owner_kind="merchant", owner_id=merchant_id)


def _merchant_human_review_list(db_path: str | Path, payload: dict[str, Any], query: dict[str, Any], merchant_id: str) -> dict[str, Any]:
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
    RouteEntry({"GET"}, "/merchants/{merchant_id}", lambda db_path, payload, query, merchant_id: _get_merchant(db_path, merchant_id)),
    RouteEntry({"PATCH"}, "/merchants/{merchant_id}", lambda db_path, payload, query, merchant_id: _update_merchant(db_path, merchant_id, payload)),
    RouteEntry({"GET"}, "/merchants/{merchant_id}/conversations", _merchant_conversation_list),
    RouteEntry({"GET"}, "/merchants/{merchant_id}/human-review", _merchant_human_review_list),
    RouteEntry({"GET"}, "/merchants/{merchant_id}/agents", lambda db_path, payload, query, merchant_id: _list_agents(
        db_path,
        payload,
        owner_id=merchant_id,
        limit=query.get("limit"),
        offset=query.get("offset"),
    )),
    RouteEntry({"POST"}, "/products", lambda db_path, payload, query, **kw: _create_product(db_path, payload)),
    RouteEntry({"GET"}, "/products/{sku}", lambda db_path, payload, query, sku: _get_product(db_path, sku)),
    RouteEntry({"PATCH"}, "/products/{sku}", lambda db_path, payload, query, sku: _update_product(db_path, sku, payload)),
    RouteEntry({"GET"}, "/search/products", lambda db_path, payload, query, **kw: _search_products(db_path, query)),
    RouteEntry({"GET"}, "/search/merchants", lambda db_path, payload, query, **kw: _search_merchants(db_path, query)),
    RouteEntry({"POST"}, "/channels/messages", lambda db_path, payload, query, **kw: _ingest_channel_message(db_path, payload)),
    RouteEntry({"POST"}, "/buyer/ask", lambda db_path, payload, query, **kw: _buyer_ask(db_path, payload)),
    RouteEntry({"POST"}, "/conversations", lambda db_path, payload, query, **kw: _create_conversation(db_path, payload)),
    RouteEntry({"GET"}, "/conversations/{conversation_id}", lambda db_path, payload, query, conversation_id: _get_conversation(db_path, conversation_id, payload)),
    RouteEntry({"POST"}, "/conversations/{conversation_id}/messages", lambda db_path, payload, query, conversation_id: _append_conversation_message(db_path, conversation_id, payload)),
    RouteEntry({"POST"}, "/conversations/{conversation_id}/close", lambda db_path, payload, query, conversation_id: _close_conversation(db_path, conversation_id, payload)),
    RouteEntry({"POST"}, "/conversations/{conversation_id}/human-review", lambda db_path, payload, query, conversation_id: _create_human_review(db_path, conversation_id, payload)),
    RouteEntry({"POST"}, "/conversations/{conversation_id}/human-review/resolve", lambda db_path, payload, query, conversation_id: _resolve_human_review(db_path, conversation_id, payload)),
    RouteEntry({"GET"}, "/buyers/{buyer_id}/conversations", _buyer_conversation_list),
    RouteEntry({"POST"}, "/agents/heartbeat", lambda db_path, payload, query, **kw: _agent_heartbeat(db_path, payload)),
    RouteEntry({"GET"}, "/agents/tokens", lambda db_path, payload, query, **kw: _list_agent_tokens(
        db_path,
        payload,
        merchant_id=str(query.get("merchant_id") or ""),
        limit=query.get("limit"),
        offset=query.get("offset"),
    )),
    RouteEntry({"POST"}, "/agents/tokens", lambda db_path, payload, query, **kw: _create_agent_token(db_path, payload)),
    RouteEntry({"POST"}, "/agents/tokens/revoke", lambda db_path, payload, query, **kw: _revoke_agent_token(db_path, payload)),
    RouteEntry({"POST"}, "/agents/tokens/rotate", lambda db_path, payload, query, **kw: _rotate_agent_token(db_path, payload)),
    RouteEntry({"POST"}, "/agents/messages/claim", lambda db_path, payload, query, **kw: _claim_agent_message(db_path, payload)),
    RouteEntry({"POST"}, "/agents/messages/complete", lambda db_path, payload, query, **kw: _complete_agent_message(db_path, payload)),
    RouteEntry({"POST"}, "/agents/messages/fail", lambda db_path, payload, query, **kw: _fail_agent_message(db_path, payload)),
    RouteEntry({"POST"}, "/agents/messages/abandon", lambda db_path, payload, query, **kw: _abandon_agent_message(db_path, payload)),
    RouteEntry({"POST"}, "/agents/messages/abandon-stale", lambda db_path, payload, query, **kw: _abandon_stale_agent_messages(db_path, payload)),
    RouteEntry({"GET"}, "/agents", lambda db_path, payload, query, **kw: _list_agents(
        db_path,
        payload,
        limit=query.get("limit"),
        offset=query.get("offset"),
    )),
    RouteEntry({"GET"}, "/agents/{agent_id}", lambda db_path, payload, query, agent_id: _get_agent(db_path, agent_id, payload)),
    RouteEntry({"POST"}, "/audit/tool-calls", lambda db_path, payload, query, **kw: _record_tool_call_audit(db_path, payload)),
    RouteEntry({"GET"}, "/audit/events", lambda db_path, payload, query, **kw: _audit_events(
        db_path,
        payload,
        merchant_id=str(query.get("merchant_id") or ""),
        event=str(query.get("event") or ""),
        limit=query.get("limit") or 50,
        offset=query.get("offset"),
    )),
    RouteEntry({"GET"}, "/human-review/queue", lambda db_path, payload, query, **kw: _human_review_queue(
        db_path,
        payload,
        merchant_id=str(query.get("merchant_id") or ""),
        limit=query.get("limit"),
        offset=query.get("offset"),
    )),
    RouteEntry({"GET"}, "/human-review/{review_id}", lambda db_path, payload, query, review_id: _get_human_review(db_path, review_id, payload)),
    RouteEntry({"POST"}, "/human-review/{review_id}/resolve", lambda db_path, payload, query, review_id: _resolve_human_review_item(db_path, review_id, payload)),
)


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
        for route in _ROUTE_TABLE:
            if method.upper() not in route.methods:
                continue
            path_params = _match_path(route.path_template, path)
            if path_params is not None:
                return 200, route.handler(db_path, payload, query, **path_params)
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
    except ValidationError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except ShoppingCliError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except Exception as exc:
        return 500, {"ok": False, "error": f"internal server error: {exc}"}


def create_app(db_path: str | Path = "shopping-cli.sqlite") -> Any:
    if FastAPI is None:
        return MarketplaceASGIApp(db_path)

    app = FastAPI(
        title="shopping-cli Marketplace API",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.db_path = str(db_path)
    app.state.fastapi_available = True

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

    @app.exception_handler(ValidationError)
    def validation_error_handler(_request: Any, exc: ValidationError) -> Any:
        return _json_error_response(400, str(exc))

    @app.exception_handler(ShoppingCliError)
    def shopping_cli_error_handler(_request: Any, exc: ShoppingCliError) -> Any:
        return _json_error_response(400, str(exc))

    if RequestValidationError is not None:  # pragma: no cover - exercised with fastapi installed
        @app.exception_handler(RequestValidationError)
        def request_validation_error_handler(_request: Any, exc: Exception) -> Any:
            return _json_error_response(400, str(exc))

    @app.get("/health")
    def health() -> dict[str, Any]:
        return _health(db_path)

    @app.get("/merchants")
    def list_merchants(limit: str = "", offset: str = "") -> dict[str, Any]:
        return _list_merchants(db_path, {"limit": limit, "offset": offset})

    @app.post("/merchants")
    def create_merchant(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _create_merchant(db_path, api_auth.payload_with_auth(payload, authorization))

    @app.get("/merchants/{merchant_id}")
    def get_merchant(merchant_id: str) -> dict[str, Any]:
        return _get_merchant(db_path, merchant_id)

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
        return _append_conversation_message(db_path, conversation_id, api_auth.payload_with_auth(payload, authorization))

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

    return app

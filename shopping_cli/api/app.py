"""Marketplace API app factory.

FastAPI is used when installed. The lightweight fallback keeps route metadata
available for local tests in environments where optional API dependencies have
not been installed yet.
"""

from __future__ import annotations

import json
import math
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

from shopping_cli import VERSION
from shopping_cli.agents import buyer_cli, merchant_agent
from shopping_cli.config import agent_stale_ttl_seconds_from
from shopping_cli.core import catalog
from shopping_cli.core.channels import ingest_buyer_message, normalize_channel
from shopping_cli.core.conversations import (
    add_flag,
    append_message,
    conversation_summary,
    ensure_conversation,
    merchant_conversations,
    normalize_structured_payload,
)
from shopping_cli.core.harness import (
    abandon_agent_message,
    abandon_stale_agent_messages,
    agent_message_process_summary,
    append_audit_event,
    audit_event_summary,
    audit_event_summary_from_row,
    claim_agent_message,
    complete_agent_message,
    fail_agent_message,
    next_actor_for_status,
)
from shopping_cli.db.session import db_session, decode_json, now_iso
from shopping_cli.core.tokens import token_digest, token_matches, token_prefix, token_suffix

try:  # pragma: no cover - exercised when optional dependency is installed
    from fastapi import FastAPI, Header
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
except ModuleNotFoundError:  # pragma: no cover - local CI currently has no fastapi
    FastAPI = None  # type: ignore[assignment]
    Header = None  # type: ignore[assignment]
    JSONResponse = None  # type: ignore[assignment]
    RequestValidationError = None  # type: ignore[assignment]


class AuthError(Exception):
    pass


HUMAN_REVIEW_ACTIONS = {"reply", "approve_public_answer", "reject", "close"}
HUMAN_REVIEW_SENDERS = {"merchant", "merchant_agent", "operator"}
MAX_SQLITE_INTEGER = 2**63 - 1
DEFAULT_RESULT_LIMIT = 50
MAX_RESULT_LIMIT = 100


def _json_error_response(status_code: int, error: str) -> Any:
    payload = {"ok": False, "error": error}
    if JSONResponse is not None:  # pragma: no cover - exercised with fastapi installed
        return JSONResponse(status_code=status_code, content=payload)
    return SimpleNamespace(status_code=status_code, body=json.dumps(payload, ensure_ascii=False).encode("utf-8"))


class RouteInfo:
    def __init__(self, path: str, methods: set[str]):
        self.path = path
        self.methods = methods


class MarketplaceASGIApp:
    title = "shopping-cli Marketplace API"

    def __init__(self, db_path: str | Path):
        self.state = SimpleNamespace(db_path=str(db_path), fastapi_available=False)
        self.routes = route_info()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"ok":false,"error":"unsupported scope"}'})
            return
        chunks: list[bytes] = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        try:
            decoded_payload = json.loads(b"".join(chunks).decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = json.dumps(
                {"ok": False, "error": "invalid JSON request body"},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        if not isinstance(decoded_payload, dict):
            body = json.dumps(
                {"ok": False, "error": "JSON request body must be an object"},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        payload = decoded_payload
        headers = {
            key.decode("latin1").lower(): value.decode("latin1")
            for key, value in scope.get("headers", [])
        }
        authorization = headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            payload["_auth_token"] = authorization.split(" ", 1)[1].strip()
        try:
            raw_query = scope.get("query_string", b"").decode("utf-8")
        except UnicodeDecodeError:
            raw_query = ""
        query = parse_qs(raw_query, keep_blank_values=True)
        status, response = handle_request(
            self.state.db_path,
            method=str(scope.get("method") or "GET").upper(),
            path=str(scope.get("path") or "/"),
            payload=payload,
            query={key: values[-1] if values else "" for key, values in query.items()},
        )
        body = json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})


def route_info() -> list[RouteInfo]:
    return [
        RouteInfo("/health", {"GET"}),
        RouteInfo("/merchants", {"GET", "POST"}),
        RouteInfo("/merchants/{merchant_id}", {"GET", "PATCH"}),
        RouteInfo("/products", {"POST"}),
        RouteInfo("/products/{sku}", {"GET", "PATCH"}),
        RouteInfo("/search/products", {"GET"}),
        RouteInfo("/search/merchants", {"GET"}),
        RouteInfo("/channels/messages", {"POST"}),
        RouteInfo("/buyer/ask", {"POST"}),
        RouteInfo("/conversations", {"POST"}),
        RouteInfo("/conversations/{conversation_id}", {"GET"}),
        RouteInfo("/conversations/{conversation_id}/messages", {"POST"}),
        RouteInfo("/conversations/{conversation_id}/close", {"POST"}),
        RouteInfo("/buyers/{buyer_id}/conversations", {"GET"}),
        RouteInfo("/agents/heartbeat", {"POST"}),
        RouteInfo("/agents/tokens", {"GET", "POST"}),
        RouteInfo("/agents/tokens/revoke", {"POST"}),
        RouteInfo("/agents/tokens/rotate", {"POST"}),
        RouteInfo("/agents/messages/claim", {"POST"}),
        RouteInfo("/agents/messages/complete", {"POST"}),
        RouteInfo("/agents/messages/fail", {"POST"}),
        RouteInfo("/agents/messages/abandon", {"POST"}),
        RouteInfo("/agents/messages/abandon-stale", {"POST"}),
        RouteInfo("/agents", {"GET"}),
        RouteInfo("/agents/{agent_id}", {"GET"}),
        RouteInfo("/merchants/{merchant_id}/agents", {"GET"}),
        RouteInfo("/audit/tool-calls", {"POST"}),
        RouteInfo("/audit/events", {"GET"}),
        RouteInfo("/human-review/queue", {"GET"}),
        RouteInfo("/human-review/{review_id}", {"GET"}),
        RouteInfo("/human-review/{review_id}/resolve", {"POST"}),
        RouteInfo("/merchants/{merchant_id}/conversations", {"GET"}),
        RouteInfo("/merchants/{merchant_id}/human-review", {"GET"}),
        RouteInfo("/conversations/{conversation_id}/human-review", {"POST"}),
        RouteInfo("/conversations/{conversation_id}/human-review/resolve", {"POST"}),
    ]


def _float_or_none(value: Any) -> Any:
    return None if value is None else value


def _bool_from_query(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _merchant_list(conn: Any, limit: int = DEFAULT_RESULT_LIMIT, offset: int = 0) -> list[dict[str, Any]]:
    return catalog.list_merchants(conn, limit=int(limit), offset=int(offset))


def _payload_token(payload: dict[str, Any]) -> str:
    return str(
        payload.get("merchant_token")
        or payload.get("agent_token")
        or payload.get("buyer_token")
        or payload.get("_auth_token")
        or ""
    )


def _payload_admin_token(payload: dict[str, Any]) -> str:
    return str(payload.get("admin_token") or payload.get("_auth_token") or "")


def _payload_channel_token(payload: dict[str, Any]) -> str:
    return str(payload.get("channel_token") or payload.get("_auth_token") or "")


def _auth_header_default() -> Any:
    if Header is None:
        return ""
    return Header(default="")


AUTHORIZATION_HEADER = _auth_header_default()


def _payload_with_auth(payload: dict[str, Any], authorization: Any = "") -> dict[str, Any]:
    merged = dict(payload or {})
    if isinstance(authorization, str) and authorization.lower().startswith("bearer "):
        merged["_auth_token"] = authorization.split(" ", 1)[1].strip()
    return merged


def _human_review_sender(payload: dict[str, Any]) -> str:
    sender = str(payload.get("sender") or "merchant").strip() or "merchant"
    if sender not in HUMAN_REVIEW_SENDERS:
        raise SystemExit(f"Unknown human-review sender: {sender}")
    return sender


def _configured_admin_token() -> str:
    return str(os.environ.get("SHOPPING_ADMIN_TOKEN") or "").strip()


def _require_admin_token(payload: dict[str, Any]) -> None:
    expected = _configured_admin_token()
    if not expected:
        raise AuthError("admin bootstrap token is not configured")
    token = _payload_admin_token(payload)
    if not token:
        raise AuthError("admin bootstrap token required")
    if not token_matches(token, expected):
        raise AuthError("invalid admin bootstrap token")


def _channel_token_map() -> dict[str, str]:
    tokens: dict[str, str] = {}
    global_token = str(os.environ.get("SHOPPING_CHANNEL_TOKEN") or "").strip()
    if global_token:
        tokens["*"] = global_token
    raw = str(os.environ.get("SHOPPING_CHANNEL_TOKENS") or "").strip()
    if not raw:
        return tokens
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        for channel, token in decoded.items():
            normalized = normalize_channel(str(channel))
            if normalized and str(token or "").strip():
                tokens[normalized] = str(token).strip()
        return tokens
    for part in raw.replace("\n", ",").split(","):
        text = part.strip()
        if not text:
            continue
        separator = ":" if ":" in text else "=" if "=" in text else ""
        if not separator:
            continue
        channel, token = text.split(separator, 1)
        normalized = normalize_channel(channel)
        if normalized and token.strip():
            tokens[normalized] = token.strip()
    return tokens


def _require_channel_token(channel: str, payload: dict[str, Any]) -> None:
    normalized = normalize_channel(channel)
    tokens = _channel_token_map()
    expected = tokens.get(normalized) or tokens.get("*") or ""
    if not expected:
        raise AuthError(f"channel token is not configured for {normalized or '-'}")
    token = _payload_channel_token(payload)
    if not token:
        raise AuthError("channel token required")
    if not token_matches(token, expected):
        raise AuthError("invalid channel token")


def _expires_at_from_ttl(ttl_seconds: Any) -> str:
    seconds = _positive_whole_seconds(ttl_seconds, "ttl_seconds")
    if seconds is None:
        return ""
    try:
        expires_at = datetime.now() + timedelta(seconds=seconds)
    except OverflowError as exc:
        raise ValueError("ttl_seconds is too large") from exc
    return expires_at.replace(microsecond=0).isoformat()


def _positive_whole_seconds(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a whole number")
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{field_name} must be a whole number")
        seconds = int(value)
    else:
        try:
            seconds = int(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a whole number") from exc
    if seconds <= 0:
        raise ValueError(f"{field_name} must be greater than 0")
    return seconds


def _non_negative_whole_int(value: Any, field_name: str, default: int = 0) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a whole number")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{field_name} must be a whole number")
        number = int(value)
    else:
        try:
            number = int(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a whole number") from exc
    if number < 0:
        raise ValueError(f"{field_name} must be non-negative")
    if number > MAX_SQLITE_INTEGER:
        raise ValueError(f"{field_name} must be <= {MAX_SQLITE_INTEGER}")
    return number


def _positive_whole_int(value: Any, field_name: str) -> int:
    number = _non_negative_whole_int(value, field_name)
    if number <= 0:
        raise ValueError(f"{field_name} must be greater than 0")
    return number


def _result_limit(value: Any, default: int = DEFAULT_RESULT_LIMIT) -> int:
    if value in (None, ""):
        return default
    return min(_positive_whole_int(value, "limit"), MAX_RESULT_LIMIT)


def _result_offset(value: Any) -> int:
    return _non_negative_whole_int(value, "offset", default=0)


def _token_is_expired(expires_at: str) -> bool:
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(str(expires_at))
    except (TypeError, ValueError):
        return True
    try:
        current = datetime.now(tz=expires.tzinfo) if expires.tzinfo is not None else datetime.now()
        return expires <= current
    except TypeError:
        return True


def _agent_token_summary(row: Any) -> dict[str, Any]:
    token_key = str(row["token"])
    prefix = str(row["token_prefix"] or token_key[:24])
    suffix = str(row["token_suffix"] or token_key[-6:])
    revoked = bool(row["revoked_at"])
    expired = _token_is_expired(row["expires_at"])
    return {
        "token_prefix": prefix,
        "token_suffix": suffix,
        "token_role": row["role"],
        "merchant_id": row["merchant_id"],
        "agent_id": row["agent_id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "revoked": revoked,
        "expired": expired,
        "active": not revoked and not expired,
    }


def _agent_token_row(conn: Any, token: str) -> Any:
    raw = str(token or "")
    digest = token_digest(raw)
    return conn.execute(
        """
        select token, token_hash, token_prefix, token_suffix, role, merchant_id, agent_id, created_at, expires_at, revoked_at
        from api_tokens
        where token = ? or token = ? or token_hash = ?
        """,
        (raw, digest, digest),
    ).fetchone()


def _resolve_agent_token(conn: Any, merchant_id: str, token: Any = "", token_prefix: Any = "") -> str:
    resolved = str(token or "")
    if resolved:
        row = _agent_token_row(conn, resolved)
        if row is None or row["role"] != "agent" or row["merchant_id"] != merchant_id:
            raise AuthError("invalid agent token")
        return str(row["token"])
    prefix = str(token_prefix or "")
    if not prefix:
        raise ValueError("token or token_prefix is required")
    rows = conn.execute(
        """
        select token from api_tokens
        where merchant_id = ? and role = 'agent' and (token_prefix like ? or token like ?)
        order by created_at desc, token
        limit 2
        """,
        (merchant_id, f"{prefix}%", f"{prefix}%"),
    ).fetchall()
    if not rows:
        raise AuthError("invalid agent token")
    if len(rows) > 1:
        raise ValueError("token_prefix is ambiguous")
    return str(rows[0]["token"])


def _append_agent_token_audit(conn: Any, merchant_id: str, event: str, details: dict[str, Any]) -> None:
    append_audit_event(conn, "", merchant_id, event, details)


def _audit_event_limit(value: Any) -> int:
    if value in (None, ""):
        return 50
    limit = _positive_whole_int(value, "limit")
    return min(limit, 200)


def _merchant_audit_events(
    conn: Any,
    merchant_id: str,
    event: str = "",
    limit: Any = 50,
    offset: Any = 0,
) -> list[dict[str, Any]]:
    sql = "select id, conversation_id, actor, event, details_json, created_at from audit_events where actor = ?"
    values: list[Any] = [merchant_id]
    if event:
        sql += " and event = ?"
        values.append(event)
    sql += " order by id desc limit ? offset ?"
    values.extend([_audit_event_limit(limit), _result_offset(offset)])
    rows = conn.execute(sql, values).fetchall()
    return [audit_event_summary_from_row(row) for row in rows]


def _issue_merchant_token(conn: Any, merchant_id: str) -> str:
    token = f"shopping_merchant_{secrets.token_urlsafe(24)}"
    digest = token_digest(token)
    conn.execute(
        """
        insert into api_tokens(token, token_hash, token_prefix, token_suffix, role, merchant_id, buyer_id, agent_id, created_at)
        values (?, ?, ?, ?, 'merchant', ?, '', '', ?)
        """,
        (digest, digest, token_prefix(token), token_suffix(token), merchant_id, now_iso()),
    )
    return token


def _issue_agent_token(conn: Any, merchant_id: str, agent_id: str, ttl_seconds: Any = None) -> tuple[str, str]:
    token = f"shopping_agent_{secrets.token_urlsafe(24)}"
    digest = token_digest(token)
    expires_at = _expires_at_from_ttl(ttl_seconds)
    conn.execute(
        """
        insert into api_tokens(token, token_hash, token_prefix, token_suffix, role, merchant_id, buyer_id, agent_id, expires_at, created_at)
        values (?, ?, ?, ?, 'agent', ?, '', ?, ?, ?)
        """,
        (digest, digest, token_prefix(token), token_suffix(token), merchant_id, agent_id, expires_at, now_iso()),
    )
    return token, expires_at


def _issue_buyer_token(conn: Any, buyer_id: str, conversation_id: str) -> str:
    token = f"shopping_buyer_{secrets.token_urlsafe(24)}"
    digest = token_digest(token)
    conn.execute(
        """
        insert into api_tokens(token, token_hash, token_prefix, token_suffix, role, merchant_id, buyer_id, agent_id, conversation_id, created_at)
        values (?, ?, ?, ?, 'buyer', '', ?, '', ?, ?)
        """,
        (digest, digest, token_prefix(token), token_suffix(token), buyer_id, conversation_id, now_iso()),
    )
    return token


def _require_api_token(conn: Any, payload: dict[str, Any], missing_error: str = "authorization token required") -> Any:
    token = _payload_token(payload)
    if not token:
        raise AuthError(missing_error)
    digest = token_digest(token)
    row = conn.execute(
        """
        select role, merchant_id, buyer_id, agent_id, conversation_id, revoked_at, expires_at
        from api_tokens
        where token_hash = ?
        """,
        (digest,),
    ).fetchone()
    if row is None:
        raise AuthError("invalid authorization token")
    if row["revoked_at"]:
        raise AuthError("revoked authorization token")
    if _token_is_expired(row["expires_at"]):
        raise AuthError("expired authorization token")
    return row


def _require_merchant_token(conn: Any, merchant_id: str, payload: dict[str, Any]) -> None:
    row = _require_api_token(conn, payload, "merchant token required")
    if row is None or row["role"] != "merchant" or row["merchant_id"] != merchant_id:
        raise AuthError("invalid merchant token")


def _require_agent_or_merchant_token(conn: Any, merchant_id: str, agent_id: str, payload: dict[str, Any]) -> None:
    if agent_id != _default_merchant_agent_id(merchant_id):
        raise AuthError(f"Agent {agent_id} cannot act for merchant {merchant_id}")
    row = _require_api_token(conn, payload, "agent or merchant token required")
    if row is None or row["merchant_id"] != merchant_id:
        raise AuthError("invalid agent or merchant token")
    if row["role"] == "merchant":
        return
    if row["role"] == "agent" and row["agent_id"] == agent_id:
        return
    raise AuthError("invalid agent or merchant token")


def _require_conversation_read_token(conn: Any, conversation: dict[str, Any], payload: dict[str, Any]) -> None:
    row = _require_api_token(conn, payload, "conversation read token required")
    if (
        row["role"] == "buyer"
        and row["buyer_id"] == conversation["buyer_id"]
        and row["conversation_id"] == conversation["id"]
    ):
        return
    if row["role"] == "merchant" and row["merchant_id"] == conversation["merchant_id"]:
        return
    if row["role"] == "agent" and row["merchant_id"] == conversation["merchant_id"]:
        return
    raise AuthError("invalid conversation read token")


def _require_buyer_conversation_token(conn: Any, conversation: dict[str, Any], payload: dict[str, Any]) -> None:
    row = _require_api_token(conn, payload, "buyer conversation token required")
    if (
        row["role"] == "buyer"
        and row["buyer_id"] == conversation["buyer_id"]
        and row["conversation_id"] == conversation["id"]
    ):
        return
    raise AuthError("invalid buyer conversation token")


def _require_buyer_read_token(conn: Any, buyer_id: str, payload: dict[str, Any]) -> Any:
    row = _require_api_token(conn, payload, "buyer conversation read token required")
    if row["role"] == "buyer" and row["buyer_id"] == buyer_id:
        return row
    raise AuthError("invalid buyer conversation read token")


def _require_merchant_read_token(conn: Any, merchant_id: str, payload: dict[str, Any]) -> None:
    row = _require_api_token(conn, payload, "merchant conversation read token required")
    if row["role"] == "merchant" and row["merchant_id"] == merchant_id:
        return
    if row["role"] == "agent" and row["merchant_id"] == merchant_id:
        return
    raise AuthError("invalid merchant conversation read token")


def _health(db_path: str | Path) -> dict[str, Any]:
    with db_session(db_path):
        return {"ok": True, "service": "shopping-cli-marketplace", "version": VERSION, "storage": "sqlite"}


def _create_merchant(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    _require_admin_token(payload)
    with db_session(db_path) as conn:
        merchant = catalog.create_merchant(
            conn,
            merchant_id=str(payload["id"]),
            name=str(payload["name"]),
            city=str(payload.get("city") or ""),
            service_area=str(payload.get("service_area") or ""),
            contact=str(payload.get("contact") or ""),
            hours=str(payload.get("hours") or ""),
            automation_boundaries=str(payload.get("automation_boundaries") or ""),
            tags=payload.get("tags") or [],
            delivery_fee=payload.get("delivery_fee", 0),
            delivery_eta_minutes=payload.get("delivery_eta_minutes", 0),
            delivery_radius_km=payload.get("delivery_radius_km", 0),
        )
        token = _issue_merchant_token(conn, merchant["id"])
        return {"ok": True, "merchant": merchant, "merchant_token": token}


def _update_merchant(db_path: str | Path, merchant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        _require_merchant_token(conn, merchant_id, payload)
        merchant = catalog.update_merchant(
            conn,
            merchant_id=merchant_id,
            name=payload.get("name"),
            city=payload.get("city"),
            service_area=payload.get("service_area"),
            contact=payload.get("contact"),
            hours=payload.get("hours"),
            automation_boundaries=payload.get("automation_boundaries"),
            tags=payload.get("tags") if "tags" in payload else None,
            delivery_fee=_float_or_none(payload.get("delivery_fee")),
            delivery_eta_minutes=payload.get("delivery_eta_minutes"),
            delivery_radius_km=_float_or_none(payload.get("delivery_radius_km")),
        )
        return {"ok": True, "merchant": merchant}


def _get_merchant(db_path: str | Path, merchant_id: str) -> dict[str, Any]:
    with db_session(db_path) as conn:
        return {"ok": True, "merchant": catalog.merchant_summary(conn, merchant_id)}


def _list_merchants(db_path: str | Path, query: dict[str, Any] | None = None) -> dict[str, Any]:
    query = query or {}
    with db_session(db_path) as conn:
        return {
            "ok": True,
            "results": _merchant_list(
                conn,
                limit=_result_limit(query.get("limit")),
                offset=_result_offset(query.get("offset")),
            ),
        }


def _create_product(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id = str(payload["merchant_id"])
        _require_merchant_token(conn, merchant_id, payload)
        product = catalog.create_product(
            conn,
            merchant_id=merchant_id,
            sku=str(payload["sku"]),
            title=str(payload["title"]),
            price=payload["price"],
            stock=payload["stock"],
            currency=str(payload.get("currency") or "CNY"),
            category=str(payload.get("category") or ""),
            tags=payload.get("tags") or [],
            description=str(payload.get("description") or ""),
            delivery_attributes=payload.get("delivery_attributes") or [],
        )
        return {"ok": True, "product": product}


def _update_product(db_path: str | Path, sku: str, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        existing = catalog.product_summary(conn, sku)
        merchant_id = str(payload.get("merchant_id") or existing["merchant_id"])
        _require_merchant_token(conn, merchant_id, payload)
        product = catalog.update_product(
            conn,
            sku=sku,
            merchant_id=merchant_id,
            title=payload.get("title"),
            price=_float_or_none(payload.get("price")),
            stock=payload.get("stock"),
            currency=payload.get("currency"),
            category=payload.get("category"),
            tags=payload.get("tags") if "tags" in payload else None,
            description=payload.get("description"),
            delivery_attributes=payload.get("delivery_attributes") if "delivery_attributes" in payload else None,
        )
        return {"ok": True, "product": product}


def _get_product(db_path: str | Path, sku: str) -> dict[str, Any]:
    with db_session(db_path) as conn:
        return {"ok": True, "product": catalog.product_summary(conn, sku)}


def _search_products(db_path: str | Path, query: dict[str, Any]) -> dict[str, Any]:
    max_price = query.get("max_price")
    with db_session(db_path) as conn:
        return {
            "ok": True,
            "results": catalog.search_products(
                conn,
                query=str(query.get("query") or ""),
                city=str(query.get("city") or ""),
                area=str(query.get("area") or ""),
                max_price=max_price if str(max_price or "") else None,
                include_out_of_stock=_bool_from_query(query.get("include_out_of_stock")),
                limit=_result_limit(query.get("limit"), default=10),
                offset=_result_offset(query.get("offset")),
            ),
        }


def _search_merchants(db_path: str | Path, query: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        return {
            "ok": True,
            "results": catalog.search_merchants(
                conn,
                query=str(query.get("query") or ""),
                city=str(query.get("city") or ""),
                limit=_result_limit(query.get("limit"), default=10),
                offset=_result_offset(query.get("offset")),
            ),
        }


def _buyer_ask(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
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
            result["buyer_token"] = _issue_buyer_token(conn, result["buyer_id"], result["conversation"]["id"])
        return result


def _ingest_channel_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("buyer_id"):
        raise SystemExit("buyer_id override is not allowed for channel ingress")
    _require_channel_token(str(payload.get("channel") or ""), payload)
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
    with db_session(db_path) as conn:
        conversation = conversation_summary(conn, conversation_id)
        _require_conversation_read_token(conn, conversation, payload)
        return {"ok": True, "conversation": conversation}


def _create_conversation(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        buyer_id = str(payload["buyer_id"])
        conversation = ensure_conversation(
            conn,
            buyer_id=buyer_id,
            merchant_id=str(payload["merchant_id"]),
            sku=str(payload.get("sku") or ""),
            reuse_open=False,
        )
        if payload.get("text"):
            append_message(
                conn,
                conversation["id"],
                "buyer",
                str(payload.get("intent") or "ask_product"),
                str(payload["text"]),
                structured_payload={"source_id": payload.get("source_id") or ""},
            )
            conversation = conversation_summary(conn, conversation["id"])
        return {
            "ok": True,
            "conversation": conversation,
            "buyer_token": _issue_buyer_token(conn, conversation["buyer_id"], conversation["id"]),
        }


def _append_conversation_message(db_path: str | Path, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        conversation = conversation_summary(conn, conversation_id)
        sender = str(payload["sender"])
        structured_payload = normalize_structured_payload(payload.get("structured_payload"))
        if payload.get("source_id"):
            structured_payload["source_id"] = payload.get("source_id")
        status = payload.get("status")
        if sender in {"buyer", "buyer_cli"}:
            _require_buyer_conversation_token(conn, conversation, payload)
            if status not in (None, ""):
                raise SystemExit("buyer messages cannot set conversation status")
        elif sender == "merchant":
            _require_merchant_token(conn, conversation["merchant_id"], payload)
        elif sender == "merchant_agent":
            agent_id = str(structured_payload.get("source_id") or _default_merchant_agent_id(conversation["merchant_id"]))
            _require_agent_or_merchant_token(conn, conversation["merchant_id"], agent_id, payload)
        elif sender == "operator":
            _require_merchant_token(conn, conversation["merchant_id"], payload)
        else:
            raise SystemExit(f"Unknown conversation sender: {sender}")
        if str(status or "").strip() == "closed":
            raise SystemExit("conversation messages cannot close conversations; use the close endpoint")
        message = append_message(
            conn,
            conversation_id,
            sender=sender,
            intent=str(payload["intent"]),
            text=str(payload["text"]),
            structured_payload=structured_payload,
            status=status,
        )
        if str(status or "").strip() == "human_required":
            add_flag(
                conn,
                conversation_id,
                reason=str(message["structured_payload"].get("reason") or "human_required"),
                severity=str(message["structured_payload"].get("severity") or "review"),
                sku=conversation.get("sku") or "",
            )
        return {"ok": True, "message": message, "conversation": conversation_summary(conn, conversation_id)}


def _close_conversation(db_path: str | Path, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        conversation = conversation_summary(conn, conversation_id)
        sender = str(payload.get("sender") or "operator")
        if sender in {"buyer", "buyer_cli"}:
            _require_buyer_conversation_token(conn, conversation, payload)
        elif sender == "merchant":
            _require_merchant_token(conn, conversation["merchant_id"], payload)
        elif sender == "merchant_agent":
            agent_id = str(payload.get("source_id") or _default_merchant_agent_id(conversation["merchant_id"]))
            _require_agent_or_merchant_token(conn, conversation["merchant_id"], agent_id, payload)
        elif sender == "operator":
            _require_merchant_token(conn, conversation["merchant_id"], payload)
        else:
            raise SystemExit(f"Unknown conversation sender: {sender}")
        if conversation["status"] == "closed":
            raise SystemExit(f"Conversation {conversation_id} is closed")
        next_actor = next_actor_for_status("closed")
        if payload.get("text"):
            append_message(
                conn,
                conversation_id,
                sender=sender,
                intent=str(payload.get("intent") or "support"),
                text=str(payload["text"]),
                structured_payload={"source_id": payload.get("source_id") or ""},
                status="closed",
            )
        else:
            conn.execute(
                "update conversations set status = 'closed', next_actor = ?, updated_at = ?, last_sender = ? where id = ?",
                (next_actor, now_iso(), sender, conversation_id),
            )
        _append_conversation_closed_audit(conn, conversation_id, sender, next_actor)
        return {"ok": True, "conversation": conversation_summary(conn, conversation_id)}


def _append_conversation_closed_audit(
    conn: Any,
    conversation_id: str,
    actor: str,
    next_actor: str,
    details: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"next_actor": next_actor}
    if details:
        payload.update(details)
    append_audit_event(conn, conversation_id, actor, "conversation_closed", payload)


def _agent_heartbeat(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id = str(payload["merchant_id"])
        agent_id = _default_merchant_agent_id(merchant_id)
        _require_agent_or_merchant_token(conn, merchant_id, agent_id, payload)
        agent = merchant_agent.heartbeat(
            conn,
            merchant_id=merchant_id,
            status=str(payload.get("status") or "online"),
            capabilities=payload.get("capabilities"),
            pid=_non_negative_whole_int(payload.get("pid"), "pid"),
            version=str(payload.get("version") or ""),
            last_error=str(payload.get("last_error") or ""),
            checked_count=_non_negative_whole_int(payload.get("checked_count"), "checked_count"),
            replied_count=_non_negative_whole_int(payload.get("replied_count"), "replied_count"),
        )
        return {"ok": True, "agent": agent}


def _default_merchant_agent_id(merchant_id: str) -> str:
    return f"shopping-cli-merchant-agent:{merchant_id}"


def _create_agent_token(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id = str(payload["merchant_id"])
        _require_merchant_token(conn, merchant_id, payload)
        agent_id = str(payload.get("agent_id") or _default_merchant_agent_id(merchant_id))
        if agent_id != _default_merchant_agent_id(merchant_id):
            raise AuthError(f"Agent {agent_id} cannot act for merchant {merchant_id}")
        token, expires_at = _issue_agent_token(conn, merchant_id, agent_id, payload.get("ttl_seconds"))
        issued = _agent_token_row(conn, token)
        _append_agent_token_audit(
            conn,
            merchant_id,
            "agent_token_issued",
            {"agent_id": agent_id, "token": _agent_token_summary(issued)},
        )
        return {"ok": True, "merchant_id": merchant_id, "agent_id": agent_id, "agent_token": token, "expires_at": expires_at}


def _list_agent_tokens(
    db_path: str | Path,
    payload: dict[str, Any],
    merchant_id: str = "",
    limit: Any = DEFAULT_RESULT_LIMIT,
    offset: Any = 0,
) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id = str(merchant_id or payload.get("merchant_id") or "")
        if not merchant_id:
            raise ValueError("merchant_id is required")
        _require_merchant_token(conn, merchant_id, payload)
        rows = conn.execute(
            """
            select token, token_hash, token_prefix, token_suffix, role, merchant_id, agent_id, created_at, expires_at, revoked_at
            from api_tokens
            where merchant_id = ? and role = 'agent'
            order by created_at desc, token desc
            limit ? offset ?
            """,
            (merchant_id, _result_limit(limit), _result_offset(offset)),
        ).fetchall()
        return {"ok": True, "merchant_id": merchant_id, "tokens": [_agent_token_summary(row) for row in rows]}


def _revoke_agent_token(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id = str(payload["merchant_id"])
        _require_merchant_token(conn, merchant_id, payload)
        token = _resolve_agent_token(conn, merchant_id, payload.get("token"), payload.get("token_prefix"))
        row = _agent_token_row(conn, token)
        if row is None or row["role"] != "agent" or row["merchant_id"] != merchant_id:
            raise AuthError("invalid agent token")
        revoked_at = row["revoked_at"] or now_iso()
        if not row["revoked_at"]:
            conn.execute("update api_tokens set revoked_at = ? where token = ?", (revoked_at, token))
        revoked = _agent_token_row(conn, token)
        _append_agent_token_audit(
            conn,
            merchant_id,
            "agent_token_revoked",
            {"agent_id": row["agent_id"], "revoked_at": revoked_at, "token": _agent_token_summary(revoked)},
        )
        return {
            "ok": True,
            "revoked": True,
            "merchant_id": merchant_id,
            "agent_id": row["agent_id"],
            "token_role": row["role"],
            "revoked_at": revoked_at,
        }


def _rotate_agent_token(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id = str(payload["merchant_id"])
        _require_merchant_token(conn, merchant_id, payload)
        old_token = _resolve_agent_token(conn, merchant_id, payload.get("token"), payload.get("token_prefix"))
        row = _agent_token_row(conn, old_token)
        if row is None or row["role"] != "agent" or row["merchant_id"] != merchant_id:
            raise AuthError("invalid agent token")
        revoked_at = row["revoked_at"] or now_iso()
        if not row["revoked_at"]:
            conn.execute("update api_tokens set revoked_at = ? where token = ?", (revoked_at, old_token))
        new_token, expires_at = _issue_agent_token(conn, merchant_id, row["agent_id"], payload.get("ttl_seconds"))
        previous = _agent_token_row(conn, old_token)
        replacement = _agent_token_row(conn, new_token)
        _append_agent_token_audit(
            conn,
            merchant_id,
            "agent_token_rotated",
            {
                "agent_id": row["agent_id"],
                "revoked_at": revoked_at,
                "previous_token": _agent_token_summary(previous),
                "new_token": _agent_token_summary(replacement),
            },
        )
        return {
            "ok": True,
            "rotated": True,
            "merchant_id": merchant_id,
            "agent_id": row["agent_id"],
            "agent_token": new_token,
            "expires_at": expires_at,
            "revoked_at": revoked_at,
            "previous_token": _agent_token_summary(previous),
        }


def _require_agent_payload(conn: Any, payload: dict[str, Any]) -> tuple[str, str]:
    merchant_id = str(payload["merchant_id"])
    agent_id = str(payload.get("agent_id") or _default_merchant_agent_id(merchant_id))
    _require_agent_or_merchant_token(conn, merchant_id, agent_id, payload)
    return merchant_id, agent_id


def _require_agent_conversation(conn: Any, merchant_id: str, conversation_id: str) -> dict[str, Any]:
    conversation = conversation_summary(conn, conversation_id)
    if conversation["merchant_id"] != merchant_id:
        raise AuthError(f"Merchant {merchant_id} cannot access conversation {conversation_id}")
    return conversation


def _require_message_in_conversation(conn: Any, conversation_id: str, message_id: int) -> None:
    row = conn.execute("select conversation_id, sender from messages where id = ?", (message_id,)).fetchone()
    if row is None:
        raise SystemExit(f"Unknown message: {message_id}")
    if row["conversation_id"] != conversation_id:
        raise SystemExit(f"Message {message_id} does not belong to conversation {conversation_id}")
    if row["sender"] != "buyer":
        raise SystemExit(f"Agent can only claim buyer messages, got {row['sender']}")


def _require_agent_process_scope(conn: Any, merchant_id: str, agent_id: str, message_id: int) -> dict[str, Any]:
    process = agent_message_process_summary(conn, agent_id, message_id)
    _require_agent_conversation(conn, merchant_id, process["conversation_id"])
    return process


def _claim_agent_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id, agent_id = _require_agent_payload(conn, payload)
        conversation_id = str(payload["conversation_id"])
        _require_agent_conversation(conn, merchant_id, conversation_id)
        message_id = _positive_whole_int(payload["message_id"], "message_id")
        _require_message_in_conversation(conn, conversation_id, message_id)
        claim = claim_agent_message(
            conn,
            agent_id,
            conversation_id,
            message_id,
            str(payload["idempotency_key"]),
        )
        return {"ok": True, "claim": claim}


def _complete_agent_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id, agent_id = _require_agent_payload(conn, payload)
        message_id = _positive_whole_int(payload["message_id"], "message_id")
        _require_agent_process_scope(conn, merchant_id, agent_id, message_id)
        process = complete_agent_message(conn, agent_id, message_id)
        return {"ok": True, "process": process}


def _fail_agent_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id, agent_id = _require_agent_payload(conn, payload)
        message_id = _positive_whole_int(payload["message_id"], "message_id")
        _require_agent_process_scope(conn, merchant_id, agent_id, message_id)
        process = fail_agent_message(conn, agent_id, message_id, str(payload.get("error") or "agent failure"))
        return {"ok": True, "process": process}


def _abandon_agent_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id, agent_id = _require_agent_payload(conn, payload)
        message_id = _positive_whole_int(payload["message_id"], "message_id")
        _require_agent_process_scope(conn, merchant_id, agent_id, message_id)
        process = abandon_agent_message(conn, agent_id, message_id, str(payload.get("error") or "agent abandoned claim"))
        return {"ok": True, "process": process}


def _abandon_stale_agent_messages(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        _merchant_id, agent_id = _require_agent_payload(conn, payload)
        abandoned = abandon_stale_agent_messages(
            conn,
            agent_id,
            stale_after_seconds=(
                _positive_whole_seconds(payload.get("stale_after_seconds", 300), "stale_after_seconds") or 300
            ),
        )
        return {"ok": True, "abandoned": abandoned}


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


def _agent_summary(row: Any) -> dict[str, Any]:
    stale_ttl = timedelta(seconds=agent_stale_ttl_seconds_from())
    last_seen_at = row["last_seen_at"]
    try:
        stale = datetime.now() - datetime.fromisoformat(last_seen_at) > stale_ttl
    except (TypeError, ValueError):
        stale = True
    return {
        "id": row["id"],
        "type": row["type"],
        "owner_id": row["owner_id"],
        "status": row["status"],
        "capabilities": decode_json(row["capabilities_json"], []),
        "last_seen_at": last_seen_at,
        "stale": stale,
        "stale_ttl_seconds": int(stale_ttl.total_seconds()),
        "pid": _safe_non_negative_int(row["pid"]),
        "version": row["version"],
        "last_error": row["last_error"],
        "checked_count": _safe_non_negative_int(row["checked_count"]),
        "replied_count": _safe_non_negative_int(row["replied_count"]),
    }


def _list_agents(
    db_path: str | Path,
    payload: dict[str, Any],
    owner_id: str = "",
    limit: Any = DEFAULT_RESULT_LIMIT,
    offset: Any = 0,
) -> dict[str, Any]:
    with db_session(db_path) as conn:
        scoped_owner_id = str(owner_id or "")
        if scoped_owner_id:
            _require_merchant_read_token(conn, scoped_owner_id, payload)
        else:
            token_row = _require_api_token(conn, payload, "agent read token required")
            if token_row["role"] not in {"merchant", "agent"} or not token_row["merchant_id"]:
                raise AuthError("invalid agent read token")
            scoped_owner_id = str(token_row["merchant_id"])
        rows = conn.execute(
            "select * from agents where owner_id = ? order by id limit ? offset ?",
            (scoped_owner_id, _result_limit(limit), _result_offset(offset)),
        ).fetchall()
        return {"ok": True, "agents": [_agent_summary(row) for row in rows]}


def _get_agent(db_path: str | Path, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        row = conn.execute("select * from agents where id = ?", (agent_id,)).fetchone()
        if row is None:
            raise SystemExit(f"Unknown agent: {agent_id}")
        _require_merchant_read_token(conn, row["owner_id"], payload)
        return {"ok": True, "agent": _agent_summary(row)}


def _conversation_list(
    db_path: str | Path,
    filters: dict[str, Any],
    payload: dict[str, Any],
    owner_kind: str,
    owner_id: str,
) -> dict[str, Any]:
    clauses: list[str] = []
    values: list[Any] = []
    for column in ("status", "merchant_id", "buyer_id", "sku"):
        if filters.get(column):
            clauses.append(f"{column} = ?")
            values.append(str(filters[column]))
    if filters.get("updated_since"):
        clauses.append("updated_at >= ?")
        values.append(str(filters["updated_since"]))
    with db_session(db_path) as conn:
        if owner_kind == "buyer":
            token_row = _require_buyer_read_token(conn, owner_id, payload)
            if token_row["conversation_id"]:
                clauses.append("id = ?")
                values.append(str(token_row["conversation_id"]))
        elif owner_kind == "merchant":
            _require_merchant_read_token(conn, owner_id, payload)
        else:
            raise AuthError("conversation list owner is required")
        sql = "select id from conversations"
        if clauses:
            sql += " where " + " and ".join(clauses)
        sql += " order by updated_at desc limit ? offset ?"
        values.extend([_result_limit(filters.get("limit")), _result_offset(filters.get("offset"))])
        rows = conn.execute(sql, values).fetchall()
        return {"ok": True, "conversations": [conversation_summary(conn, row["id"]) for row in rows]}


def _merchant_conversations(
    db_path: str | Path,
    merchant_id: str,
    payload: dict[str, Any],
    status: str = "",
    limit: Any = DEFAULT_RESULT_LIMIT,
    offset: Any = 0,
) -> dict[str, Any]:
    with db_session(db_path) as conn:
        _require_merchant_read_token(conn, merchant_id, payload)
        return {
            "ok": True,
            "merchant_id": merchant_id,
            "conversations": merchant_conversations(
                conn,
                merchant_id,
                status,
                limit=_result_limit(limit),
                offset=_result_offset(offset),
            ),
        }


def _review_summary(conn: Any, flag_row: Any) -> dict[str, Any]:
    row_keys = set(flag_row.keys()) if hasattr(flag_row, "keys") else set()
    if {"merchant_id", "buyer_id"}.issubset(row_keys):
        merchant_id = flag_row["merchant_id"]
        buyer_id = flag_row["buyer_id"]
    else:
        conversation = conversation_summary(conn, flag_row["conversation_id"])
        merchant_id = conversation["merchant_id"]
        buyer_id = conversation["buyer_id"]
    return {
        "id": flag_row["id"],
        "conversation_id": flag_row["conversation_id"],
        "merchant_id": merchant_id,
        "buyer_id": buyer_id,
        "sku": flag_row["sku"],
        "reason": flag_row["reason"],
        "severity": flag_row["severity"],
        "created_at": flag_row["created_at"],
        "resolved_at": flag_row["resolved_at"] or None,
        "resolution": flag_row["resolution"],
        "resolved_by": flag_row["resolved_by"],
    }


def _human_review_queue(
    db_path: str | Path,
    payload: dict[str, Any],
    merchant_id: str = "",
    limit: Any = DEFAULT_RESULT_LIMIT,
    offset: Any = 0,
) -> dict[str, Any]:
    if not merchant_id:
        raise AuthError("merchant_id is required for human-review queue")
    sql = """
        select f.*, c.merchant_id as merchant_id, c.buyer_id as buyer_id
        from moderation_flags f
        join conversations c on c.id = f.conversation_id
        where f.resolved_at = ''
    """
    values: list[Any] = []
    sql += " and c.merchant_id = ?"
    values.append(merchant_id)
    sql += " order by f.created_at desc, f.id desc limit ? offset ?"
    values.extend([_result_limit(limit), _result_offset(offset)])
    with db_session(db_path) as conn:
        _require_merchant_read_token(conn, merchant_id, payload)
        rows = conn.execute(sql, values).fetchall()
        return {"ok": True, "reviews": [_review_summary(conn, row) for row in rows]}


def _record_tool_call_audit(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    conversation_id = str(payload.get("conversation_id") or "")
    with db_session(db_path) as conn:
        if conversation_id:
            conversation = conversation_summary(conn, conversation_id)
            row = _require_api_token(conn, payload, "merchant or agent audit token required")
            if row["role"] == "merchant" and row["merchant_id"] == conversation["merchant_id"]:
                actor = row["merchant_id"]
                token_scope = "merchant"
            elif row["role"] == "agent" and row["merchant_id"] == conversation["merchant_id"]:
                actor = row["agent_id"] or _default_merchant_agent_id(conversation["merchant_id"])
                token_scope = "merchant_agent"
            else:
                raise AuthError("merchant or agent audit token required")
        else:
            row = _require_api_token(conn, payload, "merchant or agent audit token required")
            if row["role"] == "merchant":
                actor = row["merchant_id"]
                token_scope = "merchant"
            elif row["role"] == "agent":
                actor = row["agent_id"] or _default_merchant_agent_id(row["merchant_id"])
                token_scope = "merchant_agent"
            else:
                raise AuthError("merchant or agent audit token required")
        event = append_audit_event(
            conn,
            conversation_id,
            actor,
            "llm_tool_call",
            {
                "tool": str(payload.get("tool") or ""),
                "status": str(payload.get("status") or ""),
                "host": str(payload.get("host") or ""),
                "session_id": str(payload.get("session_id") or ""),
                "actor": actor,
                "source_id": actor,
                "token_scope": token_scope,
                "error": str(payload.get("error") or ""),
            },
        )
        return {"ok": True, "event": event}


def _audit_events(
    db_path: str | Path,
    payload: dict[str, Any],
    merchant_id: str = "",
    event: str = "",
    limit: Any = 50,
    offset: Any = 0,
) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id = str(merchant_id or payload.get("merchant_id") or "")
        if not merchant_id:
            raise AuthError("merchant_id is required for audit events")
        _require_merchant_token(conn, merchant_id, payload)
        return {
            "ok": True,
            "merchant_id": merchant_id,
            "events": _merchant_audit_events(conn, merchant_id, event=event, limit=limit, offset=offset),
        }


def _human_review_row(conn: Any, review_id: str | int) -> Any:
    normalized_review_id = _positive_whole_int(review_id, "review_id")
    row = conn.execute("select * from moderation_flags where id = ?", (normalized_review_id,)).fetchone()
    if row is None:
        raise SystemExit(f"Unknown human review: {review_id}")
    return row


def _get_human_review(db_path: str | Path, review_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        row = _human_review_row(conn, review_id)
        review = _review_summary(conn, row)
        _require_merchant_read_token(conn, review["merchant_id"], payload)
        return {"ok": True, "review": review, "conversation": conversation_summary(conn, review["conversation_id"])}


def _create_human_review(db_path: str | Path, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        conversation = conversation_summary(conn, conversation_id)
        actor = str(payload.get("source_id") or _default_merchant_agent_id(conversation["merchant_id"]))
        if actor.startswith("shopping-cli-merchant-agent:"):
            _require_agent_or_merchant_token(conn, conversation["merchant_id"], actor, payload)
        else:
            _require_merchant_token(conn, conversation["merchant_id"], payload)
        review = add_flag(
            conn,
            conversation_id,
            reason=str(payload.get("reason") or "human_required"),
            severity=str(payload.get("severity") or "review"),
            sku=conversation.get("sku") or "",
        )
        next_actor = next_actor_for_status("human_required", review["reason"])
        conn.execute(
            "update conversations set status = 'human_required', next_actor = ?, updated_at = ?, last_sender = ? where id = ?",
            (next_actor, now_iso(), actor, conversation_id),
        )
        append_audit_event(
            conn,
            conversation_id,
            actor,
            "conversation_routed",
            {"status": "human_required", "next_actor": next_actor, "reason": review["reason"]},
        )
        row = conn.execute("select * from moderation_flags where id = ?", (review["id"],)).fetchone()
        return {
            "ok": True,
            "review": _review_summary(conn, row),
            "conversation": conversation_summary(conn, conversation_id),
        }


def _resolve_human_review_item(db_path: str | Path, review_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "reply")
    if action not in HUMAN_REVIEW_ACTIONS:
        raise SystemExit(f"Unknown human-review action: {action}")
    sender = _human_review_sender(payload)
    with db_session(db_path) as conn:
        row = _human_review_row(conn, review_id)
        if row["resolved_at"]:
            raise SystemExit(f"Human review already resolved: {review_id}")
        conversation_id = row["conversation_id"]
        conversation = conversation_summary(conn, conversation_id)
        _require_merchant_token(conn, conversation["merchant_id"], payload)
        if conversation["status"] == "closed":
            raise SystemExit(f"Conversation {conversation_id} is closed")
        now = now_iso()
        conn.execute(
            """
            update moderation_flags
            set resolved_at = ?, resolution = ?, resolved_by = ?
            where id = ? and resolved_at = ''
            """,
            (now, action, sender, int(review_id)),
        )
        remaining_rows = conn.execute(
            """
            select reason from moderation_flags
            where conversation_id = ? and resolved_at = ''
            order by case when reason = 'suspicious_content' then 0 else 1 end, id
            """,
            (conversation_id,),
        ).fetchall()
        remaining = len(remaining_rows)
        remaining_reason = str(remaining_rows[0]["reason"] or "") if remaining_rows else ""
        status = "human_required" if remaining else ("closed" if action == "close" else "waiting_buyer")
        status_reason = remaining_reason if status == "human_required" else str(row["reason"] or "")
        next_actor = next_actor_for_status(status, status_reason if status == "human_required" else "")
        if payload.get("text"):
            append_message(
                conn,
                conversation_id,
                sender=sender,
                intent=str(payload.get("intent") or "support"),
                text=str(payload["text"]),
                structured_payload={
                    "resolution": action,
                    "source_id": payload.get("source_id") or sender,
                    "review_id": int(review_id),
                    "reason": status_reason,
                    "resolved_reason": row["reason"],
                },
                status=status,
            )
        else:
            conn.execute(
                "update conversations set status = ?, next_actor = ?, updated_at = ?, last_sender = ? where id = ?",
                (status, next_actor, now, sender, conversation_id),
            )
        append_audit_event(
            conn,
            conversation_id,
            payload.get("source_id") or sender,
            "human_review_resolved",
            {
                "review_id": int(review_id),
                "resolution": action,
                "status": status,
                "next_actor": next_actor,
                "remaining_unresolved_reviews": int(remaining or 0),
            },
        )
        if status == "closed":
            _append_conversation_closed_audit(
                conn,
                conversation_id,
                payload.get("source_id") or sender,
                next_actor,
                {"resolution": action, "review_id": int(review_id), "source": "human_review"},
            )
        review = _review_summary(conn, _human_review_row(conn, review_id))
        rows = conn.execute(
            "select * from moderation_flags where conversation_id = ? order by id",
            (conversation_id,),
        ).fetchall()
        return {
            "ok": True,
            "review": review,
            "reviews": [_review_summary(conn, row) for row in rows],
            "conversation": conversation_summary(conn, conversation_id),
        }


def _resolve_human_review(db_path: str | Path, conversation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "reply")
    if action not in HUMAN_REVIEW_ACTIONS:
        raise SystemExit(f"Unknown human-review action: {action}")
    sender = _human_review_sender(payload)
    status = "closed" if action == "close" else "waiting_buyer"
    with db_session(db_path) as conn:
        conversation = conversation_summary(conn, conversation_id)
        _require_merchant_token(conn, conversation["merchant_id"], payload)
        if conversation["status"] == "closed":
            raise SystemExit(f"Conversation {conversation_id} is closed")
        now = now_iso()
        resolved = conn.execute(
            """
            update moderation_flags
            set resolved_at = ?, resolution = ?, resolved_by = ?
            where conversation_id = ? and resolved_at = ''
            """,
            (now, action, sender, conversation_id),
        )
        if resolved.rowcount == 0:
            raise SystemExit(f"No unresolved human reviews for conversation: {conversation_id}")
        next_actor = next_actor_for_status(status)
        if payload.get("text"):
            append_message(
                conn,
                conversation_id,
                sender=sender,
                intent=str(payload.get("intent") or "support"),
                text=str(payload["text"]),
                structured_payload={"resolution": action, "source_id": payload.get("source_id") or ""},
                status=status,
            )
        else:
            conn.execute(
                "update conversations set status = ?, next_actor = ?, updated_at = ?, last_sender = ? where id = ?",
                (status, next_actor, now, sender, conversation_id),
            )
        append_audit_event(
            conn,
            conversation_id,
            payload.get("source_id") or sender,
            "human_review_resolved",
            {"resolution": action, "status": status, "next_actor": next_actor},
        )
        if status == "closed":
            _append_conversation_closed_audit(
                conn,
                conversation_id,
                payload.get("source_id") or sender,
                next_actor,
                {"resolution": action, "source": "human_review"},
            )
        rows = conn.execute(
            "select * from moderation_flags where conversation_id = ? order by id",
            (conversation_id,),
        ).fetchall()
        return {
            "ok": True,
            "reviews": [_review_summary(conn, row) for row in rows],
            "conversation": conversation_summary(conn, conversation_id),
        }


def handle_request(
    db_path: str | Path,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    payload = payload or {}
    query = query or {}
    parts = [part for part in path.strip("/").split("/") if part]
    try:
        if method == "GET" and path == "/health":
            return 200, _health(db_path)
        if path == "/merchants" and method == "GET":
            return 200, _list_merchants(db_path, query)
        if path == "/merchants" and method == "POST":
            return 200, _create_merchant(db_path, payload)
        if len(parts) == 2 and parts[0] == "merchants" and method == "GET":
            return 200, _get_merchant(db_path, parts[1])
        if len(parts) == 2 and parts[0] == "merchants" and method == "PATCH":
            return 200, _update_merchant(db_path, parts[1], payload)
        if path == "/products" and method == "POST":
            return 200, _create_product(db_path, payload)
        if len(parts) == 2 and parts[0] == "products" and method == "GET":
            return 200, _get_product(db_path, parts[1])
        if len(parts) == 2 and parts[0] == "products" and method == "PATCH":
            return 200, _update_product(db_path, parts[1], payload)
        if path == "/search/products" and method == "GET":
            return 200, _search_products(db_path, query)
        if path == "/search/merchants" and method == "GET":
            return 200, _search_merchants(db_path, query)
        if path == "/channels/messages" and method == "POST":
            return 200, _ingest_channel_message(db_path, payload)
        if path == "/buyer/ask" and method == "POST":
            return 200, _buyer_ask(db_path, payload)
        if path == "/conversations" and method == "POST":
            return 200, _create_conversation(db_path, payload)
        if len(parts) == 3 and parts[0] == "buyers" and parts[2] == "conversations" and method == "GET":
            filters = dict(query)
            filters["buyer_id"] = parts[1]
            return 200, _conversation_list(db_path, filters, payload, owner_kind="buyer", owner_id=parts[1])
        if len(parts) == 2 and parts[0] == "conversations" and method == "GET":
            return 200, _get_conversation(db_path, parts[1], payload)
        if len(parts) == 3 and parts[0] == "conversations" and parts[2] == "messages" and method == "POST":
            return 200, _append_conversation_message(db_path, parts[1], payload)
        if len(parts) == 3 and parts[0] == "conversations" and parts[2] == "close" and method == "POST":
            return 200, _close_conversation(db_path, parts[1], payload)
        if path == "/agents/heartbeat" and method == "POST":
            return 200, _agent_heartbeat(db_path, payload)
        if path == "/agents/tokens" and method == "GET":
            return 200, _list_agent_tokens(
                db_path,
                payload,
                merchant_id=str(query.get("merchant_id") or ""),
                limit=query.get("limit"),
                offset=query.get("offset"),
            )
        if path == "/agents/tokens" and method == "POST":
            return 200, _create_agent_token(db_path, payload)
        if path == "/agents/tokens/revoke" and method == "POST":
            return 200, _revoke_agent_token(db_path, payload)
        if path == "/agents/tokens/rotate" and method == "POST":
            return 200, _rotate_agent_token(db_path, payload)
        if path == "/agents/messages/claim" and method == "POST":
            return 200, _claim_agent_message(db_path, payload)
        if path == "/agents/messages/complete" and method == "POST":
            return 200, _complete_agent_message(db_path, payload)
        if path == "/agents/messages/fail" and method == "POST":
            return 200, _fail_agent_message(db_path, payload)
        if path == "/agents/messages/abandon" and method == "POST":
            return 200, _abandon_agent_message(db_path, payload)
        if path == "/agents/messages/abandon-stale" and method == "POST":
            return 200, _abandon_stale_agent_messages(db_path, payload)
        if path == "/agents" and method == "GET":
            return 200, _list_agents(db_path, payload, limit=query.get("limit"), offset=query.get("offset"))
        if len(parts) == 2 and parts[0] == "agents" and method == "GET":
            return 200, _get_agent(db_path, parts[1], payload)
        if len(parts) == 3 and parts[0] == "merchants" and parts[2] == "agents" and method == "GET":
            return 200, _list_agents(
                db_path,
                payload,
                owner_id=parts[1],
                limit=query.get("limit"),
                offset=query.get("offset"),
            )
        if path == "/audit/tool-calls" and method == "POST":
            return 200, _record_tool_call_audit(db_path, payload)
        if path == "/audit/events" and method == "GET":
            return 200, _audit_events(
                db_path,
                payload,
                merchant_id=str(query.get("merchant_id") or ""),
                event=str(query.get("event") or ""),
                limit=query.get("limit") or 50,
                offset=query.get("offset"),
            )
        if path == "/human-review/queue" and method == "GET":
            return 200, _human_review_queue(
                db_path,
                payload,
                merchant_id=str(query.get("merchant_id") or ""),
                limit=query.get("limit"),
                offset=query.get("offset"),
            )
        if len(parts) == 2 and parts[0] == "human-review" and method == "GET":
            return 200, _get_human_review(db_path, parts[1], payload)
        if len(parts) == 3 and parts[0] == "human-review" and parts[2] == "resolve" and method == "POST":
            return 200, _resolve_human_review_item(db_path, parts[1], payload)
        if len(parts) == 3 and parts[0] == "merchants" and parts[2] == "conversations" and method == "GET":
            filters = dict(query)
            filters["merchant_id"] = parts[1]
            return 200, _conversation_list(db_path, filters, payload, owner_kind="merchant", owner_id=parts[1])
        if len(parts) == 3 and parts[0] == "merchants" and parts[2] == "human-review" and method == "GET":
            return 200, _merchant_conversations(
                db_path,
                parts[1],
                payload,
                status="human_required",
                limit=query.get("limit"),
                offset=query.get("offset"),
            )
        if len(parts) == 3 and parts[0] == "conversations" and parts[2] == "human-review" and method == "POST":
            return 200, _create_human_review(db_path, parts[1], payload)
        if len(parts) == 4 and parts[0] == "conversations" and parts[2] == "human-review" and parts[3] == "resolve" and method == "POST":
            return 200, _resolve_human_review(db_path, parts[1], payload)
    except AuthError as exc:
        return 403, {"ok": False, "error": str(exc)}
    except (KeyError, ValueError, SystemExit) as exc:
        return 400, {"ok": False, "error": str(exc)}
    return 404, {"ok": False, "error": f"No route for {method} {path}"}


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

    @app.exception_handler(KeyError)
    def key_error_handler(_request: Any, exc: KeyError) -> Any:
        return _json_error_response(400, str(exc))

    @app.exception_handler(ValueError)
    def value_error_handler(_request: Any, exc: ValueError) -> Any:
        return _json_error_response(400, str(exc))

    @app.exception_handler(SystemExit)
    def system_exit_handler(_request: Any, exc: SystemExit) -> Any:
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
        return _create_merchant(db_path, _payload_with_auth(payload, authorization))

    @app.get("/merchants/{merchant_id}")
    def get_merchant(merchant_id: str) -> dict[str, Any]:
        return _get_merchant(db_path, merchant_id)

    @app.patch("/merchants/{merchant_id}")
    def update_merchant(
        merchant_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _update_merchant(db_path, merchant_id, _payload_with_auth(payload, authorization))

    @app.post("/products")
    def create_product(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _create_product(db_path, _payload_with_auth(payload, authorization))

    @app.get("/products/{sku}")
    def get_product(sku: str) -> dict[str, Any]:
        return _get_product(db_path, sku)

    @app.patch("/products/{sku}")
    def update_product(
        sku: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _update_product(db_path, sku, _payload_with_auth(payload, authorization))

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
        return _ingest_channel_message(db_path, _payload_with_auth(payload, authorization))

    @app.post("/buyer/ask")
    def buyer_ask(payload: dict[str, Any]) -> dict[str, Any]:
        return _buyer_ask(db_path, payload)

    @app.post("/conversations")
    def create_conversation(payload: dict[str, Any]) -> dict[str, Any]:
        return _create_conversation(db_path, payload)

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
            },
            _payload_with_auth({}, authorization),
            owner_kind="buyer",
            owner_id=buyer_id,
        )

    @app.get("/conversations/{conversation_id}")
    def get_conversation(conversation_id: str, authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _get_conversation(db_path, conversation_id, _payload_with_auth({}, authorization))

    @app.post("/conversations/{conversation_id}/messages")
    def add_message(
        conversation_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _append_conversation_message(db_path, conversation_id, _payload_with_auth(payload, authorization))

    @app.post("/conversations/{conversation_id}/close")
    def close_conversation(
        conversation_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _close_conversation(db_path, conversation_id, _payload_with_auth(payload, authorization))

    @app.post("/agents/heartbeat")
    def agent_heartbeat(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _agent_heartbeat(db_path, _payload_with_auth(payload, authorization))

    @app.post("/agents/tokens")
    def create_agent_token(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _create_agent_token(db_path, _payload_with_auth(payload, authorization))

    @app.get("/agents/tokens")
    def list_agent_tokens(
        merchant_id: str = "",
        limit: str = "",
        offset: str = "",
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _list_agent_tokens(
            db_path,
            _payload_with_auth({}, authorization),
            merchant_id=merchant_id,
            limit=limit,
            offset=offset,
        )

    @app.post("/agents/tokens/revoke")
    def revoke_agent_token(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _revoke_agent_token(db_path, _payload_with_auth(payload, authorization))

    @app.post("/agents/tokens/rotate")
    def rotate_agent_token(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _rotate_agent_token(db_path, _payload_with_auth(payload, authorization))

    @app.post("/agents/messages/claim")
    def claim_agent_message_route(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _claim_agent_message(db_path, _payload_with_auth(payload, authorization))

    @app.post("/agents/messages/complete")
    def complete_agent_message_route(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _complete_agent_message(db_path, _payload_with_auth(payload, authorization))

    @app.post("/agents/messages/fail")
    def fail_agent_message_route(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _fail_agent_message(db_path, _payload_with_auth(payload, authorization))

    @app.post("/agents/messages/abandon")
    def abandon_agent_message_route(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _abandon_agent_message(db_path, _payload_with_auth(payload, authorization))

    @app.post("/agents/messages/abandon-stale")
    def abandon_stale_agent_messages_route(
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _abandon_stale_agent_messages(db_path, _payload_with_auth(payload, authorization))

    @app.get("/agents")
    def list_agents(
        authorization: str = AUTHORIZATION_HEADER,
        limit: str = "",
        offset: str = "",
    ) -> dict[str, Any]:
        return _list_agents(db_path, _payload_with_auth({}, authorization), limit=limit, offset=offset)

    @app.get("/agents/{agent_id}")
    def get_agent(agent_id: str, authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _get_agent(db_path, agent_id, _payload_with_auth({}, authorization))

    @app.get("/merchants/{merchant_id}/agents")
    def get_merchant_agents(
        merchant_id: str,
        authorization: str = AUTHORIZATION_HEADER,
        limit: str = "",
        offset: str = "",
    ) -> dict[str, Any]:
        return _list_agents(
            db_path,
            _payload_with_auth({}, authorization),
            owner_id=merchant_id,
            limit=limit,
            offset=offset,
        )

    @app.post("/audit/tool-calls")
    def record_tool_call_audit(payload: dict[str, Any], authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _record_tool_call_audit(db_path, _payload_with_auth(payload, authorization))

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
            _payload_with_auth({}, authorization),
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
            _payload_with_auth({}, authorization),
            merchant_id=merchant_id,
            limit=limit,
            offset=offset,
        )

    @app.get("/human-review/{review_id}")
    def get_human_review(review_id: str, authorization: str = AUTHORIZATION_HEADER) -> dict[str, Any]:
        return _get_human_review(db_path, review_id, _payload_with_auth({}, authorization))

    @app.post("/human-review/{review_id}/resolve")
    def resolve_human_review_item(
        review_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _resolve_human_review_item(db_path, review_id, _payload_with_auth(payload, authorization))

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
            },
            _payload_with_auth({}, authorization),
            owner_kind="merchant",
            owner_id=merchant_id,
        )

    @app.get("/merchants/{merchant_id}/human-review")
    def human_review(
        merchant_id: str,
        limit: str = "",
        offset: str = "",
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _merchant_conversations(
            db_path,
            merchant_id,
            _payload_with_auth({}, authorization),
            status="human_required",
            limit=limit,
            offset=offset,
        )

    @app.post("/conversations/{conversation_id}/human-review")
    def create_human_review(
        conversation_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _create_human_review(db_path, conversation_id, _payload_with_auth(payload, authorization))

    @app.post("/conversations/{conversation_id}/human-review/resolve")
    def resolve_human_review(
        conversation_id: str,
        payload: dict[str, Any],
        authorization: str = AUTHORIZATION_HEADER,
    ) -> dict[str, Any]:
        return _resolve_human_review(db_path, conversation_id, _payload_with_auth(payload, authorization))

    return app

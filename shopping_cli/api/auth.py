"""API authentication and bearer-token payload helpers."""

from __future__ import annotations

import json
import os
from typing import Any

from shopping_cli.core.channels import normalize_channel
from shopping_cli.core.errors import AuthError
from shopping_cli.core.tokens import token_matches


def payload_token(payload: dict[str, Any]) -> str:
    return str(
        payload.get("merchant_token")
        or payload.get("agent_token")
        or payload.get("buyer_token")
        or payload.get("_auth_token")
        or ""
    )


def payload_admin_token(payload: dict[str, Any]) -> str:
    return str(payload.get("admin_token") or payload.get("_auth_token") or "")


def payload_channel_token(payload: dict[str, Any]) -> str:
    return str(payload.get("channel_token") or payload.get("_auth_token") or "")


def payload_buyer_bootstrap_token(payload: dict[str, Any]) -> str:
    return str(payload.get("buyer_bootstrap_token") or payload.get("_auth_token") or "")


def payload_with_auth(
    payload: dict[str, Any],
    authorization: Any = "",
    idempotency_key: Any = "",
) -> dict[str, Any]:
    merged = dict(payload or {})
    if isinstance(authorization, str) and authorization.lower().startswith("bearer "):
        merged["_auth_token"] = authorization.split(" ", 1)[1].strip()
    if isinstance(idempotency_key, str) and idempotency_key.strip():
        merged["_idempotency_key"] = idempotency_key.strip()
    return merged


def configured_admin_token() -> str:
    return str(os.environ.get("SHOPPING_ADMIN_TOKEN") or "").strip()


def configured_buyer_bootstrap_token() -> str:
    return str(os.environ.get("SHOPPING_BUYER_BOOTSTRAP_TOKEN") or "").strip()


def require_admin_token(payload: dict[str, Any]) -> None:
    expected = configured_admin_token()
    if not expected:
        raise AuthError("admin bootstrap token is not configured")
    token = payload_admin_token(payload)
    if not token:
        raise AuthError("admin bootstrap token required")
    if not token_matches(token, expected):
        raise AuthError("invalid admin bootstrap token")


def require_buyer_bootstrap_token(payload: dict[str, Any], digest_fn: Any) -> str:
    expected = configured_buyer_bootstrap_token()
    if not expected:
        raise AuthError("buyer bootstrap token is not configured")
    token = payload_buyer_bootstrap_token(payload)
    if not token:
        raise AuthError("buyer bootstrap token required")
    if not token_matches(token, expected):
        raise AuthError("invalid buyer bootstrap token")
    return digest_fn(token)


def channel_token_map() -> dict[str, str]:
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


def require_channel_token(channel: str, payload: dict[str, Any]) -> None:
    normalized = normalize_channel(channel)
    tokens = channel_token_map()
    expected = tokens.get(normalized) or tokens.get("*") or ""
    if not expected:
        raise AuthError(f"channel token is not configured for {normalized or '-'}")
    token = payload_channel_token(payload)
    if not token:
        raise AuthError("channel token required")
    if not token_matches(token, expected):
        raise AuthError("invalid channel token")

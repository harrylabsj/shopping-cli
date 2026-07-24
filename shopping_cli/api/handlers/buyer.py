"""Buyer-facing API handlers: buyer/ask, conversation creation, channel ingress."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from shopping_cli.agents import buyer_cli
from shopping_cli.api import auth as api_auth
from shopping_cli.api import idempotency as api_idempotency
from shopping_cli.api.handlers.common import MAX_SQLITE_INTEGER, non_negative_whole_int, require_field
from shopping_cli.core.channels import ingest_buyer_message
from shopping_cli.core.errors import ValidationError
from shopping_cli.core.tokens import token_digest
from shopping_cli.db.session import db_session
from shopping_cli.services import buyer_bootstrap as buyer_bootstrap_service
from shopping_cli.services import conversations as conversation_service
from shopping_cli.services import tokens as token_service

_DEFAULT_BUYER_BOOTSTRAP_RATE_LIMIT_PER_MINUTE = api_idempotency.DEFAULT_BUYER_BOOTSTRAP_RATE_LIMIT_PER_MINUTE


def _issue_buyer_token(conn: Any, buyer_id: str, conversation_id: str) -> str:
    return token_service.issue_buyer_token(conn, buyer_id, conversation_id)


def _ensure_buyer_token(conn: Any, buyer_id: str, conversation_id: str, token: str) -> str:
    return token_service.ensure_buyer_token(conn, buyer_id, conversation_id, token)


def _buyer_bootstrap_rate_limit_per_minute() -> int:
    return buyer_bootstrap_service.rate_limit_per_minute(
        os.environ.get("SHOPPING_BUYER_BOOTSTRAP_RATE_LIMIT_PER_MINUTE"),
        default=_DEFAULT_BUYER_BOOTSTRAP_RATE_LIMIT_PER_MINUTE,
        maximum=MAX_SQLITE_INTEGER,
    )


def _enforce_buyer_bootstrap_rate_limit(conn: Any, bootstrap_token_hash: str) -> None:
    api_idempotency.enforce_buyer_bootstrap_rate_limit(
        conn,
        bootstrap_token_hash,
        _buyer_bootstrap_rate_limit_per_minute(),
    )


def _replay_idempotency(
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


def _claim_idempotency(
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


def _complete_idempotency(
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
        non_negative_whole_int,
    )


def _clear_idempotency_claim(
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


def buyer_ask(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    bootstrap_token_hash = api_auth.require_buyer_bootstrap_token(payload, token_digest)
    buyer_id = str(require_field(payload, "buyer_id")).strip()
    text = str(require_field(payload, "text"))
    if not buyer_id:
        raise ValidationError("buyer_id is required")
    if not text.strip():
        raise ValidationError("text is required")
    payload = {**payload, "buyer_id": buyer_id, "text": text}
    idempotency_key = api_idempotency.idempotency_key_from_payload(payload)
    request_hash = api_idempotency.buyer_ask_request_hash(payload)
    endpoint = "/buyer/ask"
    with db_session(db_path) as conn:
        replayed = _replay_idempotency(
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
        replayed = _claim_idempotency(
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
            _complete_idempotency(conn, endpoint, bootstrap_token_hash, idempotency_key, request_hash, result)
            return result
        except KeyError as exc:
            raise ValidationError(f"missing required field: {exc.args[0]}") from exc
        except Exception:
            _clear_idempotency_claim(conn, endpoint, bootstrap_token_hash, idempotency_key, request_hash)
            raise


def ingest_channel_message(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("buyer_id"):
        raise ValidationError("buyer_id override is not allowed for channel ingress")
    channel = require_field(payload, "channel")
    api_auth.require_channel_token(str(channel), payload)
    with db_session(db_path) as conn:
        return ingest_buyer_message(
            conn,
            channel=str(channel),
            external_user_id=str(require_field(payload, "external_user_id")),
            text=str(require_field(payload, "text")),
            city=str(payload.get("city") or ""),
            area=str(payload.get("area") or ""),
            conversation_id=str(payload.get("conversation_id") or ""),
            external_message_id=str(payload.get("external_message_id") or ""),
        )


def create_conversation(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    bootstrap_token_hash = api_auth.require_buyer_bootstrap_token(payload, token_digest)
    buyer_id = str(require_field(payload, "buyer_id")).strip()
    merchant_id = str(require_field(payload, "merchant_id")).strip()
    if not buyer_id:
        raise ValidationError("buyer id is required")
    if not merchant_id:
        raise ValidationError("merchant id is required")
    payload = {**payload, "buyer_id": buyer_id, "merchant_id": merchant_id}
    idempotency_key = api_idempotency.idempotency_key_from_payload(payload)
    request_hash = api_idempotency.conversation_create_request_hash(payload)
    endpoint = "/conversations"
    with db_session(db_path) as conn:
        replayed = _replay_idempotency(
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
        replayed = _claim_idempotency(
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
                buyer_id=buyer_id,
                merchant_id=merchant_id,
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
            _complete_idempotency(conn, endpoint, bootstrap_token_hash, idempotency_key, request_hash, result)
            return result
        except KeyError as exc:
            raise ValidationError(f"missing required field: {exc.args[0]}") from exc
        except Exception:
            _clear_idempotency_claim(conn, endpoint, bootstrap_token_hash, idempotency_key, request_hash)
            raise

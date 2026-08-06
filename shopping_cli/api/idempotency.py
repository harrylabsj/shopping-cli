"""Buyer bootstrap and Agent Catalog write idempotency and rate-limit helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime
from typing import Any

from shopping_cli.api.auth import (
    payload_buyer_bootstrap_token,
    payload_token,
)
from shopping_cli.core.errors import IdempotencyConflict, RateLimitError, ValidationError
from shopping_cli.core.tokens import token_digest
from shopping_cli.db.session import decode_json, encode_json, now_iso

DEFAULT_BUYER_BOOTSTRAP_RATE_LIMIT_PER_MINUTE = 60
BUYER_BOOTSTRAP_RATE_LIMIT_WINDOW_SECONDS = 60
MAX_IDEMPOTENCY_KEY_LENGTH = 160
MAX_SQLITE_INTEGER = 2**63 - 1

# Agent Catalog write endpoints (§10.4) — a bounded per-actor budget and a
# rolling idempotency claim.  These live in dedicated tables so catalog writes
# never share (or pollute) the buyer bootstrap idempotency ledger.
CATALOG_WRITE_RATE_LIMIT_WINDOW_SECONDS = 60
CATALOG_WRITE_ENDPOINTS = frozenset({
    "/v1/agent-catalog/agents/register",
    "/v1/agent-catalog/agents/{id}/refresh",
    "/v1/agent-catalog/agents/{id}/verify",
    "/v1/agent-catalog/agents/{id}/claim",
})


def idempotency_key_from_payload(payload: dict[str, Any]) -> str:
    key = str(payload.get("idempotency_key") or payload.get("_idempotency_key") or "").strip()
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValidationError(f"idempotency_key must be <= {MAX_IDEMPOTENCY_KEY_LENGTH} characters")
    return key


def request_hash(values: dict[str, Any]) -> str:
    canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return token_digest(canonical)


def buyer_ask_request_hash(payload: dict[str, Any]) -> str:
    return request_hash(
        {
            "buyer_id": str(payload["buyer_id"]).strip(),
            "text": str(payload["text"]),
            "city": str(payload.get("city") or ""),
            "area": str(payload.get("area") or ""),
            "source_id": str(payload.get("source_id") or "buyer-cli"),
            "host": str(payload.get("host") or ""),
            "session_id": str(payload.get("session_id") or ""),
        }
    )


def conversation_create_request_hash(payload: dict[str, Any]) -> str:
    return request_hash(
        {
            "buyer_id": str(payload["buyer_id"]).strip(),
            "merchant_id": str(payload["merchant_id"]).strip(),
            "sku": str(payload.get("sku") or ""),
            "text": str(payload.get("text") or ""),
            "intent": str(payload.get("intent") or "ask_product"),
            "source_id": str(payload.get("source_id") or ""),
        }
    )


def deterministic_buyer_token(
    payload: dict[str, Any],
    endpoint: str,
    idempotency_key: str,
    buyer_id: str,
    conversation_id: str,
) -> str:
    secret = payload_buyer_bootstrap_token(payload)
    material = f"{endpoint}\n{idempotency_key}\n{buyer_id}\n{conversation_id}"
    digest = hmac.new(secret.encode("utf-8"), material.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"shopping_buyer_{digest}"


def rate_limit_window_start(current: datetime) -> str:
    epoch_seconds = int(current.timestamp())
    window_epoch = epoch_seconds - (epoch_seconds % BUYER_BOOTSTRAP_RATE_LIMIT_WINDOW_SECONDS)
    return datetime.fromtimestamp(window_epoch).replace(microsecond=0).isoformat()


def enforce_buyer_bootstrap_rate_limit(
    conn: Any,
    bootstrap_token_hash: str,
    limit: int,
    current: datetime | None = None,
) -> None:
    if limit <= 0:
        return
    current = (current or datetime.now()).replace(microsecond=0)
    cursor = conn.execute(
        """
        insert into buyer_bootstrap_rate_limits(token_hash, window_start, request_count, updated_at)
        values (?, ?, 1, ?)
        on conflict(token_hash, window_start) do update set
            request_count = buyer_bootstrap_rate_limits.request_count + 1,
            updated_at = excluded.updated_at
        where buyer_bootstrap_rate_limits.request_count < ?
        """,
        (bootstrap_token_hash, rate_limit_window_start(current), current.isoformat(), limit),
    )
    if cursor.rowcount != 1:
        raise RateLimitError(f"buyer bootstrap rate limit exceeded ({limit}/minute)")


# ── Agent Catalog write idempotency & rate limit (§10.4) ─────────────────────
# The catalog write ledger is generic: the *actor_key* is the digest of the
# presented API token (admin / merchant / verification worker), falling back to
# a digest of the canonical domain for the public register route.  claim/replay
# semantics mirror the buyer bootstrap helpers: an in-flight claim replays as
# IdempotencyConflict, a completed claim replays the stored response, and a
# request_hash mismatch is always rejected.


def catalog_write_window_start(current: datetime, window_seconds: int = CATALOG_WRITE_RATE_LIMIT_WINDOW_SECONDS) -> str:
    epoch_seconds = int(current.timestamp())
    window_epoch = epoch_seconds - (epoch_seconds % window_seconds)
    return datetime.fromtimestamp(window_epoch).replace(microsecond=0).isoformat()


def catalog_write_actor_key(payload: dict[str, Any], canonical_domain: str = "") -> str:
    """Derive the idempotency/rate-limit actor from a request payload.

    The presented API token (admin / merchant / verification worker) is the
    actor when present.  The public register route may be unauthenticated; its
    actor is a *constant* anonymous bucket so that (a) reusing an idempotency
    key with a different request is detected even when the canonical domain
    differs (the request_hash mismatch is what raises IdempotencyConflict),
    and (b) all unauthenticated catalog writes share one bounded per-minute
    budget.  The §17.4 per-domain registration limit independently caps each
    domain, so the constant bucket adds a global bound without becoming an
    SSRF amplification vector.
    """
    token = payload_token(payload) or payload.get("_auth_token") or ""
    if token:
        return token_digest(str(token))
    return "anon:" + token_digest("catalog-write")


def catalog_register_request_hash(payload: dict[str, Any]) -> str:
    return request_hash(
        {
            "domain": str(payload.get("domain") or "").strip(),
            "agent_card_url": str(payload.get("agent_card_url") or "").strip(),
            "ucp_profile_url": str(payload.get("ucp_profile_url") or "").strip(),
            "merchant_id": str(payload.get("merchant_id") or "").strip(),
        }
    )


def catalog_agent_action_request_hash(payload: dict[str, Any], catalog_agent_id: str) -> str:
    return request_hash(
        {
            "catalog_agent_id": str(catalog_agent_id or "").strip(),
            "merchant_id": str(payload.get("merchant_id") or "").strip(),
            "action": str(payload.get("action") or "").strip(),
        }
    )


def enforce_agent_catalog_rate_limit(
    conn: Any,
    actor_key: str,
    limit: int,
    current: datetime | None = None,
) -> None:
    """Raise RateLimitError when *actor_key* exceeds its per-minute write budget.

    Delegates to the shared fixed-window core (v3.0-P5) — see
    ``shopping_cli.services.rate_limit`` for the backend abstraction.
    """
    from shopping_cli.services.rate_limit import SQLiteRateLimitBackend, enforce_rate_limit

    backend = SQLiteRateLimitBackend(
        conn, table="agent_catalog_write_rate_limits", key_column="actor_key"
    )
    enforce_rate_limit(
        backend,
        key=actor_key,
        limit=limit,
        window_seconds=CATALOG_WRITE_RATE_LIMIT_WINDOW_SECONDS,
        description="agent catalog write",
        current=current,
    )


def catalog_write_idempotency_row(
    conn: Any,
    endpoint: str,
    actor_key: str,
    idempotency_key: str,
) -> Any:
    return conn.execute(
        """
        select endpoint, actor_key, idempotency_key, request_hash, status, response_json
        from agent_catalog_write_idempotency
        where endpoint = ? and actor_key = ? and idempotency_key = ?
        """,
        (endpoint, actor_key, idempotency_key),
    ).fetchone()


def replay_catalog_write_idempotency(
    conn: Any,
    endpoint: str,
    actor_key: str,
    idempotency_key: str,
    request_hash_value: str,
) -> dict[str, Any] | None:
    if not idempotency_key:
        return None
    row = catalog_write_idempotency_row(conn, endpoint, actor_key, idempotency_key)
    if row is None:
        return None
    if str(row["request_hash"]) != request_hash_value:
        raise IdempotencyConflict("idempotency key was reused with a different request")
    if row["status"] != "completed":
        raise IdempotencyConflict("idempotent request is still processing")
    response = decode_json(row["response_json"], {})
    if not isinstance(response, dict):
        response = {"ok": True}
    result = dict(response)
    result["idempotent"] = True
    return result


def claim_catalog_write_idempotency(
    conn: Any,
    endpoint: str,
    actor_key: str,
    idempotency_key: str,
    request_hash_value: str,
) -> dict[str, Any] | None:
    if not idempotency_key:
        return None
    current = now_iso()
    try:
        conn.execute(
            """
            insert into agent_catalog_write_idempotency(
                endpoint, actor_key, idempotency_key, request_hash, status,
                response_json, created_at, updated_at
            )
            values (?, ?, ?, ?, 'processing', '{}', ?, ?)
            """,
            (endpoint, actor_key, idempotency_key, request_hash_value, current, current),
        )
    except sqlite3.IntegrityError:
        return replay_catalog_write_idempotency(
            conn, endpoint, actor_key, idempotency_key, request_hash_value
        )
    return None


def complete_catalog_write_idempotency(
    conn: Any,
    endpoint: str,
    actor_key: str,
    idempotency_key: str,
    request_hash_value: str,
    response: dict[str, Any],
) -> None:
    if not idempotency_key:
        return
    stored = dict(response)
    conn.execute(
        """
        update agent_catalog_write_idempotency
        set status = 'completed', response_json = ?, updated_at = ?
        where endpoint = ? and actor_key = ? and idempotency_key = ? and request_hash = ?
        """,
        (
            encode_json(stored),
            now_iso(),
            endpoint,
            actor_key,
            idempotency_key,
            request_hash_value,
        ),
    )


def clear_catalog_write_idempotency_claim(
    conn: Any,
    endpoint: str,
    actor_key: str,
    idempotency_key: str,
    request_hash_value: str,
) -> None:
    if not idempotency_key:
        return
    conn.execute(
        """
        delete from agent_catalog_write_idempotency
        where endpoint = ? and actor_key = ? and idempotency_key = ? and request_hash = ?
          and status = 'processing'
        """,
        (endpoint, actor_key, idempotency_key, request_hash_value),
    )


def idempotency_row(
    conn: Any,
    endpoint: str,
    bootstrap_token_hash: str,
    idempotency_key: str,
) -> Any:
    return conn.execute(
        """
        select endpoint, token_hash, idempotency_key, request_hash, status, response_json,
               buyer_id, conversation_id, message_id
        from buyer_request_idempotency
        where endpoint = ? and token_hash = ? and idempotency_key = ?
        """,
        (endpoint, bootstrap_token_hash, idempotency_key),
    ).fetchone()


def response_from_idempotency_row(
    conn: Any,
    payload: dict[str, Any],
    row: Any,
    ensure_buyer_token: Any,
) -> dict[str, Any]:
    response = decode_json(row["response_json"], {})
    if not isinstance(response, dict):
        response = {"ok": True}
    result = dict(response)
    buyer_id = str(row["buyer_id"] or "")
    conversation_id = str(row["conversation_id"] or "")
    if buyer_id and conversation_id:
        token = deterministic_buyer_token(
            payload,
            str(row["endpoint"]),
            str(row["idempotency_key"]),
            buyer_id,
            conversation_id,
        )
        result["buyer_token"] = ensure_buyer_token(conn, buyer_id, conversation_id, token)
    result["idempotent"] = True
    return result


def replay_buyer_idempotency(
    conn: Any,
    payload: dict[str, Any],
    endpoint: str,
    bootstrap_token_hash: str,
    idempotency_key: str,
    request_hash_value: str,
    ensure_buyer_token: Any,
) -> dict[str, Any] | None:
    if not idempotency_key:
        return None
    row = idempotency_row(conn, endpoint, bootstrap_token_hash, idempotency_key)
    if row is None:
        return None
    if str(row["request_hash"]) != request_hash_value:
        raise IdempotencyConflict("idempotency key was reused with a different request")
    if row["status"] != "completed":
        raise IdempotencyConflict("idempotent request is still processing")
    return response_from_idempotency_row(conn, payload, row, ensure_buyer_token)


def claim_buyer_idempotency(
    conn: Any,
    payload: dict[str, Any],
    endpoint: str,
    bootstrap_token_hash: str,
    idempotency_key: str,
    request_hash_value: str,
    ensure_buyer_token: Any,
) -> dict[str, Any] | None:
    if not idempotency_key:
        return None
    current = now_iso()
    try:
        conn.execute(
            """
            insert into buyer_request_idempotency(
                endpoint, token_hash, idempotency_key, request_hash, status,
                response_json, created_at, updated_at
            )
            values (?, ?, ?, ?, 'processing', '{}', ?, ?)
            """,
            (endpoint, bootstrap_token_hash, idempotency_key, request_hash_value, current, current),
        )
    except sqlite3.IntegrityError:
        return replay_buyer_idempotency(
            conn,
            payload,
            endpoint,
            bootstrap_token_hash,
            idempotency_key,
            request_hash_value,
            ensure_buyer_token,
        )
    return None


def complete_buyer_idempotency(
    conn: Any,
    endpoint: str,
    bootstrap_token_hash: str,
    idempotency_key: str,
    request_hash_value: str,
    response: dict[str, Any],
    non_negative_whole_int: Any,
) -> None:
    if not idempotency_key:
        return
    stored_response = dict(response)
    stored_response.pop("buyer_token", None)
    conversation = stored_response.get("conversation") if isinstance(stored_response.get("conversation"), dict) else {}
    message = stored_response.get("message") if isinstance(stored_response.get("message"), dict) else {}
    conn.execute(
        """
        update buyer_request_idempotency
        set status = 'completed',
            response_json = ?,
            buyer_id = ?,
            conversation_id = ?,
            message_id = ?,
            updated_at = ?
        where endpoint = ? and token_hash = ? and idempotency_key = ? and request_hash = ?
        """,
        (
            encode_json(stored_response),
            str(stored_response.get("buyer_id") or (conversation or {}).get("buyer_id") or ""),
            str((conversation or {}).get("id") or ""),
            non_negative_whole_int((message or {}).get("id"), "message_id"),
            now_iso(),
            endpoint,
            bootstrap_token_hash,
            idempotency_key,
            request_hash_value,
        ),
    )


def clear_buyer_idempotency_claim(
    conn: Any,
    endpoint: str,
    bootstrap_token_hash: str,
    idempotency_key: str,
    request_hash_value: str,
) -> None:
    if not idempotency_key:
        return
    conn.execute(
        """
        delete from buyer_request_idempotency
        where endpoint = ? and token_hash = ? and idempotency_key = ? and request_hash = ? and status = 'processing'
        """,
        (endpoint, bootstrap_token_hash, idempotency_key, request_hash_value),
    )

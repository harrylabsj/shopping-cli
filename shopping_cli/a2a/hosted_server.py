"""Hosted A2A JSON-RPC endpoint core (v2.4-W3).

Framework-agnostic JSON-RPC ``message/send`` processing for the shared-host
A2A endpoint ``POST /a2a/agents/{catalog_agent_id}``.  The HTTP layer only
parses the body and authenticates the caller; everything after that lives in
this module so the protocol logic is unit-testable without a web framework.

Wire contract (binding rc1 §3.1–§3.6, §4–§6):

* ``message/send`` params carry an A2A Message whose Data Part holds the KNP
  envelope under the ``knp_envelope`` key (Kiwi §24.3 convention).  Exactly
  one Data Part is required; multiple Data Parts fail closed.  Text Parts are
  human-readable only and never participate in semantics.
* The KNP envelope is schema-validated (``validate_envelope``) and its wire
  digest verified (``verify_envelope_digest``) before any business use.
* Idempotency (rc1 §3.6) is authoritative on
  ``(sender_identity, KNP message_id, digest)``: same id + same digest
  replays the previous response with no second business effect; same id +
  different digest fails closed as ``idempotency_conflict``.  The ledger is
  the ``a2a_inbound_idempotency`` table (schema v14).
* Every envelope is classified by ``HostedNegotiationCompatibilityAdapter``:
  lossless → authoritative ``submit_decision`` write path (policy gate);
  fail_closed → structured KNP protocol error (no state advance);
  requires_human → human-review flag via ``core.conversations.add_flag``.
* Responses are A2A Messages whose Data Part carries a KNP **result
  envelope** (actor=merchant, ``in_reply_to`` = the inbound KNP message_id,
  with this endpoint's own digest).

sender_identity decision
------------------------

``sender_identity`` is derived **only** from the authenticated buyer token:
it is the token-bound ``buyer_id`` (``NegotiationActor.owner_id``).  The
envelope's ``actor`` field is untrusted and is asserted against the
authenticated role (buyer); a mismatch is a KNP ``identity_rejected``
protocol error.  ``sender_identity`` is never read from the envelope.

Error layering decision (Kiwi ``errors.ts`` semantics)
------------------------------------------------------

* Transport/frame failures  → HTTP 400 + JSON-RPC -32600 (invalid request).
  (-32700 parse errors are reserved for the HTTP layer's JSON parse step,
  which runs before this module and produces its own 400; this core only sees
  already-parsed objects.)
* Unknown method / bad params → HTTP 200 + -32601 / -32602.
* KNP protocol / validation / idempotency / adapter fail-closed errors →
  HTTP 200 + -32050 with ``data.protocol_code`` from the KNP §18 vocabulary
  (e.g. ``schema_invalid``, ``idempotency_conflict``, ``capability_incompatible``).
* Unexpected internal exceptions → HTTP 200 + -32603 ``internal error`` with
  ``data.protocol_code = temporarily_unavailable``; internal details are
  never echoed.
* Authentication failures → HTTP 401/403 + -32051 with
  ``data.protocol_code = authentication_required`` / ``authorization_failed``
  (produced by the HTTP handler, not this module).

A KNP protocol error is **never** converted into a commercial decline, and a
protocol error **never** advances negotiation state.

Result envelope convention (v1)
-------------------------------

The result envelope reuses the inbound ``action`` value (the only KNP action
vocabulary that is schema-valid for an acknowledgment), with ``actor="merchant"``,
a fresh ``message_id`` (``msg_legacy_<id>`` when a legacy message was
written, else ``msg_ack_<inbound message_id>``), ``in_reply_to`` = the inbound
message_id, and a ``payload`` of ``{"type": "result", "outcome": ...,
"acknowledges": <inbound message_id>}``.  This is a documented v1 local
convention; a future KNP sub-spec freeze may introduce a dedicated ack action.

Binding: docs/a2a/shopping-cli-a2a-binding-1.0-rc1.md §3.1–§3.6, §4–§6
Design: docs/shopping-cli-a2a-upgrade-design-v1.2.1.md §14, §20
"""

from __future__ import annotations

import sqlite3
from typing import Any

from shopping_cli.a2a._common import load_hosted_agent
from shopping_cli.a2a.binding import HostedNegotiationCompatibilityAdapter, TranslationResult
from shopping_cli.a2a.knp import (
    KNP_PROTOCOL_VERSION,
    KnpValidationError,
    NegotiationEnvelope,
    finalize_envelope,
    validate_envelope,
    verify_envelope_digest,
)
from shopping_cli.core.conversations import add_flag, require_conversation
from shopping_cli.core.errors import (
    AuthError,
    ConflictError,
    IdempotencyConflict,
    NotFoundError,
    ValidationError,
)
from shopping_cli.core.harness import append_audit_event
from shopping_cli.core.negotiation import now_rfc3339
from shopping_cli.db.session import decode_json, encode_json, now_iso
from shopping_cli.services import negotiation as negotiation_service

# JSON-RPC 2.0 standard codes + the A2A/KNP application codes (kiwi errors.ts).
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
# KNP protocol error carrier: the real code lives in data.protocol_code (§18).
KNP_PROTOCOL_ERROR_CODE = -32050
# Authentication failure carrier (HTTP handler produces these).
AUTH_ERROR_CODE = -32051

#: The one method this synchronous endpoint supports (rc1 §2 D2 / §24).
SUPPORTED_METHODS = frozenset({"message/send"})

#: actor permitted on this endpoint: only buyer tokens authenticate here.
HOSTED_BUYER_ACTOR = "buyer"

#: Idempotency claim states.
_IDEM_CLAIMED = "claimed"
_IDEM_REPLAY = "replay"
_IDEM_CONFLICT = "conflict"

_AUDIT_EVENT = "a2a_inbound_message"
_AUDIT_ACTOR = "system/a2a"

ADAPTER = HostedNegotiationCompatibilityAdapter


# ---------------------------------------------------------------------------
# JSON-RPC response builders
# ---------------------------------------------------------------------------


def _jsonrpc_success(request_id: str, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: str | None, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _knp_protocol_error(request_id: str | None, protocol_code: str, message: str) -> dict[str, Any]:
    return _jsonrpc_error(
        request_id,
        KNP_PROTOCOL_ERROR_CODE,
        message,
        {"protocol_code": protocol_code, "detail": message},
    )


def auth_error_response(
    request_id: str | None,
    protocol_code: str,
    message: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Build the authentication JSON-RPC error (kiwi ``authError`` semantics).

    ``authentication_required`` → HTTP 401; every other code → HTTP 403.
    """
    if protocol_code == "authentication_required":
        http_status = 401
        text = message or "authentication required"
    else:
        http_status = 403
        text = message or "authorization failed"
    return http_status, _jsonrpc_error(
        request_id,
        AUTH_ERROR_CODE,
        text,
        {"protocol_code": protocol_code},
    )


def _protocol_code_for_exception(exc: Exception) -> str:
    """Map an execution exception to a KNP §18 protocol_code for audit/response.

    ``NotFoundError`` is deliberately not mapped here: it is re-raised as the
    404 gate and handled by the HTTP layer.
    """
    if isinstance(exc, IdempotencyConflict):
        return "idempotency_conflict"
    if isinstance(exc, ConflictError):
        return "state_conflict"
    if isinstance(exc, AuthError):
        return "identity_rejected"
    if isinstance(exc, ValidationError):
        return "schema_invalid"
    return "temporarily_unavailable"


class JsonRpcFrameError(Exception):
    """A structurally invalid JSON-RPC request frame.

    Carries the complete HTTP status + JSON-RPC error response so the caller
    can return it without further mapping.
    """

    def __init__(self, http_status: int, body: dict[str, Any]) -> None:
        super().__init__(str(body.get("error", {}).get("message", "invalid JSON-RPC request")))
        self.http_status = http_status
        self.body = body


def _validate_frame(body: Any) -> tuple[str, str, Any]:
    """Validate the JSON-RPC frame; return ``(id, method, params)``.

    Fails closed: non-object body, ``jsonrpc != "2.0"``, missing/empty string
    ``id`` and ``method`` are all -32600 invalid request (HTTP 400).  ``id``
    must be a string (A2A JSONRPC binding, kiwi ``isJsonRpcRequest``).
    """
    if not isinstance(body, dict):
        raise JsonRpcFrameError(400, _jsonrpc_error(None, JSONRPC_INVALID_REQUEST, "invalid JSON-RPC request"))
    if body.get("jsonrpc") != "2.0":
        raise JsonRpcFrameError(400, _jsonrpc_error(None, JSONRPC_INVALID_REQUEST, "invalid JSON-RPC request"))
    request_id = body.get("id")
    if not isinstance(request_id, str) or not request_id:
        raise JsonRpcFrameError(400, _jsonrpc_error(None, JSONRPC_INVALID_REQUEST, "invalid JSON-RPC request"))
    method = body.get("method")
    if not isinstance(method, str) or not method:
        raise JsonRpcFrameError(400, _jsonrpc_error(None, JSONRPC_INVALID_REQUEST, "invalid JSON-RPC request"))
    return request_id, method, body.get("params")


# ---------------------------------------------------------------------------
# A2A Message / KNP envelope extraction (untrusted, fail-closed)
# ---------------------------------------------------------------------------


def _parse_a2a_message(value: Any) -> dict[str, Any]:
    """Strictly parse an inbound A2A Message (kiwi ``parseInboundMessage``)."""
    if not isinstance(value, dict):
        raise KnpValidationError("schema_invalid", "params.message must be an object", "/message")
    role = value.get("role")
    if role not in ("agent", "user"):
        raise KnpValidationError("schema_invalid", "message role must be agent or user", "/role")
    message_id = value.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        raise KnpValidationError("schema_invalid", "message messageId must be a non-empty string", "/messageId")
    parts = value.get("parts")
    if not isinstance(parts, list) or not parts:
        raise KnpValidationError("schema_invalid", "message parts must be a non-empty array", "/parts")
    return value


def _extract_knp_envelope(message: dict[str, Any]) -> dict[str, Any]:
    """Extract the raw KNP envelope from the A2A Message's Data Part.

    Exactly one Data Part is required; its ``data`` must be an object carrying
    the envelope under the ``knp_envelope`` key (kiwi §24.3 convention).
    Multiple Data Parts or a non-object data part fail closed.
    """
    parts = message.get("parts")
    if not isinstance(parts, list):
        raise KnpValidationError("schema_invalid", "message parts must be an array", "/parts")
    data_parts = [part for part in parts if isinstance(part, dict) and part.get("kind") == "data"]
    if not data_parts:
        raise KnpValidationError(
            "schema_invalid", "message has no data part carrying the KNP envelope", "/parts"
        )
    if len(data_parts) != 1:
        raise KnpValidationError("schema_invalid", "message must carry exactly one data part", "/parts")
    data = data_parts[0].get("data")
    if not isinstance(data, dict):
        raise KnpValidationError("schema_invalid", "data part must carry an object", "/parts")
    envelope = data.get("knp_envelope")
    if not isinstance(envelope, dict):
        raise KnpValidationError("schema_invalid", "data part must carry a knp_envelope object", "/parts")
    return envelope


def _best_effort_message_id(envelope_raw: Any) -> str | None:
    if isinstance(envelope_raw, dict):
        value = envelope_raw.get("message_id")
        if isinstance(value, str) and value:
            return value
    return None


# ---------------------------------------------------------------------------
# Idempotency ledger (a2a_inbound_idempotency, schema v14)
# ---------------------------------------------------------------------------


def _idempotency_claim(
    conn: sqlite3.Connection,
    sender_identity: str,
    envelope: NegotiationEnvelope,
) -> tuple[str, dict[str, Any] | None]:
    """Claim the idempotency key; return ``(status, stored_response_part)``.

    ``claimed`` — this call owns a fresh processing claim.  ``replay`` — the
    key was already completed with an identical digest; ``stored_response_part``
    holds the prior ``{"result": ...}`` or ``{"error": ...}``.  ``conflict`` —
    the key exists with a different digest (fail closed), or the prior claim
    is still in flight.
    """
    current = now_iso()
    try:
        conn.execute(
            """
            insert into a2a_inbound_idempotency(
                sender_identity, message_id, digest, status, response_json, created_at, updated_at
            ) values (?, ?, ?, 'processing', '{}', ?, ?)
            """,
            (sender_identity, envelope.message_id, envelope.digest, current, current),
        )
        return _IDEM_CLAIMED, None
    except sqlite3.IntegrityError:
        row = conn.execute(
            """
            select digest, status, response_json from a2a_inbound_idempotency
            where sender_identity = ? and message_id = ?
            """,
            (sender_identity, envelope.message_id),
        ).fetchone()
        if row is None:
            return _IDEM_CLAIMED, None
        if str(row["digest"]) != envelope.digest:
            return _IDEM_CONFLICT, None
        if str(row["status"]) != "completed":
            return _IDEM_CONFLICT, None
        stored = decode_json(str(row["response_json"]), {})
        if not isinstance(stored, dict):
            stored = {}
        return _IDEM_REPLAY, stored


def _idempotency_complete(
    conn: sqlite3.Connection,
    sender_identity: str,
    envelope: NegotiationEnvelope,
    response_part: dict[str, Any],
) -> None:
    """Persist the processed response under the claimed idempotency key.

    *response_part* is either ``{"result": ...}`` or ``{"error": ...}`` — the
    JSON-RPC response body minus the frame/id, so a replay can rebuild the
    identical response for the current request id.
    """
    conn.execute(
        """
        update a2a_inbound_idempotency
        set status = 'completed', response_json = ?, updated_at = ?
        where sender_identity = ? and message_id = ? and digest = ? and status = 'processing'
        """,
        (
            encode_json(response_part),
            now_iso(),
            sender_identity,
            envelope.message_id,
            envelope.digest,
        ),
    )


def _idempotency_release(
    conn: sqlite3.Connection,
    sender_identity: str,
    envelope: NegotiationEnvelope,
) -> None:
    """Drop an un-finished processing claim so a retry can re-process.

    Used when processing raises (state conflict, internal error): the message
    never produced a deterministic result, so it must not be persisted as a
    replayable outcome (rc1 §3.6 recovery semantics).
    """
    conn.execute(
        """
        delete from a2a_inbound_idempotency
        where sender_identity = ? and message_id = ? and status = 'processing'
        """,
        (sender_identity, envelope.message_id),
    )


# ---------------------------------------------------------------------------
# Result envelope + A2A response builders
# ---------------------------------------------------------------------------


def build_result_envelope(
    inbound: NegotiationEnvelope,
    *,
    outcome: str,
    legacy_message_id: int | None = None,
    public_message: str | None = None,
) -> dict[str, Any]:
    """Build the KNP result envelope acknowledging *inbound*.

    Reuses the inbound action (the only schema-valid action for an
    acknowledgment), actor=merchant, ``in_reply_to`` = the inbound message_id,
    and a ``payload`` carrying the outcome.  The digest is computed by
    ``finalize_envelope`` and the result is schema-validated before return.
    """
    if legacy_message_id is not None:
        message_id = f"msg_legacy_{int(legacy_message_id)}"
    else:
        message_id = f"msg_ack_{inbound.message_id}"
    payload: dict[str, Any] = {
        "type": "result",
        "outcome": outcome,
        "acknowledges": inbound.message_id,
    }
    if legacy_message_id is not None:
        payload["legacy_message_id"] = int(legacy_message_id)
    fields: dict[str, Any] = {
        "capability": inbound.capability,
        "protocol_version": KNP_PROTOCOL_VERSION,
        "negotiation_id": inbound.negotiation_id,
        "exchange_id": inbound.exchange_id,
        "message_id": message_id,
        "actor": "merchant",
        "action": inbound.action,
        "created_at": now_rfc3339(),
        "payload": payload,
        "in_reply_to": inbound.message_id,
    }
    if public_message:
        fields["public_message"] = str(public_message)
    result = finalize_envelope(fields)
    validate_envelope(result)  # defensive: the result must be a valid KNP envelope
    return result


def _a2a_message_response(
    request_id: str,
    context_id: str | None,
    result_envelope: dict[str, Any],
    *,
    public_message: str = "",
) -> dict[str, Any]:
    """Wrap a KNP result envelope in a JSON-RPC success A2A Message."""
    message: dict[str, Any] = {
        "role": "agent",
        "messageId": str(result_envelope["message_id"]),
        "parts": [{"kind": "data", "data": {"knp_envelope": result_envelope}}],
    }
    if context_id:
        message["contextId"] = context_id
    if public_message:
        message["parts"].append({"kind": "text", "text": public_message})
    return _jsonrpc_success(request_id, {"message": message})


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _audit(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    message_id: str | None = None,
    negotiation_id: str | None = None,
    classification: str | None = None,
    outcome: str | None = None,
    error_code: str | None = None,
    legacy_message_id: int | None = None,
    replayed: bool = False,
) -> None:
    """Write one inbound-processing audit event (no payload content)."""
    details: dict[str, Any] = {}
    if message_id is not None:
        details["message_id"] = message_id
    if negotiation_id is not None:
        details["negotiation_id"] = negotiation_id
    if classification is not None:
        details["classification"] = classification
    if outcome is not None:
        details["outcome"] = outcome
    if error_code is not None:
        details["error_code"] = error_code
    if legacy_message_id is not None:
        details["legacy_message_id"] = legacy_message_id
    if replayed:
        details["replayed"] = True
    append_audit_event(conn, conversation_id, _AUDIT_ACTOR, _AUDIT_EVENT, details)


def _audit_envelope(
    conn: sqlite3.Connection,
    envelope: NegotiationEnvelope,
    *,
    conversation_id: str,
    classification: str | None = None,
    outcome: str | None = None,
    error_code: str | None = None,
    legacy_message_id: int | None = None,
    replayed: bool = False,
) -> None:
    _audit(
        conn,
        conversation_id=conversation_id,
        message_id=envelope.message_id,
        negotiation_id=envelope.negotiation_id,
        classification=classification,
        outcome=outcome,
        error_code=error_code,
        legacy_message_id=legacy_message_id,
        replayed=replayed,
    )


# ---------------------------------------------------------------------------
# Lossless execution (authoritative submit_decision write path)
# ---------------------------------------------------------------------------


def _execute_lossless(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    context_id: str | None,
    envelope: NegotiationEnvelope,
    result: TranslationResult,
    sender_identity: str,
    actor: negotiation_service.NegotiationActor,
    agent_row: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Execute a lossless translation through the policy-gated write path.

    The claim + submit sequence is the same authoritative flow a local buyer
    agent uses (``services.negotiation.claim_message`` →
    ``submit_decision``), so the policy gate, idempotent replay and audit of
    the legacy protocol all apply.  Deterministic outcomes (including binding
    mismatches) complete the idempotency claim; only raised exceptions are
    released by the caller.
    """
    conversation_id = result.target_conversation_id or ""

    # Token-bound binding checks: the negotiation must be the buyer's own
    # bound conversation and served by this hosted agent's merchant.
    if conversation_id != actor.conversation_id:
        error = _knp_protocol_error(
            request_id,
            "identity_rejected",
            "negotiation_id does not map to the authenticated buyer's conversation",
        )
        _audit_envelope(conn, envelope, conversation_id="", classification="lossless", error_code="identity_rejected")
        _idempotency_complete(conn, sender_identity, envelope, {"error": error["error"]})
        return 200, error
    conversation = require_conversation(conn, conversation_id)
    if str(conversation["merchant_id"]) != str(agent_row.get("merchant_id") or ""):
        error = _knp_protocol_error(
            request_id,
            "identity_rejected",
            "negotiation_id is not served by this hosted agent",
        )
        _audit_envelope(conn, envelope, conversation_id="", classification="lossless", error_code="identity_rejected")
        _idempotency_complete(conn, sender_identity, envelope, {"error": error["error"]})
        return 200, error

    legacy_payload = result.legacy_structured_payload
    if legacy_payload is None:
        # Defensive: a lossless classification always carries the legacy write
        # description (adapter invariant); a missing payload fails closed.
        error = _knp_protocol_error(
            request_id,
            "schema_invalid",
            "lossless translation is missing the legacy write details",
        )
        _audit_envelope(conn, envelope, conversation_id="", classification="lossless", error_code="schema_invalid")
        _idempotency_complete(conn, sender_identity, envelope, {"error": error["error"]})
        return 200, error
    decision = legacy_payload["decision"]
    legacy_id = int(decision["in_reply_to_message_id"])
    claim_key = str(legacy_payload["idempotency_key"])

    negotiation_service.claim_message(conn, actor, conversation_id, legacy_id, claim_key)
    policy_result = negotiation_service.submit_decision(conn, actor, decision, claim_key)

    outcome = str(policy_result.get("result") or "rejected_retryable")
    legacy_message_id: int | None = None
    if outcome == "accepted":
        message_id_value = policy_result.get("message_id")
        if message_id_value is not None:
            legacy_message_id = int(message_id_value)
        negotiation_service.complete_claim(conn, actor, legacy_id)

    public_message = str(policy_result.get("public_reason") or "")
    result_envelope = build_result_envelope(
        envelope,
        outcome=outcome,
        legacy_message_id=legacy_message_id,
        public_message=public_message,
    )
    response = _a2a_message_response(request_id, context_id, result_envelope, public_message=public_message)
    _audit_envelope(
        conn,
        envelope,
        conversation_id=conversation_id,
        classification="lossless",
        outcome=outcome,
        legacy_message_id=legacy_message_id,
    )
    _idempotency_complete(conn, sender_identity, envelope, {"result": response["result"]})
    return 200, response


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _dispatch(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    method: str,
    params: Any,
    sender_identity: str,
    actor: negotiation_service.NegotiationActor,
    catalog_agent_id: str,
) -> tuple[int, dict[str, Any]]:
    # Hosted-agent gate: only hosted + active catalog agents are addressable.
    # Unknown / non-hosted / inactive agents raise NotFoundError (404, same
    # shape as the W1 publication routes — no existence oracle).  The gate runs
    # before method/params validation so an unknown agent always 404s.
    agent_row = load_hosted_agent(conn, catalog_agent_id)

    if method not in SUPPORTED_METHODS:
        return 200, _jsonrpc_error(request_id, JSONRPC_METHOD_NOT_FOUND, f"method {method} not found")
    if not isinstance(params, dict):
        return 200, _jsonrpc_error(request_id, JSONRPC_INVALID_PARAMS, "params must be an object")

    message_value = params.get("message")
    if message_value is None:
        return 200, _jsonrpc_error(request_id, JSONRPC_INVALID_PARAMS, "params.message is required")
    try:
        message = _parse_a2a_message(message_value)
    except KnpValidationError as exc:
        _audit(conn, conversation_id="", error_code=exc.code)
        return 200, _knp_protocol_error(request_id, exc.code, str(exc))

    params_context = params.get("contextId")
    context_id = str(params_context) if isinstance(params_context, str) and params_context else None
    if context_id is None:
        message_context = message.get("contextId")
        context_id = str(message_context) if isinstance(message_context, str) and message_context else None

    envelope_raw: dict[str, Any] | None = None
    try:
        envelope_raw = _extract_knp_envelope(message)
        envelope = validate_envelope(envelope_raw)
    except KnpValidationError as exc:
        _audit(
            conn,
            conversation_id="",
            message_id=_best_effort_message_id(envelope_raw),
            error_code=exc.code,
        )
        return 200, _knp_protocol_error(request_id, exc.code, str(exc))

    if not verify_envelope_digest(envelope):
        _audit_envelope(conn, envelope, conversation_id="", error_code="schema_invalid")
        return 200, _knp_protocol_error(
            request_id,
            "schema_invalid",
            "envelope digest mismatch (wire digest does not match content)",
        )

    # Token-bound actor assertion (rc1 §3.5 / §6): the envelope's actor field
    # is untrusted.  This endpoint only serves buyer tokens.
    if envelope.actor != HOSTED_BUYER_ACTOR:
        _audit_envelope(conn, envelope, conversation_id="", error_code="identity_rejected")
        return 200, _knp_protocol_error(
            request_id,
            "identity_rejected",
            "envelope actor is not bound to the authenticated buyer",
        )

    # Idempotency (rc1 §3.6) — authoritative (sender_identity, message_id, digest).
    idem_status, idem_stored = _idempotency_claim(conn, sender_identity, envelope)
    if idem_status == _IDEM_REPLAY:
        _audit_envelope(conn, envelope, conversation_id="", classification="replayed", replayed=True)
        stored = idem_stored if idem_stored is not None else {}
        return 200, {"jsonrpc": "2.0", "id": request_id, **stored}
    if idem_status == _IDEM_CONFLICT:
        _audit_envelope(conn, envelope, conversation_id="", error_code="idempotency_conflict")
        return 200, _knp_protocol_error(
            request_id,
            "idempotency_conflict",
            f"message_id {envelope.message_id} already processed with a different digest "
            "(replay conflict, §3.6)",
        )

    # Classification + execution.  Deterministic outcomes complete the claim;
    # raised exceptions release it so a retry can re-process, and are audited
    # with the envelope before they propagate.
    result: TranslationResult | None = None
    try:
        result = ADAPTER.translate_envelope(conn, envelope, sender_identity=sender_identity)
        if result.classification == "fail_closed":
            error = _knp_protocol_error(
                request_id,
                "capability_incompatible",
                result.reason or "KNP action cannot be expressed by the hosted negotiation protocol",
            )
            _audit_envelope(
                conn,
                envelope,
                conversation_id=result.target_conversation_id or "",
                classification="fail_closed",
                error_code="capability_incompatible",
            )
            _idempotency_complete(conn, sender_identity, envelope, {"error": error["error"]})
            return 200, error

        if result.classification == "requires_human":
            conversation_id = result.target_conversation_id or ""
            add_flag(
                conn,
                conversation_id,
                reason=str((result.human_review or {}).get("reason") or "knp_action_unsupported"),
                severity=str((result.human_review or {}).get("severity") or "review"),
            )
            result_envelope = build_result_envelope(
                envelope,
                outcome="human_required",
                public_message="该请求需要人工处理。",
            )
            response = _a2a_message_response(
                request_id,
                context_id,
                result_envelope,
                public_message="该请求需要人工处理。",
            )
            _audit_envelope(
                conn,
                envelope,
                conversation_id=conversation_id,
                classification="requires_human",
                outcome="human_required",
            )
            _idempotency_complete(conn, sender_identity, envelope, {"result": response["result"]})
            return 200, response

        return _execute_lossless(
            conn,
            request_id=request_id,
            context_id=context_id,
            envelope=envelope,
            result=result,
            sender_identity=sender_identity,
            actor=actor,
            agent_row=agent_row,
        )
    except Exception as exc:
        _idempotency_release(conn, sender_identity, envelope)
        _audit_envelope(
            conn,
            envelope,
            conversation_id=(
                result.target_conversation_id
                if result is not None and result.target_conversation_id
                else ""
            ),
            error_code=_protocol_code_for_exception(exc),
        )
        raise


def process_jsonrpc_request(
    conn: sqlite3.Connection,
    body: Any,
    *,
    sender_identity: str,
    actor: negotiation_service.NegotiationActor,
    catalog_agent_id: str,
) -> tuple[int, dict[str, Any]]:
    """Process one JSON-RPC request body; return ``(http_status, jsonrpc_body)``.

    ``sender_identity`` is the token-derived buyer identity and ``actor`` the
    token-derived ``NegotiationActor`` (see module docstring).  ``conn`` must
    be an open, write-capable connection (the HTTP layer opens one
    ``db_session`` per request).
    """
    request_id: str | None = None
    try:
        request_id, method, params = _validate_frame(body)
        return _dispatch(
            conn,
            request_id=request_id,
            method=method,
            params=params,
            sender_identity=sender_identity,
            actor=actor,
            catalog_agent_id=catalog_agent_id,
        )
    except JsonRpcFrameError as exc:
        return exc.http_status, exc.body
    except KnpValidationError as exc:
        _audit(conn, conversation_id="", error_code=exc.code)
        return 200, _knp_protocol_error(request_id, exc.code, str(exc))
    except NotFoundError:
        # Hosted-agent gate (404) and unknown message/negotiation references.
        # The gate must stay an indistinguishable 404 like the W1 routes.
        raise
    except Exception as exc:
        # The dispatch layer already audited the failure with the envelope;
        # here we only map it to the response shape.  Internal detail must
        # never be echoed (kiwi §4.5 / rc1 §6).
        code = _protocol_code_for_exception(exc)
        if code == "temporarily_unavailable":
            return 200, _jsonrpc_error(
                request_id,
                JSONRPC_INTERNAL_ERROR,
                "internal error",
                {"protocol_code": "temporarily_unavailable"},
            )
        return 200, _knp_protocol_error(request_id, code, str(exc))


__all__ = [
    "AUTH_ERROR_CODE",
    "JSONRPC_INTERNAL_ERROR",
    "JSONRPC_INVALID_PARAMS",
    "JSONRPC_INVALID_REQUEST",
    "JSONRPC_METHOD_NOT_FOUND",
    "JSONRPC_PARSE_ERROR",
    "KNP_PROTOCOL_ERROR_CODE",
    "SUPPORTED_METHODS",
    "JsonRpcFrameError",
    "auth_error_response",
    "build_result_envelope",
    "process_jsonrpc_request",
]

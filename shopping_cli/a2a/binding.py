"""HostedNegotiationCompatibilityAdapter — KNP/1.0 → shopping.negotiation/0.1.

Pure service layer (v2.4-W2) that describes how a KNP/1.0 envelope maps onto
a legacy ``shopping.negotiation/0.1`` write.  It never writes to the database
or over the wire: it returns a description object the v2.4-W3 JSON-RPC
endpoint will execute.  ``conn`` is used read-only to locate the hosted
conversation for a ``negotiation_id``.

Semantics ported from the Kiwi legacy adapter
(``kiwi/src/protocol/legacy-shopping-negotiation/mapping.ts`` +
``adapter.ts``):

* ``classify_action`` implements the Kiwi ``KNP_TO_LEGACY_ACTION`` matrix
  verbatim:

  * lossless:        inquiry→ask, offer→propose, counter_offer→counter,
                     clarification→ask, accept_nonbinding→accept_nonbinding,
                     decline→decline
  * fail_closed:     conditional_offer (conditions are protected semantics the
                     legacy protocol cannot express)
  * requires_human:  rfq / clarification_response / withdraw / cancel
                     (legacy has no equivalent)

* protected semantics are never silently dropped (binding rc1 §4).  Even a
  lossless action is downgraded to ``fail_closed`` when its payload carries
  conditions, a negotiation-scoped decline, or identity references that
  cannot be mapped onto a legacy message id.

* ``accept_nonbinding`` maps to the legacy ``accept_nonbinding`` decision,
  which is inherently non-binding: ``shopping.negotiation/0.1`` never creates
  orders, payments or inventory reservations.  This adapter therefore never
  produces any order/payment/reservation semantics.

Conversation mapping: v1 uses a deterministic reversible encoding.  A hosted
conversation ``CONV-0001`` is addressed as ``negotiation_id = "neg_" + id``
(``neg_CONV-0001``).  The reverse strips the prefix and verifies the row
exists.  This needs no DB migration; a foreign ``negotiation_id`` that does
not carry the hosted prefix (or names a missing conversation) is rejected
fail-closed.  Binding D3 keeps A2A ``contextId`` opaque; the negotiation-id
encoding is the local v1 conversation-location mechanism, not a claim about
the peer's id format.

Legacy intent decision: every translated negotiation write uses
``intent="negotiate"``, the same intent the authoritative legacy write path
uses (``shopping_cli.services.negotiation.NEGOTIATION_INTENT``).
``quote_request`` / ``purchase_intent`` are buyer-facing intent-record
messages (CLI ``/intent``), not negotiation decisions, and are never emitted
here.

Binding: docs/a2a/shopping-cli-a2a-binding-1.0-rc1.md §3.6 (idempotency
triple), §4 (lossless / fail-closed / human review), §6 (no-order invariant)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, TypeGuard

from shopping_cli.a2a.knp import KnpValidationError, NegotiationEnvelope
from shopping_cli.core.conversations import require_conversation
from shopping_cli.core.errors import NotFoundError
from shopping_cli.core.negotiation import is_rfc3339_datetime

# The frozen legacy negotiation protocol this adapter writes against.
PROTOCOL_VERSION = "shopping.negotiation/0.1"

# Legacy message intent for every translated negotiation write (see module
# docstring for the decision).
LEGACY_NEGOTIATION_INTENT = "negotiate"

# Legacy decision actions (decision.schema.json enum).
LEGACY_DECISION_ACTIONS = ("ask", "propose", "counter", "accept_nonbinding", "decline", "escalate")

#: KNP action → legacy decision action / rejection marker (kiwi mapping.ts).
#: ``fail_closed`` = protected semantics cannot be expressed; ``requires_human`` =
#: legacy has no equivalent and the message routes to human review.
KNP_TO_LEGACY_ACTION: dict[str, str] = {
    "inquiry": "ask",
    "rfq": "requires_human",
    "offer": "propose",
    "counter_offer": "counter",
    "conditional_offer": "fail_closed",
    "clarification": "ask",
    "clarification_response": "requires_human",
    "accept_nonbinding": "accept_nonbinding",
    "withdraw": "requires_human",
    "decline": "decline",
    "cancel": "requires_human",
}

_FAIL_CLOSED_REASON: dict[str, str] = {
    "conditional_offer": "conditions are protected semantics that shopping.negotiation/0.1 cannot express",
}

# Hosted conversation encoding: negotiation_id == "neg_" + conversation_id.
HOSTED_NEGOTIATION_PREFIX = "neg_"

# Legacy message ids are integers; a KNP message_id that encodes one uses the
# reversible ``msg_legacy_<int>`` form (kiwi mapping.ts §35 identity).
_LEGACY_MSG_ID_PATTERN = re.compile(r"^msg_legacy_(\d{1,10})$")

STOCK_STATUSES = ("available", "low", "out_of_stock", "unknown")

# Currency minor-unit exponents for KNP amount_minor → legacy unit_price.
_CURRENCY_EXPONENTS: dict[str, int] = {
    "CNY": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "HKD": 2,
    "JPY": 0,
    "KRW": 0,
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdempotencyKey:
    """KNP idempotency triple (binding rc1 §3.6 / §4)."""

    sender_identity: str
    message_id: str
    digest: str


@dataclass(frozen=True)
class TranslationResult:
    """Structured outcome of translating one KNP envelope to the legacy side.

    ``classification`` is one of ``lossless | fail_closed | requires_human``.

    For ``lossless``: ``legacy_intent`` / ``legacy_text`` /
    ``legacy_structured_payload`` / ``target_conversation_id`` describe the
    legacy write for the v2.4-W3 executor (which runs the authoritative
    ``submit_decision`` path).

    For ``fail_closed``: ``reason`` names the protected semantic that cannot
    be expressed.

    For ``requires_human``: ``human_review`` carries the moderation-flag
    routing (aligned with ``shopping_cli.services.human_review`` semantics:
    a moderation flag reason + severity on the target conversation).
    """

    classification: str  # "lossless" | "fail_closed" | "requires_human"
    idempotency: IdempotencyKey
    target_conversation_id: str | None = None
    legacy_intent: str | None = None
    legacy_text: str | None = None
    legacy_structured_payload: dict[str, Any] | None = None
    reason: str | None = None
    human_review: dict[str, Any] | None = None
    notes: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Action classification
# ---------------------------------------------------------------------------


def classify_action(action: str) -> str:
    """Classify a KNP action as ``lossless | fail_closed | requires_human``.

    Matches the Kiwi ``KNP_TO_LEGACY_ACTION`` matrix exactly (see module
    docstring).  Unknown actions fail closed.
    """
    mapped = KNP_TO_LEGACY_ACTION.get(action)
    if mapped is None:
        raise KnpValidationError(
            "schema_invalid", f"unknown KNP action: {action}", "/action"
        )
    if mapped in ("fail_closed", "requires_human"):
        return mapped
    return "lossless"


# ---------------------------------------------------------------------------
# Identifier mapping
# ---------------------------------------------------------------------------


def negotiation_id_for_conversation(conversation_id: str) -> str:
    """Hosted negotiation id for a legacy conversation (reversible encoding)."""
    return f"{HOSTED_NEGOTIATION_PREFIX}{conversation_id}"


def conversation_id_from_negotiation_id(negotiation_id: str) -> str | None:
    """Reverse of ``negotiation_id_for_conversation``; ``None`` when the
    negotiation id does not carry the hosted prefix."""
    if not negotiation_id.startswith(HOSTED_NEGOTIATION_PREFIX):
        return None
    conversation_id = negotiation_id[len(HOSTED_NEGOTIATION_PREFIX):]
    return conversation_id or None


def knp_message_id_to_legacy(message_id: str) -> int | None:
    """Decode a ``msg_legacy_<int>`` KNP message_id to the legacy integer id;
    ``None`` when the id is not of that reversible shape (identity §35)."""
    match = _LEGACY_MSG_ID_PATTERN.fullmatch(message_id)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Legacy proposal conversion (KNP TermSet → decision.schema proposal)
#
# The legacy proposal is single-SKU and mandates delivery eta / fee / stock /
# valid_until.  Every missing or inexpressible field fails closed — the
# adapter must not invent inventory, delivery or expiry facts.
# ---------------------------------------------------------------------------


def _currency_exponent(currency: str) -> int:
    return _CURRENCY_EXPONENTS.get(currency, 2)


def _is_money(value: Any) -> TypeGuard[dict[str, Any]]:
    if not isinstance(value, dict):
        return False
    amount = value.get("amount_minor")
    return (
        isinstance(value.get("currency"), str)
        and isinstance(amount, int)
        and not isinstance(amount, bool)
        and amount >= 0
    )


def _is_legacy_stock_state(value: Any) -> TypeGuard[dict[str, Any]]:
    if not isinstance(value, dict):
        return False
    quantity = value.get("quantity")
    return (
        isinstance(value.get("status"), str)
        and value["status"] in STOCK_STATUSES
        and isinstance(quantity, int)
        and not isinstance(quantity, bool)
        and quantity >= 0
        and isinstance(value.get("observed_at"), str)
        and is_rfc3339_datetime(value["observed_at"])
        and value.get("reserved") is False
    )


def _terms_to_proposal(terms: Any) -> tuple[dict[str, Any] | None, str]:
    """Convert a KNP TermSet to a legacy proposal, losslessly or not at all."""
    if not isinstance(terms, dict):
        return None, "KNP terms must be an object"
    items = terms.get("items")
    if not isinstance(items, list) or len(items) != 1:
        return None, "legacy proposal is single-SKU; KNP terms must carry exactly one item"
    item = items[0]
    if not isinstance(item, dict):
        return None, "legacy proposal requires a structured item"
    sku = item.get("sku")
    if not isinstance(sku, str) or not sku:
        return None, "legacy proposal requires a non-empty item.sku"
    if len(sku) > 128:
        return None, "legacy proposal sku exceeds the 128-character legacy limit"
    quantity = item.get("quantity")
    if not isinstance(quantity, dict):
        return None, "legacy proposal requires item.quantity"
    qty = quantity.get("value")
    if isinstance(qty, bool) or not isinstance(qty, (int, float)) or not math.isfinite(qty):
        return None, "legacy proposal requires a finite item.quantity.value"
    if isinstance(qty, float) and not qty.is_integer():
        return None, "legacy proposal requires an integer quantity; KNP carries a fractional quantity"
    qty_int = int(qty)
    if qty_int < 1 or qty_int > 100000:
        return None, "legacy proposal quantity must be within 1..100000"
    unit_price = item.get("unit_price")
    if not _is_money(unit_price):
        return None, "legacy proposal requires an integer unit_price (amount_minor)"
    currency = unit_price["currency"]
    if not 3 <= len(currency) <= 8:
        return None, "legacy proposal currency must be 3-8 characters"

    fulfillment = terms.get("fulfillment_terms")
    if not isinstance(fulfillment, dict):
        return None, (
            "legacy proposal requires delivery eta_start/eta_end and fee; "
            "KNP terms carry none"
        )
    eta_start = fulfillment.get("eta_start")
    eta_end = fulfillment.get("eta_end")
    if not (
        isinstance(eta_start, str)
        and is_rfc3339_datetime(eta_start)
        and isinstance(eta_end, str)
        and is_rfc3339_datetime(eta_end)
    ):
        return None, "legacy proposal requires delivery eta_start/eta_end (RFC 3339)"
    delivery_fee = fulfillment.get("delivery_fee")
    if not _is_money(delivery_fee) or delivery_fee["currency"] != currency:
        return None, "legacy proposal requires a delivery fee in the item currency"
    stock = fulfillment.get("legacy_stock")
    if not _is_legacy_stock_state(stock):
        return None, "legacy proposal requires a valid stock observation"

    valid_until = terms.get("valid_until")
    if not (isinstance(valid_until, str) and is_rfc3339_datetime(valid_until)):
        return None, (
            "legacy proposal requires valid_until; KNP offer has no expiry and "
            "the adapter must not invent one"
        )

    service_terms = terms.get("service_terms")
    after_sales = service_terms.get("after_sales_policy_refs") if isinstance(service_terms, dict) else None
    if after_sales is None:
        after_sales = []
    if not isinstance(after_sales, list):
        return None, "after_sales_policy_refs must be an array"
    refs: list[str] = []
    for ref in after_sales:
        if not isinstance(ref, str) or not ref.strip():
            return None, "after_sales_policy_refs items must be non-empty strings"
        if len(ref) > 128:
            return None, "after_sales_policy_refs item exceeds the 128-character legacy limit"
        refs.append(ref)
    if len(refs) > 32:
        return None, "after_sales_policy_refs exceeds the 32-item legacy limit"

    exponent = _currency_exponent(currency)
    proposal: dict[str, Any] = {
        "sku": sku,
        "quantity": qty_int,
        "unit_price": unit_price["amount_minor"] / (10**exponent),
        "currency": currency,
        "stock": dict(stock),
        "delivery": {
            "eta_start": eta_start,
            "eta_end": eta_end,
            "fee": delivery_fee["amount_minor"] / (10**exponent),
        },
        "after_sales_policy_refs": refs,
        "valid_until": valid_until,
    }
    return proposal, ""


# ---------------------------------------------------------------------------
# Legacy decision construction
# ---------------------------------------------------------------------------


def _open_issues_from_questions(envelope: NegotiationEnvelope) -> list[str]:
    """Flatten KNP clarification/inquiry questions into legacy ``open_issues``
    free text (structure is dropped, content is preserved — never silent)."""
    payload = envelope.payload
    if envelope.action == "clarification":
        questions = payload.get("questions")
        if not isinstance(questions, list):
            return []
        issues: list[str] = []
        for q in questions:
            if not isinstance(q, dict):
                continue
            field_name = q.get("field")
            if not isinstance(field_name, str) or not field_name:
                continue
            reason = q.get("reason")
            text = f"{field_name}: {reason}" if isinstance(reason, str) and reason else field_name
            issues.append(text)
        return issues[:32]
    if envelope.action == "inquiry":
        questions = payload.get("questions")
        if not isinstance(questions, list):
            return []
        codes = [
            str(q.get("code", ""))
            for q in questions
            if isinstance(q, dict) and isinstance(q.get("code"), str) and q["code"]
        ]
        return codes[:32]
    return []


def _offer_like_terms(envelope: NegotiationEnvelope) -> Any:
    payload = envelope.payload
    if envelope.action == "offer":
        terms = payload.get("terms")
        return terms if isinstance(terms, dict) else None
    if envelope.action == "counter_offer":
        terms = payload.get("proposed_terms")
        return terms if isinstance(terms, dict) else None
    return None


def _protected_semantics_reason(envelope: NegotiationEnvelope) -> str | None:
    """Return a fail-closed reason when a nominally-lossless action still
    carries protected semantics the legacy side cannot express."""
    payload = envelope.payload
    # Conditions inside a TermSet would be silently dropped by a legacy
    # proposal — never translate them away.
    for key in ("terms", "proposed_terms", "base_terms"):
        terms = payload.get(key)
        if isinstance(terms, dict) and "conditions" in terms:
            return (
                f"KNP payload carries conditional semantics at payload.{key}.conditions "
                f"which {PROTOCOL_VERSION} cannot express"
            )
    if envelope.action == "decline":
        if payload.get("scope") == "negotiation":
            return (
                f"KNP decline scope=negotiation is not expressible in {PROTOCOL_VERSION} "
                f"(legacy decline is offer-scoped only)"
            )
        target = payload.get("target_message_id")
        if not isinstance(target, str) or knp_message_id_to_legacy(target) is None:
            return (
                "KNP decline target_message_id cannot be mapped to a legacy message id "
                "(identity reference)"
            )
    return None


def _build_legacy_decision(
    envelope: NegotiationEnvelope, conversation_id: str
) -> tuple[dict[str, Any] | None, str]:
    """Construct the ``shopping.negotiation/0.1`` decision for a lossless
    envelope, or return ``(None, reason)`` when an identity reference or
    offer-like terms cannot be mapped losslessly."""
    action = KNP_TO_LEGACY_ACTION[envelope.action]

    if envelope.action == "decline":
        target = envelope.payload.get("target_message_id")
        legacy_id = knp_message_id_to_legacy(target) if isinstance(target, str) else None
        if legacy_id is None or legacy_id < 1:
            return None, (
                "KNP decline target_message_id cannot be mapped to a legacy message id "
                "(identity reference)"
            )
        in_reply_to = legacy_id
        open_issues: list[str] = []
    else:
        if envelope.in_reply_to is None:
            return None, (
                "legacy decision requires in_reply_to_message_id; KNP envelope has "
                "no in_reply_to"
            )
        legacy_id = knp_message_id_to_legacy(envelope.in_reply_to)
        if legacy_id is None or legacy_id < 1:
            return None, (
                f"KNP in_reply_to {envelope.in_reply_to!r} cannot be mapped to a legacy "
                f"message id (identity reference)"
            )
        in_reply_to = legacy_id
        open_issues = _open_issues_from_questions(envelope)

    proposal: dict[str, Any] | None = None
    if envelope.action in ("offer", "counter_offer"):
        terms = _offer_like_terms(envelope)
        if terms is None:
            return None, "KNP envelope action/payload mismatch for offer-like action"
        proposal, error = _terms_to_proposal(terms)
        if proposal is None:
            return None, error

    decision: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "conversation_id": conversation_id,
        "in_reply_to_message_id": in_reply_to,
        "action": action,
        "open_issues": open_issues,
        "public_message": envelope.public_message or "",
        "reason_codes": [],
        "request_human_review": False,
    }
    if proposal is not None:
        decision["proposal"] = proposal
    return decision, ""


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class HostedNegotiationCompatibilityAdapter:
    """KNP/1.0 → shopping.negotiation/0.1 compatibility adapter (binding §4).

    Pure translation layer: no HTTP, no writes.  ``translate_envelope`` reads
    ``conn`` only to locate the hosted conversation for the negotiation.
    """

    @staticmethod
    def classify_action(action: str) -> str:
        return classify_action(action)

    @staticmethod
    def translate_envelope(
        conn: Any,
        envelope: NegotiationEnvelope,
        *,
        sender_identity: str,
    ) -> TranslationResult:
        idempotency = IdempotencyKey(
            sender_identity=sender_identity,
            message_id=envelope.message_id,
            digest=envelope.digest,
        )

        mapped = KNP_TO_LEGACY_ACTION.get(envelope.action)
        if mapped is None:
            raise KnpValidationError(
                "schema_invalid", f"unknown KNP action: {envelope.action}", "/action"
            )

        # Matrix-level fail-closed (conditional_offer) needs no conversation:
        # the protected semantics are rejected regardless of negotiation scope.
        if mapped == "fail_closed":
            return TranslationResult(
                classification="fail_closed",
                idempotency=idempotency,
                reason=(
                    f"KNP action {envelope.action!r} cannot be expressed in "
                    f"{PROTOCOL_VERSION}: {_FAIL_CLOSED_REASON.get(envelope.action, '')}"
                ),
            )

        conversation_id = conversation_id_from_negotiation_id(envelope.negotiation_id)
        if conversation_id is None:
            return TranslationResult(
                classification="fail_closed",
                idempotency=idempotency,
                reason=(
                    f"negotiation_id {envelope.negotiation_id!r} does not map to a "
                    f"hosted conversation (missing {HOSTED_NEGOTIATION_PREFIX!r} prefix)"
                ),
            )
        try:
            require_conversation(conn, conversation_id)
        except NotFoundError:
            return TranslationResult(
                classification="fail_closed",
                idempotency=idempotency,
                reason=(
                    f"negotiation_id {envelope.negotiation_id!r} does not map to a "
                    f"known hosted conversation ({conversation_id})"
                ),
            )

        if mapped == "requires_human":
            return TranslationResult(
                classification="requires_human",
                idempotency=idempotency,
                target_conversation_id=conversation_id,
                legacy_text=envelope.public_message or "",
                reason=(
                    f"KNP action {envelope.action!r} is unsupported by "
                    f"{PROTOCOL_VERSION}; route to human review"
                ),
                human_review={
                    "reason": f"knp_action_unsupported:{envelope.action}",
                    "severity": "review",
                },
            )

        # lossless path — protected-semantics downgrade first.
        protected = _protected_semantics_reason(envelope)
        if protected is not None:
            return TranslationResult(
                classification="fail_closed",
                idempotency=idempotency,
                reason=protected,
            )

        decision, error = _build_legacy_decision(envelope, conversation_id)
        if decision is None:
            return TranslationResult(
                classification="fail_closed",
                idempotency=idempotency,
                reason=error or "legacy decision could not be built losslessly",
            )

        legacy_structured_payload: dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "idempotency_key": f"knp:{envelope.message_id}",
            "agent_id": sender_identity,
            "role": envelope.actor,
            "source_id": sender_identity,
            "decision": decision,
        }
        return TranslationResult(
            classification="lossless",
            idempotency=idempotency,
            target_conversation_id=conversation_id,
            legacy_intent=LEGACY_NEGOTIATION_INTENT,
            legacy_text=envelope.public_message or "",
            legacy_structured_payload=legacy_structured_payload,
            notes=(
                "envelope.actor MUST be asserted by the legacy transport (token-bound); "
                "shopping.negotiation/0.1 decisions carry no role field",
                "KNP message_id / exchange_id / digest / capability / created_at are "
                "not expressible in legacy decisions",
            ),
        )


__all__ = [
    "HostedNegotiationCompatibilityAdapter",
    "IdempotencyKey",
    "KNP_TO_LEGACY_ACTION",
    "LEGACY_DECISION_ACTIONS",
    "LEGACY_NEGOTIATION_INTENT",
    "TranslationResult",
    "classify_action",
    "conversation_id_from_negotiation_id",
    "knp_message_id_to_legacy",
    "negotiation_id_for_conversation",
]

"""shopping.negotiation/0.1 orchestration-facing constants and role helpers.

Contract validation, RFC 3339 time normalization and JSON canonicalization
moved move-only to :mod:`shopping_cli.core.negotiation_contracts`; the public
names are re-exported here (``as`` aliases) so ``from shopping_cli.core import
negotiation`` keeps its exact surface, error types/messages, schema-loader
cache behavior and call signatures. This module still owns the negotiation
state vocabulary (decision actions, policy results, stock statuses, next-actor
and sender role sets, retry budget, stale-observation age, audit event names)
and the pure role/next-actor/identity mapping helpers used by the services and
snapshot projection.
"""

from __future__ import annotations

from typing import Any

from shopping_cli.core.negotiation_contracts import (
    CONTRACTS_DIR as CONTRACTS_DIR,
    PROTOCOL_VERSION as PROTOCOL_VERSION,
    canonical_json as canonical_json,
    capabilities_report as capabilities_report,
    is_rfc3339_datetime as is_rfc3339_datetime,
    load_contract_schema as load_contract_schema,
    normalize_db_timestamp as normalize_db_timestamp,
    now_rfc3339 as now_rfc3339,
    parse_rfc3339 as parse_rfc3339,
    validate_contract as validate_contract,
)

DECISION_ACTIONS = ("ask", "propose", "counter", "accept_nonbinding", "decline", "escalate")
POLICY_RESULTS = ("accepted", "rejected_retryable", "human_required")
STOCK_STATUSES = ("available", "low", "out_of_stock", "unknown")

# Conversation next_actor values that mean "the merchant side must act".
MERCHANT_NEXT_ACTORS = {"merchant_agent", "merchant"}
BUYER_NEXT_ACTORS = {"buyer"}
# Message senders owned by each side of the negotiation.
BUYER_SENDERS = {"buyer", "buyer_cli"}
MERCHANT_SENDERS = {"merchant", "merchant_agent", "operator"}

# Retry budget surfaced through policy_result.retries_remaining. A claim
# attempt consumes one; the fixtures show attempts=1 -> retries_remaining=2.
MAX_DECISION_ATTEMPTS = 3
# Merchant stock observations older than this are treated as stale.
STOCK_OBSERVATION_MAX_AGE_SECONDS = 900

AUDIT_DECISION_SUBMITTED = "negotiation_decision_submitted"
AUDIT_POLICY_ACCEPTED = "negotiation_policy_accepted"
AUDIT_POLICY_DENIED = "negotiation_policy_denied"
AUDIT_HUMAN_REQUIRED = "negotiation_human_required"


def snapshot_next_actor(next_actor: str) -> str:
    if next_actor in MERCHANT_NEXT_ACTORS:
        return "merchant"
    if next_actor in BUYER_NEXT_ACTORS:
        return "buyer"
    return "none"


def role_for_next_actor(next_actor: str) -> str:
    """The negotiation role that may act for a conversation next_actor, or ''."""
    if next_actor in MERCHANT_NEXT_ACTORS:
        return "merchant"
    if next_actor in BUYER_NEXT_ACTORS:
        return "buyer"
    return ""


def buyer_agent_identity(buyer_id: str) -> str:
    return f"shopping-cli-buyer-agent:{buyer_id}"


def truncate_text(value: Any, limit: int) -> str:
    return str(value or "")[:limit]

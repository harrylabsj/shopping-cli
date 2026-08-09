"""Pure negotiation decision-message write-intent projection.

This leaf owns the pure message/state shaping used right before an accepted
decision is appended to the conversation: the sender derived from the acting
role, the conversation status the decision message moves the negotiation to
(closed for decline, otherwise the counterpart's waiting state), and the
structured payload that records the decision, its idempotency key and the
server-derived agent identity. It never touches SQLite, claims, turn/state
transitions, audit events or negotiation writes; ``submit_decision`` in
:mod:`shopping_cli.services.negotiation` composes these pure projections
around its DB writes and ownership checks.
"""

from __future__ import annotations

from typing import Any

from shopping_cli.core import negotiation as protocol


def decision_sender(role: str) -> str:
    """The message sender for a decision written by the given role."""
    return "merchant_agent" if role == "merchant" else "buyer"


def decision_status(role: str, action: str) -> str:
    """The conversation status an accepted decision message moves to.

    A decline closes the conversation; any other action hands the turn to the
    counterpart (merchant decisions put the negotiation back on the buyer,
    buyer decisions on the merchant).
    """
    if action == "decline":
        return "closed"
    return "waiting_buyer" if role == "merchant" else "waiting_merchant"


def decision_structured_payload(
    agent_id: str,
    role: str,
    decision: dict[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    """Shape the structured payload written alongside an accepted decision.

    The decision dict is embedded by reference, exactly as the facade passed
    it; the idempotency key and server-derived agent identity are recorded so
    a replay lookup can match the exact same payload without a fresh write.
    """
    return {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "idempotency_key": idempotency_key,
        "agent_id": agent_id,
        "role": role,
        "source_id": agent_id,
        "decision": decision,
    }

"""Pure negotiation snapshot block projection.

This leaf owns the pure projections that shape a negotiation snapshot from
already-loaded catalog and message data: the product / stock / delivery
blocks, the stock-status bucket derivation, the newest proposal, and the
newest decision's open issues. It never touches SQLite, claims, turn/state
transitions, or negotiation writes; ``build_snapshot`` in
:mod:`shopping_cli.services.negotiation` composes these pure projections
around its DB reads and ownership checks.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shopping_cli.core import negotiation as protocol


def stock_status_for(quantity: int) -> str:
    """Derive the public stock-status bucket from a server-side quantity."""
    return "available" if quantity > 2 else "low" if quantity > 0 else "out_of_stock"


def project_product(product: dict[str, Any]) -> dict[str, Any]:
    """Shape the frozen-schema ``product`` block from a catalog product row.

    The optional description is included only when it survives truncation, so
    absent/empty descriptions keep the block minimal.
    """
    block: dict[str, Any] = {
        "sku": protocol.truncate_text(product["sku"], 128),
        "title": protocol.truncate_text(product["title"], 500),
        "currency": protocol.truncate_text(product["currency"], 8),
        "list_price": float(product["price"]),
    }
    description = protocol.truncate_text(product.get("description"), 2000)
    if description:
        block["description"] = description
    return block


def project_stock(quantity: int, observed_at: str) -> dict[str, Any]:
    """Shape the frozen-schema ``stock`` block for an observed quantity.

    The observation timestamp is carried verbatim (already RFC 3339); the
    status bucket is derived from the server-side quantity.
    """
    return {
        "status": stock_status_for(quantity),
        "quantity": quantity,
        "observed_at": observed_at,
        "reserved": False,
        "source": {"backend": "local_marketplace", "observed_at": observed_at},
    }


def project_delivery(delivery_rule: dict[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any]:
    """Shape the frozen-schema ``delivery`` block from a product delivery rule.

    ETA bounds are derived from the rule's ``eta_minutes`` (default 60) with a
    120-minute window. ``now`` is injectable for deterministic callers and
    defaults to the current UTC time; both ETA bounds use the same ``now``.
    """
    rule = delivery_rule or {}
    eta_minutes = int(rule.get("eta_minutes") or 60)
    now = now or datetime.now(timezone.utc)
    eta_start = now.timestamp() + eta_minutes * 60
    eta_end = now.timestamp() + (eta_minutes + 120) * 60
    block: dict[str, Any] = {
        "eta_start": datetime.fromtimestamp(eta_start, timezone.utc).isoformat(timespec="seconds"),
        "eta_end": datetime.fromtimestamp(eta_end, timezone.utc).isoformat(timespec="seconds"),
        "fee": float(rule.get("fee") or 0),
    }
    notes = protocol.truncate_text(rule.get("notes"), 500)
    if notes:
        block["notes"] = notes
    return block


def latest_proposal(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the newest proposal carried by an already-projected message list.

    Projected snapshot messages carry a ``proposal`` key that is a dict or
    ``None``; scanning in reverse returns the most recent non-None value.
    """
    for message in reversed(messages):
        proposal = message.get("proposal")
        if isinstance(proposal, dict):
            return proposal
    return None


def latest_open_issues(messages: list[dict[str, Any]]) -> list[str]:
    """Return the newest decision's open issues from raw DB message dicts.

    Only decisions matching the current protocol version are considered. The
    newest such decision contributes its ``open_issues`` (each truncated to
    500 chars, empty entries dropped, capped at 32); any other shape yields an
    empty list, matching the facade's fail-closed projection.
    """
    for message in reversed(messages):
        payload = message.get("structured_payload") or {}
        decision = payload.get("decision") if payload.get("protocol_version") == protocol.PROTOCOL_VERSION else None
        if isinstance(decision, dict):
            issues = decision.get("open_issues")
            if isinstance(issues, list):
                return [protocol.truncate_text(issue, 500) for issue in issues if str(issue or "").strip()][:32]
            return []
    return []

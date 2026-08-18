"""Pure negotiation gate decision strategies.

This leaf owns the decision strategies shared by the negotiation policy gate
that are truly pure: the proposal fact checks and the buyer non-binding gate.
They read catalog/policy facts through the ``conn`` argument and return a
:class:`~shopping_cli.services.negotiation_policy_result.GateOutcome` without
writing, mutating shared state, or changing turn/state transitions.

The merchant gate stays in :mod:`shopping_cli.services.negotiation`: it reads
the merchant automation boundary floor through
``merchant_agent._effective_floor_price`` (structured ``floor_price`` /
``max_discount_percent`` first, free-text ``automation_boundaries`` fallback) —
a cross-module private access that a pure leaf must not own. The facade imports
``check_proposal_facts`` from here so the merchant gate keeps calling the
identical shared check.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from shopping_cli.core import negotiation as protocol
from shopping_cli.core.catalog import product_summary
from shopping_cli.core.errors import NotFoundError
from shopping_cli.core.policies import list_policies
from shopping_cli.services.negotiation_policy_result import (
    ACCEPTED_OUTCOME as _ACCEPT,
    GateOutcome,
    rejected as _reject,
)


def check_proposal_facts(
    conn: sqlite3.Connection,
    conversation: sqlite3.Row,
    proposal: dict[str, Any],
) -> GateOutcome:
    """Fact checks shared by both roles, re-read from the latest catalog data.

    Beyond purchase feasibility (quantity <= stock) this verifies the
    proposal's observed stock quantity/status against the authoritative
    server-side stock, so a stale or forged inventory observation is never
    written into a public structured message."""
    sku = str(conversation["sku"] or "").strip()
    if not sku or proposal["sku"] != sku:
        return _reject("unknown_product", "磋商商品与会话商品不一致，请基于最新快照中的商品报价。")
    try:
        product = product_summary(conn, sku)
    except NotFoundError:
        return _reject("unknown_product", "商品当前不可用，请重新获取快照。")
    if product["merchant_id"] != conversation["merchant_id"]:
        return _reject("unknown_product", "商品不属于该商家，请重新获取快照。")
    if proposal["currency"] != product["currency"]:
        return _reject("currency_mismatch", f"币种必须为 {product['currency']}。")
    stock_qty = int(product["stock"])
    if stock_qty <= 0:
        return _reject("insufficient_stock", "商品当前无库存，请重新获取快照后再报价。")
    if int(proposal["quantity"]) > stock_qty:
        return _reject("insufficient_stock", f"当前可售库存为 {stock_qty} 件，请调整数量。")
    # Authoritative inventory cross-check: the observed stock carried by the
    # proposal must match the latest server-side stock, otherwise a forged
    # observation would be written into the public structured message.
    expected_status = "available" if stock_qty > 2 else "low" if stock_qty > 0 else "out_of_stock"
    observed = proposal["stock"]
    if int(observed["quantity"]) != stock_qty or observed["status"] != expected_status:
        return _reject(
            "stale_inventory",
            f"库存观察与服务端最新库存（{stock_qty} 件，{expected_status}）不一致，请重新获取快照后再报价。",
        )
    now = datetime.now(timezone.utc)
    observed_at = protocol.parse_rfc3339(proposal["stock"]["observed_at"])
    valid_until = protocol.parse_rfc3339(proposal["valid_until"])
    eta_start = protocol.parse_rfc3339(proposal["delivery"]["eta_start"])
    eta_end = protocol.parse_rfc3339(proposal["delivery"]["eta_end"])
    if observed_at is None or valid_until is None or eta_start is None or eta_end is None:
        return _reject("invalid_timestamp", "时间字段必须是带时区的 RFC 3339 时间戳。")
    if valid_until <= now:
        return _reject("quote_expired", "报价有效期已过期，请重新获取快照后再报价。")
    if eta_end < eta_start:
        return _reject("invalid_delivery", "配送时效结束时间早于开始时间，请修正。")
    valid_refs = {
        f"policy:{policy['code']}" for policy in list_policies(conn, str(conversation["merchant_id"]), limit=100)
    }
    unknown_refs = [ref for ref in proposal["after_sales_policy_refs"] if ref not in valid_refs]
    if unknown_refs:
        return _reject("unknown_policy_ref", f"售后政策引用不存在或已失效: {unknown_refs[0]}。")
    return _ACCEPT


def buyer_gate(
    conn: sqlite3.Connection,
    conversation: sqlite3.Row,
    decision: dict[str, Any],
) -> GateOutcome:
    proposal = decision.get("proposal")
    if decision["action"] in {"propose", "counter"} and proposal is None:
        return _reject("missing_proposal", "propose/counter 必须携带结构化 proposal。")
    if proposal is None:
        return _ACCEPT
    # Non-binding boundary: the buyer side checks structure, facts and expiry
    # only. Private budgets live in the Kiwi profile and are never sent here.
    # A buyer proposal is a non-binding intent, but an accepted one is still
    # written into the public structured message, so the same authoritative
    # stock-observation consistency (quantity/status vs latest server stock)
    # is enforced for buyers as for merchants.
    return check_proposal_facts(conn, conversation, proposal)

"""Deterministic buyer-side consultation helpers."""

from __future__ import annotations

import sqlite3
from typing import Any

from shopping_cli.core.catalog import product_summary, search_products
from shopping_cli.core.conversations import append_message, conversation_summary, ensure_conversation
from shopping_cli.core.risk import infer_intent

MVP_WARNINGS = [
    "MVP records consultation only; no order is created.",
    "No stock is reserved by shopping-cli.",
    "Payment, refund, escrow, and delivery-success handling are outside this version.",
]


def ask(
    conn: sqlite3.Connection,
    buyer_id: str,
    text: str,
    city: str = "",
    area: str = "",
    limit: int = 3,
    source_id: str = "buyer-cli",
    host: str = "",
    session_id: str = "",
    reuse_open: bool = True,
) -> dict[str, Any]:
    buyer_id = str(buyer_id or "").strip()
    if not buyer_id:
        raise SystemExit("buyer id is required")
    candidates = search_products(conn, query=text, city=city, area=area, limit=limit)
    if not candidates:
        return {
            "ok": True,
            "buyer_id": buyer_id,
            "candidates": [],
            "conversation": None,
            "warnings": ["No matching merchant or product found.", *MVP_WARNINGS],
            "missing_facts": ["merchant", "product"],
        }
    selected = candidates[0]
    conversation = ensure_conversation(conn, buyer_id, selected["merchant_id"], selected["sku"], reuse_open=reuse_open)
    message = append_message(
        conn,
        conversation["id"],
        "buyer",
        infer_intent(text),
        text,
        structured_payload={
            "city": city,
            "area": area,
            "selected_sku": selected["sku"],
            "source_id": source_id or "buyer-cli",
            "host": host or "",
            "session_id": session_id or "",
        },
    )
    return {
        "ok": True,
        "buyer_id": buyer_id,
        "candidates": candidates,
        "selected": selected,
        "conversation": conversation_summary(conn, conversation["id"]),
        "message": message,
        "warnings": MVP_WARNINGS,
    }


def summarize(conn: sqlite3.Connection, conversation_id: str) -> dict[str, Any]:
    conversation = conversation_summary(conn, conversation_id)
    option = conversation.get("product")
    if option is None and conversation.get("sku"):
        try:
            option = product_summary(conn, conversation["sku"])
        except SystemExit:
            option = None
    missing_facts: list[str] = []
    warnings = list(MVP_WARNINGS)
    if option is None:
        missing_facts.append("product")
    else:
        if not option["merchant"].get("contact"):
            missing_facts.append("merchant contact")
        if not option["delivery"].get("service_area"):
            missing_facts.append("delivery rule")
        if option["stock"] <= 0:
            warnings.append("Product is out of stock.")
    if conversation["status"] == "human_required":
        warnings.append("Merchant human review is required before any commitment.")
    for flag in conversation["flags"]:
        warnings.append(f"Human review flag: {flag['reason']}")
    next_action = (
        "Wait for merchant human review."
        if conversation["status"] == "human_required"
        else "Use purchase_intent only to record interest; confirm order and payment outside this MVP."
    )
    return {
        "ok": True,
        "conversation": conversation,
        "option": option,
        "missing_facts": missing_facts,
        "warnings": warnings,
        "next_action": next_action,
        "no_order_created": True,
        "no_stock_reserved": True,
    }


def record_intent(conn: sqlite3.Connection, conversation_id: str, intent: str, text: str) -> dict[str, Any]:
    if intent not in {"purchase_intent", "quote_request"}:
        raise SystemExit("--intent must be purchase_intent or quote_request")
    message = append_message(conn, conversation_id, "buyer", intent, text, structured_payload={"source_id": "buyer-cli"})
    return {
        "ok": True,
        "message": message,
        "conversation": conversation_summary(conn, conversation_id),
        "warnings": MVP_WARNINGS,
    }

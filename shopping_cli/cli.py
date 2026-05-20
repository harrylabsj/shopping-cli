"""Argparse CLI for the standalone shopping-cli MVP."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

from shopping_cli import VERSION
from shopping_cli.adapters import hermes, openclaw
from shopping_cli.adapters.shopping_legacy import import_json_store
from shopping_cli.agents import buyer_cli, merchant_agent, merchant_daemon
from shopping_cli.agents.tools import HTTPMerchantAgentTools
from shopping_cli.api.app import (
    AuthError,
    HUMAN_REVIEW_SENDERS,
    _agent_token_row,
    _agent_token_summary,
    _default_merchant_agent_id,
    _issue_agent_token,
    _merchant_audit_events,
    _require_merchant_token,
    _resolve_agent_token,
    _safe_non_negative_int,
    create_app,
)
from shopping_cli.config import DEFAULT_DB_PATH
from shopping_cli.core.catalog import (
    create_merchant,
    create_product,
    list_merchants,
    merchant_summary,
    require_merchant,
    search_merchants,
    search_products,
    set_stock,
    update_merchant,
    update_product,
    upsert_delivery_rule,
)
from shopping_cli.core.channels import ingest_buyer_message
from shopping_cli.core.conversations import merchant_conversations
from shopping_cli.core.conversations import add_flag, append_message, conversation_summary, ensure_conversation, require_open_conversation
from shopping_cli.core.harness import append_audit_event, next_actor_for_status
from shopping_cli.core.risk import infer_intent
from shopping_cli.db.session import db_session, decode_json, now_iso
from shopping_cli.llm.dispatcher import HTTPMarketplaceToolDispatcher, MarketplaceToolDispatcher
from shopping_cli.llm.prompts import buyer_system_prompt, merchant_system_prompt
from shopping_cli.llm.providers import provider_from_env
from shopping_cli.llm.runner import (
    MAX_LLM_PROVIDER_RETRIES,
    MAX_LLM_PROVIDER_RETRY_DELAY_SECONDS,
    MAX_LLM_TOOL_CALL_BUDGET,
    MAX_LLM_TOOL_LOOP_STEPS,
    run_marketplace_tool_loop,
)

MAX_SQLITE_INTEGER = 2**63 - 1


def emit_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def emit(value: Any, fmt: str) -> None:
    if fmt == "json":
        emit_json(value)
    else:
        if isinstance(value, dict) and isinstance(value.get("message"), str):
            print(value["message"])
        else:
            print(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a whole number") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    if number > MAX_SQLITE_INTEGER:
        raise argparse.ArgumentTypeError(f"must be <= {MAX_SQLITE_INTEGER}")
    return number


def non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a whole number") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    if number > MAX_SQLITE_INTEGER:
        raise argparse.ArgumentTypeError(f"must be <= {MAX_SQLITE_INTEGER}")
    return number


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("must be finite")
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def float_value(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc


def positive_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a whole number") from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return seconds


def positive_int_at_most(maximum: int) -> Any:
    def parse(value: str) -> int:
        number = positive_int(value)
        if number > maximum:
            raise argparse.ArgumentTypeError(f"must be <= {maximum}")
        return number

    return parse


def non_negative_int_at_most(maximum: int) -> Any:
    def parse(value: str) -> int:
        number = non_negative_int(value)
        if number > maximum:
            raise argparse.ArgumentTypeError(f"must be <= {maximum}")
        return number

    return parse


def non_negative_float_at_most(maximum: float) -> Any:
    def parse(value: str) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise argparse.ArgumentTypeError("must be a number") from exc
        if not math.isfinite(number):
            raise argparse.ArgumentTypeError("must be finite")
        if number < 0:
            raise argparse.ArgumentTypeError("must be non-negative")
        if number > maximum:
            raise argparse.ArgumentTypeError(f"must be <= {maximum:g}")
        return number

    return parse


def tcp_port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a whole number") from exc
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return port


def resolve_agent_token_for_cli(conn: Any, merchant_id: str, token: str | None, token_prefix: str | None) -> str:
    try:
        return _resolve_agent_token(conn, merchant_id, token, token_prefix)
    except (AuthError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


def db_path_from_args(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "agent_db", None) or args.db or args.data or DEFAULT_DB_PATH).expanduser()


def adapter_for_host(host: str) -> Any:
    if host == "openclaw":
        return openclaw
    if host == "hermes":
        return hermes
    raise SystemExit(f"Unknown adapter host: {host}")


def cmd_merchant_create(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        merchant = create_merchant(
            conn,
            merchant_id=args.id,
            name=args.name,
            city=args.city or "",
            service_area=args.service_area or "",
            contact=args.contact or "",
            hours=args.hours or "",
            automation_boundaries=args.automation_boundaries or "",
            tags=args.tags or "",
            delivery_fee=args.delivery_fee,
            delivery_eta_minutes=args.delivery_eta_minutes,
            delivery_radius_km=args.delivery_radius_km,
        )
    emit({"ok": True, "merchant": merchant, "message": f"Merchant created: {args.id}"}, args.format)


def cmd_merchant_list(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        merchants = list_merchants(conn, limit=args.limit, offset=args.offset)
    if args.format == "text":
        if not merchants:
            print("No merchants found.")
            return
        print(f"{'MERCHANT_ID':<14} {'NAME':<24} {'CITY':<14} SERVICE_AREA")
        for merchant in merchants:
            print(
                f"{merchant['id']:<14} "
                f"{merchant['name']:<24} "
                f"{merchant['city'] or '-':<14} "
                f"{merchant['service_area'] or '-'}"
            )
        return
    emit({"ok": True, "results": merchants}, args.format)


def cmd_merchant_update(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        merchant = update_merchant(
            conn,
            merchant_id=args.id,
            name=args.name,
            city=args.city,
            service_area=args.service_area,
            contact=args.contact,
            hours=args.hours,
            automation_boundaries=args.automation_boundaries,
            tags=args.tags,
            delivery_fee=args.delivery_fee,
            delivery_eta_minutes=args.delivery_eta_minutes,
            delivery_radius_km=args.delivery_radius_km,
        )
    emit({"ok": True, "merchant": merchant, "message": f"Merchant updated: {args.id}"}, args.format)


def cmd_merchant_human_review(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        conversations = merchant_conversations(
            conn,
            args.merchant,
            "human_required",
            limit=args.limit,
            offset=args.offset,
        )
    if args.format == "text":
        emit_conversation_table(conversations, f"No human-review conversations for {args.merchant}.")
        return
    emit({"ok": True, "merchant_id": args.merchant, "conversations": conversations}, args.format)


def cmd_delivery_set(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        delivery = upsert_delivery_rule(
            conn,
            args.merchant,
            service_area=args.service_area or "",
            fee=args.fee,
            eta_minutes=args.eta_minutes,
            radius_km=args.radius_km,
            notes=args.notes or "",
        )
    if args.format == "text":
        emit_delivery_rule_text(args.merchant, delivery)
        return
    emit({"ok": True, "merchant_id": args.merchant, "delivery": delivery}, args.format)


def emit_delivery_rule_text(merchant_id: str, delivery: dict[str, Any]) -> None:
    print(f"Delivery rule updated: {merchant_id}")
    print(f"Service area: {delivery.get('service_area') or '-'}")
    print(f"Fee: {delivery.get('currency') or 'CNY'} {float(delivery.get('fee') or 0):g}")
    print(f"ETA: {int(delivery.get('eta_minutes') or 0)} minutes")
    print(f"Radius: {float(delivery.get('radius_km') or 0):g} km")
    print(f"Notes: {delivery.get('notes') or '-'}")


def cmd_product_add(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        product = create_product(
            conn,
            merchant_id=args.merchant,
            sku=args.sku,
            title=args.title,
            price=args.price,
            stock=args.stock,
            currency=args.currency,
            category=args.category or "",
            tags=args.tags or "",
            description=args.description or "",
            delivery_attributes=args.delivery_attributes or "",
        )
    emit({"ok": True, "product": product, "message": f"Product added: {args.sku}"}, args.format)


def cmd_product_stock(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        product = set_stock(conn, args.sku, args.stock, args.merchant or "")
    emit({"ok": True, "product": product, "message": f"Stock set: {args.sku} -> {args.stock}"}, args.format)


def cmd_product_update(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        product = update_product(
            conn,
            sku=args.sku,
            merchant_id=args.merchant or "",
            title=args.title,
            price=args.price,
            stock=args.stock,
            currency=args.currency,
            category=args.category,
            tags=args.tags,
            description=args.description,
            delivery_attributes=args.delivery_attributes,
        )
    emit({"ok": True, "product": product, "message": f"Product updated: {args.sku}"}, args.format)


def cmd_search_products(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        results = search_products(
            conn,
            query=args.query or "",
            city=args.city or "",
            area=args.area or "",
            max_price=args.max_price,
            include_out_of_stock=args.include_out_of_stock,
            limit=args.limit,
            offset=args.offset,
        )
    if args.format == "text":
        if not results:
            query = args.query or "all products"
            print(f"No products found for {query}.")
            return
        print(f"{'SKU':<14} {'STOCK':<6} {'PRICE':<10} {'MERCHANT':<20} TITLE")
        for product in results:
            price = f"{product['currency']} {product['price']:g}"
            print(
                f"{product['sku']:<14} "
                f"{product['stock']:<6} "
                f"{price:<10} "
                f"{product['merchant']['name']:<20} "
                f"{product['title']}"
            )
        return
    emit({"ok": True, "query": args.query or "", "results": results}, args.format)


def cmd_search_merchants(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        results = search_merchants(
            conn,
            query=args.query or "",
            city=args.city or "",
            limit=args.limit,
            offset=args.offset,
        )
    if args.format == "text":
        if not results:
            query = args.query or "all merchants"
            print(f"No merchants found for {query}.")
            return
        print(f"{'MERCHANT_ID':<16} {'CITY':<14} {'PRODUCTS':<8} {'SERVICE_AREA':<22} NAME")
        for merchant in results:
            print(
                f"{merchant['id']:<16} "
                f"{merchant['city'] or '-':<14} "
                f"{merchant['product_count']:<8} "
                f"{merchant['service_area'] or '-':<22} "
                f"{merchant['name']}"
            )
        return
    emit({"ok": True, "query": args.query or "", "results": results}, args.format)


def cmd_buyer_ask(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        result = buyer_cli.ask(conn, args.buyer, args.text, city=args.city or "", area=args.area or "")
    if args.format == "text":
        print(f"Buyer: {result['buyer_id']}")
        conversation = result.get("conversation")
        selected = result.get("selected")
        if not conversation or not selected:
            print("No matching merchant or product found.")
            warnings = result.get("warnings") or []
            if warnings:
                print("Warnings:")
                for warning in warnings:
                    print(f"- {warning}")
            return
        print(f"Conversation: {conversation['id']}")
        print(f"Selected: {selected['sku']} - {selected['title']}")
        print(f"Merchant: {selected['merchant']['name']}")
        print(f"Status: {conversation['status']}")
        print(f"Next actor: {conversation['next_actor']}")
        warnings = result.get("warnings") or []
        if warnings:
            print("Warnings:")
            for warning in warnings:
                print(f"- {warning}")
        return
    emit(result, args.format)


def cmd_channel_ingest(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        result = ingest_buyer_message(
            conn,
            channel=args.channel,
            external_user_id=args.external_user,
            text=args.text,
            city=args.city or "",
            area=args.area or "",
            conversation_id=args.conversation or "",
            external_message_id=args.external_message_id or "",
        )
    if args.format == "text":
        emit_channel_ingest_text(result)
        return
    emit(result, args.format)


def emit_channel_ingest_text(result: dict[str, Any]) -> None:
    message = result.get("message") or {}
    payload = message.get("structured_payload") or {}
    conversation = result.get("conversation") or {}
    selected = result.get("selected") or {}
    print(f"Channel: {result.get('channel') or payload.get('channel') or '-'}")
    print(f"External user: {payload.get('external_user_id') or '-'}")
    print(f"Buyer: {result.get('buyer_id') or '-'}")
    print(f"Idempotent: {yes_no(result.get('idempotent'))}")
    if not conversation:
        print("No matching merchant or product found.")
        missing_facts = result.get("missing_facts") or []
        if missing_facts:
            print(f"Missing facts: {', '.join(missing_facts)}")
        warnings = result.get("warnings") or []
        if warnings:
            print("Warnings:")
            for warning in warnings:
                print(f"- {warning}")
        return
    print(f"Conversation: {conversation.get('id') or '-'}")
    print(f"Message: {message.get('id') or '-'}")
    print(f"Status: {conversation.get('status') or '-'}")
    print(f"Next actor: {conversation.get('next_actor') or '-'}")
    if selected:
        print(f"Selected: {selected.get('sku') or '-'} - {selected.get('title') or '-'}")
    warnings = result.get("warnings") or []
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")


def cmd_buyer_summarize(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        result = buyer_cli.summarize(conn, args.conversation)
    if args.format == "text":
        conversation = result["conversation"]
        print(f"Conversation: {conversation['id']}")
        print(f"Buyer: {conversation['buyer_id']}")
        print(f"Merchant: {conversation['merchant_id']}")
        print(f"Status: {conversation['status']}")
        print(f"Next actor: {conversation['next_actor']}")
        option = result.get("option")
        if option:
            print(f"Option: {option['sku']} - {option['title']}")
            print(f"Price: {option['currency']} {option['price']:g}")
            print(f"Stock: {option['stock']}")
        missing_facts = result.get("missing_facts") or []
        if missing_facts:
            print(f"Missing facts: {', '.join(missing_facts)}")
        print(f"Next action: {result['next_action']}")
        warnings = result.get("warnings") or []
        if warnings:
            print("Warnings:")
            for warning in warnings:
                print(f"- {warning}")
        return
    emit(result, args.format)


def cmd_buyer_intent(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        result = buyer_cli.record_intent(conn, args.conversation, args.intent, args.text)
    if args.format == "text":
        message = result["message"]
        conversation = result["conversation"]
        print(f"Buyer intent recorded: {message['id']}")
        print(f"Conversation: {conversation['id']}")
        print(f"Intent: {message['intent']}")
        print(f"Status: {conversation['status']}")
        print(f"Next actor: {conversation['next_actor']}")
        return
    emit(result, args.format)


def emit_chat_event(payload: dict[str, Any], fmt: str) -> None:
    if fmt == "json":
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    if payload.get("ok") is False:
        print(f"error: {payload.get('error')}")
        return
    event = payload.get("event")
    conversation = payload.get("conversation") or payload.get("summary", {}).get("conversation") or {}
    conversation_id = conversation.get("id", "")
    status = conversation.get("status", "")
    next_actor = conversation.get("next_actor", "")
    detail = f" {conversation_id}" if conversation_id else ""
    state = f" status={status} next_actor={next_actor}" if status else ""
    print(f"{event}{detail}{state}".strip())


def cmd_buyer_chat(args: argparse.Namespace) -> None:
    db_path = db_path_from_args(args)
    conversation_id = args.conversation or ""
    for raw_line in sys.stdin:
        text = raw_line.strip()
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            emit_chat_event({"ok": True, "event": "quit"}, args.format)
            break
        if text == "/summary":
            if not conversation_id:
                emit_chat_event({"ok": False, "event": "error", "error": "No active conversation."}, args.format)
                continue
            with db_session(db_path) as conn:
                summary = buyer_cli.summarize(conn, conversation_id)
            emit_chat_event({"ok": True, "event": "summary", "summary": summary}, args.format)
            continue
        if text == "/history":
            if not conversation_id:
                emit_chat_event({"ok": False, "event": "error", "error": "No active conversation."}, args.format)
                continue
            with db_session(db_path) as conn:
                conversation = conversation_summary(conn, conversation_id)
            emit_chat_event(
                {"ok": True, "event": "history", "conversation": conversation, "messages": conversation["messages"]},
                args.format,
            )
            continue
        if text.startswith("/intent "):
            if not conversation_id:
                emit_chat_event({"ok": False, "event": "error", "error": "No active conversation."}, args.format)
                continue
            parts = text.split(" ", 2)
            if len(parts) < 3 or parts[1] not in {"purchase_intent", "quote_request"}:
                emit_chat_event(
                    {"ok": False, "event": "error", "error": "Use /intent purchase_intent <text> or /intent quote_request <text>."},
                    args.format,
                )
                continue
            with db_session(db_path) as conn:
                message = append_message(
                    conn,
                    conversation_id,
                    "buyer",
                    parts[1],
                    parts[2],
                    structured_payload={"source_id": "buyer-chat"},
                )
                conversation = conversation_summary(conn, conversation_id)
            emit_chat_event({"ok": True, "event": "intent", "message": message, "conversation": conversation}, args.format)
            continue
        if conversation_id:
            with db_session(db_path) as conn:
                message = append_message(
                    conn,
                    conversation_id,
                    "buyer",
                    infer_intent(text),
                    text,
                    structured_payload={"source_id": "buyer-chat", "city": args.city or "", "area": args.area or ""},
                )
                conversation = conversation_summary(conn, conversation_id)
            emit_chat_event({"ok": True, "event": "message", "message": message, "conversation": conversation}, args.format)
            continue
        with db_session(db_path) as conn:
            result = buyer_cli.ask(conn, args.buyer, text, city=args.city or "", area=args.area or "")
        if result.get("conversation"):
            conversation_id = result["conversation"]["id"]
        result = dict(result)
        result["event"] = "ask"
        emit_chat_event(result, args.format)


def cmd_conversation_create(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        conversation = ensure_conversation(conn, args.buyer, args.merchant, args.sku or "")
        if args.text:
            append_message(
                conn,
                conversation["id"],
                "buyer",
                args.intent,
                args.text,
                structured_payload={"source_id": args.source_id or "buyer-cli"},
            )
            conversation = conversation_summary(conn, conversation["id"])
    if args.format == "text":
        print(f"Conversation created: {conversation['id']}")
        print(f"Buyer: {conversation['buyer_id']}")
        print(f"Merchant: {conversation['merchant_id']}")
        if conversation["sku"]:
            print(f"SKU: {conversation['sku']}")
        print(f"Status: {conversation['status']}")
        print(f"Next actor: {conversation['next_actor']}")
        return
    emit({"ok": True, "conversation": conversation}, args.format)


def cmd_conversation_show(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        conversation = conversation_summary(conn, args.conversation)
    if args.format == "text":
        print(f"Conversation: {conversation['id']}")
        print(f"Buyer: {conversation['buyer_id']}")
        print(f"Merchant: {conversation['merchant_id']}")
        if conversation["sku"]:
            print(f"SKU: {conversation['sku']}")
        print(f"Status: {conversation['status']}")
        print(f"Next actor: {conversation['next_actor']}")
        if conversation["flags"]:
            unresolved = [flag for flag in conversation["flags"] if not flag["resolved_at"]]
            print(f"Human reviews: {len(unresolved)} unresolved / {len(conversation['flags'])} total")
        print("Messages:")
        for message in conversation["messages"]:
            print(f"- {message['sender']}/{message['intent']}: {message['text']}")
        return
    emit({"ok": True, "conversation": conversation}, args.format)


def cmd_conversation_list(args: argparse.Namespace) -> None:
    clauses: list[str] = []
    values: list[Any] = []
    for column, value in (
        ("status", args.status),
        ("merchant_id", args.merchant),
        ("buyer_id", args.buyer),
        ("sku", args.sku),
    ):
        if value:
            clauses.append(f"{column} = ?")
            values.append(value)
    if args.updated_since:
        clauses.append("updated_at >= ?")
        values.append(args.updated_since)
    sql = "select id from conversations"
    if clauses:
        sql += " where " + " and ".join(clauses)
    sql += " order by updated_at desc limit ? offset ?"
    values.extend([args.limit, args.offset])
    with db_session(db_path_from_args(args)) as conn:
        rows = conn.execute(sql, values).fetchall()
        conversations = [conversation_summary(conn, row["id"]) for row in rows]
    if args.format == "text":
        emit_conversation_table(conversations, "No conversations found.")
        return
    emit({"ok": True, "conversations": conversations}, args.format)


def emit_conversation_table(conversations: list[dict[str, Any]], empty_message: str) -> None:
    if not conversations:
        print(empty_message)
        return
    print(f"{'ID':<12} {'BUYER':<14} {'MERCHANT':<14} {'STATUS':<18} {'NEXT_ACTOR':<16} UPDATED_AT")
    for conversation in conversations:
        print(
            f"{conversation['id']:<12} "
            f"{conversation['buyer_id']:<14} "
            f"{conversation['merchant_id']:<14} "
            f"{conversation['status']:<18} "
            f"{conversation['next_actor']:<16} "
            f"{conversation['updated_at']}"
        )


def cmd_conversation_message(args: argparse.Namespace) -> None:
    structured_payload = {"source_id": args.source_id or args.sender}
    status = str(args.status or "").strip()
    if args.sender in {"buyer", "buyer_cli"} and status:
        raise SystemExit("buyer messages cannot set conversation status")
    if status == "closed":
        raise SystemExit("conversation messages cannot close conversations; use conversation close")
    with db_session(db_path_from_args(args)) as conn:
        message = append_message(
            conn,
            args.conversation,
            args.sender,
            args.intent,
            args.text,
            structured_payload=structured_payload,
            status=args.status,
        )
        if status == "human_required":
            conversation = conversation_summary(conn, args.conversation)
            add_flag(
                conn,
                args.conversation,
                reason=str(message["structured_payload"].get("reason") or "human_required"),
                severity=str(message["structured_payload"].get("severity") or "review"),
                sku=conversation.get("sku") or "",
            )
        conversation = conversation_summary(conn, args.conversation)
    if args.format == "text":
        print(f"Message appended: {message['id']}")
        print(f"Conversation: {conversation['id']}")
        print(f"Sender: {message['sender']}")
        print(f"Intent: {message['intent']}")
        print(f"Status: {conversation['status']}")
        print(f"Next actor: {conversation['next_actor']}")
        return
    emit({"ok": True, "message": message, "conversation": conversation}, args.format)


def append_conversation_closed_audit(
    conn: Any,
    conversation_id: str,
    actor: str,
    next_actor: str,
    details: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"next_actor": next_actor}
    if details:
        payload.update(details)
    append_audit_event(conn, conversation_id, actor, "conversation_closed", payload)


def cmd_conversation_close(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        require_open_conversation(conn, args.conversation)
        next_actor = next_actor_for_status("closed")
        if args.text:
            append_message(
                conn,
                args.conversation,
                args.sender,
                args.intent,
                args.text,
                structured_payload={"source_id": args.source_id or args.sender},
                status="closed",
            )
        else:
            conn.execute(
                "update conversations set status = 'closed', next_actor = ?, updated_at = ?, last_sender = ? where id = ?",
                (next_actor, now_iso(), args.sender, args.conversation),
            )
        append_conversation_closed_audit(conn, args.conversation, args.sender, next_actor)
        conversation = conversation_summary(conn, args.conversation)
    if args.format == "text":
        print(f"Conversation closed: {conversation['id']}")
        print(f"Closed by: {conversation['last_sender']}")
        print(f"Status: {conversation['status']}")
        print(f"Next actor: {conversation['next_actor'] or '-'}")
        return
    emit({"ok": True, "conversation": conversation}, args.format)


def _review_summary(conn: Any, flag_id: int) -> dict[str, Any]:
    row = conn.execute("select * from moderation_flags where id = ?", (flag_id,)).fetchone()
    if row is None:
        raise SystemExit(f"Unknown human review: {flag_id}")
    conversation = conversation_summary(conn, row["conversation_id"])
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "merchant_id": conversation["merchant_id"],
        "buyer_id": conversation["buyer_id"],
        "sku": row["sku"],
        "reason": row["reason"],
        "severity": row["severity"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"] or None,
        "resolution": row["resolution"],
        "resolved_by": row["resolved_by"],
    }


def cmd_conversation_human_review(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        conversation = conversation_summary(conn, args.conversation)
        flag = add_flag(conn, args.conversation, args.reason, severity=args.severity, sku=conversation.get("sku") or "")
        next_actor = next_actor_for_status("human_required", flag["reason"])
        conn.execute(
            "update conversations set status = 'human_required', next_actor = ?, updated_at = ?, last_sender = ? where id = ?",
            (next_actor, now_iso(), args.source_id or "operator", args.conversation),
        )
        append_audit_event(
            conn,
            args.conversation,
            args.source_id or "operator",
            "conversation_routed",
            {"status": "human_required", "next_actor": next_actor, "reason": flag["reason"]},
        )
        review = _review_summary(conn, flag["id"])
        conversation = conversation_summary(conn, args.conversation)
    if args.format == "text":
        print(f"Human review flagged: {review['id']}")
        print(f"Conversation: {conversation['id']}")
        print(f"Reason: {review['reason']}")
        print(f"Severity: {review['severity']}")
        print(f"Status: {conversation['status']}")
        print(f"Next actor: {conversation['next_actor']}")
        return
    emit({"ok": True, "review": review, "conversation": conversation}, args.format)


def cmd_conversation_resolve_review(args: argparse.Namespace) -> None:
    status = "closed" if args.action == "close" else "waiting_buyer"
    with db_session(db_path_from_args(args)) as conn:
        require_open_conversation(conn, args.conversation)
        now = now_iso()
        resolved = conn.execute(
            """
            update moderation_flags
            set resolved_at = ?, resolution = ?, resolved_by = ?
            where conversation_id = ? and resolved_at = ''
            """,
            (now, args.action, args.sender, args.conversation),
        )
        if resolved.rowcount == 0:
            raise SystemExit(f"No unresolved human reviews for conversation: {args.conversation}")
        next_actor = next_actor_for_status(status)
        if args.text:
            append_message(
                conn,
                args.conversation,
                args.sender,
                args.intent,
                args.text,
                structured_payload={"source_id": args.source_id or args.sender, "resolution": args.action},
                status=status,
            )
        else:
            conn.execute(
                "update conversations set status = ?, next_actor = ?, updated_at = ?, last_sender = ? where id = ?",
                (status, next_actor, now, args.sender, args.conversation),
            )
        append_audit_event(
            conn,
            args.conversation,
            args.source_id or args.sender,
            "human_review_resolved",
            {"resolution": args.action, "status": status, "next_actor": next_actor},
        )
        if status == "closed":
            append_conversation_closed_audit(
                conn,
                args.conversation,
                args.source_id or args.sender,
                next_actor,
                {"resolution": args.action, "source": "human_review"},
            )
        rows = conn.execute("select id from moderation_flags where conversation_id = ? order by id", (args.conversation,)).fetchall()
        reviews = [_review_summary(conn, row["id"]) for row in rows]
        conversation = conversation_summary(conn, args.conversation)
    if args.format == "text":
        resolved_count = resolved.rowcount if resolved.rowcount >= 0 else 0
        print(f"Human review resolved: {conversation['id']}")
        print(f"Resolution: {args.action}")
        print(f"Resolved reviews: {resolved_count}")
        print(f"Status: {conversation['status']}")
        print(f"Next actor: {conversation['next_actor'] or '-'}")
        return
    emit({"ok": True, "reviews": reviews, "conversation": conversation}, args.format)


def cmd_agent_run(args: argparse.Namespace) -> None:
    api_url = args.api_url or os.environ.get("SHOPPING_MARKETPLACE_API_URL") or os.environ.get("SHOPPING_API_URL") or ""
    if api_url:
        token = args.agent_token or os.environ.get("SHOPPING_AGENT_TOKEN") or args.merchant_token or os.environ.get("SHOPPING_MERCHANT_TOKEN")
        if not token:
            raise SystemExit("--merchant-token or --agent-token is required with --api-url")
        host = args.host or os.environ.get("SHOPPING_AGENT_HOST") or ""
        session_id = args.session_id or os.environ.get("SHOPPING_AGENT_SESSION_ID") or ""
        tool_kwargs = {"host": host, "session_id": session_id} if host or session_id else {}
        tools = HTTPMerchantAgentTools(api_url, args.merchant, token, **tool_kwargs)
        if args.once:
            result = merchant_agent.process_once_with_tools(tools, args.merchant)
            if args.format == "text":
                emit_agent_run_once_text(result)
                return
            emit(result, args.format)
            return
        merchant_daemon.run_tools_forever(
            tools,
            args.merchant,
            interval=args.interval,
            state_file=args.state_file,
            stop_file=args.stop_file,
        )
        return
    if args.once:
        with db_session(db_path_from_args(args)) as conn:
            result = merchant_agent.process_once(conn, args.merchant)
        if args.format == "text":
            emit_agent_run_once_text(result)
            return
        emit(result, args.format)
        return
    merchant_daemon.run_forever(
        db_path_from_args(args),
        args.merchant,
        interval=args.interval,
        state_file=args.state_file,
        stop_file=args.stop_file,
    )


def emit_agent_run_once_text(result: dict[str, Any]) -> None:
    replied = result.get("replied") or []
    failed = result.get("failed") or []
    abandoned = result.get("abandoned") or []
    print(f"Agent run: {result.get('merchant_id') or '-'}")
    print(f"Checked: {_safe_non_negative_int(result.get('checked'))}")
    print(f"Replied: {len(replied)}")
    print(f"Failed: {len(failed)}")
    print(f"Abandoned: {len(abandoned)}")
    for item in replied:
        line = (
            f"- replied {item.get('conversation_id') or '-'} "
            f"message={item.get('message_id') or '-'} "
            f"human_required={yes_no(item.get('human_required'))}"
        )
        if item.get("reason"):
            line = f"{line} reason={item['reason']}"
        print(line)
    for item in failed:
        print(
            f"- failed {item.get('conversation_id') or '-'} "
            f"message={item.get('message_id') or '-'} "
            f"error={redact_secret_text(item.get('error')) or '-'}"
        )
    for item in abandoned:
        target = item.get("conversation_id") or "-"
        print(f"- abandoned {target} message={item.get('message_id') or '-'}")


def yes_no(value: Any) -> str:
    return "yes" if value else "no"


SECRET_VALUE_RE = re.compile(r"shopping_(?:merchant|agent|buyer)_[^\s\"',]+")
SECRET_KEY_RE = re.compile(
    r"((?:merchant_token|agent_token|buyer_token|auth_token|authorization)\s*[:=]\s*)(?:Bearer\s+)?[^\s\"',]+",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"(Bearer\s+)[^\s\"',]+", re.IGNORECASE)


def redact_secret_text(value: Any) -> str:
    text = str(value or "")
    text = BEARER_RE.sub(r"\1[redacted-token]", text)
    text = SECRET_KEY_RE.sub(r"\1[redacted-token]", text)
    return SECRET_VALUE_RE.sub("[redacted-token]", text)


def emit_agent_runtime_metadata(result: dict[str, Any]) -> None:
    print(f"Running: {yes_no(result.get('running'))}")
    print(f"PID: {result.get('pid') or '-'}")
    print(f"Mode: {result.get('mode') or 'sqlite'}")
    if result.get("api_url"):
        print(f"API URL: {result['api_url']}")
    if result.get("host"):
        print(f"Host: {result['host']}")
    if result.get("session_id"):
        print(f"Session: {result['session_id']}")
    print(f"Log: {result.get('log_file') or '-'}")
    print(f"State: {result.get('state_file') or '-'}")


def emit_agent_start_text(result: dict[str, Any]) -> None:
    print(f"Agent started: {result.get('merchant_id') or '-'}")
    emit_agent_runtime_metadata(result)
    if result.get("stale_replaced"):
        print("Stale pid replaced: yes")


def emit_agent_stop_text(result: dict[str, Any]) -> None:
    print(f"Agent stopped: {result.get('merchant_id') or '-'}")
    print(f"Stopped: {yes_no(result.get('ok'))}")
    print(f"Was running: {yes_no(result.get('was_running'))}")
    emit_agent_runtime_metadata(result)


def cmd_agent_start(args: argparse.Namespace) -> None:
    api_url = args.api_url or os.environ.get("SHOPPING_MARKETPLACE_API_URL") or os.environ.get("SHOPPING_API_URL") or ""
    agent_token = args.agent_token or os.environ.get("SHOPPING_AGENT_TOKEN") or ""
    merchant_token = args.merchant_token or os.environ.get("SHOPPING_MERCHANT_TOKEN") or ""
    result = merchant_daemon.start_agent(
        db_path_from_args(args),
        args.merchant,
        interval=args.interval,
        state_dir=args.state_dir,
        api_url=api_url,
        agent_token=agent_token,
        merchant_token=merchant_token,
        host=args.host or os.environ.get("SHOPPING_AGENT_HOST") or "",
        session_id=args.session_id or os.environ.get("SHOPPING_AGENT_SESSION_ID") or "",
    )
    if args.format == "text":
        emit_agent_start_text(result)
        return
    emit(result, args.format)


def cmd_agent_stop(args: argparse.Namespace) -> None:
    result = merchant_daemon.stop_agent(
        db_path_from_args(args),
        args.merchant,
        state_dir=args.state_dir,
        timeout=args.timeout,
    )
    if args.format == "text":
        emit_agent_stop_text(result)
        return
    emit(result, args.format)


def emit_agent_status_text(result: dict[str, Any]) -> None:
    heartbeat = result.get("heartbeat") or {}
    counters = result.get("counters") or {}
    print(f"Merchant: {result.get('merchant_id') or '-'}")
    emit_agent_runtime_metadata(result)
    print(f"Heartbeat: {heartbeat.get('status') or '-'}")
    print(f"Last seen: {heartbeat.get('last_seen_at') or '-'}")
    print(f"Checked: {_safe_non_negative_int(counters.get('checked'))}")
    print(f"Replied: {_safe_non_negative_int(counters.get('replied'))}")
    print(f"Last error: {redact_secret_text(result.get('last_error')) or '-'}")
    print(f"Started: {result.get('started_at') or '-'}")
    print(f"Updated: {result.get('updated_at') or '-'}")


def cmd_agent_status(args: argparse.Namespace) -> None:
    result = merchant_daemon.status_agent(db_path_from_args(args), args.merchant, state_dir=args.state_dir)
    if args.format == "text":
        emit_agent_status_text(result)
        return
    emit(result, args.format)


def emit_agent_logs_text(result: dict[str, Any]) -> None:
    print(f"Logs: {result.get('merchant_id') or '-'}")
    print(f"File: {result.get('log_file') or '-'}")
    entries = result.get("entries") or []
    if not entries:
        print("No log entries.")
        return
    for entry in entries:
        if not isinstance(entry, dict):
            print(redact_secret_text(entry))
            continue
        if entry.get("event") == "raw":
            print(redact_secret_text(entry.get("text")))
            continue
        fields = [f"{entry.get('at') or '-'} {entry.get('event') or 'event'}"]
        if "checked" in entry:
            fields.append(f"checked={_safe_non_negative_int(entry.get('checked'))}")
        if "replied_count" in entry:
            fields.append(f"replied={_safe_non_negative_int(entry.get('replied_count'))}")
        if entry.get("error"):
            fields.append(f"error={redact_secret_text(entry['error'])}")
        print(" ".join(fields))


def cmd_agent_logs(args: argparse.Namespace) -> None:
    result = merchant_daemon.logs_agent(args.merchant, tail=args.tail, state_dir=args.state_dir)
    if args.format == "text":
        emit_agent_logs_text(result)
        return
    emit(result, args.format)


def cmd_agent_heartbeat(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        result = merchant_agent.heartbeat(conn, args.merchant, args.status)
    if args.format == "text":
        emit_agent_heartbeat_text(result)
        return
    emit({"ok": True, "agent": result}, args.format)


def emit_agent_heartbeat_text(agent: dict[str, Any]) -> None:
    print(f"Heartbeat recorded: {agent['id']}")
    print(f"Owner: {agent['owner_id']}")
    print(f"Status: {agent['status']}")
    print(f"Last seen: {agent['last_seen_at']}")
    print(f"Capabilities: {', '.join(agent['capabilities']) if agent['capabilities'] else '-'}")
    print(f"Checked: {_safe_non_negative_int(agent.get('checked_count'))}")
    print(f"Replied: {_safe_non_negative_int(agent.get('replied_count'))}")
    if agent.get("last_error"):
        print(f"Last error: {agent['last_error']}")


def cmd_llm_run(args: argparse.Namespace) -> None:
    role = str(args.role)
    actor = str(args.actor)
    source_id = args.source_id or f"shopping-cli-{role}-llm:{actor}"
    token_scope = args.token_scope or ("merchant_agent" if role == "merchant" else "buyer")
    api_url = args.api_url or os.environ.get("SHOPPING_MARKETPLACE_API_URL") or os.environ.get("SHOPPING_API_URL") or ""
    auth_token = args.auth_token or os.environ.get("SHOPPING_LLM_AUTH_TOKEN") or ""
    if api_url and not auth_token:
        if token_scope in {"buyer", "buyer_cli"}:
            auth_token = os.environ.get("SHOPPING_BUYER_TOKEN") or ""
        elif token_scope in {"merchant", "merchant_agent"}:
            auth_token = os.environ.get("SHOPPING_AGENT_TOKEN") or os.environ.get("SHOPPING_MERCHANT_TOKEN") or ""
    if api_url and not auth_token:
        raise SystemExit("--auth-token or SHOPPING_LLM_AUTH_TOKEN is required with --api-url")
    dispatcher: Any
    if api_url:
        dispatcher = HTTPMarketplaceToolDispatcher(
            api_url,
            auth_token=auth_token,
            source_id=source_id,
            host=args.host,
            session_id=args.session_id,
            actor=actor,
            token_scope=token_scope,
        )
    else:
        dispatcher = MarketplaceToolDispatcher(
            db_path_from_args(args),
            source_id=source_id,
            host=args.host,
            session_id=args.session_id,
            actor=actor,
            token_scope=token_scope,
        )
    user_text = str(args.text)
    if args.conversation:
        if api_url:
            conversation = dispatcher.conversation_summary(args.conversation)
        else:
            with db_session(db_path_from_args(args)) as conn:
                conversation = conversation_summary(conn, args.conversation)
        if not api_url and token_scope not in {"local_trusted", "operator"}:
            owner_key = "merchant_id" if role == "merchant" else "buyer_id"
            if str(conversation.get(owner_key) or "") != actor:
                raise SystemExit(f"conversation {args.conversation} is not owned by {role} actor {actor}")
        context = {
            "conversation_id": conversation["id"],
            "buyer_id": conversation["buyer_id"],
            "merchant_id": conversation["merchant_id"],
            "sku": conversation.get("sku") or "",
            "status": conversation["status"],
            "next_actor": conversation["next_actor"],
            "messages": [
                {
                    "sender": message["sender"],
                    "intent": message["intent"],
                    "text": message["text"],
                }
                for message in conversation["messages"]
            ],
            "flags": conversation.get("flags") or [],
        }
        user_text = f"{user_text}\n\nConversation context:\n{json.dumps(context, ensure_ascii=False, sort_keys=True)}"
    if role == "merchant":
        automation_boundaries = ""
        if not api_url:
            with db_session(db_path_from_args(args)) as conn:
                row = conn.execute("select automation_boundaries from merchants where id = ?", (actor,)).fetchone()
                if row is not None:
                    automation_boundaries = str(row["automation_boundaries"] or "")
        system_prompt = merchant_system_prompt(automation_boundaries)
    else:
        system_prompt = buyer_system_prompt()
    result = run_marketplace_tool_loop(
        provider_from_env(),
        dispatcher,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        max_steps=args.max_steps,
        max_tool_calls=args.max_tool_calls,
        provider_retries=args.provider_retries,
        provider_retry_delay_seconds=args.provider_retry_delay_seconds,
    )
    if args.format == "text":
        emit_llm_run_text(result)
        return
    emit(result, args.format)


def emit_llm_run_text(result: dict[str, Any]) -> None:
    print(f"OK: {yes_no(result.get('ok'))}")
    if result.get("error"):
        print(f"Error: {result['error']}")
    print("Answer:")
    print(str(result.get("content") or ""))
    tool_results = result.get("tool_results") or []
    if not tool_results:
        return
    print("Tool results:")
    for item in tool_results:
        tool = str(item.get("tool") or item.get("name") or "-")
        status = "ok" if item.get("ok", True) else "error"
        if item.get("error"):
            print(f"- {tool}: {status} error={item['error']}")
        else:
            print(f"- {tool}: {status}")


def cmd_adapter_inspect(args: argparse.Namespace) -> None:
    adapter = adapter_for_host(args.host)
    result = adapter.inspect_host(
        db_path=db_path_from_args(args),
        project_root=args.project_root or None,
        skill_root=args.skill_root or None,
    )
    if args.format == "text":
        emit_adapter_inspect_text(result)
        return
    emit(result, args.format)


def emit_adapter_inspect_text(result: dict[str, Any]) -> None:
    print(f"Adapter: {result.get('host') or '-'}")
    print(f"OK: {yes_no(result.get('ok'))}")
    print(f"Command: {result.get('command') or '-'}")
    print(f"Command available: {yes_no(result.get('command_available'))}")
    print(f"Command path: {result.get('command_path') or '-'}")
    print(f"Project root: {result.get('project_root') or '-'}")
    print(f"Project root valid: {yes_no(result.get('project_root_valid'))}")
    print(f"Skill root: {result.get('skill_root') or '-'}")
    print(f"Skill installed: {yes_no(result.get('skill_installed'))}")
    print(f"Skill symlink: {yes_no(result.get('skill_is_symlink'))}")
    print(f"Skill target: {result.get('skill_target') or '-'}")
    print(f"Skill points to project: {yes_no(result.get('skill_points_to_project'))}")
    if result.get("db_path"):
        print(f"DB: {result['db_path']}")


def cmd_adapter_doctor(args: argparse.Namespace) -> None:
    adapter = adapter_for_host(args.host)
    result = adapter.doctor(
        db_path=db_path_from_args(args),
        project_root=args.project_root or None,
        skill_root=args.skill_root or None,
    )
    if args.format == "text":
        emit_adapter_doctor_text(result)
        return
    emit(result, args.format)


def emit_adapter_doctor_text(result: dict[str, Any]) -> None:
    print(f"Adapter doctor: {result.get('host') or '-'}")
    print(f"OK: {yes_no(result.get('ok'))}")
    issues = result.get("issues") or []
    if not issues:
        print("Issues: none")
        return
    print("Issues:")
    for issue in issues:
        print(f"- {issue}")


def cmd_adapter_install_command(args: argparse.Namespace) -> None:
    adapter = adapter_for_host(args.host)
    command = adapter.install_command(project_root=args.project_root or None, dry_run=args.dry_run, force=args.force)
    emit(
        {
            "ok": True,
            "host": args.host,
            "command": command,
            "message": " ".join(command),
        },
        args.format,
    )


def cmd_agent_token(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        require_merchant(conn, args.merchant)
        if args.merchant_token:
            _require_merchant_token(conn, args.merchant, {"merchant_token": args.merchant_token})
        agent_id = _default_merchant_agent_id(args.merchant)
        try:
            token, expires_at = _issue_agent_token(conn, args.merchant, agent_id, args.ttl_seconds)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        issued = _agent_token_row(conn, token)
        append_audit_event(
            conn,
            "",
            args.merchant,
            "agent_token_issued",
            {"agent_id": agent_id, "token": _agent_token_summary(issued)},
        )
    emit(
        {
            "ok": True,
            "merchant_id": args.merchant,
            "agent_id": agent_id,
            "agent_token": token,
            "expires_at": expires_at,
            "message": f"Agent token issued for {agent_id}: {token}",
        },
        args.format,
    )


def cmd_agent_tokens(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        require_merchant(conn, args.merchant)
        if args.merchant_token:
            _require_merchant_token(conn, args.merchant, {"merchant_token": args.merchant_token})
        rows = conn.execute(
            """
            select token, token_hash, token_prefix, token_suffix, role, merchant_id, agent_id, created_at, expires_at, revoked_at
            from api_tokens
            where merchant_id = ? and role = 'agent'
            order by created_at desc, token desc
            limit ? offset ?
            """,
            (args.merchant, args.limit, args.offset),
        ).fetchall()
        tokens = [_agent_token_summary(row) for row in rows]
    if args.format == "text":
        if not tokens:
            print(f"No scoped agent tokens for {args.merchant}.")
            return
        print(f"{'TOKEN_PREFIX':<26} {'SUFFIX':<8} {'STATUS':<8} {'EXPIRES_AT':<20} AGENT_ID")
        for token in tokens:
            status = "revoked" if token["revoked"] else "expired" if token["expired"] else "active"
            expires_at = token["expires_at"] or "-"
            print(
                f"{token['token_prefix']:<26} "
                f"{token['token_suffix']:<8} "
                f"{status:<8} "
                f"{expires_at:<20} "
                f"{token['agent_id']}"
            )
        return
    emit({"ok": True, "merchant_id": args.merchant, "tokens": tokens}, args.format)


def cmd_agent_rotate_token(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        require_merchant(conn, args.merchant)
        if args.merchant_token:
            _require_merchant_token(conn, args.merchant, {"merchant_token": args.merchant_token})
        token = resolve_agent_token_for_cli(conn, args.merchant, args.token, args.token_prefix)
        row = _agent_token_row(conn, token)
        if row is None or row["role"] != "agent" or row["merchant_id"] != args.merchant:
            raise SystemExit("Unknown scoped agent token for merchant")
        revoked_at = row["revoked_at"] or now_iso()
        if not row["revoked_at"]:
            conn.execute("update api_tokens set revoked_at = ? where token = ?", (revoked_at, token))
        try:
            new_token, expires_at = _issue_agent_token(conn, args.merchant, row["agent_id"], args.ttl_seconds)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        previous = _agent_token_row(conn, token)
        replacement = _agent_token_row(conn, new_token)
        append_audit_event(
            conn,
            "",
            args.merchant,
            "agent_token_rotated",
            {
                "agent_id": row["agent_id"],
                "revoked_at": revoked_at,
                "previous_token": _agent_token_summary(previous),
                "new_token": _agent_token_summary(replacement),
            },
        )
    emit(
        {
            "ok": True,
            "rotated": True,
            "merchant_id": args.merchant,
            "agent_id": row["agent_id"],
            "agent_token": new_token,
            "expires_at": expires_at,
            "revoked_at": revoked_at,
            "previous_token": _agent_token_summary(previous),
            "message": f"Agent token rotated for {row['agent_id']}: {new_token}",
        },
        args.format,
    )


def cmd_agent_revoke_token(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        require_merchant(conn, args.merchant)
        if args.merchant_token:
            _require_merchant_token(conn, args.merchant, {"merchant_token": args.merchant_token})
        token = resolve_agent_token_for_cli(conn, args.merchant, args.token, args.token_prefix)
        row = _agent_token_row(conn, token)
        if row is None or row["role"] != "agent" or row["merchant_id"] != args.merchant:
            raise SystemExit("Unknown scoped agent token for merchant")
        revoked_at = row["revoked_at"] or now_iso()
        if not row["revoked_at"]:
            conn.execute("update api_tokens set revoked_at = ? where token = ?", (revoked_at, token))
        revoked = _agent_token_row(conn, token)
        append_audit_event(
            conn,
            "",
            args.merchant,
            "agent_token_revoked",
            {"agent_id": row["agent_id"], "revoked_at": revoked_at, "token": _agent_token_summary(revoked)},
        )
    emit(
        {
            "ok": True,
            "revoked": True,
            "merchant_id": args.merchant,
            "agent_id": row["agent_id"],
            "token_role": row["role"],
            "revoked_at": revoked_at,
            "message": f"Agent token revoked for {row['agent_id']}",
        },
        args.format,
    )


def _agent_summary(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "owner_id": row["owner_id"],
        "status": row["status"],
        "capabilities": decode_json(row["capabilities_json"], []),
        "last_seen_at": row["last_seen_at"],
        "pid": _safe_non_negative_int(row["pid"]),
        "version": row["version"],
        "last_error": row["last_error"],
        "checked_count": _safe_non_negative_int(row["checked_count"]),
        "replied_count": _safe_non_negative_int(row["replied_count"]),
    }


def cmd_agent_list(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        if args.merchant:
            rows = conn.execute(
                "select * from agents where owner_id = ? order by id limit ? offset ?",
                (args.merchant, args.limit, args.offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "select * from agents order by id limit ? offset ?",
                (args.limit, args.offset),
            ).fetchall()
        agents = [_agent_summary(row) for row in rows]
    if args.format == "text":
        if not agents:
            scope = f" for {args.merchant}" if args.merchant else ""
            print(f"No marketplace agents{scope}.")
            return
        print(f"{'AGENT_ID':<36} {'OWNER':<14} {'STATUS':<14} {'LAST_SEEN':<20} {'CHECKED':<7} REPLIED")
        for agent in agents:
            print(
                f"{agent['id']:<36} "
                f"{agent['owner_id']:<14} "
                f"{agent['status']:<14} "
                f"{agent['last_seen_at']:<20} "
                f"{agent['checked_count']:<7} "
                f"{agent['replied_count']}"
            )
        return
    emit({"ok": True, "agents": agents}, args.format)


def cmd_agent_show(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        row = conn.execute("select * from agents where id = ?", (args.agent,)).fetchone()
        if row is None:
            raise SystemExit(f"Unknown agent: {args.agent}")
        agent = _agent_summary(row)
    if args.format == "text":
        print(f"Agent: {agent['id']}")
        print(f"Type: {agent['type']}")
        print(f"Owner: {agent['owner_id']}")
        print(f"Status: {agent['status']}")
        print(f"Last seen: {agent['last_seen_at']}")
        print(f"Version: {agent['version'] or '-'}")
        if agent["pid"]:
            print(f"PID: {agent['pid']}")
        print(f"Capabilities: {', '.join(agent['capabilities']) if agent['capabilities'] else '-'}")
        print(f"Checked: {agent['checked_count']}")
        print(f"Replied: {agent['replied_count']}")
        if agent["last_error"]:
            print(f"Last error: {agent['last_error']}")
        return
    emit({"ok": True, "agent": agent}, args.format)


def cmd_human_review_queue(args: argparse.Namespace) -> None:
    sql = """
        select f.id from moderation_flags f
        join conversations c on c.id = f.conversation_id
        where f.resolved_at = ''
    """
    values: list[Any] = []
    if args.merchant:
        sql += " and c.merchant_id = ?"
        values.append(args.merchant)
    sql += " order by f.created_at desc, f.id desc limit ? offset ?"
    values.extend([args.limit, args.offset])
    with db_session(db_path_from_args(args)) as conn:
        rows = conn.execute(sql, values).fetchall()
        reviews = [_review_summary(conn, row["id"]) for row in rows]
    if args.format == "text":
        if not reviews:
            scope = f" for {args.merchant}" if args.merchant else ""
            print(f"No unresolved human-review items{scope}.")
            return
        print(f"{'ID':<5} {'CONVERSATION':<14} {'MERCHANT':<14} {'SEVERITY':<10} REASON")
        for review in reviews:
            print(
                f"{review['id']:<5} "
                f"{review['conversation_id']:<14} "
                f"{review['merchant_id']:<14} "
                f"{review['severity']:<10} "
                f"{review['reason']}"
            )
        return
    emit({"ok": True, "reviews": reviews}, args.format)


def cmd_human_review_show(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        review = _review_summary(conn, int(args.review))
        conversation = conversation_summary(conn, review["conversation_id"])
    if args.format == "text":
        print(f"Review {review['id']}")
        print(f"Conversation: {review['conversation_id']}")
        print(f"Merchant: {review['merchant_id']}")
        print(f"Buyer: {review['buyer_id']}")
        if review["sku"]:
            print(f"SKU: {review['sku']}")
        print(f"Severity: {review['severity']}")
        print(f"Reason: {review['reason']}")
        print(f"Status: {conversation['status']}")
        print(f"Next actor: {conversation['next_actor']}")
        print("Latest messages:")
        for message in conversation["messages"][-5:]:
            print(f"- {message['sender']}/{message['intent']}: {message['text']}")
        return
    emit({"ok": True, "review": review, "conversation": conversation}, args.format)


def cmd_human_review_resolve(args: argparse.Namespace) -> None:
    review_id = int(args.review)
    with db_session(db_path_from_args(args)) as conn:
        row = conn.execute("select * from moderation_flags where id = ?", (review_id,)).fetchone()
        if row is None:
            raise SystemExit(f"Unknown human review: {review_id}")
        if row["resolved_at"]:
            raise SystemExit(f"Human review already resolved: {review_id}")
        conversation_id = row["conversation_id"]
        require_open_conversation(conn, conversation_id)
        now = now_iso()
        conn.execute(
            """
            update moderation_flags
            set resolved_at = ?, resolution = ?, resolved_by = ?
            where id = ? and resolved_at = ''
            """,
            (now, args.action, args.sender, review_id),
        )
        remaining_rows = conn.execute(
            """
            select reason from moderation_flags
            where conversation_id = ? and resolved_at = ''
            order by case when reason = 'suspicious_content' then 0 else 1 end, id
            """,
            (conversation_id,),
        ).fetchall()
        remaining = len(remaining_rows)
        remaining_reason = str(remaining_rows[0]["reason"] or "") if remaining_rows else ""
        status = "human_required" if remaining else ("closed" if args.action == "close" else "waiting_buyer")
        status_reason = remaining_reason if status == "human_required" else str(row["reason"] or "")
        next_actor = next_actor_for_status(status, status_reason if status == "human_required" else "")
        if args.text:
            append_message(
                conn,
                conversation_id,
                args.sender,
                args.intent,
                args.text,
                structured_payload={
                    "source_id": args.source_id or args.sender,
                    "resolution": args.action,
                    "review_id": review_id,
                    "reason": status_reason,
                    "resolved_reason": row["reason"],
                },
                status=status,
            )
        else:
            conn.execute(
                "update conversations set status = ?, next_actor = ?, updated_at = ?, last_sender = ? where id = ?",
                (status, next_actor, now, args.sender, conversation_id),
            )
        append_audit_event(
            conn,
            conversation_id,
            args.source_id or args.sender,
            "human_review_resolved",
            {
                "review_id": review_id,
                "resolution": args.action,
                "status": status,
                "next_actor": next_actor,
                "remaining_unresolved_reviews": int(remaining or 0),
            },
        )
        if status == "closed":
            append_conversation_closed_audit(
                conn,
                conversation_id,
                args.source_id or args.sender,
                next_actor,
                {"resolution": args.action, "review_id": review_id, "source": "human_review"},
            )
        review = _review_summary(conn, review_id)
        rows = conn.execute("select id from moderation_flags where conversation_id = ? order by id", (conversation_id,)).fetchall()
        reviews = [_review_summary(conn, row["id"]) for row in rows]
        conversation = conversation_summary(conn, conversation_id)
    if args.format == "text":
        remaining_unresolved = sum(1 for item in reviews if not item["resolved_at"])
        print(f"Review {review['id']} resolved")
        print(f"Resolution: {review['resolution']}")
        print(f"Conversation: {conversation['id']}")
        print(f"Status: {conversation['status']}")
        print(f"Next actor: {conversation['next_actor']}")
        print(f"Remaining unresolved reviews: {remaining_unresolved}")
        return
    emit({"ok": True, "review": review, "reviews": reviews, "conversation": conversation}, args.format)


def _audit_details_text(details: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "agent_id",
        "review_id",
        "resolution",
        "status",
        "next_actor",
        "tool",
        "token_scope",
        "host",
        "session_id",
    ):
        value = details.get(key)
        if value not in (None, "", []):
            parts.append(f"{key}={value}")
    for key, label in (("token", "token"), ("previous_token", "previous_token"), ("new_token", "new_token")):
        token = details.get(key)
        if not isinstance(token, dict):
            continue
        prefix = token.get("token_prefix")
        if prefix:
            parts.append(f"{label}_prefix={prefix}")
        if token.get("revoked"):
            parts.append(f"{label}_status=revoked")
        elif token.get("expired"):
            parts.append(f"{label}_status=expired")
        elif token.get("active"):
            parts.append(f"{label}_status=active")
    return " ".join(str(part) for part in parts) or "-"


def cmd_audit_events(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        require_merchant(conn, args.merchant)
        if args.merchant_token:
            _require_merchant_token(conn, args.merchant, {"merchant_token": args.merchant_token})
        events = _merchant_audit_events(conn, args.merchant, event=args.event, limit=args.limit, offset=args.offset)
    if args.format == "text":
        if not events:
            print(f"No audit events for {args.merchant}.")
            return
        print(f"{'ID':<5} {'CREATED_AT':<20} {'EVENT':<28} {'ACTOR':<14} DETAILS")
        for event in events:
            print(
                f"{event['id']:<5} "
                f"{event['created_at']:<20} "
                f"{event['event']:<28} "
                f"{event['actor']:<14} "
                f"{_audit_details_text(event['details'])}"
            )
        return
    emit({"ok": True, "merchant_id": args.merchant, "events": events}, args.format)


def cmd_legacy_import(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        result = import_json_store(conn, args.from_json)
    if args.format == "text":
        imported = result.get("imported") or {}
        skipped = result.get("skipped") or {}
        print("Legacy import complete.")
        print(f"Merchants: {int(imported.get('merchants') or 0)}")
        print(f"Products: {int(imported.get('products') or 0)}")
        skipped_merchants = int(skipped.get("merchants") or 0)
        skipped_products = int(skipped.get("products") or 0)
        if skipped_merchants:
            print(f"Skipped merchants: {skipped_merchants}")
        if skipped_products:
            print(f"Skipped products: {skipped_products}")
        return
    emit(result, args.format)


def cmd_api_routes(args: argparse.Namespace) -> None:
    app = create_app(db_path_from_args(args))
    route_methods: dict[str, set[str]] = {}
    for route in getattr(app, "routes", []):
        path = getattr(route, "path", "")
        if not path:
            continue
        methods = {
            str(method)
            for method in getattr(route, "methods", set())
            if str(method) not in {"HEAD", "OPTIONS"}
        }
        route_methods.setdefault(path, set()).update(methods)
    routes = sorted(route_methods)
    route_details = [
        {"path": path, "methods": sorted(methods)}
        for path, methods in sorted(route_methods.items())
    ]
    if args.format == "text":
        for route in route_details:
            methods = route["methods"] or ["-"]
            for method in methods:
                print(f"{method:<6} {route['path']}")
        return
    emit(
        {
            "ok": True,
            "title": getattr(app, "title", "shopping-cli Marketplace API"),
            "fastapi_available": bool(getattr(getattr(app, "state", None), "fastapi_available", False)),
            "routes": routes,
            "route_details": route_details,
        },
        args.format,
    )


def cmd_api_serve(args: argparse.Namespace) -> None:
    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency environment specific
        raise SystemExit("uvicorn is required to serve the FastAPI app. Install shopping-cli[api].") from exc
    app = create_app(db_path_from_args(args))
    uvicorn.run(app, host=args.host, port=args.port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="shopping-cli local commerce consultation runtime.", add_help=True)
    parser.add_argument("--db", help=f"SQLite database path. Default: {DEFAULT_DB_PATH}")
    parser.add_argument("--data", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_agent_runtime_options(command_parser: argparse.ArgumentParser, include_db: bool = True) -> None:
        if include_db:
            command_parser.add_argument("--db", dest="agent_db", help="SQLite database path")
        command_parser.add_argument("--state-dir", default=None, help=argparse.SUPPRESS)

    merchant = subparsers.add_parser("merchant", help="Manage merchant profiles and review queues")
    merchant_sub = merchant.add_subparsers(dest="merchant_command", required=True)
    merchant_create = merchant_sub.add_parser("create", help="Create a merchant profile and delivery rule")
    merchant_create.add_argument("--id", required=True)
    merchant_create.add_argument("--name", required=True)
    merchant_create.add_argument("--city", default="")
    merchant_create.add_argument("--service-area", default="")
    merchant_create.add_argument("--contact", default="")
    merchant_create.add_argument("--hours", default="")
    merchant_create.add_argument("--automation-boundaries", default="")
    merchant_create.add_argument("--tags", default="")
    merchant_create.add_argument("--delivery-fee", type=float_value, default=0)
    merchant_create.add_argument("--delivery-eta-minutes", type=non_negative_int, default=0)
    merchant_create.add_argument("--delivery-radius-km", type=float_value, default=0)
    merchant_create.add_argument("--format", choices=["text", "json"], default="text")
    merchant_create.set_defaults(func=cmd_merchant_create)
    merchant_list = merchant_sub.add_parser("list", help="List merchants")
    merchant_list.add_argument("--limit", type=positive_int, default=50)
    merchant_list.add_argument("--offset", type=non_negative_int, default=0)
    merchant_list.add_argument("--format", choices=["text", "json"], default="text")
    merchant_list.set_defaults(func=cmd_merchant_list)
    merchant_update = merchant_sub.add_parser("update", help="Update a merchant profile and delivery rule")
    merchant_update.add_argument("--id", required=True)
    merchant_update.add_argument("--name")
    merchant_update.add_argument("--city")
    merchant_update.add_argument("--service-area")
    merchant_update.add_argument("--contact")
    merchant_update.add_argument("--hours")
    merchant_update.add_argument("--automation-boundaries")
    merchant_update.add_argument("--tags")
    merchant_update.add_argument("--delivery-fee", type=float_value)
    merchant_update.add_argument("--delivery-eta-minutes", type=non_negative_int)
    merchant_update.add_argument("--delivery-radius-km", type=float_value)
    merchant_update.add_argument("--format", choices=["text", "json"], default="text")
    merchant_update.set_defaults(func=cmd_merchant_update)
    human_review = merchant_sub.add_parser("human-review", help="View conversations requiring merchant human review")
    human_review.add_argument("--merchant", required=True)
    human_review.add_argument("--limit", type=positive_int, default=50)
    human_review.add_argument("--offset", type=non_negative_int, default=0)
    human_review.add_argument("--format", choices=["text", "json"], default="text")
    human_review.set_defaults(func=cmd_merchant_human_review)

    delivery = subparsers.add_parser("delivery", help="Configure merchant delivery rules")
    delivery_sub = delivery.add_subparsers(dest="delivery_command", required=True)
    delivery_set = delivery_sub.add_parser("set", help="Create or update a delivery rule")
    delivery_set.add_argument("--merchant", required=True)
    delivery_set.add_argument("--service-area", default="")
    delivery_set.add_argument("--fee", type=float_value, default=0)
    delivery_set.add_argument("--eta-minutes", type=non_negative_int, default=0)
    delivery_set.add_argument("--radius-km", type=float_value, default=0)
    delivery_set.add_argument("--notes", default="")
    delivery_set.add_argument("--format", choices=["text", "json"], default="text")
    delivery_set.set_defaults(func=cmd_delivery_set)

    product = subparsers.add_parser("product", help="Manage products and stock")
    product_sub = product.add_subparsers(dest="product_command", required=True)
    product_add = product_sub.add_parser("add", help="Publish a product")
    product_add.add_argument("--merchant", required=True)
    product_add.add_argument("--sku", required=True)
    product_add.add_argument("--title", required=True)
    product_add.add_argument("--price", required=True, type=float_value)
    product_add.add_argument("--stock", required=True, type=non_negative_int)
    product_add.add_argument("--currency", default="CNY")
    product_add.add_argument("--category", default="")
    product_add.add_argument("--tags", default="")
    product_add.add_argument("--description", default="")
    product_add.add_argument("--delivery-attributes", default="")
    product_add.add_argument("--format", choices=["text", "json"], default="text")
    product_add.set_defaults(func=cmd_product_add)
    product_stock = product_sub.add_parser("stock", help="Set product stock")
    product_stock.add_argument("--sku", required=True)
    product_stock.add_argument("--merchant", default="")
    product_stock.add_argument("--stock", required=True, type=non_negative_int)
    product_stock.add_argument("--format", choices=["text", "json"], default="text")
    product_stock.set_defaults(func=cmd_product_stock)
    product_update = product_sub.add_parser("update", help="Update product catalog fields or stock")
    product_update.add_argument("--sku", required=True)
    product_update.add_argument("--merchant", default="")
    product_update.add_argument("--title")
    product_update.add_argument("--price", type=float_value)
    product_update.add_argument("--stock", type=non_negative_int)
    product_update.add_argument("--currency")
    product_update.add_argument("--category")
    product_update.add_argument("--tags")
    product_update.add_argument("--description")
    product_update.add_argument("--delivery-attributes")
    product_update.add_argument("--format", choices=["text", "json"], default="text")
    product_update.set_defaults(func=cmd_product_update)

    search = subparsers.add_parser("search", help="Search marketplace inventory")
    search_sub = search.add_subparsers(dest="search_command", required=True)
    search_products_parser = search_sub.add_parser("products", help="Search products")
    search_products_parser.add_argument("--query", default="")
    search_products_parser.add_argument("--city", default="")
    search_products_parser.add_argument("--area", default="")
    search_products_parser.add_argument("--max-price", type=float_value)
    search_products_parser.add_argument("--include-out-of-stock", action="store_true")
    search_products_parser.add_argument("--limit", type=positive_int, default=10)
    search_products_parser.add_argument("--offset", type=non_negative_int, default=0)
    search_products_parser.add_argument("--format", choices=["text", "json"], default="text")
    search_products_parser.set_defaults(func=cmd_search_products)
    search_merchants_parser = search_sub.add_parser("merchants", help="Search merchants")
    search_merchants_parser.add_argument("--query", default="")
    search_merchants_parser.add_argument("--city", default="")
    search_merchants_parser.add_argument("--limit", type=positive_int, default=10)
    search_merchants_parser.add_argument("--offset", type=non_negative_int, default=0)
    search_merchants_parser.add_argument("--format", choices=["text", "json"], default="text")
    search_merchants_parser.set_defaults(func=cmd_search_merchants)

    channel = subparsers.add_parser("channel", help="Ingest external channel messages")
    channel_sub = channel.add_subparsers(dest="channel_command", required=True)
    channel_ingest = channel_sub.add_parser("ingest", help="Ingest an external buyer message")
    channel_ingest.add_argument("--channel", required=True)
    channel_ingest.add_argument("--external-user", required=True)
    channel_ingest.add_argument("--text", required=True)
    channel_ingest.add_argument("--conversation", default="")
    channel_ingest.add_argument("--city", default="")
    channel_ingest.add_argument("--area", default="")
    channel_ingest.add_argument("--external-message-id", default="")
    channel_ingest.add_argument("--format", choices=["text", "json"], default="text")
    channel_ingest.set_defaults(func=cmd_channel_ingest)

    buyer = subparsers.add_parser("buyer", help="Buyer consultation commands")
    buyer_sub = buyer.add_subparsers(dest="buyer_command", required=True)
    buyer_ask = buyer_sub.add_parser("ask", help="Search and open a merchant consultation")
    buyer_ask.add_argument("--buyer", required=True)
    buyer_ask.add_argument("--text", required=True)
    buyer_ask.add_argument("--city", default="")
    buyer_ask.add_argument("--area", default="")
    buyer_ask.add_argument("--format", choices=["text", "json"], default="text")
    buyer_ask.set_defaults(func=cmd_buyer_ask)
    buyer_summary = buyer_sub.add_parser("summarize", help="Summarize a consultation")
    buyer_summary.add_argument("--conversation", required=True)
    buyer_summary.add_argument("--format", choices=["text", "json"], default="text")
    buyer_summary.set_defaults(func=cmd_buyer_summarize)
    buyer_intent = buyer_sub.add_parser("intent", help="Record quote_request or purchase_intent as a message")
    buyer_intent.add_argument("--conversation", required=True)
    buyer_intent.add_argument("--intent", required=True, choices=["quote_request", "purchase_intent"])
    buyer_intent.add_argument("--text", required=True)
    buyer_intent.add_argument("--format", choices=["text", "json"], default="text")
    buyer_intent.set_defaults(func=cmd_buyer_intent)
    buyer_chat = buyer_sub.add_parser(
        "chat",
        help="Run a lightweight buyer chat REPL from stdin",
        description="Run a lightweight buyer chat REPL from stdin",
    )
    buyer_chat.add_argument("--buyer", required=True)
    buyer_chat.add_argument("--conversation", default="")
    buyer_chat.add_argument("--city", default="")
    buyer_chat.add_argument("--area", default="")
    buyer_chat.add_argument("--format", choices=["text", "json"], default="text")
    buyer_chat.set_defaults(func=cmd_buyer_chat)

    conversation = subparsers.add_parser("conversation", help="Manage consultations and messages")
    conversation_sub = conversation.add_subparsers(dest="conversation_command", required=True)
    conversation_create = conversation_sub.add_parser("create", help="Create a conversation and optional buyer message")
    conversation_create.add_argument("--buyer", required=True)
    conversation_create.add_argument("--merchant", required=True)
    conversation_create.add_argument("--sku", default="")
    conversation_create.add_argument("--intent", default="ask_product")
    conversation_create.add_argument("--text", default="")
    conversation_create.add_argument("--source-id", default="buyer-cli")
    conversation_create.add_argument("--format", choices=["text", "json"], default="text")
    conversation_create.set_defaults(func=cmd_conversation_create)
    conversation_show = conversation_sub.add_parser("show", help="Show one conversation")
    conversation_show.add_argument("--conversation", required=True)
    conversation_show.add_argument("--format", choices=["text", "json"], default="text")
    conversation_show.set_defaults(func=cmd_conversation_show)
    conversation_list = conversation_sub.add_parser("list", help="List conversations with simple filters")
    conversation_list.add_argument("--buyer", default="")
    conversation_list.add_argument("--merchant", default="")
    conversation_list.add_argument("--status", default="")
    conversation_list.add_argument("--sku", default="")
    conversation_list.add_argument("--updated-since", default="")
    conversation_list.add_argument("--limit", type=positive_int, default=50)
    conversation_list.add_argument("--offset", type=non_negative_int, default=0)
    conversation_list.add_argument("--format", choices=["text", "json"], default="text")
    conversation_list.set_defaults(func=cmd_conversation_list)
    conversation_message = conversation_sub.add_parser("message", help="Append a message to a conversation")
    conversation_message.add_argument("--conversation", required=True)
    conversation_message.add_argument("--sender", required=True, choices=["buyer", "buyer_cli", "merchant_agent", "merchant", "operator"])
    conversation_message.add_argument("--intent", required=True)
    conversation_message.add_argument("--text", required=True)
    conversation_message.add_argument("--status")
    conversation_message.add_argument("--source-id", default="")
    conversation_message.add_argument("--format", choices=["text", "json"], default="text")
    conversation_message.set_defaults(func=cmd_conversation_message)
    conversation_close = conversation_sub.add_parser("close", help="Close a conversation")
    conversation_close.add_argument("--conversation", required=True)
    conversation_close.add_argument(
        "--sender",
        default="operator",
        choices=["buyer", "buyer_cli", "merchant_agent", "merchant", "operator"],
    )
    conversation_close.add_argument("--intent", default="support")
    conversation_close.add_argument("--text", default="")
    conversation_close.add_argument("--source-id", default="")
    conversation_close.add_argument("--format", choices=["text", "json"], default="text")
    conversation_close.set_defaults(func=cmd_conversation_close)
    conversation_review = conversation_sub.add_parser("human-review", help="Mark a conversation for human review")
    conversation_review.add_argument("--conversation", required=True)
    conversation_review.add_argument("--reason", required=True)
    conversation_review.add_argument("--severity", default="review")
    conversation_review.add_argument("--source-id", default="operator")
    conversation_review.add_argument("--format", choices=["text", "json"], default="text")
    conversation_review.set_defaults(func=cmd_conversation_human_review)
    conversation_resolve = conversation_sub.add_parser("resolve-review", help="Resolve human-review flags")
    conversation_resolve.add_argument("--conversation", required=True)
    conversation_resolve.add_argument("--action", required=True, choices=["reply", "approve_public_answer", "reject", "close"])
    conversation_resolve.add_argument("--sender", choices=sorted(HUMAN_REVIEW_SENDERS), default="merchant")
    conversation_resolve.add_argument("--intent", default="support")
    conversation_resolve.add_argument("--text", default="")
    conversation_resolve.add_argument("--source-id", default="")
    conversation_resolve.add_argument("--format", choices=["text", "json"], default="text")
    conversation_resolve.set_defaults(func=cmd_conversation_resolve_review)

    agent = subparsers.add_parser("agent", help="Run resident merchant agents")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_start = agent_sub.add_parser("start", help="Start a background merchant agent daemon")
    agent_start.add_argument("--merchant", required=True)
    agent_start.add_argument("--interval", type=positive_float, default=3.0)
    agent_start.add_argument("--api-url", default="", help="Start a background agent through the marketplace API")
    agent_start.add_argument("--merchant-token", default="", help="Merchant API token for --api-url")
    agent_start.add_argument("--agent-token", default="", help="Scoped agent API token for --api-url")
    agent_start.add_argument("--host", default="", help="Host name for API-backed tool-call audit metadata")
    agent_start.add_argument("--session-id", default="", help="Host session id for API-backed tool-call audit metadata")
    agent_start.add_argument("--format", choices=["text", "json"], default="text")
    add_agent_runtime_options(agent_start)
    agent_start.set_defaults(func=cmd_agent_start)
    agent_stop = agent_sub.add_parser("stop", help="Stop a background merchant agent daemon")
    agent_stop.add_argument("--merchant", required=True)
    agent_stop.add_argument("--timeout", type=positive_float, default=5.0)
    agent_stop.add_argument("--format", choices=["text", "json"], default="text")
    add_agent_runtime_options(agent_stop)
    agent_stop.set_defaults(func=cmd_agent_stop)
    agent_status = agent_sub.add_parser("status", help="Show merchant agent daemon status")
    agent_status.add_argument("--merchant", required=True)
    agent_status.add_argument("--format", choices=["text", "json"], default="text")
    add_agent_runtime_options(agent_status)
    agent_status.set_defaults(func=cmd_agent_status)
    agent_logs = agent_sub.add_parser("logs", help="Show merchant agent daemon logs")
    agent_logs.add_argument("--merchant", required=True)
    agent_logs.add_argument("--tail", type=positive_int, default=20)
    agent_logs.add_argument("--format", choices=["text", "json"], default="text")
    add_agent_runtime_options(agent_logs, include_db=False)
    agent_logs.set_defaults(func=cmd_agent_logs)
    agent_list = agent_sub.add_parser("list", help="List marketplace agent heartbeats")
    agent_list.add_argument("--merchant", default="")
    agent_list.add_argument("--limit", type=positive_int, default=50)
    agent_list.add_argument("--offset", type=non_negative_int, default=0)
    agent_list.add_argument("--format", choices=["text", "json"], default="text")
    agent_list.set_defaults(func=cmd_agent_list)
    agent_show = agent_sub.add_parser("show", help="Show one marketplace agent heartbeat")
    agent_show.add_argument("--agent", required=True)
    agent_show.add_argument("--format", choices=["text", "json"], default="text")
    agent_show.set_defaults(func=cmd_agent_show)
    agent_run = agent_sub.add_parser("run", help="Poll and answer waiting merchant conversations")
    agent_run.add_argument("--merchant", required=True)
    agent_run.add_argument("--once", action="store_true")
    agent_run.add_argument("--interval", type=positive_float, default=3.0)
    agent_run.add_argument("--api-url", default="", help="Run through the marketplace API instead of direct SQLite")
    agent_run.add_argument("--merchant-token", default="", help="Merchant API token for --api-url")
    agent_run.add_argument("--agent-token", default="", help="Scoped agent API token for --api-url")
    agent_run.add_argument("--host", default="", help="Host name for API-backed tool-call audit metadata")
    agent_run.add_argument("--session-id", default="", help="Host session id for API-backed tool-call audit metadata")
    agent_run.add_argument("--format", choices=["text", "json"], default="text")
    agent_run.add_argument("--state-file", default=None, help=argparse.SUPPRESS)
    agent_run.add_argument("--stop-file", default=None, help=argparse.SUPPRESS)
    add_agent_runtime_options(agent_run)
    agent_run.set_defaults(func=cmd_agent_run)
    agent_heartbeat = agent_sub.add_parser("heartbeat", help="Record merchant agent health")
    agent_heartbeat.add_argument("--merchant", required=True)
    agent_heartbeat.add_argument("--status", choices=["online", "away", "human_required"], default="online")
    agent_heartbeat.add_argument("--format", choices=["text", "json"], default="text")
    add_agent_runtime_options(agent_heartbeat)
    agent_heartbeat.set_defaults(func=cmd_agent_heartbeat)
    agent_token = agent_sub.add_parser("token", help="Issue a scoped merchant-agent API token")
    agent_token.add_argument("--merchant", required=True)
    agent_token.add_argument("--merchant-token", default="")
    agent_token.add_argument("--ttl-seconds", type=positive_seconds, default=None, help="Optional scoped token lifetime in seconds")
    agent_token.add_argument("--format", choices=["text", "json"], default="text")
    agent_token.set_defaults(func=cmd_agent_token)
    agent_tokens = agent_sub.add_parser("tokens", help="List scoped merchant-agent API tokens")
    agent_tokens.add_argument("--merchant", required=True)
    agent_tokens.add_argument("--merchant-token", default="")
    agent_tokens.add_argument("--limit", type=positive_int, default=50)
    agent_tokens.add_argument("--offset", type=non_negative_int, default=0)
    agent_tokens.add_argument("--format", choices=["text", "json"], default="text")
    agent_tokens.set_defaults(func=cmd_agent_tokens)
    agent_rotate_token = agent_sub.add_parser("rotate-token", help="Rotate a scoped merchant-agent API token")
    agent_rotate_token.add_argument("--merchant", required=True)
    agent_rotate_token_target = agent_rotate_token.add_mutually_exclusive_group(required=True)
    agent_rotate_token_target.add_argument("--token")
    agent_rotate_token_target.add_argument("--token-prefix")
    agent_rotate_token.add_argument("--merchant-token", default="")
    agent_rotate_token.add_argument("--ttl-seconds", type=positive_seconds, default=None, help="Optional new token lifetime in seconds")
    agent_rotate_token.add_argument("--format", choices=["text", "json"], default="text")
    agent_rotate_token.set_defaults(func=cmd_agent_rotate_token)
    agent_revoke_token = agent_sub.add_parser("revoke-token", help="Revoke a scoped merchant-agent API token")
    agent_revoke_token.add_argument("--merchant", required=True)
    agent_revoke_token_target = agent_revoke_token.add_mutually_exclusive_group(required=True)
    agent_revoke_token_target.add_argument("--token")
    agent_revoke_token_target.add_argument("--token-prefix")
    agent_revoke_token.add_argument("--merchant-token", default="")
    agent_revoke_token.add_argument("--format", choices=["text", "json"], default="text")
    agent_revoke_token.set_defaults(func=cmd_agent_revoke_token)

    human_review_cli = subparsers.add_parser("human-review", help="Review flagged conversations")
    human_review_sub = human_review_cli.add_subparsers(dest="human_review_command", required=True)
    human_review_queue = human_review_sub.add_parser("queue", help="List unresolved human-review flags")
    human_review_queue.add_argument("--merchant", default="")
    human_review_queue.add_argument("--limit", type=positive_int, default=50)
    human_review_queue.add_argument("--offset", type=non_negative_int, default=0)
    human_review_queue.add_argument("--format", choices=["text", "json"], default="text")
    human_review_queue.set_defaults(func=cmd_human_review_queue)
    human_review_show = human_review_sub.add_parser("show", help="Show one human-review item with conversation context")
    human_review_show.add_argument("--review", required=True, type=positive_int)
    human_review_show.add_argument("--format", choices=["text", "json"], default="text")
    human_review_show.set_defaults(func=cmd_human_review_show)
    human_review_resolve = human_review_sub.add_parser("resolve", help="Resolve one human-review item by id")
    human_review_resolve.add_argument("--review", required=True, type=positive_int)
    human_review_resolve.add_argument("--action", required=True, choices=["reply", "approve_public_answer", "reject", "close"])
    human_review_resolve.add_argument("--sender", choices=sorted(HUMAN_REVIEW_SENDERS), default="merchant")
    human_review_resolve.add_argument("--intent", default="support")
    human_review_resolve.add_argument("--text", default="")
    human_review_resolve.add_argument("--source-id", default="")
    human_review_resolve.add_argument("--format", choices=["text", "json"], default="text")
    human_review_resolve.set_defaults(func=cmd_human_review_resolve)

    audit = subparsers.add_parser("audit", help="Inspect merchant audit events")
    audit_sub = audit.add_subparsers(dest="audit_command", required=True)
    audit_events = audit_sub.add_parser("events", help="List merchant audit events")
    audit_events.add_argument("--merchant", required=True)
    audit_events.add_argument("--event", default="")
    audit_events.add_argument("--limit", type=positive_int, default=50)
    audit_events.add_argument("--offset", type=non_negative_int, default=0)
    audit_events.add_argument("--merchant-token", default="")
    audit_events.add_argument("--format", choices=["text", "json"], default="text")
    audit_events.set_defaults(func=cmd_audit_events)

    llm = subparsers.add_parser("llm", help="Run optional LLM marketplace tool loops")
    llm_sub = llm.add_subparsers(dest="llm_command", required=True)
    llm_run = llm_sub.add_parser("run", help="Run one scoped LLM marketplace tool loop")
    llm_run.add_argument("--role", choices=["buyer", "merchant"], default="buyer")
    llm_run.add_argument("--actor", required=True)
    llm_run.add_argument("--text", required=True)
    llm_run.add_argument("--conversation", default="")
    llm_run.add_argument("--source-id", default="")
    llm_run.add_argument("--host", default="shopping-cli")
    llm_run.add_argument("--session-id", default="")
    llm_run.add_argument("--api-url", default="", help="Run LLM tools through the marketplace API instead of direct SQLite")
    llm_run.add_argument("--auth-token", default="", help="Bearer token for --api-url")
    llm_run.add_argument(
        "--token-scope",
        choices=["buyer", "buyer_cli", "merchant", "merchant_agent", "local_trusted", "operator"],
        default="",
    )
    llm_run.add_argument("--max-steps", type=positive_int_at_most(MAX_LLM_TOOL_LOOP_STEPS), default=4)
    llm_run.add_argument("--max-tool-calls", type=non_negative_int_at_most(MAX_LLM_TOOL_CALL_BUDGET), default=None)
    llm_run.add_argument("--provider-retries", type=non_negative_int_at_most(MAX_LLM_PROVIDER_RETRIES), default=0)
    llm_run.add_argument(
        "--provider-retry-delay-seconds",
        type=non_negative_float_at_most(MAX_LLM_PROVIDER_RETRY_DELAY_SECONDS),
        default=0.0,
    )
    llm_run.add_argument("--format", choices=["text", "json"], default="text")
    llm_run.set_defaults(func=cmd_llm_run)

    adapter = subparsers.add_parser("adapter", help="Inspect optional OpenClaw/Hermes adapters")
    adapter_sub = adapter.add_subparsers(dest="adapter_command", required=True)
    adapter_inspect = adapter_sub.add_parser("inspect", help="Inspect host adapter paths and commands")
    adapter_inspect.add_argument("--host", required=True, choices=["openclaw", "hermes"])
    adapter_inspect.add_argument("--project-root", default="")
    adapter_inspect.add_argument("--skill-root", default="")
    adapter_inspect.add_argument("--format", choices=["text", "json"], default="text")
    adapter_inspect.set_defaults(func=cmd_adapter_inspect)
    adapter_doctor = adapter_sub.add_parser("doctor", help="Report host adapter setup issues")
    adapter_doctor.add_argument("--host", required=True, choices=["openclaw", "hermes"])
    adapter_doctor.add_argument("--project-root", default="")
    adapter_doctor.add_argument("--skill-root", default="")
    adapter_doctor.add_argument("--format", choices=["text", "json"], default="text")
    adapter_doctor.set_defaults(func=cmd_adapter_doctor)
    adapter_install = adapter_sub.add_parser("install-command", help="Print the adapter install command")
    adapter_install.add_argument("--host", required=True, choices=["openclaw", "hermes"])
    adapter_install.add_argument("--project-root", default="")
    adapter_install.add_argument("--dry-run", action="store_true")
    adapter_install.add_argument("--force", action="store_true")
    adapter_install.add_argument("--format", choices=["text", "json"], default="text")
    adapter_install.set_defaults(func=cmd_adapter_install_command)

    legacy = subparsers.add_parser("legacy", help="Import existing Shopping catalog data")
    legacy_sub = legacy.add_subparsers(dest="legacy_command", required=True)
    legacy_import = legacy_sub.add_parser("import", help="Import merchants and products from a legacy JSON store")
    legacy_import.add_argument("--from-json", required=True)
    legacy_import.add_argument("--format", choices=["text", "json"], default="text")
    legacy_import.set_defaults(func=cmd_legacy_import)

    api = subparsers.add_parser("api", help="Inspect or run the marketplace API")
    api_sub = api.add_subparsers(dest="api_command", required=True)
    api_routes = api_sub.add_parser("routes", help="List marketplace API routes")
    api_routes.add_argument("--format", choices=["text", "json"], default="text")
    api_routes.set_defaults(func=cmd_api_routes)
    api_serve = api_sub.add_parser("serve", help="Serve the FastAPI marketplace API")
    api_serve.add_argument("--host", default="127.0.0.1")
    api_serve.add_argument("--port", type=tcp_port, default=8765)
    api_serve.set_defaults(func=cmd_api_serve)
    return parser


def _is_top_level_help(args_list: list[str]) -> bool:
    if not any(arg in {"-h", "--help"} for arg in args_list):
        return False
    remaining: list[str] = []
    skip_next = False
    for arg in args_list:
        if skip_next:
            skip_next = False
            continue
        if arg == "--db":
            skip_next = True
            continue
        if arg.startswith("--db=") or arg in {"-h", "--help"}:
            continue
        remaining.append(arg)
    return not remaining


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args_list = list(sys.argv[1:] if argv is None else argv)
    if _is_top_level_help(args_list):
        parser.print_help()
        return
    args = parser.parse_args(args_list)
    args.func(args)


if __name__ == "__main__":
    main()

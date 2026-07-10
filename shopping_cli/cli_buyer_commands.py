"""Buyer and external-channel CLI command handlers."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from shopping_cli.agents import buyer_cli
from shopping_cli.cli_common import db_path_from_args, emit, yes_no
from shopping_cli.core.channels import ingest_buyer_message
from shopping_cli.core.conversations import append_message, conversation_summary
from shopping_cli.core.risk import infer_intent
from shopping_cli.db.session import db_session


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
        print(f"Next action: {result['next_action']}")
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
        print(f"Next action: {result['next_action']}")
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

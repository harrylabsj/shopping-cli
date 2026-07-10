"""Conversation CLI command handlers."""

from __future__ import annotations

import argparse
from typing import Any

from shopping_cli.cli_common import db_path_from_args, emit
from shopping_cli.core.conversations import (
    conversation_list_summary,
    conversation_summary,
)
from shopping_cli.db.session import db_session
from shopping_cli.services import conversations as conversation_service


def cmd_conversation_create(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        conversation = conversation_service.create_conversation(
            conn,
            buyer_id=args.buyer,
            merchant_id=args.merchant,
            sku=args.sku or "",
            text=args.text or "",
            intent=args.intent,
            source_id=args.source_id or "buyer-cli",
            reuse_open=True,
        )
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
        summarize = conversation_summary if args.details else conversation_list_summary
        conversations = [summarize(conn, row["id"]) for row in rows]
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
        existing = conversation_summary(conn, args.conversation)
        result = conversation_service.append_conversation_message(
            conn,
            existing,
            args.conversation,
            sender=args.sender,
            intent=args.intent,
            text=args.text,
            structured_payload=structured_payload,
            status=args.status,
        )
        message = result["message"]
        conversation = result["conversation"]
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
    conversation_service.append_conversation_closed_audit(conn, conversation_id, actor, next_actor, details)


def cmd_conversation_close(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        existing = conversation_summary(conn, args.conversation)
        conversation = conversation_service.close_conversation(
            conn,
            existing,
            args.conversation,
            sender=args.sender,
            intent=args.intent,
            text=args.text or "",
            source_id=args.source_id or args.sender,
        )
    if args.format == "text":
        print(f"Conversation closed: {conversation['id']}")
        print(f"Closed by: {conversation['last_sender']}")
        print(f"Status: {conversation['status']}")
        print(f"Next actor: {conversation['next_actor'] or '-'}")
        return
    emit({"ok": True, "conversation": conversation}, args.format)

"""Argparse CLI for the standalone shopping-cli MVP."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from shopping_cli import VERSION
from shopping_cli.api.app import create_app
from shopping_cli.cli_common import (
    db_path_from_args,
    emit,
    float_value,
    non_negative_int,
    positive_float,
    positive_int,
    positive_seconds,
    tcp_port,
)
from shopping_cli.cli_agent_commands import (
    cmd_agent_heartbeat,
    cmd_agent_list,
    cmd_agent_logs,
    cmd_agent_revoke_token,
    cmd_agent_rotate_token,
    cmd_agent_run,
    cmd_agent_show,
    cmd_agent_start,
    cmd_agent_status,
    cmd_agent_stop,
    cmd_agent_token,
    cmd_agent_tokens,
)
from shopping_cli.cli_catalog_commands import (
    cmd_delivery_set,
    cmd_merchant_create,
    cmd_merchant_list,
    cmd_merchant_update,
    cmd_policy_add,
    cmd_policy_list,
    cmd_policy_show,
    cmd_product_add,
    cmd_product_stock,
    cmd_product_update,
    cmd_search_merchants,
    cmd_search_policies,
    cmd_search_products,
)
from shopping_cli.cli_buyer_commands import (
    cmd_buyer_ask,
    cmd_buyer_chat,
    cmd_buyer_intent,
    cmd_buyer_summarize,
    cmd_channel_ingest,
)
from shopping_cli.cli_conversation_commands import (
    append_conversation_closed_audit,
    cmd_conversation_close,
    cmd_conversation_create,
    cmd_conversation_list,
    cmd_conversation_message,
    cmd_conversation_show,
    emit_conversation_table,
)
from shopping_cli.cli_erp_commands import cmd_erp_sync
from shopping_cli.cli_listing_commands import cmd_listing_projections_list
from shopping_cli.config import ConfigError, DEFAULT_DB_PATH, validate_production_config
from shopping_cli.core.catalog import require_merchant
from shopping_cli.core.conversations import merchant_conversations
from shopping_cli.core.conversations import (
    add_flag,
    append_message,
    conversation_summary,
    require_open_conversation,
)
from shopping_cli.core.errors import ShoppingCliError
from shopping_cli.core.harness import append_audit_event, next_actor_for_status
from shopping_cli.db.session import db_session
from shopping_cli.services import audit as audit_service
from shopping_cli.services import human_review as human_review_service
from shopping_cli.services import tokens as token_service

HUMAN_REVIEW_SENDERS = human_review_service.HUMAN_REVIEW_SENDERS

def cmd_merchant_human_review(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        conversations = merchant_conversations(
            conn,
            args.merchant,
            "human_required",
            limit=args.limit,
            offset=args.offset,
            summary_only=False,
            include_flags=True,
        )
    if args.format == "text":
        emit_conversation_table(conversations, f"No human-review conversations for {args.merchant}.")
        return
    emit({"ok": True, "merchant_id": args.merchant, "conversations": conversations}, args.format)


def _review_summary(conn: Any, flag_id: int) -> dict[str, Any]:
    try:
        row = human_review_service.human_review_row(conn, flag_id, lambda value, _field: int(value))
    except ShoppingCliError as exc:
        raise SystemExit(str(exc)) from exc
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
        append_audit_event(
            conn,
            args.conversation,
            args.source_id or "operator",
            "conversation_routed",
            {"status": "human_required", "next_actor": next_actor_for_status("human_required", flag["reason"]), "reason": flag["reason"]},
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
        resolved_count = human_review_service.resolve_all_conversation_reviews(
            conn, args.conversation, action=args.action, sender=args.sender
        )
        if resolved_count == 0:
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
            human_review_service.update_conversation_status(
                conn,
                args.conversation,
                status=status,
                next_actor=next_actor,
                sender=args.sender,
                expected_status="human_required",
                reject_if_unresolved=(status == "closed"),
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
        rows = human_review_service.list_conversation_reviews(conn, args.conversation)
        reviews = [_review_summary(conn, row["id"]) for row in rows]
        conversation = conversation_summary(conn, args.conversation)
    if args.format == "text":
        print(f"Human review resolved: {conversation['id']}")
        print(f"Resolution: {args.action}")
        print(f"Resolved reviews: {resolved_count}")
        print(f"Status: {conversation['status']}")
        print(f"Next actor: {conversation['next_actor'] or '-'}")
        return
    emit({"ok": True, "reviews": reviews, "conversation": conversation}, args.format)


def cmd_human_review_queue(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        rows = human_review_service.list_unresolved_reviews(
            conn, args.merchant or "", limit=args.limit, offset=args.offset
        )
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
        try:
            row = human_review_service.human_review_row(conn, review_id, lambda value, _field: int(value))
        except ShoppingCliError as exc:
            raise SystemExit(str(exc)) from exc
        if row["resolved_at"]:
            raise SystemExit(f"Human review already resolved: {review_id}")
        conversation_id = row["conversation_id"]
        require_open_conversation(conn, conversation_id)
        resolved_count = human_review_service.resolve_review(
            conn, review_id, action=args.action, sender=args.sender
        )
        if resolved_count != 1:
            raise SystemExit(f"Human review already resolved: {review_id}")
        remaining_reasons = human_review_service.remaining_unresolved_reviews(conn, conversation_id)
        remaining = len(remaining_reasons)
        remaining_reason = remaining_reasons[0] if remaining_reasons else ""
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
            human_review_service.update_conversation_status(
                conn,
                conversation_id,
                status=status,
                next_actor=next_actor,
                sender=args.sender,
                expected_status="human_required",
                reject_if_unresolved=(status == "closed"),
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
        rows = human_review_service.list_conversation_reviews(conn, conversation_id)
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
            token_service.require_merchant_token(conn, args.merchant, args.merchant_token)
        events = audit_service.merchant_audit_events(
            conn,
            args.merchant,
            event=args.event,
            limit=args.limit,
            offset=args.offset,
        )
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
            rendered_methods = route["methods"] or ["-"]
            for method in rendered_methods:
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
        validate_production_config()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc
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

    policy = subparsers.add_parser("policy", help="Manage merchant policy reference clauses")
    policy_sub = policy.add_subparsers(dest="policy_command", required=True)
    policy_add = policy_sub.add_parser("add", help="Publish a policy clause")
    policy_add.add_argument("--merchant", required=True)
    policy_add.add_argument("--code", required=True)
    policy_add.add_argument("--body", required=True)
    policy_add.add_argument("--category", default="")
    policy_add.add_argument("--title", default="")
    policy_add.add_argument("--tags", default="")
    policy_add.add_argument("--high-risk", action="store_true")
    policy_add.add_argument("--format", choices=["text", "json"], default="text")
    policy_add.set_defaults(func=cmd_policy_add)
    policy_list = policy_sub.add_parser("list", help="List policy clauses")
    policy_list.add_argument("--merchant", default="")
    policy_list.add_argument("--category", default="")
    policy_list.add_argument("--limit", type=positive_int, default=50)
    policy_list.add_argument("--offset", type=non_negative_int, default=0)
    policy_list.add_argument("--format", choices=["text", "json"], default="text")
    policy_list.set_defaults(func=cmd_policy_list)
    policy_show = policy_sub.add_parser("show", help="Show one policy clause")
    policy_show.add_argument("--merchant", required=True)
    policy_show.add_argument("--code", required=True)
    policy_show.add_argument("--format", choices=["text", "json"], default="text")
    policy_show.set_defaults(func=cmd_policy_show)

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
    search_policies_parser = search_sub.add_parser("policies", help="Search merchant policies")
    search_policies_parser.add_argument("--query", default="")
    search_policies_parser.add_argument("--merchant", default="")
    search_policies_parser.add_argument("--category", default="")
    search_policies_parser.add_argument("--limit", type=positive_int, default=10)
    search_policies_parser.add_argument("--offset", type=non_negative_int, default=0)
    search_policies_parser.add_argument("--format", choices=["text", "json"], default="text")
    search_policies_parser.set_defaults(func=cmd_search_policies)

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
    conversation_list.add_argument("--details", action="store_true", help="Return full conversation details instead of lightweight summaries")
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

    api = subparsers.add_parser("api", help="Inspect or run the marketplace API")
    api_sub = api.add_subparsers(dest="api_command", required=True)
    api_routes = api_sub.add_parser("routes", help="List marketplace API routes")
    api_routes.add_argument("--format", choices=["text", "json"], default="text")
    api_routes.set_defaults(func=cmd_api_routes)
    api_serve = api_sub.add_parser("serve", help="Serve the FastAPI marketplace API")
    api_serve.add_argument("--host", default="127.0.0.1")
    api_serve.add_argument("--port", type=tcp_port, default=8765)
    api_serve.set_defaults(func=cmd_api_serve)

    # ── erp（shopping-cli v0.3 §3/#3：外部数据接入）──────────────────────────
    erp = subparsers.add_parser("erp", help="External ERP product data source")
    erp_sub = erp.add_subparsers(dest="erp_command", required=True)
    erp_sync = erp_sub.add_parser("sync", help="Sync ERP products into the local store (push-first, manual)")
    erp_sync.add_argument("--base-url", default="", dest="base_url", help="ERP base URL (or SHOPPING_ERP_BASE_URL)")
    erp_sync.add_argument("--auth-token", default="", dest="auth_token", help="ERP bearer token (or SHOPPING_ERP_AUTH_TOKEN)")
    erp_sync.add_argument("--default-merchant", default="", dest="default_merchant", help="Merchant for ERP products without merchant_id (or SHOPPING_ERP_DEFAULT_MERCHANT)")
    erp_sync.add_argument("--page-size", type=int, default=100, dest="page_size", help="ERP page size (1-500)")
    erp_sync.add_argument("--timeout", type=int, default=15, help="HTTP timeout seconds (1-60)")
    erp_sync.add_argument("--format", choices=["text", "json"], default="text")
    erp_sync.set_defaults(func=cmd_erp_sync)

    # ── listings（shopping-cli v0.3 §14：PublicListingProjection 只读预览）───
    listings = subparsers.add_parser("listings", help="Product-first listing projections (read-only preview)")
    listings_sub = listings.add_subparsers(dest="listings_command", required=True)
    listings_proj = listings_sub.add_parser("projections", help="Preview publishable projections (public-only)")
    listings_proj.add_argument("--merchant", default="", help="Filter by merchant id")
    listings_proj.add_argument("--format", choices=["text", "json"], default="text")
    listings_proj.set_defaults(func=cmd_listing_projections_list)
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
    try:
        args.func(args)
    except ShoppingCliError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()

"""LLM argparse command handlers."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from shopping_cli.cli_common import db_path_from_args, emit, yes_no
from shopping_cli.core.conversations import conversation_summary
from shopping_cli.db.session import db_session
from shopping_cli.llm.dispatcher import HTTPMarketplaceToolDispatcher, MarketplaceToolDispatcher
from shopping_cli.llm.prompts import buyer_system_prompt, merchant_system_prompt
from shopping_cli.llm.providers import provider_from_env
from shopping_cli.llm.runner import run_marketplace_tool_loop


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

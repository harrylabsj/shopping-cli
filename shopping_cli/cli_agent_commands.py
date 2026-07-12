"""Agent-related argparse command handlers."""

from __future__ import annotations

import argparse
import os
import re
from typing import Any

from shopping_cli.agents import merchant_agent, merchant_daemon
from shopping_cli.agents.tools import HTTPMerchantAgentTools
from shopping_cli.cli_common import db_path_from_args, emit, yes_no
from shopping_cli.core.catalog import require_merchant
from shopping_cli.core.errors import AuthError, ValidationError
from shopping_cli.db.session import db_session
from shopping_cli.services import agents as agent_service
from shopping_cli.services import tokens as token_service


def _safe_non_negative_int(value: Any) -> int:
    return agent_service.safe_non_negative_int(value)


def _positive_whole_seconds(value: Any, field_name: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        seconds = int(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a whole number") from exc
    if seconds <= 0:
        raise ValueError(f"{field_name} must be greater than 0")
    return seconds


def cmd_agent_run(args: argparse.Namespace) -> None:
    api_url = args.api_url or os.environ.get("SHOPPING_MARKETPLACE_API_URL") or os.environ.get("SHOPPING_API_URL") or ""
    if api_url:
        token = args.agent_token or os.environ.get("SHOPPING_AGENT_TOKEN") or args.merchant_token or os.environ.get("SHOPPING_MERCHANT_TOKEN")
        if not token:
            raise AuthError("--merchant-token or --agent-token is required with --api-url")
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


def cmd_agent_token(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        require_merchant(conn, args.merchant)
        if args.merchant_token:
            token_service.require_merchant_token(conn, args.merchant, args.merchant_token)
        try:
            result = agent_service.issue_agent_token_for_merchant(
                conn,
                args.merchant,
                ttl_seconds=args.ttl_seconds,
                positive_whole_seconds=_positive_whole_seconds,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    result["message"] = f"Agent token issued for {result['agent_id']}: {result['agent_token']}"
    emit(result, args.format)


def cmd_agent_tokens(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        require_merchant(conn, args.merchant)
        if args.merchant_token:
            token_service.require_merchant_token(conn, args.merchant, args.merchant_token)
        tokens = agent_service.list_agent_tokens(
            conn,
            args.merchant,
            limit=args.limit,
            offset=args.offset,
        )["tokens"]
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
            token_service.require_merchant_token(conn, args.merchant, args.merchant_token)
        try:
            result = agent_service.rotate_agent_token(
                conn,
                args.merchant,
                token=args.token,
                token_prefix=args.token_prefix,
                ttl_seconds=args.ttl_seconds,
                positive_whole_seconds=_positive_whole_seconds,
            )
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
    result["message"] = f"Agent token rotated for {result['agent_id']}: {result['agent_token']}"
    emit(result, args.format)


def cmd_agent_revoke_token(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        require_merchant(conn, args.merchant)
        if args.merchant_token:
            token_service.require_merchant_token(conn, args.merchant, args.merchant_token)
        result = agent_service.revoke_agent_token(
            conn,
            args.merchant,
            token=args.token,
            token_prefix=args.token_prefix,
        )
    result["message"] = f"Agent token revoked for {result['agent_id']}"
    emit(result, args.format)


def cmd_agent_list(args: argparse.Namespace) -> None:
    with db_session(db_path_from_args(args)) as conn:
        agents = agent_service.list_agent_summaries(
            conn,
            owner_id=args.merchant or "",
            limit=args.limit,
            offset=args.offset,
        )
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
        agent = agent_service.get_agent_summary(conn, args.agent)
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

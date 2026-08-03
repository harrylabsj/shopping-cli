"""Dispatch optional LLM tool calls into trusted marketplace operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable
import urllib.parse

from shopping_cli.agents import buyer_cli
from shopping_cli.core.catalog import search_products
from shopping_cli.core.conversations import add_flag, conversation_summary
from shopping_cli.core.harness import append_audit_event, next_actor_for_status
from shopping_cli.db.session import db_session
from shopping_cli.llm.contracts import (
    BUYER_SCOPES,
    MERCHANT_SCOPES,
    PRIVILEGED_CONVERSATION_SCOPES,
    SOURCE_OWNER_PREFIXES,
    ToolContractError,
    prepare_tool_call,
)
from shopping_cli.http_client import HTTPTransport, MarketplaceHTTPClient, MarketplaceHTTPError
from shopping_cli.services import conversations as conversation_service


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]
class ToolAccessDenied(Exception):
    """Raised when a scoped tool call targets a conversation owned by another actor."""


HTTPMarketplaceError = MarketplaceHTTPError


class MarketplaceToolDispatcher:
    def __init__(
        self,
        db_path: str | Path,
        source_id: str = "llm-tool",
        host: str = "local",
        session_id: str = "",
        actor: str = "",
        token_scope: str = "local_trusted",
    ):
        self.db_path = Path(db_path).expanduser()
        self.source_id = source_id
        self.host = host
        self.session_id = session_id
        self.actor = actor
        self.token_scope = token_scope

    def dispatch(self, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        raw_arguments = arguments if isinstance(arguments, dict) else {}
        try:
            prepared = prepare_tool_call(tool_name, self.token_scope, arguments)
            arguments = prepared.arguments
        except ToolContractError as exc:
            self._audit_tool_call(tool_name, raw_arguments, "denied", str(exc))
            raise
        handler = getattr(self, prepared.contract.handler_name)
        try:
            result = handler(arguments)
        except ToolAccessDenied as exc:
            error = str(exc)
            self._audit_tool_call(tool_name, arguments, "denied", error)
            raise
        except Exception as exc:
            self._audit_tool_call(tool_name, arguments, "error", str(exc))
            raise
        self._audit_tool_call(tool_name, arguments, "ok", "")
        return {"ok": True, "tool": tool_name, "result": result}

    def _audit_tool_call(self, tool_name: str, arguments: dict[str, Any], status: str, error: str = "") -> None:
        conversation_id = str(arguments.get("conversation_id") or "")
        with db_session(self.db_path) as conn:
            append_audit_event(
                conn,
                conversation_id,
                self.actor or self.source_id,
                "llm_tool_call",
                {
                    "tool": tool_name,
                    "status": status,
                    "host": self.host,
                    "session_id": self.session_id,
                    "actor": self.actor,
                    "source_id": self.source_id,
                    "token_scope": self.token_scope,
                    "error": error,
                },
            )

    def _conversation_for_tool(self, conn: Any, conversation_id: str, tool_name: str) -> dict[str, Any]:
        conversation = conversation_summary(conn, conversation_id)
        self._require_conversation_access(conversation, tool_name)
        return conversation

    def _identity_candidates(self) -> set[str]:
        candidates: set[str] = set()
        for value in (self.actor, self.source_id):
            identity = str(value or "").strip()
            if not identity:
                continue
            candidates.add(identity)
            for prefix in SOURCE_OWNER_PREFIXES:
                if identity.startswith(prefix):
                    owner_id = identity[len(prefix) :].strip()
                    if owner_id:
                        candidates.add(owner_id)
        return candidates

    def _require_conversation_access(self, conversation: dict[str, Any], tool_name: str) -> None:
        if self.token_scope in PRIVILEGED_CONVERSATION_SCOPES:
            return
        if self.token_scope in MERCHANT_SCOPES:
            owner_key = "merchant_id"
        elif self.token_scope in BUYER_SCOPES:
            owner_key = "buyer_id"
        else:
            raise ToolAccessDenied(f"tool {tool_name} is not allowed for token scope {self.token_scope}")

        owner_id = str(conversation.get(owner_key) or "")
        if owner_id and owner_id in self._identity_candidates():
            return
        actor = self.actor or self.source_id or "<missing>"
        raise ToolAccessDenied(
            f"tool {tool_name} is not allowed for token scope {self.token_scope} actor {actor} "
            f"on conversation {conversation.get('id')}"
        )

    def _dispatch_catalog_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        with db_session(self.db_path) as conn:
            results = search_products(
                conn,
                query=str(arguments["query"]),
                city=str(arguments.get("city") or ""),
                area=str(arguments.get("area") or ""),
                max_price=arguments.get("max_price"),
                include_out_of_stock=bool(arguments.get("include_out_of_stock") or False),
            )
        return {"ok": True, "query": str(arguments["query"]), "results": results}

    def _dispatch_conversation_send(self, arguments: dict[str, Any]) -> dict[str, Any]:
        sender = str(arguments["sender"])
        conversation_id = str(arguments["conversation_id"])
        with db_session(self.db_path) as conn:
            existing = self._conversation_for_tool(conn, conversation_id, "conversation_send")
            result = conversation_service.append_conversation_message(
                conn,
                existing,
                conversation_id,
                sender=sender,
                intent=str(arguments["intent"]),
                text=str(arguments["text"]),
                structured_payload={"source_id": self.source_id, "tool": "conversation_send"},
            )
            message = result["message"]
            conversation = result["conversation"]
        return {
            "ok": True,
            "message": message,
            "conversation": conversation,
            **buyer_cli.status_guidance(conversation),
        }

    def _dispatch_conversation_summarize(self, arguments: dict[str, Any]) -> dict[str, Any]:
        conversation_id = str(arguments["conversation_id"])
        with db_session(self.db_path) as conn:
            self._conversation_for_tool(conn, conversation_id, "conversation_summarize")
            summary = buyer_cli.summarize(conn, conversation_id)
        return {"ok": True, "summary": summary}

    def _dispatch_human_review_flag(self, arguments: dict[str, Any]) -> dict[str, Any]:
        conversation_id = str(arguments["conversation_id"])
        reason = str(arguments.get("reason") or "human_required")
        severity = str(arguments.get("severity") or "review")
        with db_session(self.db_path) as conn:
            conversation = self._conversation_for_tool(conn, conversation_id, "human_review_flag")
            flag = add_flag(conn, conversation_id, reason=reason, severity=severity, sku=conversation.get("sku") or "")
            # add_flag already transitions the conversation to human_required
            # with a rowcount-guarded UPDATE.
            append_audit_event(
                conn,
                conversation_id,
                self.source_id,
                "conversation_routed",
                {"status": "human_required", "next_actor": next_actor_for_status("human_required", flag["reason"]), "reason": flag["reason"], "tool": "human_review_flag"},
            )
            review = add_review_source(flag, self.source_id)
            conversation = conversation_summary(conn, conversation_id)
        return {"ok": True, "review": review, "conversation": conversation}

    def _dispatch_merchant_reply(self, arguments: dict[str, Any]) -> dict[str, Any]:
        conversation_id = str(arguments["conversation_id"])
        human_required = bool(arguments.get("human_required") or False)
        reason = str(arguments.get("reason") or "").strip()
        if human_required and not reason:
            reason = "human_required"
        status = "human_required" if human_required else "waiting_buyer"
        with db_session(self.db_path) as conn:
            existing = self._conversation_for_tool(conn, conversation_id, "merchant_reply")
            result = conversation_service.append_conversation_message(
                conn,
                existing,
                conversation_id,
                sender="merchant_agent",
                intent=str(arguments["intent"]),
                text=str(arguments["text"]),
                structured_payload={
                    "source_id": self.source_id,
                    "tool": "merchant_reply",
                    "human_required": human_required,
                    "reason": reason,
                },
                status=status,
            )
            message = result["message"]
            conversation = result["conversation"]
            flags = []
            if human_required:
                new_flag = result.get("new_flag")
                if new_flag is not None:
                    flags.append(add_review_source(new_flag, self.source_id))
        return {"ok": True, "message": message, "flags": flags, "conversation": conversation}


class HTTPMarketplaceToolDispatcher:
    def __init__(
        self,
        base_url: str,
        auth_token: str,
        source_id: str = "llm-tool",
        host: str = "local",
        session_id: str = "",
        actor: str = "",
        token_scope: str = "local_trusted",
        timeout: float = 10.0,
        transport: HTTPTransport | None = None,
    ):
        self.http = MarketplaceHTTPClient(base_url, auth_token, timeout=timeout, transport=transport)
        self.base_url = self.http.base_url
        self.auth_token = self.http.auth_token
        self.source_id = source_id
        self.host = host
        self.session_id = session_id
        self.actor = actor
        self.token_scope = token_scope
        self.timeout = self.http.timeout
        self.transport = transport

    def dispatch(self, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        raw_arguments = arguments if isinstance(arguments, dict) else {}
        try:
            prepared = prepare_tool_call(tool_name, self.token_scope, arguments)
            arguments = prepared.arguments
        except ToolContractError as exc:
            self._audit_tool_call(tool_name, raw_arguments, "denied", str(exc))
            raise
        handler = getattr(self, prepared.contract.handler_name)
        try:
            result = handler(arguments)
        except (ToolAccessDenied, ToolContractError, HTTPMarketplaceError) as exc:
            self._audit_tool_call(tool_name, arguments, "denied", str(exc))
            raise
        except Exception as exc:
            self._audit_tool_call(tool_name, arguments, "error", str(exc))
            raise
        self._audit_tool_call(tool_name, arguments, "ok", "")
        return {"ok": True, "tool": tool_name, "result": result}

    def _audit_tool_call(self, tool_name: str, arguments: dict[str, Any], status: str, error: str = "") -> None:
        try:
            self._request(
                "POST",
                "/audit/tool-calls",
                {
                    "conversation_id": str(arguments.get("conversation_id") or ""),
                    "tool": tool_name,
                    "status": status,
                    "host": self.host,
                    "session_id": self.session_id,
                    "actor": self.actor,
                    "source_id": self.source_id,
                    "token_scope": self.token_scope,
                    "error": error,
                },
            )
        except Exception:
            return

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.http.request(method, path, payload, query)

    @staticmethod
    def _response_object(result: dict[str, Any], key: str) -> dict[str, Any]:
        return MarketplaceHTTPClient.response_object(result, key)

    @staticmethod
    def _response_list(result: dict[str, Any], key: str) -> list[Any]:
        return MarketplaceHTTPClient.response_list(result, key)

    @staticmethod
    def _conversation_path(conversation_id: str) -> str:
        return f"/conversations/{urllib.parse.quote(str(conversation_id), safe='')}"

    def conversation_summary(self, conversation_id: str) -> dict[str, Any]:
        return self._response_object(self._request("GET", self._conversation_path(conversation_id)), "conversation")

    def merchant_private_config(self, merchant_id: str) -> dict[str, Any]:
        merchant_path = urllib.parse.quote(str(merchant_id), safe="")
        result = self._request("GET", f"/merchants/{merchant_path}/private-config")
        boundaries = result.get("automation_boundaries")
        version = result.get("version")
        if not isinstance(boundaries, str) or not isinstance(version, str):
            raise HTTPMarketplaceError("Marketplace API response missing merchant private configuration")
        return {
            "merchant_id": str(result.get("merchant_id") or merchant_id),
            "automation_boundaries": boundaries,
            "version": version,
        }

    def _dispatch_catalog_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._request(
            "GET",
            "/search/products",
            query={
                "query": str(arguments["query"]),
                "city": str(arguments.get("city") or ""),
                "area": str(arguments.get("area") or ""),
                "max_price": arguments.get("max_price"),
                "include_out_of_stock": bool(arguments.get("include_out_of_stock") or False),
            },
        )
        return {"ok": True, "query": str(arguments["query"]), "results": self._response_list(result, "results")}

    def _dispatch_conversation_send(self, arguments: dict[str, Any]) -> dict[str, Any]:
        sender = str(arguments["sender"])
        conversation_id = str(arguments["conversation_id"])
        result = self._request(
            "POST",
            f"{self._conversation_path(conversation_id)}/messages",
            {
                "sender": sender,
                "intent": str(arguments["intent"]),
                "text": str(arguments["text"]),
                "source_id": self.source_id,
            },
        )
        conversation = self._response_object(result, "conversation")
        return {
            "ok": True,
            "message": self._response_object(result, "message"),
            "conversation": conversation,
            **buyer_cli.status_guidance(conversation),
        }

    def _dispatch_conversation_summarize(self, arguments: dict[str, Any]) -> dict[str, Any]:
        conversation_id = str(arguments["conversation_id"])
        result = self._request("GET", self._conversation_path(conversation_id))
        conversation = self._response_object(result, "conversation")
        warnings = list(buyer_cli.MVP_WARNINGS)
        warnings.extend(buyer_cli.status_warnings(conversation))
        for flag in conversation.get("flags") or []:
            warnings.append(f"Human review flag: {flag['reason']}")
        guidance = buyer_cli.status_guidance(conversation)
        summary = {
            "ok": True,
            "conversation": conversation,
            "option": conversation.get("product"),
            "missing_facts": [],
            "warnings": warnings,
            **guidance,
            "no_order_created": True,
            "no_stock_reserved": True,
        }
        return {"ok": True, "summary": summary}

    def _dispatch_human_review_flag(self, arguments: dict[str, Any]) -> dict[str, Any]:
        conversation_id = str(arguments["conversation_id"])
        result = self._request(
            "POST",
            f"{self._conversation_path(conversation_id)}/human-review",
            {
                "reason": str(arguments.get("reason") or "human_required"),
                "severity": str(arguments.get("severity") or "review"),
                "source_id": self.source_id,
            },
        )
        return {
            "ok": True,
            "review": self._response_object(result, "review"),
            "conversation": self._response_object(result, "conversation"),
        }

    def _dispatch_merchant_reply(self, arguments: dict[str, Any]) -> dict[str, Any]:
        conversation_id = str(arguments["conversation_id"])
        human_required = bool(arguments.get("human_required") or False)
        reason = str(arguments.get("reason") or "").strip()
        if human_required and not reason:
            reason = "human_required"
        message_result = self._request(
            "POST",
            f"{self._conversation_path(conversation_id)}/messages",
            {
                "sender": "merchant_agent",
                "intent": str(arguments["intent"]),
                "text": str(arguments["text"]),
                "status": "human_required" if human_required else "waiting_buyer",
                "structured_payload": {
                    "source_id": self.source_id,
                    "tool": "merchant_reply",
                    "human_required": human_required,
                    "reason": reason,
                },
            },
        )
        flags = []
        message = self._response_object(message_result, "message")
        conversation = self._response_object(message_result, "conversation")
        if human_required:
            existing_flags = [
                flag
                for flag in conversation.get("flags") or []
                if isinstance(flag, dict) and flag.get("reason") == reason and not flag.get("resolved_at")
            ]
            if existing_flags:
                flags.append(existing_flags[-1])
            else:
                review_result = self._request(
                    "POST",
                    f"{self._conversation_path(conversation_id)}/human-review",
                    {
                        "reason": reason,
                        "source_id": self.source_id,
                    },
                )
                flags.append(self._response_object(review_result, "review"))
                conversation = self._response_object(review_result, "conversation")
        return {
            "ok": True,
            "message": message,
            "flags": flags,
            "conversation": conversation,
        }


def add_review_source(review: dict[str, Any], source_id: str) -> dict[str, Any]:
    sourced = dict(review)
    sourced["source_id"] = source_id
    return sourced


def dispatch_marketplace_tool(
    db_path: str | Path,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    token_scope: str,
    source_id: str = "llm-tool",
    actor: str = "",
) -> dict[str, Any]:
    return MarketplaceToolDispatcher(
        db_path,
        source_id=source_id,
        actor=actor,
        token_scope=token_scope,
    ).dispatch(tool_name, arguments)

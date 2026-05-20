"""Dispatch optional LLM tool calls into trusted marketplace operations."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

from shopping_cli.agents import buyer_cli
from shopping_cli.core.catalog import search_products
from shopping_cli.core.conversations import add_flag, append_message, conversation_summary
from shopping_cli.core.harness import append_audit_event, next_actor_for_status
from shopping_cli.db.session import db_session, now_iso
from shopping_cli.llm.tools import marketplace_tool_schema_objects


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]
HTTPTransport = Callable[
    [str, str, dict[str, Any] | None, dict[str, Any] | None, dict[str, str]],
    dict[str, Any],
]
MAX_HTTP_TOOL_TIMEOUT_SECONDS = 60.0


def _safe_positive_float(value: Any, default: float, maximum: float | None = None) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    if not math.isfinite(number) or number <= 0:
        return default
    if maximum is not None:
        return min(number, maximum)
    return number


BUYER_SCOPES = {"buyer", "buyer_cli"}
MERCHANT_SCOPES = {"merchant", "merchant_agent"}
PRIVILEGED_CONVERSATION_SCOPES = {"local_trusted", "operator"}
SOURCE_OWNER_PREFIXES = ("shopping-cli-merchant-agent:", "shopping-cli-buyer-agent:", "merchant:", "buyer:")

TOOL_SCOPE_ALLOWLIST = {
    "catalog_search": {"local_trusted", "buyer", "buyer_cli", "merchant", "merchant_agent", "operator"},
    "conversation_send": {"local_trusted", "buyer", "buyer_cli"},
    "conversation_summarize": {"local_trusted", "buyer", "buyer_cli", "merchant", "merchant_agent", "operator"},
    "human_review_flag": {"local_trusted", "merchant", "merchant_agent", "operator"},
    "merchant_reply": {"local_trusted", "merchant", "merchant_agent"},
}


class ToolAccessDenied(Exception):
    """Raised when a scoped tool call targets a conversation owned by another actor."""


class HTTPMarketplaceError(RuntimeError):
    """Raised when the Marketplace API returns an invalid or failed response."""


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
        self.allowed_tools = {tool.name for tool in marketplace_tool_schema_objects()}

    def dispatch(self, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        if tool_name not in self.allowed_tools:
            self._audit_tool_call(tool_name, arguments, "denied", f"Unknown or disallowed marketplace tool: {tool_name}")
            raise SystemExit(f"Unknown or disallowed marketplace tool: {tool_name}")
        allowed_scopes = TOOL_SCOPE_ALLOWLIST.get(tool_name, set())
        if self.token_scope not in allowed_scopes:
            error = f"tool {tool_name} is not allowed for token scope {self.token_scope}"
            self._audit_tool_call(tool_name, arguments, "denied", error)
            raise SystemExit(error)
        handler = getattr(self, f"_dispatch_{tool_name}")
        try:
            result = handler(arguments)
        except ToolAccessDenied as exc:
            error = str(exc)
            self._audit_tool_call(tool_name, arguments, "denied", error)
            raise SystemExit(error) from exc
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
        if sender not in {"buyer", "buyer_cli"}:
            raise SystemExit("conversation_send only supports buyer or buyer_cli senders")
        conversation_id = str(arguments["conversation_id"])
        with db_session(self.db_path) as conn:
            self._conversation_for_tool(conn, conversation_id, "conversation_send")
            message = append_message(
                conn,
                conversation_id,
                sender,
                str(arguments["intent"]),
                str(arguments["text"]),
                structured_payload={"source_id": self.source_id, "tool": "conversation_send"},
            )
            conversation = conversation_summary(conn, conversation_id)
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
            next_actor = next_actor_for_status("human_required", flag["reason"])
            conn.execute(
                "update conversations set status = 'human_required', next_actor = ?, updated_at = ?, last_sender = ? where id = ?",
                (next_actor, now_iso(), self.source_id, conversation_id),
            )
            append_audit_event(
                conn,
                conversation_id,
                self.source_id,
                "conversation_routed",
                {"status": "human_required", "next_actor": next_actor, "reason": flag["reason"], "tool": "human_review_flag"},
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
            conversation = self._conversation_for_tool(conn, conversation_id, "merchant_reply")
            message = append_message(
                conn,
                conversation_id,
                "merchant_agent",
                str(arguments["intent"]),
                str(arguments["text"]),
                structured_payload={
                    "source_id": self.source_id,
                    "tool": "merchant_reply",
                    "human_required": human_required,
                    "reason": reason,
                },
                status=status,
            )
            flags = []
            if human_required:
                flag = add_flag(conn, conversation_id, reason, sku=conversation.get("sku") or "")
                flags.append(add_review_source(flag, self.source_id))
            conversation = conversation_summary(conn, conversation_id)
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
        self.base_url = str(base_url or "").rstrip("/")
        if not self.base_url:
            raise ValueError("base_url is required")
        self.auth_token = str(auth_token or "").strip()
        if not self.auth_token:
            raise ValueError("auth_token is required")
        self.source_id = source_id
        self.host = host
        self.session_id = session_id
        self.actor = actor
        self.token_scope = token_scope
        self.timeout = _safe_positive_float(timeout, 10.0, maximum=MAX_HTTP_TOOL_TIMEOUT_SECONDS)
        self.transport = transport
        self.allowed_tools = {tool.name for tool in marketplace_tool_schema_objects()}

    def dispatch(self, tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = arguments or {}
        if tool_name not in self.allowed_tools:
            error = f"Unknown or disallowed marketplace tool: {tool_name}"
            self._audit_tool_call(tool_name, arguments, "denied", error)
            raise SystemExit(error)
        allowed_scopes = TOOL_SCOPE_ALLOWLIST.get(tool_name, set())
        if self.token_scope not in allowed_scopes:
            error = f"tool {tool_name} is not allowed for token scope {self.token_scope}"
            self._audit_tool_call(tool_name, arguments, "denied", error)
            raise SystemExit(error)
        handler = getattr(self, f"_dispatch_{tool_name}")
        try:
            result = handler(arguments)
        except SystemExit as exc:
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
        except (Exception, SystemExit):
            return

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "Authorization": f"Bearer {self.auth_token}"}

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = self._headers()
        if self.transport is not None:
            result = self.transport(method.upper(), path, payload, query, headers)
            return self._validate_response(result)

        url = f"{self.base_url}/{path.lstrip('/')}"
        clean_query = {key: value for key, value in (query or {}).items() if value not in (None, "")}
        if clean_query:
            url = f"{url}?{urllib.parse.urlencode(clean_query)}"
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw_body = response.read()
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            raw_body = exc.read()
            raise SystemExit(self._error_message(raw_body, f"Marketplace API returned HTTP {exc.code}")) from exc
        except TimeoutError as exc:
            raise SystemExit(f"Marketplace API request timed out: {exc}") from exc
        except urllib.error.URLError as exc:
            raise SystemExit(f"Marketplace API request failed: {exc.reason}") from exc
        return self._validate_response(self._decode_body(raw_body))

    @staticmethod
    def _decode_body(raw_body: bytes) -> dict[str, Any]:
        if not raw_body:
            return {}
        try:
            decoded = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPMarketplaceError("Marketplace API returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise HTTPMarketplaceError("Marketplace API returned a non-object response")
        return decoded

    @classmethod
    def _error_message(cls, raw_body: bytes, fallback: str) -> str:
        try:
            decoded = cls._decode_body(raw_body)
        except HTTPMarketplaceError:
            return fallback
        return str(decoded.get("error") or fallback)

    @staticmethod
    def _validate_response(result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise HTTPMarketplaceError("Marketplace API returned a non-object response")
        if result.get("ok") is False:
            raise SystemExit(str(result.get("error") or "Marketplace API request failed"))
        return result

    @staticmethod
    def _response_object(result: dict[str, Any], key: str) -> dict[str, Any]:
        value = result.get(key)
        if not isinstance(value, dict):
            raise HTTPMarketplaceError(f"Marketplace API response missing object: {key}")
        return dict(value)

    @staticmethod
    def _response_list(result: dict[str, Any], key: str) -> list[Any]:
        value = result.get(key)
        if not isinstance(value, list):
            raise HTTPMarketplaceError(f"Marketplace API response missing list: {key}")
        return list(value)

    @staticmethod
    def _conversation_path(conversation_id: str) -> str:
        return f"/conversations/{urllib.parse.quote(str(conversation_id), safe='')}"

    def conversation_summary(self, conversation_id: str) -> dict[str, Any]:
        return self._response_object(self._request("GET", self._conversation_path(conversation_id)), "conversation")

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
        if sender not in {"buyer", "buyer_cli"}:
            raise SystemExit("conversation_send only supports buyer or buyer_cli senders")
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
    source_id: str = "llm-tool",
) -> dict[str, Any]:
    return MarketplaceToolDispatcher(db_path, source_id=source_id).dispatch(tool_name, arguments)

"""Typed marketplace tools used by resident merchant agents."""

from __future__ import annotations

import math
import sqlite3
import urllib.parse
from typing import Any, Protocol

from shopping_cli import VERSION
from shopping_cli.core.catalog import product_summary, require_merchant
from shopping_cli.core.conversations import add_flag, append_message, waiting_merchant_conversations
from shopping_cli.core.errors import ValidationError
from shopping_cli.core.harness import abandon_agent_message, abandon_stale_agent_messages, claim_agent_message, complete_agent_message, fail_agent_message
from shopping_cli.db.session import encode_json, now_iso
from shopping_cli.http_client import MarketplaceHTTPClient, MarketplaceHTTPError

DEFAULT_CAPABILITIES = ["catalog", "inventory", "delivery", "consultation"]
AGENT_STATUSES = {"online", "away", "human_required"}
MAX_SQLITE_INTEGER = 2**63 - 1


def _non_negative_whole_int(value: Any, field_name: str, default: int = 0) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a whole number")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{field_name} must be a whole number")
        number = int(value)
    else:
        try:
            number = int(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a whole number") from exc
    if number < 0:
        raise ValueError(f"{field_name} must be non-negative")
    if number > MAX_SQLITE_INTEGER:
        raise ValueError(f"{field_name} must be <= {MAX_SQLITE_INTEGER}")
    return number


def _positive_whole_int(value: Any, field_name: str) -> int:
    number = _non_negative_whole_int(value, field_name)
    if number <= 0:
        raise ValueError(f"{field_name} must be greater than 0")
    return number


def _normalize_agent_status(value: Any) -> str:
    status = str(value or "").strip() or "online"
    if status not in AGENT_STATUSES:
        raise ValidationError(f"Unknown agent status: {status}")
    return status


def _normalize_capabilities(value: Any) -> list[str]:
    if value is None:
        return list(DEFAULT_CAPABILITIES)
    if not isinstance(value, list):
        raise ValidationError("agent capabilities must be a list of strings")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValidationError("agent capabilities must be a list of strings")
        text = item.strip()
        if text:
            normalized.append(text)
    return normalized or list(DEFAULT_CAPABILITIES)


class MerchantAgentTools(Protocol):
    def heartbeat(
        self,
        merchant_id: str,
        status: str = "online",
        last_error: str = "",
        checked_count: int = 0,
        replied_count: int = 0,
    ) -> dict[str, Any]:
        ...

    def waiting_merchant_conversations(self, merchant_id: str) -> list[dict[str, Any]]:
        ...

    def product_summary(self, sku: str) -> dict[str, Any]:
        ...

    def append_message(
        self,
        conversation_id: str,
        sender: str,
        intent: str,
        text: str,
        structured_payload: dict[str, Any],
        status: str,
    ) -> dict[str, Any]:
        ...

    def add_flag(self, conversation_id: str, reason: str, sku: str = "") -> dict[str, Any]:
        ...

    def claim_message(self, agent_id: str, conversation_id: str, message_id: int, idempotency_key: str) -> dict[str, Any]:
        ...

    def complete_message(self, agent_id: str, message_id: int) -> dict[str, Any]:
        ...

    def fail_message(self, agent_id: str, message_id: int, error: str) -> dict[str, Any]:
        ...

    def abandon_message(self, agent_id: str, message_id: int, error: str) -> dict[str, Any]:
        ...

    def abandon_stale_messages(self, agent_id: str, stale_after_seconds: int = 300) -> list[dict[str, Any]]:
        ...


def record_heartbeat(
    conn: sqlite3.Connection,
    merchant_id: str,
    status: str = "online",
    capabilities: list[str] | None = None,
    pid: int = 0,
    version: str = VERSION,
    last_error: str = "",
    checked_count: int = 0,
    replied_count: int = 0,
) -> dict[str, Any]:
    merchant_id = str(merchant_id or "").strip()
    status = _normalize_agent_status(status)
    pid = _non_negative_whole_int(pid, "pid")
    checked_count = _non_negative_whole_int(checked_count, "checked_count")
    replied_count = _non_negative_whole_int(replied_count, "replied_count")
    capabilities = _normalize_capabilities(capabilities)
    require_merchant(conn, merchant_id)
    agent_id = f"shopping-cli-merchant-agent:{merchant_id}"
    now = now_iso()
    conn.execute(
        """
        insert into agents(
            id, type, owner_id, status, capabilities_json, last_seen_at,
            pid, version, last_error, checked_count, replied_count
        )
        values (?, 'merchant', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(id) do update set
            status = excluded.status,
            capabilities_json = excluded.capabilities_json,
            last_seen_at = excluded.last_seen_at,
            pid = excluded.pid,
            version = excluded.version,
            last_error = excluded.last_error,
            checked_count = excluded.checked_count,
            replied_count = excluded.replied_count
        """,
        (
            agent_id,
            merchant_id,
            status,
            encode_json(capabilities),
            now,
            pid,
            version or VERSION,
            last_error or "",
            checked_count,
            replied_count,
        ),
    )
    return {
        "id": agent_id,
        "type": "merchant",
        "owner_id": merchant_id,
        "status": status,
        "capabilities": capabilities,
        "last_seen_at": now,
        "pid": pid,
        "version": version or VERSION,
        "last_error": last_error or "",
        "checked_count": checked_count,
        "replied_count": replied_count,
    }


class SQLiteMerchantAgentTools:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def heartbeat(
        self,
        merchant_id: str,
        status: str = "online",
        last_error: str = "",
        checked_count: int = 0,
        replied_count: int = 0,
    ) -> dict[str, Any]:
        return record_heartbeat(
            self.conn,
            merchant_id,
            status=status,
            last_error=last_error,
            checked_count=checked_count,
            replied_count=replied_count,
        )

    def waiting_merchant_conversations(self, merchant_id: str) -> list[dict[str, Any]]:
        return waiting_merchant_conversations(self.conn, merchant_id)

    def product_summary(self, sku: str) -> dict[str, Any]:
        return product_summary(self.conn, sku)

    def append_message(
        self,
        conversation_id: str,
        sender: str,
        intent: str,
        text: str,
        structured_payload: dict[str, Any],
        status: str,
    ) -> dict[str, Any]:
        return append_message(
            self.conn,
            conversation_id,
            sender,
            intent,
            text,
            structured_payload=structured_payload,
            status=status,
        )

    def add_flag(self, conversation_id: str, reason: str, sku: str = "") -> dict[str, Any]:
        return add_flag(self.conn, conversation_id, reason, sku=sku)

    def claim_message(self, agent_id: str, conversation_id: str, message_id: int, idempotency_key: str) -> dict[str, Any]:
        return claim_agent_message(self.conn, agent_id, conversation_id, message_id, idempotency_key)

    def complete_message(self, agent_id: str, message_id: int) -> dict[str, Any]:
        return complete_agent_message(self.conn, agent_id, message_id)

    def fail_message(self, agent_id: str, message_id: int, error: str) -> dict[str, Any]:
        return fail_agent_message(self.conn, agent_id, message_id, error)

    def abandon_message(self, agent_id: str, message_id: int, error: str) -> dict[str, Any]:
        return abandon_agent_message(self.conn, agent_id, message_id, error)

    def abandon_stale_messages(self, agent_id: str, stale_after_seconds: int = 300) -> list[dict[str, Any]]:
        return abandon_stale_agent_messages(self.conn, agent_id, stale_after_seconds=stale_after_seconds)


HTTPMarketplaceError = MarketplaceHTTPError


class HTTPMerchantAgentTools:
    def __init__(
        self,
        base_url: str,
        merchant_id: str,
        merchant_token: str,
        timeout: float = 10.0,
        opener: Any | None = None,
        host: str = "",
        session_id: str = "",
        allow_insecure_http: bool = False,
    ):
        self.merchant_id = str(merchant_id or "").strip()
        if not self.merchant_id:
            raise ValueError("merchant_id is required")
        self.merchant_token = str(merchant_token or "").strip()
        if not self.merchant_token:
            raise ValueError("merchant_token is required")
        self.http = MarketplaceHTTPClient(
            base_url,
            self.merchant_token,
            timeout=timeout,
            opener=opener,
            allow_insecure_http=allow_insecure_http,
        )
        self.base_url = self.http.base_url
        self.timeout = self.http.timeout
        self.host = str(host or "")
        self.session_id = str(session_id or "")
        self._message_created_review_flags: dict[tuple[str, str], dict[str, Any]] = {}

    def _merchant_payload(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = dict(payload or {})
        merged["merchant_id"] = self.merchant_id
        merged["merchant_token"] = self.merchant_token
        return merged

    def _token_payload(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = dict(payload or {})
        merged["merchant_token"] = self.merchant_token
        return merged

    def _check_merchant(self, merchant_id: str) -> None:
        if merchant_id != self.merchant_id:
            raise ValueError(f"HTTP tools are scoped to merchant {self.merchant_id}, not {merchant_id}")

    def _should_record_tool_calls(self) -> bool:
        return bool(self.host or self.session_id)

    def _record_tool_call(
        self,
        tool: str,
        conversation_id: str = "",
        status: str = "ok",
        error: str = "",
    ) -> None:
        if not self._should_record_tool_calls():
            return
        payload = self._token_payload(
            {
                "merchant_id": self.merchant_id,
                "tool": tool,
                "status": status,
                "host": self.host,
                "session_id": self.session_id,
                "actor": f"shopping-cli-merchant-agent:{self.merchant_id}",
                "source_id": f"shopping-cli-merchant-agent:{self.merchant_id}",
                "token_scope": "merchant_agent",
                "error": error,
            }
        )
        if conversation_id:
            payload["conversation_id"] = conversation_id
        try:
            self._request("POST", "/audit/tool-calls", payload)
        except HTTPMarketplaceError:
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

    def heartbeat(
        self,
        merchant_id: str,
        status: str = "online",
        last_error: str = "",
        checked_count: int = 0,
        replied_count: int = 0,
    ) -> dict[str, Any]:
        self._check_merchant(merchant_id)
        result = self._request(
            "POST",
            "/agents/heartbeat",
            self._merchant_payload(
                {
                    "status": status,
                    "last_error": last_error,
                    "checked_count": _non_negative_whole_int(checked_count, "checked_count"),
                    "replied_count": _non_negative_whole_int(replied_count, "replied_count"),
                }
            ),
        )
        self._record_tool_call("agent_heartbeat")
        return self._response_object(result, "agent")

    def waiting_merchant_conversations(self, merchant_id: str) -> list[dict[str, Any]]:
        self._check_merchant(merchant_id)
        path = f"/merchants/{urllib.parse.quote(merchant_id, safe='')}/conversations"
        result = self._request("GET", path, query={"status": "waiting_merchant", "include": "details", "limit": 100})
        return self._response_list(result, "conversations")

    def product_summary(self, sku: str) -> dict[str, Any]:
        result = self._request("GET", f"/products/{urllib.parse.quote(str(sku), safe='')}")
        return self._response_object(result, "product")

    def append_message(
        self,
        conversation_id: str,
        sender: str,
        intent: str,
        text: str,
        structured_payload: dict[str, Any],
        status: str,
    ) -> dict[str, Any]:
        result = self._request(
            "POST",
            f"/conversations/{urllib.parse.quote(conversation_id, safe='')}/messages",
            self._token_payload(
                {
                    "sender": sender,
                    "intent": intent,
                    "text": text,
                    "structured_payload": structured_payload,
                    "status": status,
                }
            ),
        )
        self._record_tool_call("conversation_message", conversation_id=conversation_id)
        conversation = result.get("conversation")
        if isinstance(conversation, dict):
            for flag in conversation.get("flags") or []:
                if isinstance(flag, dict) and flag.get("reason") and not flag.get("resolved_at"):
                    self._message_created_review_flags[(conversation_id, str(flag["reason"]))] = dict(flag)
        return self._response_object(result, "message")

    def add_flag(self, conversation_id: str, reason: str, sku: str = "") -> dict[str, Any]:
        cached = self._message_created_review_flags.pop((conversation_id, reason), None)
        if cached is not None:
            self._record_tool_call("human_review_flag", conversation_id=conversation_id)
            return cached
        result = self._request(
            "POST",
            f"/conversations/{urllib.parse.quote(conversation_id, safe='')}/human-review",
            self._merchant_payload({"reason": reason, "sku": sku, "source_id": f"shopping-cli-merchant-agent:{self.merchant_id}"}),
        )
        self._record_tool_call("human_review_flag", conversation_id=conversation_id)
        return self._response_object(result, "review")

    def claim_message(self, agent_id: str, conversation_id: str, message_id: int, idempotency_key: str) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/agents/messages/claim",
            self._merchant_payload(
                {
                    "agent_id": agent_id,
                    "conversation_id": conversation_id,
                    "message_id": _positive_whole_int(message_id, "message_id"),
                    "idempotency_key": idempotency_key,
                }
            ),
        )
        self._record_tool_call("agent_message_claim", conversation_id=conversation_id)
        return self._response_object(result, "claim")

    def complete_message(self, agent_id: str, message_id: int) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/agents/messages/complete",
            self._merchant_payload(
                {"agent_id": agent_id, "message_id": _positive_whole_int(message_id, "message_id")}
            ),
        )
        self._record_tool_call("agent_message_complete")
        return self._response_object(result, "process")

    def fail_message(self, agent_id: str, message_id: int, error: str) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/agents/messages/fail",
            self._merchant_payload(
                {"agent_id": agent_id, "message_id": _positive_whole_int(message_id, "message_id"), "error": error}
            ),
        )
        self._record_tool_call("agent_message_fail", status="failed", error=error)
        return self._response_object(result, "process")

    def abandon_message(self, agent_id: str, message_id: int, error: str) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/agents/messages/abandon",
            self._merchant_payload(
                {"agent_id": agent_id, "message_id": _positive_whole_int(message_id, "message_id"), "error": error}
            ),
        )
        self._record_tool_call("agent_message_abandon", error=error)
        return self._response_object(result, "process")

    def abandon_stale_messages(self, agent_id: str, stale_after_seconds: int = 300) -> list[dict[str, Any]]:
        result = self._request(
            "POST",
            "/agents/messages/abandon-stale",
            self._merchant_payload(
                {
                    "agent_id": agent_id,
                    "stale_after_seconds": _positive_whole_int(stale_after_seconds, "stale_after_seconds"),
                }
            ),
        )
        self._record_tool_call("agent_message_abandon_stale")
        return self._response_list(result, "abandoned")

"""Lightweight ASGI fallback used when FastAPI is unavailable."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import parse_qs

from shopping_cli.api import auth as api_auth


HandleRequest = Callable[[str | Path, str, str, dict[str, Any] | None, dict[str, Any] | None], tuple[int, dict[str, Any]]]
RouteProvider = Callable[[], list[Any]]


class MarketplaceASGIApp:
    title = "shopping-cli Marketplace API"

    def __init__(
        self,
        db_path: str | Path,
        handle_request_fn: HandleRequest | None = None,
        route_provider: RouteProvider | None = None,
    ):
        self.state = SimpleNamespace(db_path=str(db_path), fastapi_available=False)
        self._handle_request = handle_request_fn
        self._route_provider = route_provider
        self.routes = self._routes()

    def _routes(self) -> list[Any]:
        provider = self._route_provider
        if provider is None:
            from shopping_cli.api.route_registry import route_info

            provider = route_info
        return provider()

    def _handler(self) -> HandleRequest:
        if self._handle_request is None:
            from shopping_cli.api.app import handle_request

            self._handle_request = handle_request
        return self._handle_request

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"ok":false,"error":"unsupported scope"}'})
            return
        chunks: list[bytes] = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        try:
            decoded_payload = json.loads(b"".join(chunks).decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = json.dumps(
                {"ok": False, "error": "invalid JSON request body"},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        if not isinstance(decoded_payload, dict):
            body = json.dumps(
                {"ok": False, "error": "JSON request body must be an object"},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
            await send({"type": "http.response.start", "status": 400, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        payload = decoded_payload
        headers = {
            key.decode("latin1").lower(): value.decode("latin1")
            for key, value in scope.get("headers", [])
        }
        payload = api_auth.payload_with_auth(
            payload,
            authorization=headers.get("authorization", ""),
            idempotency_key=headers.get("idempotency-key", ""),
        )
        try:
            raw_query = scope.get("query_string", b"").decode("utf-8")
        except UnicodeDecodeError:
            raw_query = ""
        query = parse_qs(raw_query, keep_blank_values=True)
        status, response = self._handler()(
            self.state.db_path,
            method=str(scope.get("method") or "GET").upper(),
            path=str(scope.get("path") or "/"),
            payload=payload,
            query={key: values[-1] if values else "" for key, values in query.items()},
        )
        body = json.dumps(response, ensure_ascii=False, sort_keys=True).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

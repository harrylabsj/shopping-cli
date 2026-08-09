"""ASGI request-body size-limit middleware.

Rejects oversized HTTP bodies before the app (FastAPI/Starlette) attempts to
parse them, enforcing :func:`shopping_cli.api.limits.max_request_body_bytes`.
The limit is applied to both the declared ``Content-Length`` header and the
accumulated streamed body, so a client cannot bypass it by chunking.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from shopping_cli.api.limits import max_request_body_bytes

# ASGI 类型别名（镜像 starlette.types 的结构，避免在本模块需要无 fastapi/starlette
# 环境导入第三方包）：让 RequestBodyLimitMiddleware 与 Starlette/FastAPI 的
# add_middleware（_MiddlewareFactory 协议）类型兼容。
ASGIReceive = Callable[[], Awaitable[MutableMapping[str, Any]]]
ASGISend = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[MutableMapping[str, Any], ASGIReceive, ASGISend], Awaitable[None]]


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP bodies before FastAPI attempts to parse them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app: ASGIApp = app

    async def __call__(
        self, scope: MutableMapping[str, Any], receive: ASGIReceive, send: ASGISend
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        maximum = max_request_body_bytes()
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        try:
            content_length = int(headers.get(b"content-length", b"0") or b"0")
        except ValueError:
            content_length = 0
        if content_length > maximum:
            await self._send_too_large(send)
            return

        messages: list[MutableMapping[str, Any]] = []
        received = 0
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") != "http.request":
                break
            received += len(message.get("body", b""))
            if received > maximum:
                await self._send_too_large(send)
                return
            if not message.get("more_body", False):
                break

        message_index = 0

        async def replay_receive() -> MutableMapping[str, Any]:
            nonlocal message_index
            if message_index < len(messages):
                message = messages[message_index]
                message_index += 1
                return message
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _send_too_large(send: ASGISend) -> None:
        body = json.dumps({"ok": False, "error": "request body is too large"}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

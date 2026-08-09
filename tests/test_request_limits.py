"""Characterization/regression tests for the ASGI request-body size middleware."""

import asyncio
import json
import os
import unittest
from unittest.mock import patch

from shopping_cli.api.request_limits import RequestBodyLimitMiddleware


class _InnerApp:
    """Record whether it was called and what body messages it replayed."""

    def __init__(self):
        self.called = False
        self.scope = None
        self.messages = []
        self.sent = []

    async def __call__(self, scope, receive, send):
        self.called = True
        self.scope = scope
        while True:
            message = await receive()
            self.messages.append(message)
            if message.get("type") != "http.request" or not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"ok": true}'})


class _Driver:
    """Feed ASGI messages to the middleware and record send() output."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []
        self.receive_calls = 0

    async def receive(self):
        self.receive_calls += 1
        if self._messages:
            return self._messages.pop(0)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(self, message):
        self.sent.append(message)


def _run(middleware, scope, messages):
    driver = _Driver(messages)
    asyncio.run(middleware(scope, driver.receive, driver.send))
    return driver


def _http_scope(headers=None):
    request_headers = [(b"content-type", b"application/json")]
    request_headers.extend((key, value) for key, value in (headers or {}).items())
    return {"type": "http", "method": "POST", "path": "/products", "headers": request_headers}


def _too_large_envelope(sent):
    """Extract status + parsed body from a 413 response in *sent*."""
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    raw = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return status, json.loads(raw.decode("utf-8") or "{}"), raw


class RequestBodyLimitMiddlewareTest(unittest.TestCase):
    def test_non_http_scope_is_passed_through(self):
        received = []

        async def inner_app(scope, receive, send):
            received.append(await receive())
            await send({"type": "lifespan.startup.complete"})

        middleware = RequestBodyLimitMiddleware(inner_app)
        scope = {"type": "lifespan"}
        driver = _Driver([{"type": "lifespan.startup"}])
        asyncio.run(middleware(scope, driver.receive, driver.send))

        self.assertEqual(received, [{"type": "lifespan.startup"}])
        self.assertEqual(driver.sent, [{"type": "lifespan.startup.complete"}])

    def test_content_length_over_limit_returns_413(self):
        with patch.dict(os.environ, {"SHOPPING_MAX_REQUEST_BODY_BYTES": "1024"}, clear=False):
            inner = _InnerApp()
            middleware = RequestBodyLimitMiddleware(inner)
            scope = _http_scope(headers={b"content-length": b"2048"})
            driver = _run(middleware, scope, [{"type": "http.request", "body": b"{}", "more_body": False}])

        self.assertFalse(inner.called)
        status, body, raw = _too_large_envelope(driver.sent)
        self.assertEqual(status, 413)
        self.assertEqual(body, {"ok": False, "error": "request body is too large"})
        self.assertEqual(raw, b'{"ok": false, "error": "request body is too large"}')
        start = driver.sent[0]
        headers = dict(start["headers"])
        self.assertEqual(headers[b"content-type"], b"application/json")
        self.assertEqual(headers[b"content-length"], str(len(raw)).encode("ascii"))

    def test_chunked_body_over_limit_returns_same_413_envelope(self):
        with patch.dict(os.environ, {"SHOPPING_MAX_REQUEST_BODY_BYTES": "1024"}, clear=False):
            inner = _InnerApp()
            middleware = RequestBodyLimitMiddleware(inner)
            messages = [
                {"type": "http.request", "body": b'{"pad":"' + b"x" * 600, "more_body": True},
                {"type": "http.request", "body": b"y" * 600, "more_body": False},
            ]
            scope = _http_scope()  # no content-length header
            chunked = _run(middleware, scope, messages)

            inner2 = _InnerApp()
            middleware2 = RequestBodyLimitMiddleware(inner2)
            scope2 = _http_scope(headers={b"content-length": b"2048"})
            declared = _run(middleware2, scope2, [{"type": "http.request", "body": b"{}", "more_body": False}])

        self.assertFalse(inner.called)
        self.assertFalse(inner2.called)
        chunked_status, chunked_body, chunked_raw = _too_large_envelope(chunked.sent)
        declared_status, declared_body, declared_raw = _too_large_envelope(declared.sent)
        self.assertEqual(chunked_status, 413)
        self.assertEqual(chunked_body, {"ok": False, "error": "request body is too large"})
        # Both over-limit paths emit byte-identical 413 envelopes.
        self.assertEqual((chunked_status, chunked_raw), (declared_status, declared_raw))
        self.assertEqual(chunked.sent[0]["headers"], declared.sent[0]["headers"])

    def test_exact_body_at_limit_is_allowed_and_replayed(self):
        with patch.dict(os.environ, {"SHOPPING_MAX_REQUEST_BODY_BYTES": "1024"}, clear=False):
            inner = _InnerApp()
            middleware = RequestBodyLimitMiddleware(inner)
            prefix = b'{"pad":"'
            payload = prefix + b"x" * (1024 - len(prefix) - 2) + b'"}'
            self.assertEqual(len(payload), 1024)
            messages = [
                {"type": "http.request", "body": payload[:512], "more_body": True},
                {"type": "http.request", "body": payload[512:], "more_body": False},
            ]
            driver = _run(middleware, _http_scope(), messages)

        self.assertTrue(inner.called)
        self.assertEqual([m["body"] for m in inner.messages], [payload[:512], payload[512:]])
        self.assertEqual([m["more_body"] for m in inner.messages], [True, False])
        self.assertEqual(inner.messages[-1], messages[-1])
        status = next(m["status"] for m in driver.sent if m["type"] == "http.response.start")
        self.assertEqual(status, 200)
        # The middleware never consumes the underlying receive() after buffering.
        self.assertEqual(driver.receive_calls, 2)

    def test_replay_returns_terminal_empty_message_after_buffer(self):
        received = []

        async def inner_app(scope, receive, send):
            # Read one message past the buffered body to exercise the terminal fallback.
            received.append(await receive())
            received.append(await receive())

        middleware = RequestBodyLimitMiddleware(inner_app)
        scope = _http_scope()
        messages = [{"type": "http.request", "body": b'{"a": 1}', "more_body": False}]
        _run(middleware, scope, messages)
        self.assertEqual(
            received,
            [
                {"type": "http.request", "body": b'{"a": 1}', "more_body": False},
                {"type": "http.request", "body": b"", "more_body": False},
            ],
        )

    def test_inner_app_exception_propagates_without_extra_cleanup(self):
        async def inner_app(scope, receive, send):
            await receive()
            raise RuntimeError("boom")

        with patch.dict(os.environ, {"SHOPPING_MAX_REQUEST_BODY_BYTES": "1024"}, clear=False):
            middleware = RequestBodyLimitMiddleware(inner_app)
            scope = _http_scope()
            driver = _Driver([{"type": "http.request", "body": b"{}", "more_body": False}])
            with self.assertRaises(RuntimeError) as raised:
                asyncio.run(middleware(scope, driver.receive, driver.send))

        self.assertEqual(str(raised.exception), "boom")
        # No ASGI response messages were emitted by the middleware, and it did
        # not touch the underlying receive() after the buffering phase.
        self.assertEqual(driver.sent, [])
        self.assertEqual(driver.receive_calls, 1)

    def test_cancellation_propagates_without_extra_cleanup(self):
        inner_started = asyncio.Event()

        async def inner_app(scope, receive, send):
            inner_started.set()
            await asyncio.sleep(3600)

        async def exercise():
            middleware = RequestBodyLimitMiddleware(inner_app)
            scope = _http_scope()
            driver = _Driver([{"type": "http.request", "body": b"{}", "more_body": False}])
            task = asyncio.create_task(middleware(scope, driver.receive, driver.send))
            await inner_started.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                return driver
            return driver

        with patch.dict(os.environ, {"SHOPPING_MAX_REQUEST_BODY_BYTES": "1024"}, clear=False):
            driver = asyncio.run(exercise())

        self.assertEqual(driver.sent, [])
        # Body buffered before the inner app ran; nothing was cleaned up after.
        self.assertEqual(driver.receive_calls, 1)

    def test_app_module_compat_alias_is_preserved(self):
        from shopping_cli.api import app as app_module
        from shopping_cli.api.request_limits import ASGIApp, ASGIReceive, ASGISend, RequestBodyLimitMiddleware

        self.assertIs(app_module._RequestBodyLimitMiddleware, RequestBodyLimitMiddleware)
        self.assertIs(app_module._ASGIApp, ASGIApp)
        self.assertIs(app_module._ASGISend, ASGISend)
        self.assertIs(app_module._ASGIReceive, ASGIReceive)


if __name__ == "__main__":
    unittest.main()

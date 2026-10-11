from __future__ import annotations
import asyncio
import io
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import httpx
from shopping_cli.api import app as app_module
from shopping_cli.api.fallback_asgi import MarketplaceASGIApp
from shopping_cli.api.route_matching import match_path


class ApiBoundariesTest(unittest.TestCase):
    def actual_busy(self, path):
        blocker = sqlite3.connect(path)
        blocker.execute("CREATE TABLE IF NOT EXISTS contention(v text)")
        blocker.commit()
        blocker.execute("BEGIN IMMEDIATE")
        self.addCleanup(blocker.close)

        def attempt():
            c = sqlite3.connect(path, timeout=0.02)
            try:
                c.execute("INSERT INTO contention VALUES ('not committed')")
                c.commit()
            finally:
                c.close()

        return attempt

    async def call(self, app, method="GET", path="/__busy", body=None):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://synthetic.test"
        ) as client:
            return (
                await client.request(method, path, json=body)
                if body is not None
                else await client.request(method, path)
            )

    def both_error_stacks(self, operation, path):
        async def run():
            app = app_module.create_app(path)

            @app.get("/__busy")
            def busy():
                operation()
                return {"ok": True}

            fast = await self.call(app)
            fallback = MarketplaceASGIApp(
                path,
                handle_request_fn=app_module.handle_request,
                route_provider=lambda: [],
                route_resolver=lambda method, route: (True, True),
            )
            with mock.patch.object(app_module, "dispatch_request", side_effect=lambda *a, **k: operation()):
                slow = await self.call(fallback)
            return fast, slow

        return asyncio.run(run())

    def test_actual_two_connection_busy_has_503_retryafter_both_stacks(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "database.sqlite"
            attempt = self.actual_busy(path)
            responses = self.both_error_stacks(attempt, path)
            for response in responses:
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.headers.get("retry-after"), "1")
                self.assertIn("reconcile", response.json()["error"])

    def test_actual_shared_cache_locked_code_is_transient_but_unknown_and_integrity_are_not(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "database.sqlite"
            uri = f"file:{path}?cache=shared"
            a = sqlite3.connect(uri, uri=True)
            b = sqlite3.connect(uri, uri=True)
            try:
                a.execute("CREATE TABLE t(v)")
                a.execute("INSERT INTO t VALUES (1)")
                a.commit()
                cursor = b.execute("SELECT * FROM t")
                self.assertIsNotNone(cursor.fetchone())
                b.execute("BEGIN")
                cursor = b.execute("SELECT * FROM t")
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    a.execute("DROP TABLE t")
                locked = caught.exception
                self.assertEqual(locked.sqlite_errorcode & 255, sqlite3.SQLITE_LOCKED)

                def fail():
                    raise locked

                for response in self.both_error_stacks(fail, path):
                    self.assertEqual(response.status_code, 503, response.text)
                cursor.close()
                b.rollback()
                for exc in (
                    sqlite3.OperationalError("unknown operational failure"),
                    sqlite3.IntegrityError("known integrity failure"),
                ):

                    def fail_unknown():
                        raise exc

                    for response in self.both_error_stacks(fail_unknown, path):
                        self.assertEqual(response.status_code, 500, response.text)
            finally:
                b.close()
                a.close()

    def test_fallback_lifespan_acknowledges_startup_and_shutdown_not_http(self):
        messages = []
        queue = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

        async def receive():
            return queue.pop(0)

        async def send(message):
            messages.append(message)

        app = MarketplaceASGIApp("unused-synthetic.sqlite", route_provider=lambda: [])
        asyncio.run(app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send))
        self.assertEqual(messages, [{"type": "lifespan.startup.complete"}, {"type": "lifespan.shutdown.complete"}])

    def test_route_exact_end_rejects_trailing_newline_and_preserves_legal_parameters(self):
        self.assertIsNone(match_path("/health", "/health\n"))
        self.assertIsNone(match_path("/products/{sku}", "/products/synthetic-sku\n"))
        self.assertIsNone(match_path("/products/{sku}", "/products/synthetic-sku\r"))
        self.assertEqual(match_path("/health", "/health"), {})
        self.assertEqual(match_path("/products/{sku}", "/products/synthetic-sku"), {"sku": "synthetic-sku"})

    def test_actual_fastapi_invalid_body_logs_no_canary_input(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("shopping-cli")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        with tempfile.TemporaryDirectory() as root:
            app = app_module.create_app(Path(root) / "database.sqlite")
            canary = "synthetic-body-credential-a423"
            response = asyncio.run(self.call(app, "POST", "/products", [{"merchant_token": canary}]))
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json(), {"ok": False, "error": "invalid request body"})
            self.assertNotIn(canary, stream.getvalue())
            self.assertNotIn("'input'", stream.getvalue())

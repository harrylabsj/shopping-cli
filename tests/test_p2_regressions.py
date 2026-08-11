import ast
import asyncio
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from shopping_cli.agents import agent_cli, merchant_daemon
from shopping_cli.agents.tools import HTTPMerchantAgentTools
from shopping_cli.api.app import create_app, handle_request
from shopping_cli.api.fallback_asgi import MarketplaceASGIApp
from shopping_cli.api.limits import MAX_JSON_DEPTH
from shopping_cli.core.catalog import create_merchant, create_product, search_products, update_product
from shopping_cli.core.errors import ValidationError
from shopping_cli.core.conversations import (
    append_message,
    ensure_conversation,
    merchant_conversations,
    waiting_merchant_conversations,
)
from shopping_cli.db import session as session_module
from shopping_cli.db.migrations import CURRENT_SCHEMA_VERSION
from shopping_cli.db.session import db_session, open_connection
from shopping_cli.services import conversations as conversation_service


ROOT = Path(__file__).resolve().parents[1]


def _asgi_request(app, method, path, chunks, headers=None):
    """Run one ASGI request, delivering the body as the given chunk sequence."""
    sent = []
    request_headers = [(b"content-type", b"application/json")]
    for key, value in (headers or {}).items():
        request_headers.append((str(key).lower().encode("latin1"), str(value).encode("latin1")))
    pending = list(chunks)

    async def receive():
        if pending:
            chunk = pending.pop(0)
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    async def run():
        await app(
            {
                "type": "http",
                "method": method,
                "path": path,
                "headers": request_headers,
                "query_string": b"",
            },
            receive,
            send,
        )

    asyncio.run(run())
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return status, json.loads(body.decode("utf-8") or "{}")


class P2RegressionTest(unittest.TestCase):
    def test_missing_bootstrap_fields_are_400_and_duplicate_product_is_409(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with patch.dict(
                os.environ,
                {"SHOPPING_BUYER_BOOTSTRAP_TOKEN": "buyer-secret", "SHOPPING_ADMIN_TOKEN": "admin-secret"},
                clear=False,
            ):
                status, body = handle_request(
                    db_file, "POST", "/buyer/ask", {"buyer_bootstrap_token": "buyer-secret"}
                )
                self.assertEqual(status, 400)
                self.assertIn("missing required field", body["error"])
                _status, merchant = handle_request(
                    db_file, "POST", "/merchants", {"id": "seller-a", "name": "Tea", "admin_token": "admin-secret"}
                )
                product = {
                    "merchant_id": "seller-a", "sku": "tea-a", "title": "Tea", "price": 1, "stock": 1,
                    "merchant_token": merchant["merchant_token"],
                }
                self.assertEqual(handle_request(db_file, "POST", "/products", product)[0], 200)
                self.assertEqual(handle_request(db_file, "POST", "/products", product)[0], 409)

    def test_transport_method_and_payload_limits_are_stable(self):
        status, body = handle_request(":memory:", "DELETE", "/merchants")
        self.assertEqual(status, 405)
        self.assertEqual(body["ok"], False)
        nested = {}
        current = nested
        for _ in range(MAX_JSON_DEPTH + 1):
            current["child"] = {}
            current = current["child"]
        status, body = handle_request(":memory:", "POST", "/products", nested)
        self.assertEqual(status, 400)
        self.assertIn("nesting", body["error"])

    def test_fallback_runs_blocking_dispatch_off_event_loop(self):
        release = threading.Event()

        def handler(_db, _method, path, _payload, _query):
            if path == "/slow":
                release.wait(timeout=2)
            return 200, {"ok": True, "path": path}

        app = MarketplaceASGIApp(":memory:", handle_request_fn=handler, route_provider=lambda: [])

        async def request(path):
            sent = []
            delivered = False

            async def receive():
                nonlocal delivered
                if delivered:
                    return {"type": "http.request", "body": b"", "more_body": False}
                delivered = True
                return {"type": "http.request", "body": b"{}", "more_body": False}

            async def send(message):
                sent.append(message)

            await app({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""}, receive, send)
            return sent

        async def run():
            slow = asyncio.create_task(request("/slow"))
            await asyncio.sleep(0.02)
            fast = await asyncio.wait_for(request("/fast"), timeout=0.5)
            release.set()
            await slow
            return fast

        messages = asyncio.run(run())
        self.assertEqual(messages[0]["status"], 200)

    def test_remote_plain_http_requires_explicit_opt_in(self):
        with patch.dict(os.environ, {"SHOPPING_ALLOW_INSECURE_HTTP": ""}, clear=False):
            with self.assertRaises(ValueError):
                HTTPMerchantAgentTools("http://marketplace.example", "seller-a", "token")
        HTTPMerchantAgentTools("http://127.0.0.1:8765", "seller-a", "token")

    def test_merchant_can_rotate_and_admin_can_revoke_expiring_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": "admin-secret"}, clear=False):
                _status, created = handle_request(
                    db_file, "POST", "/merchants", {"id": "seller-a", "name": "Tea", "admin_token": "admin-secret"}
                )
                old_token = created["merchant_token"]
                status, rotated = handle_request(
                    db_file, "POST", "/merchants/seller-a/token/rotate", {"merchant_token": old_token}
                )
                self.assertEqual(status, 200)
                status, revoked = handle_request(
                    db_file, "POST", "/merchants/seller-a/token/revoke", {"admin_token": "admin-secret"}
                )
            self.assertEqual(status, 200)
            self.assertEqual(revoked["revoked_count"], 1)
            with db_session(db_file) as conn:
                active = conn.execute(
                    "select count(*) from api_tokens where role = 'merchant' and revoked_at = ''"
                ).fetchone()[0]
                expiring = conn.execute(
                    "select count(*) from api_tokens where role = 'merchant' and expires_at != ''"
                ).fetchone()[0]
            self.assertEqual(active, 0)
            self.assertEqual(expiring, 2)
            self.assertNotEqual(old_token, rotated["merchant_token"])

    def test_conversation_summary_list_uses_constant_query_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                create_merchant(conn, "seller-a", "Tea")
                for index in range(10):
                    ensure_conversation(conn, f"buyer-{index}", "seller-a", reuse_open=False)
                statements = []
                conn.set_trace_callback(statements.append)
                results = merchant_conversations(conn, "seller-a", limit=10)
                conn.set_trace_callback(None)
            selects = [statement for statement in statements if statement.lstrip().lower().startswith("select")]
            self.assertEqual(len(results), 10)
            self.assertLessEqual(len(selects), 2)

    def test_fallback_content_length_over_limit_is_stable_413(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = MarketplaceASGIApp(Path(tmp) / "marketplace.sqlite")
            with patch.dict(os.environ, {"SHOPPING_MAX_REQUEST_BODY_BYTES": "1024"}, clear=False):
                status, body = _asgi_request(
                    app, "POST", "/products", [b"{}"], headers={"content-length": "2048"}
                )
            self.assertEqual(status, 413)
            self.assertFalse(body["ok"])
            self.assertIn("too large", body["error"])

    def test_fallback_chunked_body_without_content_length_is_stable_413(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = MarketplaceASGIApp(Path(tmp) / "marketplace.sqlite")
            chunks = [b'{"pad":"' + b"x" * 600, b"y" * 600, b'"}']
            with patch.dict(os.environ, {"SHOPPING_MAX_REQUEST_BODY_BYTES": "1024"}, clear=False):
                status, body = _asgi_request(app, "POST", "/products", chunks)
            self.assertEqual(status, 413)
            self.assertFalse(body["ok"])
            self.assertIn("too large", body["error"])

    def test_fallback_exact_body_limit_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = MarketplaceASGIApp(Path(tmp) / "marketplace.sqlite")
            prefix = b'{"pad":"'
            payload = prefix + b"x" * (1024 - len(prefix) - 2) + b'"}'
            self.assertEqual(len(payload), 1024)
            with patch.dict(os.environ, {"SHOPPING_MAX_REQUEST_BODY_BYTES": "1024"}, clear=False):
                status, body = _asgi_request(app, "POST", "/products", [payload])
            self.assertEqual(status, 400)
            self.assertIn("missing required field", body["error"])

    def test_fallback_unknown_path_with_bad_json_is_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = MarketplaceASGIApp(Path(tmp) / "marketplace.sqlite")
            status, body = _asgi_request(app, "POST", "/no-such-route", [b"not json"])
            self.assertEqual(status, 404)
            self.assertFalse(body["ok"])
            self.assertIn("No route", body["error"])

    def test_fallback_wrong_method_with_bad_json_is_405(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = MarketplaceASGIApp(Path(tmp) / "marketplace.sqlite")
            status, body = _asgi_request(app, "DELETE", "/health", [b"not json"])
            self.assertEqual(status, 405)
            self.assertFalse(body["ok"])
            self.assertIn("Method not allowed", body["error"])

    def test_fastapi_body_limit_counts_streamed_bytes(self):
        try:
            import fastapi  # noqa: F401
            import httpx  # noqa: F401
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(str(exc)) from exc
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Path(tmp) / "marketplace.sqlite")
            client = TestClient(app)
            with patch.dict(os.environ, {"SHOPPING_MAX_REQUEST_BODY_BYTES": "1024"}, clear=False):
                declared = client.post(
                    "/products",
                    content=b"x" * 2048,
                    headers={"content-type": "application/json"},
                )
                self.assertEqual(declared.status_code, 413)
                self.assertFalse(declared.json()["ok"])
                chunks = [b'{"pad":"' + b"x" * 600, b"y" * 600, b'"}']
                streamed = client.post(
                    "/products",
                    content=iter(chunks),
                    headers={"content-type": "application/json"},
                )
                self.assertNotIn("content-length", streamed.request.headers)
                self.assertEqual(streamed.status_code, 413)
                self.assertFalse(streamed.json()["ok"])
                prefix = b'{"pad":"'
                payload = prefix + b"x" * (1024 - len(prefix) - 2) + b'"}'
                boundary = client.post(
                    "/products",
                    content=payload,
                    headers={"content-type": "application/json"},
                )
                self.assertEqual(boundary.status_code, 400)
                self.assertIn("missing required field", boundary.json()["error"])

    def test_fastapi_routing_precedes_body_parsing(self):
        try:
            import fastapi  # noqa: F401
            import httpx  # noqa: F401
        except ModuleNotFoundError as exc:
            raise unittest.SkipTest(str(exc)) from exc
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(Path(tmp) / "marketplace.sqlite")
            client = TestClient(app)
            unknown = client.post(
                "/no-such-route",
                content=b"not json",
                headers={"content-type": "application/json"},
            )
            self.assertEqual(unknown.status_code, 404)
            self.assertFalse(unknown.json()["ok"])
            wrong_method = client.request(
                "DELETE",
                "/health",
                content=b"not json",
                headers={"content-type": "application/json"},
            )
            self.assertEqual(wrong_method.status_code, 405)
            self.assertFalse(wrong_method.json()["ok"])

    def _seed_waiting_conversations(self, conn, merchant_id, count):
        for index in range(count):
            conversation = ensure_conversation(conn, f"buyer-{index}", merchant_id, reuse_open=False)
            append_message(conn, conversation["id"], "buyer", "ask_product", f"question {index}")

    def test_waiting_merchant_backlog_query_count_is_constant(self):
        select_counts = {}
        for conversation_count in (10, 100):
            with tempfile.TemporaryDirectory() as tmp:
                db_file = Path(tmp) / "shopping.sqlite"
                with db_session(db_file) as conn:
                    create_merchant(conn, "seller-a", "Tea")
                    self._seed_waiting_conversations(conn, "seller-a", conversation_count)
                    statements = []
                    conn.set_trace_callback(statements.append)
                    results = waiting_merchant_conversations(conn, "seller-a")
                    conn.set_trace_callback(None)
                selects = [statement for statement in statements if statement.lstrip().lower().startswith("select")]
                select_counts[conversation_count] = len(selects)
                self.assertEqual(len(results), conversation_count)
                for result in results:
                    for field in ("id", "merchant_id", "buyer_id", "sku", "status", "messages"):
                        self.assertIn(field, result)
                    self.assertEqual(result["status"], "waiting_merchant")
                    buyer_messages = [m for m in result["messages"] if m["sender"] == "buyer"]
                    self.assertTrue(buyer_messages)
                    self.assertEqual(buyer_messages[-1]["text"], f"question {int(result['buyer_id'].split('-')[1])}")
        self.assertEqual(select_counts[10], select_counts[100])
        self.assertLessEqual(select_counts[100], 4)

    def test_waiting_merchant_backlog_limit_is_capped_at_100(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                create_merchant(conn, "seller-a", "Tea")
                self._seed_waiting_conversations(conn, "seller-a", 105)
                results = waiting_merchant_conversations(conn, "seller-a", limit=500)
            self.assertEqual(len(results), 100)

    def test_conversation_details_list_query_count_is_constant(self):
        select_counts = {}
        for conversation_count in (10, 100):
            with tempfile.TemporaryDirectory() as tmp:
                db_file = Path(tmp) / "shopping.sqlite"
                with db_session(db_file) as conn:
                    create_merchant(conn, "seller-a", "Tea")
                    self._seed_waiting_conversations(conn, "seller-a", conversation_count)
                    statements = []
                    conn.set_trace_callback(statements.append)
                    results = conversation_service.list_conversation_details(
                        conn, clauses=["merchant_id = ?"], values=["seller-a"], limit=100
                    )
                    conn.set_trace_callback(None)
                selects = [statement for statement in statements if statement.lstrip().lower().startswith("select")]
                select_counts[conversation_count] = len(selects)
                self.assertEqual(len(results), conversation_count)
                for result in results:
                    self.assertIn("messages", result)
                    self.assertNotIn("audit_events", result)
                    self.assertTrue(any(m["sender"] == "buyer" for m in result["messages"]))
        self.assertEqual(select_counts[10], select_counts[100])
        self.assertLessEqual(select_counts[100], 3)

    def test_search_hot_path_has_no_full_table_health_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                create_merchant(conn, "seller-a", "西湖茶庄")
                create_product(conn, "seller-a", "tea-a", "西湖龙井", 1, 1)
                statements = []
                conn.set_trace_callback(statements.append)
                self.assertEqual(search_products(conn, query="龙井")[0]["sku"], "tea-a")
                conn.set_trace_callback(None)
            sql = "\n".join(statements).lower()
            self.assertNotIn("not in (select", sql)
            self.assertNotIn("count(*) from product_search_index", sql)

    def test_handoff_destination_rejects_unsafe_schemes(self):
        """审查 P2-A：handoff_destination 拒绝 javascript:/file:/data: 等非
        http(s) scheme（create 与 update 同一校验门；API 透传为 400）。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                create_merchant(conn, "seller-a", "Tea")
                create_product(conn, "seller-a", "tea-a", "Tea", 1, 1)
                for index, bad in enumerate(
                    ("javascript:alert(1)", "file:///etc/passwd", "data:text/html;base64,PHNjcmlwdD4=")
                ):
                    with self.assertRaises(ValidationError, msg=bad):
                        create_product(
                            conn, "seller-a", f"tea-bad-{index}", "Tea", 1, 1, handoff_destination=bad
                        )
                    with self.assertRaises(ValidationError, msg=bad):
                        update_product(conn, "tea-a", handoff_destination=bad)
            with patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": "admin-secret"}, clear=False):
                _status, merchant = handle_request(
                    db_file, "POST", "/merchants", {"id": "seller-b", "name": "Tea", "admin_token": "admin-secret"}
                )
                status, body = handle_request(
                    db_file,
                    "POST",
                    "/products",
                    {
                        "merchant_id": "seller-b",
                        "sku": "tea-b",
                        "title": "Tea",
                        "price": 1,
                        "stock": 1,
                        "merchant_token": merchant["merchant_token"],
                        "handoff_destination": "javascript:alert(1)",
                    },
                )
                self.assertEqual(status, 400)
                self.assertIn("handoff destination", body["error"])

    def test_handoff_destination_allows_https_origin_and_opaque_refs(self):
        """审查 P2-A：合法 http(s) origin 与 opaque 引用串（chat-id、文档引用）
        原样通过并存储。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                create_merchant(conn, "seller-a", "Tea")
                for index, good in enumerate(
                    (
                        "https://shop.example/checkout/abc",
                        "http://127.0.0.1:8080/pay",
                        "wechat:merchant-001",
                        "po-draft-123",
                        "",
                    )
                ):
                    product = create_product(
                        conn, "seller-a", f"tea-{index}", "Tea", 1, 1, handoff_destination=good
                    )
                    self.assertEqual(product["handoff_destination"], good)
                updated = update_product(conn, "tea-0", handoff_destination="https://shop.example/checkout/new")
                self.assertEqual(updated["handoff_destination"], "https://shop.example/checkout/new")

    def test_public_product_reads_strip_handoff_destination(self):
        """审查 P2-B：匿名/公开读（/products/{sku}、/search/products）不出网
        handoff_destination；所属商户本人持有效 token 读保留完整字段。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": "admin-secret"}, clear=False):
                _status, merchant = handle_request(
                    db_file, "POST", "/merchants", {"id": "seller-a", "name": "Tea", "admin_token": "admin-secret"}
                )
                token = merchant["merchant_token"]
                status, _created = handle_request(
                    db_file,
                    "POST",
                    "/products",
                    {
                        "merchant_id": "seller-a",
                        "sku": "tea-a",
                        "title": "Longjing",
                        "price": 88,
                        "stock": 5,
                        "merchant_token": token,
                        "handoff_destination": "https://shop.example/checkout/tea-a",
                    },
                )
                self.assertEqual(status, 200)

                status, shown = handle_request(db_file, "GET", "/products/tea-a")
                self.assertEqual(status, 200)
                self.assertNotIn("handoff_destination", shown["product"])
                status, search = handle_request(db_file, "GET", "/search/products", query={"query": "longjing"})
                self.assertEqual(status, 200)
                self.assertNotIn("handoff_destination", search["results"][0])

                auth = {"_auth_token": token}
                status, shown = handle_request(db_file, "GET", "/products/tea-a", auth)
                self.assertEqual(shown["product"]["handoff_destination"], "https://shop.example/checkout/tea-a")
                status, search = handle_request(db_file, "GET", "/search/products", auth, {"query": "longjing"})
                self.assertEqual(search["results"][0]["handoff_destination"], "https://shop.example/checkout/tea-a")

    def test_listing_projection_api_strips_handoff_destination_unless_owner(self):
        """审查 P2-B：匿名 listing-projection 出口（含无 merchant_id 过滤时枚举
        全部商家投影）剥离 handoff_destination；所属商户本人持 token 保留
        （kiwi merchant agent 发布/成交取数路径）。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": "admin-secret"}, clear=False):
                _status, merchant = handle_request(
                    db_file, "POST", "/merchants", {"id": "seller-a", "name": "Tea", "admin_token": "admin-secret"}
                )
                token = merchant["merchant_token"]
                status, _created = handle_request(
                    db_file,
                    "POST",
                    "/products",
                    {
                        "merchant_id": "seller-a",
                        "sku": "tea-a",
                        "title": "Longjing",
                        "price": 88,
                        "stock": 5,
                        "merchant_token": token,
                        "handoff_destination": "https://shop.example/checkout/tea-a",
                    },
                )
                self.assertEqual(status, 200)

                status, listed = handle_request(db_file, "GET", "/v1/merchant/listings/projections")
                self.assertEqual(status, 200)
                self.assertNotIn("handoff_destination", listed["results"][0])
                status, listed = handle_request(
                    db_file, "GET", "/v1/merchant/listings/projections", query={"merchant_id": "seller-a"}
                )
                self.assertNotIn("handoff_destination", listed["results"][0])
                status, single = handle_request(db_file, "GET", "/v1/merchant/listings/tea-a/projection")
                self.assertEqual(status, 200)
                self.assertNotIn("handoff_destination", single["projection"])

                auth = {"_auth_token": token}
                status, listed = handle_request(
                    db_file, "GET", "/v1/merchant/listings/projections", auth, {"merchant_id": "seller-a"}
                )
                self.assertEqual(listed["results"][0]["handoff_destination"], "https://shop.example/checkout/tea-a")
                status, single = handle_request(db_file, "GET", "/v1/merchant/listings/tea-a/projection", auth)
                self.assertEqual(single["projection"]["handoff_destination"], "https://shop.example/checkout/tea-a")

    def test_current_schema_connection_skips_reinitialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            with patch.object(session_module, "init_db", wraps=session_module.init_db) as init:
                conn = open_connection(db_file)
                conn.close()
            init.assert_not_called()

    def test_newer_schema_version_is_rejected_without_reinit_or_downgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            future_version = CURRENT_SCHEMA_VERSION + 1
            raw = sqlite3.connect(db_file)
            raw.execute(f"pragma user_version = {future_version}")
            raw.close()
            created: list[sqlite3.Connection] = []
            real_connect = sqlite3.connect

            def tracking_connect(*args, **kwargs):
                conn = real_connect(*args, **kwargs)
                created.append(conn)
                return conn

            with patch.object(session_module, "init_db") as init, patch.object(
                session_module.sqlite3, "connect", tracking_connect
            ):
                with self.assertRaises(RuntimeError) as raised:
                    open_connection(db_file)
            init.assert_not_called()
            self.assertEqual(len(created), 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                created[0].execute("select 1")
            message = str(raised.exception)
            self.assertIn(str(future_version), message)
            self.assertIn(str(CURRENT_SCHEMA_VERSION), message)
            raw = sqlite3.connect(db_file)
            self.assertEqual(raw.execute("pragma user_version").fetchone()[0], future_version)
            raw.close()

    def test_failed_initialization_closes_connection_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            created: list[sqlite3.Connection] = []
            real_connect = sqlite3.connect

            def tracking_connect(*args, **kwargs):
                conn = real_connect(*args, **kwargs)
                created.append(conn)
                return conn

            with patch.object(session_module, "init_db", side_effect=RuntimeError("boom")), patch.object(
                session_module.sqlite3, "connect", tracking_connect
            ):
                with self.assertRaises(RuntimeError):
                    open_connection(db_file)
            self.assertEqual(len(created), 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                created[0].execute("select 1")

    def test_agent_entrypoints_do_not_reverse_import_cli_module(self):
        for path in (
            ROOT / "scripts" / "shopping_agent.py",
            ROOT / "shopping_cli" / "agents" / "agent_cli.py",
        ):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotEqual(node.module, "shopping_cli.cli", str(path))
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotEqual(alias.name, "shopping_cli.cli", str(path))
                        self.assertFalse(alias.name.startswith("shopping_cli.cli."), str(path))

    def test_agent_cli_once_processes_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            with patch.object(
                agent_cli.merchant_agent, "process_once", return_value={"checked": 1, "replied": []}
            ) as once, patch.object(agent_cli.merchant_daemon, "run_forever") as forever, patch.object(
                agent_cli, "emit"
            ) as emit_mock:
                agent_cli.main(["--db", str(db_file), "--merchant", "seller-a", "--once"])
            once.assert_called_once()
            forever.assert_not_called()
            emit_mock.assert_called_once()

    def test_agent_cli_help_output_is_unchanged(self):
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            agent_cli.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        help_text = output.getvalue()
        self.assertIn("Run a resident shopping-cli merchant agent.", help_text)
        for flag in ("--db", "--merchant", "--once", "--interval", "--format"):
            self.assertIn(flag, help_text)

    def test_process_loop_rotates_oversized_log_while_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = merchant_daemon.agent_paths("seller-a", tmp)
            merchant_daemon.ensure_agent_dirs(paths)
            log_file = paths["log_file"]
            stop_file = paths["stop_file"]
            log_file.write_bytes(b"x" * 128)
            iterations: list[int] = []

            def process_once() -> dict[str, object]:
                iterations.append(len(iterations))
                with log_file.open("ab") as handle:
                    handle.write(b"y" * 1024)
                if len(iterations) >= 2:
                    stop_file.write_text("stop", encoding="utf-8")
                return {"checked": 0, "replied": []}

            with patch.object(merchant_daemon, "MAX_AGENT_LOG_BYTES", 512), redirect_stdout(StringIO()):
                merchant_daemon._run_process_loop(
                    "seller-a",
                    process_once,
                    lambda: None,
                    interval=0.05,
                    stop_file=stop_file,
                    log_file=log_file,
                )
            self.assertEqual(len(iterations), 2)
            self.assertTrue(Path(str(log_file) + ".1").exists())
            self.assertLessEqual(log_file.stat().st_size, 512 + 1024)

    def test_log_rotation_and_tail_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = merchant_daemon.agent_paths("seller-a", tmp)
            merchant_daemon.ensure_agent_dirs(paths)
            paths["log_file"].write_bytes(b"x" * (merchant_daemon.MAX_AGENT_LOG_BYTES + 1))
            self.assertTrue(merchant_daemon.rotate_agent_log(paths["log_file"]))
            self.assertTrue(Path(str(paths["log_file"]) + ".1").exists())
            paths["log_file"].write_text("\n".join(json.dumps({"index": i}) for i in range(2000)), encoding="utf-8")
            result = merchant_daemon.logs_agent("seller-a", tail=10, state_dir=tmp)
            self.assertEqual([entry["index"] for entry in result["entries"]], list(range(1990, 2000)))


if __name__ == "__main__":
    unittest.main()

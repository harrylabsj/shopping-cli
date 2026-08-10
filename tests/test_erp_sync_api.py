"""ERP 同步接线测试（shopping-cli v0.3 §3/#3 MVP #3）。

覆盖：
- CLI ``erp sync``：--flag/env 配置、同步结果报告（注入 ERP 响应）；
- API POST /v1/merchant/erp/sync：merchant token 鉴权（缺 token 拒绝）、
  同步结果、fail-closed（ERP 网络错误返回 ok:false 信封）；
- 双栈注册（fallback + FastAPI/FakeFastAPI）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shopping_cli.api.app import create_app
from shopping_cli.core import catalog
from shopping_cli.db.session import db_session, open_connection
from shopping_cli.services import tokens as token_service
from helpers import run_cli  # noqa: E402

ERP_PAGE = {"results": [{"sku": "ERP-001", "title": "ERP Item", "price": 42.0, "stock": 7}]}


def _fake_fetch(url: str, auth_token: str = "", timeout_seconds: int = 15) -> tuple[int, bytes]:
    return (200, json.dumps(ERP_PAGE).encode())


class ErpSyncCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = mock.patch(
            "shopping_cli.data_sources.erp_source._resolve_verified_host",
            return_value="203.0.113.1",
        )
        self.resolver.start()
        self.addCleanup(self.resolver.stop)
        # per-merchant 同步限流 bucket 是进程内共享的——每个测试重置。
        from shopping_cli.api.handlers.erp import _erp_sync_buckets
        _erp_sync_buckets.clear()
        self.tmp = tempfile.mkdtemp()
        self.db_file = Path(self.tmp) / "shop.sqlite"
        with db_session(self.db_file) as conn:
            catalog.create_merchant(
                conn,
                merchant_id="merchant-1",
                name="ERP Merchant",
                city="Hangzhou",
                service_area="Xihu",
                tags=["erp"],
                contact="erp@example.com",
                automation_boundaries="full-auto",
            )

    def test_cli_sync_reports_fetched_and_upserted(self) -> None:
        with mock.patch(
            "shopping_cli.data_sources.erp_source._default_fetch", side_effect=_fake_fetch
        ):
            output = run_cli(
                self.db_file,
                "erp", "sync",
                "--base-url", "https://erp.example",
                "--default-merchant", "merchant-1",
                "--format", "json",
            )
        report = json.loads(output)
        self.assertEqual(report["fetched"], 1)
        self.assertEqual(report["upserted"], 1)
        self.assertEqual(report["source"], "erp")
        with open_connection(self.db_file) as conn:
            row = conn.execute("select * from products where sku = 'ERP-001'").fetchone()
        self.assertEqual(row["source"], "erp")
        self.assertTrue(row["source_revision"].startswith("erp-sync:"))

    def test_cli_sync_env_config_fallback(self) -> None:
        with mock.patch(
            "shopping_cli.data_sources.erp_source._default_fetch", side_effect=_fake_fetch
        ), mock.patch.dict(
            os.environ,
            {"SHOPPING_ERP_BASE_URL": "https://erp.env.example", "SHOPPING_ERP_DEFAULT_MERCHANT": "merchant-1"},
            clear=False,
        ):
            output = run_cli(self.db_file, "erp", "sync", "--format", "json")
        self.assertEqual(json.loads(output)["fetched"], 1)

    def test_cli_sync_missing_base_url_fails(self) -> None:
        with self.assertRaises(SystemExit):
            run_cli(self.db_file, "erp", "sync", "--format", "json")

    def test_cli_sync_unknown_default_merchant_fails_cleanly(self) -> None:
        """default_merchant 不存在：干净报错而非裸 sqlite IntegrityError 栈。"""
        with mock.patch(
            "shopping_cli.data_sources.erp_source._default_fetch", side_effect=_fake_fetch
        ):
            with self.assertRaises(SystemExit) as ctx:
                run_cli(
                    self.db_file,
                    "erp", "sync",
                    "--base-url", "https://erp.example",
                    "--default-merchant", "ghost-merchant",
                    "--format", "json",
                )
        self.assertIn("ghost-merchant", str(ctx.exception))


class ErpSyncApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.resolver = mock.patch(
            "shopping_cli.data_sources.erp_source._resolve_verified_host",
            return_value="203.0.113.1",
        )
        self.resolver.start()
        self.addCleanup(self.resolver.stop)
        # per-merchant 同步限流 bucket 是进程内共享的——每个测试重置。
        from shopping_cli.api.handlers.erp import _erp_sync_buckets
        _erp_sync_buckets.clear()
        self.tmp = tempfile.mkdtemp()
        self.db_file = Path(self.tmp) / "marketplace.sqlite"
        with db_session(self.db_file) as conn:
            catalog.create_merchant(
                conn,
                merchant_id="mrc-erp",
                name="ERP Merchant",
                city="Hangzhou",
                service_area="Xihu",
                tags=["erp"],
                contact="erp@example.com",
                automation_boundaries="full-auto",
            )
            self.token = token_service.issue_merchant_token(conn, "mrc-erp")

    def _post(self, app, path: str, payload: dict) -> tuple[int, dict]:
        import asyncio

        body = json.dumps(payload).encode()
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            # FastAPI/Starlette intentionally only decode a JSON body when the
            # request advertises its media type; mirror a real HTTP client so
            # the dual-stack test exercises the same contract as production.
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "http_version": "1.1",
            "scheme": "http",
        }
        sent = {"body": body}
        received: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": sent["body"], "more_body": False}

        async def send(msg: dict) -> None:
            received.append(msg)

        async def run():
            await app(scope, receive, send)

        asyncio.run(run())
        status = next(
            (m.get("status") for m in received if m["type"] == "http.response.start"), None
        )
        chunks = b"".join(m.get("body", b"") for m in received if m["type"] == "http.response.body")
        return status or 500, json.loads(chunks.decode() or "{}")

    def test_sync_requires_merchant_token(self) -> None:
        app = create_app(self.db_file)
        status, body = self._post(
            app, "/v1/merchant/erp/sync", {"base_url": "https://erp.example"}
        )
        self.assertEqual(status, 403, body)

    def test_sync_with_merchant_token_succeeds(self) -> None:
        app = create_app(self.db_file)
        with mock.patch(
            "shopping_cli.data_sources.erp_source._default_fetch", side_effect=_fake_fetch
        ):
            status, body = self._post(
                app,
                "/v1/merchant/erp/sync",
                {
                    "base_url": "https://erp.example",
                    "merchant_id": "mrc-erp",
                    "merchant_token": self.token,
                    "default_merchant_id": "mrc-erp",
                },
            )
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["upserted"], 1)
        self.assertEqual(body["actor"], "merchant:mrc-erp")

    def test_sync_network_failure_fails_closed(self) -> None:
        app = create_app(self.db_file)

        def failing_fetch(url: str, auth_token: str = "", timeout_seconds: int = 15) -> tuple[int, bytes]:
            raise OSError("connection refused")

        with mock.patch(
            "shopping_cli.data_sources.erp_source._default_fetch", side_effect=failing_fetch
        ):
            status, body = self._post(
                app,
                "/v1/merchant/erp/sync",
                {
                    "base_url": "https://erp.example",
                    "merchant_id": "mrc-erp",
                    "merchant_token": self.token,
                },
            )
        self.assertEqual(status, 200, body)
        self.assertFalse(body["ok"])
        self.assertIn("error", body)

    def test_sync_merchant_rejects_foreign_default_merchant(self) -> None:
        """跨租户防护：merchant token 调用者不得把数据写进其他商户命名空间。"""
        app = create_app(self.db_file)
        status, body = self._post(
            app,
            "/v1/merchant/erp/sync",
            {
                "base_url": "https://erp.example",
                "merchant_id": "mrc-erp",
                "merchant_token": self.token,
                "default_merchant_id": "merchant-B",
            },
        )
        self.assertEqual(status, 400, body)
        self.assertIn("own merchant", body.get("error", ""))

    def test_sync_merchant_skips_other_merchants_feed_items(self) -> None:
        """feed 自带其他商户 merchant_id 的行：跳过 + 记 errors，不改动对方行。"""
        with db_session(self.db_file) as conn:
            catalog.create_merchant(
                conn,
                merchant_id="merchant-B",
                name="Other Merchant",
                city="Hangzhou",
                service_area="Xihu",
                tags=[],
                contact="b@example.com",
                automation_boundaries="full-auto",
            )
            catalog.create_product(conn, "merchant-B", "ERP-B-001", "B original", 1.0, 5)

        feed = {
            "results": [
                {
                    "sku": "ERP-B-001",
                    "title": "B hijacked",
                    "price": 99.0,
                    "stock": 1,
                    "merchant_id": "merchant-B",
                },
                {"sku": "ERP-A-001", "title": "A item", "price": 42.0, "stock": 7},
            ]
        }

        def feed_fetch(url: str, auth_token: str = "", timeout_seconds: int = 15) -> tuple[int, bytes]:
            return (200, json.dumps(feed).encode())

        app = create_app(self.db_file)
        with mock.patch(
            "shopping_cli.data_sources.erp_source._default_fetch", side_effect=feed_fetch
        ):
            status, body = self._post(
                app,
                "/v1/merchant/erp/sync",
                {
                    "base_url": "https://erp.example",
                    "merchant_id": "mrc-erp",
                    "merchant_token": self.token,
                },
            )
        self.assertEqual(status, 200, body)
        self.assertFalse(body["ok"])  # 越界行记入 errors → fail-closed 报告
        self.assertTrue(
            any("merchant-B" in error and "does not match" in error for error in body["errors"]),
            body["errors"],
        )
        with open_connection(self.db_file) as conn:
            b_row = conn.execute(
                "select title, price from products where sku = 'ERP-B-001'"
            ).fetchone()
            self.assertEqual(b_row["title"], "B original")  # 未被改写
            self.assertEqual(b_row["price"], 1.0)
            a_row = conn.execute(
                "select merchant_id from products where sku = 'ERP-A-001'"
            ).fetchone()
            self.assertEqual(a_row["merchant_id"], "mrc-erp")  # 无 feed merchant_id → 落自己名下

    def test_sync_admin_unknown_default_merchant_rejected(self) -> None:
        """default_merchant_id 不存在：4xx 而非 FK IntegrityError 500。"""
        app = create_app(self.db_file)
        with mock.patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": "admin-secret"}, clear=False):
            status, body = self._post(
                app,
                "/v1/merchant/erp/sync",
                {
                    "base_url": "https://erp.example",
                    "admin_token": "admin-secret",
                    "default_merchant_id": "ghost-merchant",
                },
            )
        self.assertEqual(status, 404, body)
        self.assertIn("ghost-merchant", body.get("error", ""))

    def test_sync_invalid_timeout_returns_400(self) -> None:
        """timeout_seconds 非数字：ValidationError 400，不落 500。"""
        app = create_app(self.db_file)
        status, body = self._post(
            app,
            "/v1/merchant/erp/sync",
            {
                "base_url": "https://erp.example",
                "merchant_id": "mrc-erp",
                "merchant_token": self.token,
                "timeout_seconds": "abc",
            },
        )
        self.assertEqual(status, 400, body)
        self.assertIn("timeout_seconds", body.get("error", ""))

    def test_route_registered_in_both_stacks(self) -> None:
        from shopping_cli.api import app as app_module

        with mock.patch("shopping_cli.api.app.FastAPI", None):
            fallback_app = create_app(self.db_file)
        status, _ = self._post(
            fallback_app, "/v1/merchant/erp/sync", {"base_url": "https://erp.example"}
        )
        # 路由存在（403 = 鉴权失败，而非 404 路由缺失）
        self.assertEqual(status, 403)

        if app_module.FastAPI is not None:
            fastapi_app = create_app(self.db_file)
            route_paths = {
                route.path for route in getattr(fastapi_app, "routes", []) if hasattr(route, "path")
            }
            self.assertIn("/v1/merchant/erp/sync", route_paths)


if __name__ == "__main__":
    unittest.main()

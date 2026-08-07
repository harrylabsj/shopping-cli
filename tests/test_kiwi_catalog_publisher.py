"""KiwiCatalogPublisher 测试（shopping-cli v0.3 DoD #4-#6；跨仓契约锁定）。

覆盖：
- owner token 测试向量：固定 secret 断言 HMAC 精确值（与 kiwi-catalog
  api/auth.py 逐字节一致，防双仓复制漂移）；
- DoD #4：同内容 digest 不重复发布（skip）；内容变化后重发（upsert）；
- DoD #5：active=0（sku 已删）→ withdraw；
- publish payload 剥离 provenance（wire 七键白名单）；
- HTTP 失败 fail-closed（PublishError）。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from shopping_cli.db.session import open_connection
from shopping_cli.kiwi_catalog.publisher import (
    KiwiCatalogPublisher,
    PublishError,
    owner_token,
    projection_digest,
    resolve_merchant_agent_id,
)
from shopping_cli.listings.projection import project_product_listing

MERCHANT = "merchant-1"
SECRET = "fixed-test-secret"
AGENT = "cagt_owner_001"

# 固定测试向量：owner_token("fixed-test-secret", "merchant-1") 的精确 HMAC 值
EXPECTED_TOKEN = hmac.new(
    b"fixed-test-secret", b"kiwi-catalog-owner:merchant-1", hashlib.sha256
).hexdigest()


def _seed_product(conn, sku: str, *, active: int = 1, title: str = "Coffee") -> None:
    conn.execute(
        """
        insert into products(
            sku, merchant_id, title, description, category, tags_json,
            price, currency, stock, delivery_attributes_json, active,
            source, source_revision, observed_at, fresh_until, created_at, updated_at
        ) values (?, ?, ?, '', 'beverage', '[]', 99.0, 'CNY', 12, '[]', ?,
                  'local', 'rev-1', '2026-08-07T00:00:00Z', '2026-08-08T00:00:00Z', 't', 't')
        """,
        (sku, MERCHANT, title, active),
    )


class FakeCatalogServer:
    """kiwi-catalog publish API 桩（记录请求；可断言 payload）。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.next_listing_id = 0
        self.fail_next_publish = False
        self.merchant_agents: list[dict] = [
            {"catalog_agent_id": "cagt_resolved_001", "display_name": "Resolved"},
        ]

    def fetch(self, method: str, url: str, body: bytes | None, headers: dict) -> tuple[int, bytes]:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "body": json.loads(body.decode("utf-8")) if body else None,
            }
        )
        if self.fail_next_publish and "/publish" in url:
            self.fail_next_publish = False
            return (400, json.dumps({"ok": False, "error": "simulated failure"}).encode())
        if url.endswith("/v1/listings/publish"):
            self.next_listing_id += 1
            return (
                200,
                json.dumps(
                    {
                        "ok": True,
                        "created": True,
                        "listing": {"listing_id": f"lst_fake_{self.next_listing_id}"},
                    }
                ).encode(),
            )
        if "/v1/agent-catalog/merchants/" in url and "/agents" in url:
            return (
                200,
                json.dumps({"ok": True, "results": self.merchant_agents, "next_cursor": ""}).encode(),
            )
        if "/withdraw" in url:
            return (200, json.dumps({"ok": True}).encode())
        return (404, json.dumps({"error": "not found"}).encode())


class KiwiCatalogPublisherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db_path = str(Path(self.tmp) / "shop.sqlite")
        self.conn = open_connection(self.db_path)
        self.conn.execute(
            f"insert into merchants(id, name, created_at, updated_at) values ('{MERCHANT}', 'Acme', 't', 't')"
        )
        self.conn.commit()
        self.server = FakeCatalogServer()
        self.publisher = KiwiCatalogPublisher(
            base_url="http://127.0.0.1:8600",
            owner_token_secret=SECRET,
            merchant_id=MERCHANT,
            owner_agent_id=AGENT,
            fetch=self.server.fetch,
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_owner_token_vector_matches_kiwi_catalog(self) -> None:
        self.assertEqual(owner_token(SECRET, MERCHANT), EXPECTED_TOKEN)

    def test_publish_payload_strips_provenance_and_binds_owner(self) -> None:
        _seed_product(self.conn, "SKU-001")
        projection = project_product_listing(self.conn, "SKU-001", merchant_id=MERCHANT)
        outcome = self.publisher.publish_listing(self.conn, projection, source_key="SKU-001")
        self.assertTrue(outcome["listing_id"].startswith("lst_fake_"))
        sent = self.server.requests[-1]["body"]
        self.assertEqual(sent["owner_agent_id"], AGENT)
        self.assertEqual(sent["merchant_id"], MERCHANT)
        self.assertEqual(sent["owner_token"], EXPECTED_TOKEN)
        self.assertNotIn("_provenance", sent)
        self.assertEqual(
            set(sent["commercial_hints"].keys()),
            {"price_range_hint", "availability_hint", "moq", "supports_bulk_quote"},
        )

    def test_same_digest_not_republished_doD4(self) -> None:
        _seed_product(self.conn, "SKU-001")
        projection = project_product_listing(self.conn, "SKU-001", merchant_id=MERCHANT)
        first = self.publisher.publish_listing(self.conn, projection, source_key="SKU-001")
        second = self.publisher.publish_listing(self.conn, projection, source_key="SKU-001")
        self.assertTrue(second["skipped"])
        self.assertEqual(first["listing_id"], second["listing_id"])
        self.assertEqual(len(self.server.requests), 1, "同内容不应重复发 publish")

    def test_content_change_republishes(self) -> None:
        _seed_product(self.conn, "SKU-001")
        projection = project_product_listing(self.conn, "SKU-001", merchant_id=MERCHANT)
        self.publisher.publish_listing(self.conn, projection, source_key="SKU-001")
        self.conn.execute(
            "update products set title = 'Coffee Deluxe', updated_at = 't2' where sku = 'SKU-001'"
        )
        self.conn.commit()
        changed = project_product_listing(self.conn, "SKU-001", merchant_id=MERCHANT)
        self.assertNotEqual(projection_digest(projection), projection_digest(changed))
        outcome = self.publisher.publish_listing(self.conn, changed, source_key="SKU-001")
        self.assertFalse(outcome["skipped"])
        self.assertEqual(len(self.server.requests), 2)

    def test_reconcile_withdraws_inactive_products_doD5(self) -> None:
        _seed_product(self.conn, "SKU-001")
        projection = project_product_listing(self.conn, "SKU-001", merchant_id=MERCHANT)
        self.publisher.publish_listing(self.conn, projection, source_key="SKU-001")
        # 商品停用 → 镜像表里已发布但 active_skus 不再含 SKU-001 → withdraw
        self.conn.execute("update products set active = 0 where sku = 'SKU-001'")
        self.conn.commit()
        report = self.publisher.reconcile(self.conn, active_skus=set())
        self.assertEqual(len(report.withdrawn), 1)
        self.assertIn("/withdraw", self.server.requests[-1]["url"])
        with open_connection(self.db_path) as conn:
            state = conn.execute(
                "select publication_state from listing_publications where source_key = 'SKU-001'"
            ).fetchone()[0]
        self.assertEqual(state, "WITHDRAWN")

    def test_withdrawn_reactivates_same_content_republishes(self) -> None:
        """WITHDRAWN 后同内容 reactivate：digest 相同也必须重发（恢复发布）。"""
        _seed_product(self.conn, "SKU-001")
        projection = project_product_listing(self.conn, "SKU-001", merchant_id=MERCHANT)
        self.publisher.publish_listing(self.conn, projection, source_key="SKU-001")
        # 停用 → reconcile withdraw（镜像 WITHDRAWN）
        self.conn.execute("update products set active = 0 where sku = 'SKU-001'")
        self.conn.commit()
        self.publisher.reconcile(self.conn, active_skus=set())
        requests_before = len(self.server.requests)
        # 恢复：同内容重新激活 → 必须重发 publish（服务端 update_listing 重置 ACTIVE）
        self.conn.execute("update products set active = 1 where sku = 'SKU-001'")
        self.conn.commit()
        reactivated = project_product_listing(self.conn, "SKU-001", merchant_id=MERCHANT)
        self.assertEqual(projection_digest(reactivated), projection_digest(projection))
        outcome = self.publisher.publish_listing(self.conn, reactivated, source_key="SKU-001")
        self.assertFalse(outcome["skipped"], "WITHDRAWN 后同内容必须重发")
        self.assertEqual(len(self.server.requests), requests_before + 1)
        self.assertTrue(self.server.requests[-1]["url"].endswith("/v1/listings/publish"))
        with open_connection(self.db_path) as conn:
            state = conn.execute(
                "select publication_state from listing_publications where source_key = 'SKU-001'"
            ).fetchone()[0]
        self.assertEqual(state, "ACTIVE")

    def test_resolve_merchant_agent_id_returns_first_agent(self) -> None:
        agent_id = resolve_merchant_agent_id(
            "http://127.0.0.1:8600", MERCHANT, fetch=self.server.fetch
        )
        self.assertEqual(agent_id, "cagt_resolved_001")

    def test_resolve_merchant_agent_id_no_agent_raises(self) -> None:
        self.server.merchant_agents = []
        with self.assertRaises(PublishError) as ctx:
            resolve_merchant_agent_id(
                "http://127.0.0.1:8600", MERCHANT, fetch=self.server.fetch
            )
        self.assertIn("no catalog agent", str(ctx.exception))
        self.assertIn("--owner-agent-id", str(ctx.exception))

    def test_resolve_merchant_agent_id_empty_merchant_raises(self) -> None:
        with self.assertRaises(PublishError):
            resolve_merchant_agent_id("http://127.0.0.1:8600", "", fetch=self.server.fetch)

    def test_http_failure_fails_closed(self) -> None:
        _seed_product(self.conn, "SKU-001")
        projection = project_product_listing(self.conn, "SKU-001", merchant_id=MERCHANT)
        self.server.fail_next_publish = True
        with self.assertRaises(PublishError):
            self.publisher.publish_listing(self.conn, projection, source_key="SKU-001")


class ListingCliPublisherArgsTest(unittest.TestCase):
    """publish-listings 缺省路径：--merchant 必填、--owner-agent-id 回退解析。"""

    def _args(self, **overrides: Any) -> argparse.Namespace:
        base = {
            "kiwi_catalog_url": "http://127.0.0.1:8600",
            "owner_token_secret": SECRET,
            "merchant": MERCHANT,
            "owner_agent_id": "",
        }
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_default_owner_agent_resolves_from_catalog(self) -> None:
        from shopping_cli.cli_listing_commands import _publisher_from_args

        with mock.patch(
            "shopping_cli.cli_listing_commands.resolve_merchant_agent_id",
            return_value="cagt_resolved_001",
        ) as resolve:
            publisher = _publisher_from_args(self._args())
        resolve.assert_called_once_with("http://127.0.0.1:8600", MERCHANT)
        self.assertEqual(publisher.owner_agent_id, "cagt_resolved_001")

    def test_explicit_owner_agent_skips_lookup(self) -> None:
        from shopping_cli.cli_listing_commands import _publisher_from_args

        with mock.patch(
            "shopping_cli.cli_listing_commands.resolve_merchant_agent_id",
            return_value="should-not-be-used",
        ) as resolve:
            publisher = _publisher_from_args(self._args(owner_agent_id=AGENT))
        resolve.assert_not_called()
        self.assertEqual(publisher.owner_agent_id, AGENT)

    def test_merchant_required(self) -> None:
        from shopping_cli.cli_listing_commands import _publisher_from_args

        with self.assertRaises(PublishError) as ctx:
            _publisher_from_args(self._args(merchant=""))
        self.assertIn("--merchant is required", str(ctx.exception))

    def test_withdraw_skips_owner_resolution(self) -> None:
        from shopping_cli.cli_listing_commands import _publisher_from_args

        with mock.patch(
            "shopping_cli.cli_listing_commands.resolve_merchant_agent_id"
        ) as resolve:
            _publisher_from_args(self._args(), resolve_owner=False)
        resolve.assert_not_called()


if __name__ == "__main__":
    unittest.main()

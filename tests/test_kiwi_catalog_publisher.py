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

import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path

from shopping_cli.db.session import open_connection
from shopping_cli.kiwi_catalog.publisher import (
    KiwiCatalogPublisher,
    PublishError,
    owner_token,
    projection_digest,
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

    def test_http_failure_fails_closed(self) -> None:
        _seed_product(self.conn, "SKU-001")
        projection = project_product_listing(self.conn, "SKU-001", merchant_id=MERCHANT)
        self.server.fail_next_publish = True
        with self.assertRaises(PublishError):
            self.publisher.publish_listing(self.conn, projection, source_key="SKU-001")


if __name__ == "__main__":
    unittest.main()

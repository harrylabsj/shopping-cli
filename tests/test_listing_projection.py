"""PublicListingProjection 测试（shopping-cli v0.3 DoD #1-#3；测试计划 v0.3 §7）。

覆盖：
- DoD #2：私有字段（cost/floor/credentials）永不进入 projection；
- DoD #1/#3：projection 带 source_product_ref/source_revision/freshness；
- active=0 排除（withdraw 信号，DoD #5 前段）；
- capability projection 不虚构 SKU；
- availability/price hint 带 provenance 并注明 discovery hint（v0.3 §14）；
- strip_provenance 发布前剥离（wire commercial_hints 七键白名单）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shopping_cli.core.catalog import create_product
from shopping_cli.db.session import open_connection
from shopping_cli.listings.projection import (
    list_publishable_listings,
    project_capability_listing,
    project_product_listing,
    strip_provenance,
)

MERCHANT = "merchant-1"


def _seed_product(
    conn,
    sku: str,
    *,
    price: float = 99.0,
    stock: int = 12,
    active: int = 1,
    source: str = "local",
    source_revision: str = "rev-1",
) -> None:
    conn.execute(
        """
        insert into products(
            sku, merchant_id, title, description, category, tags_json,
            price, currency, stock, delivery_attributes_json, active,
            source, source_revision, observed_at, fresh_until, created_at, updated_at
        ) values (?, ?, 'Coffee', 'desc', 'beverage', '["hot"]', ?, 'CNY', ?,
                  '[]', ?, ?, ?, '2026-08-07T00:00:00Z', '2026-08-08T00:00:00Z', 't', 't')
        """,
        (sku, MERCHANT, price, stock, active, source, source_revision),
    )


class ListingProjectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db_path = str(Path(self.tmp) / "shop.sqlite")
        self.conn = open_connection(self.db_path)
        self.conn.execute(
            f"insert into merchants(id, name, created_at, updated_at) values ('{MERCHANT}', 'Acme', 't', 't')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_projection_carries_handoff_destination(self) -> None:
        """每商品成交入口（v22）：create_product 带 handoff_destination →
        投影携带该值（publish 同步进 listing 的数据源）。"""
        create_product(
            self.conn,
            merchant_id=MERCHANT,
            sku="SKU-HD",
            title="HD Product",
            price=88.0,
            stock=5,
            handoff_destination="https://merchant.example/checkout/sku-hd",
        )
        projection = project_product_listing(self.conn, "SKU-HD", merchant_id=MERCHANT)
        self.assertEqual(
            projection["handoff_destination"], "https://merchant.example/checkout/sku-hd"
        )

    def test_projection_is_public_only_doD2(self) -> None:
        _seed_product(self.conn, "SKU-001")
        projection = project_product_listing(self.conn, "SKU-001", merchant_id=MERCHANT)
        serialized = str(projection)
        for private in ("floor", "cost", "credential", "password", "token", "customer"):
            self.assertNotIn(private.lower(), serialized.lower(), f"private field leaked: {private}")
        self.assertEqual(projection["listing_type"], "product")
        self.assertEqual(projection["source_product_ref"], "SKU-001")

    def test_projection_carries_source_revision_and_freshness_doD3(self) -> None:
        _seed_product(self.conn, "SKU-001", source="erp", source_revision="erp-sync:2026-08-07T00:00:00Z")
        projection = project_product_listing(self.conn, "SKU-001", merchant_id=MERCHANT)
        self.assertEqual(projection["source_revision"], "erp-sync:2026-08-07T00:00:00Z")
        provenance = projection["_provenance"]
        self.assertEqual(provenance["authority"], "UPSTREAM_PROXY")
        self.assertIn("fresh_until", provenance)
        # hint 携带 provenance 并注明 discovery hint（v0.3 §14）
        self.assertIn("discovery hint only", provenance["note"])
        self.assertIn("availability_hint", projection["commercial_hints"])
        self.assertIn("price_range_hint", projection["commercial_hints"])

    def test_active_zero_excluded_from_publishable(self) -> None:
        _seed_product(self.conn, "SKU-001", active=1)
        _seed_product(self.conn, "SKU-002", active=0)
        projections = list_publishable_listings(self.conn, merchant_id=MERCHANT)
        self.assertEqual([p["source_product_ref"] for p in projections], ["SKU-001"])

    def test_capability_projection_has_no_fake_sku(self) -> None:
        projection = project_capability_listing(
            self.conn,
            "touch-display-mfg",
            title="Touch Display Manufacturing",
            summary="MOQ >= 100",
            merchant_id=MERCHANT,
        )
        self.assertEqual(projection["listing_type"], "capability")
        self.assertEqual(projection["publisher_listing_key"], "touch-display-mfg")
        self.assertNotIn("source_product_ref", projection)
        with self.assertRaises(Exception):
            project_capability_listing(self.conn, "  ")

    def test_strip_provenance_removes_metadata_before_wire(self) -> None:
        _seed_product(self.conn, "SKU-001")
        projection = project_product_listing(self.conn, "SKU-001", merchant_id=MERCHANT)
        wire = strip_provenance(projection)
        self.assertNotIn("_provenance", wire)
        # wire 只含 kiwi-catalog 七键白名单（v0.4 §4.1）
        self.assertEqual(
            set(wire["commercial_hints"].keys()),
            {"price_range_hint", "availability_hint", "moq", "supports_bulk_quote"},
        )

    def test_projection_unknown_sku_fails_closed(self) -> None:
        from shopping_cli.listings.projection import ProjectionError

        with self.assertRaises(ProjectionError):
            project_product_listing(self.conn, "NOPE", merchant_id=MERCHANT)


if __name__ == "__main__":
    unittest.main()

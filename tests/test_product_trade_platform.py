"""商品交易入口字段（平台 + 平台商品ID）的存储与校验。

目录侧只拿名称类发现信息：这两个字段不进 listing 投影，成交入口在磋商达成后
点对点交付。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shopping_cli.core import catalog
from shopping_cli.core.errors import ValidationError
from shopping_cli.db.session import db_session


class ProductTradePlatformTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "shopping.sqlite"
        with db_session(self.db) as conn:
            catalog.create_merchant(conn, "m1", "Merchant", delivery_fee=0.0)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _add(self, **kwargs):
        with db_session(self.db) as conn:
            return catalog.create_product(
                conn, merchant_id="m1", sku=kwargs.pop("sku", "SKU-1"), title="T",
                price=10.0, stock=5, **kwargs,
            )

    def test_stores_the_pair_and_keeps_the_id_as_text(self) -> None:
        product = self._add(trade_platform="京东", platform_product_id="100012043978")
        self.assertEqual(product["trade_platform"], "jd")
        # 不转数字：超长 ID 不丢精度
        self.assertEqual(product["platform_product_id"], "100012043978")
        long_id = self._add(sku="SKU-2", trade_platform="tmall", platform_product_id="1234567890123456789012345678901")
        self.assertEqual(long_id["platform_product_id"], "1234567890123456789012345678901")

    def test_rejects_a_half_pair_or_unknown_platform(self) -> None:
        with self.assertRaises(ValidationError):
            self._add(trade_platform="jd")
        with self.assertRaises(ValidationError):
            self._add(platform_product_id="100012043978")
        with self.assertRaises(ValidationError):
            self._add(trade_platform="pdd", platform_product_id="1")
        with self.assertRaises(ValidationError):
            self._add(trade_platform="jd", platform_product_id="abc")
        with self.assertRaises(ValidationError):
            self._add(trade_platform="jd", platform_product_id="１２３")  # 全角数字

    def test_update_keeps_the_other_half_and_can_clear_the_pair(self) -> None:
        self._add(trade_platform="jd", platform_product_id="100012043978")
        with db_session(self.db) as conn:
            updated = catalog.update_product(conn, "SKU-1", merchant_id="m1", trade_platform="taobao")
        self.assertEqual((updated["trade_platform"], updated["platform_product_id"]), ("taobao", "100012043978"))
        with db_session(self.db) as conn:
            cleared = catalog.update_product(
                conn, "SKU-1", merchant_id="m1", trade_platform="", platform_product_id=""
            )
        self.assertEqual((cleared["trade_platform"], cleared["platform_product_id"]), ("", ""))

    def test_rejects_handoff_url_that_points_at_a_different_platform_product(self) -> None:
        # VQ-101 事故：保温杯的成交入口指向京东另一件商品（茅台）。
        with self.assertRaises(ValidationError):
            self._add(
                trade_platform="jd",
                platform_product_id="100253036806",
                handoff_destination="https://item.jd.com/100012043978.html",
            )
        with self.assertRaises(ValidationError):
            self._add(
                trade_platform="jd",
                platform_product_id="100253036806",
                handoff_destination="https://item.taobao.com/item.htm?id=100253036806",
            )
        # 一致时放行（同一平台同一 ID）。
        product = self._add(
            sku="SKU-OK",
            trade_platform="jd",
            platform_product_id="100253036806",
            handoff_destination="https://item.jd.com/100253036806.html",
        )
        self.assertEqual(product["platform_product_id"], "100253036806")

    def test_handoff_url_consistency_on_update(self) -> None:
        self._add(trade_platform="jd", platform_product_id="100253036806")
        with db_session(self.db) as conn, self.assertRaises(ValidationError):
            catalog.update_product(
                conn,
                "SKU-1",
                merchant_id="m1",
                handoff_destination="https://item.jd.com/100012043978.html",
            )
        # 无法识别的 opaque 引用维持原行为，不做猜测性拒绝。
        with db_session(self.db) as conn:
            updated = catalog.update_product(
                conn, "SKU-1", merchant_id="m1", handoff_destination="chat-session-42"
            )
        self.assertEqual(updated["handoff_destination"], "chat-session-42")

    def test_listing_projection_never_carries_the_trade_fields(self) -> None:
        self._add(trade_platform="jd", platform_product_id="100012043978")
        from shopping_cli.listings.projection import list_publishable_listings

        with db_session(self.db) as conn:
            projections = list_publishable_listings(conn, merchant_id="m1")
        self.assertEqual(len(projections), 1)
        self.assertNotIn("trade_platform", projections[0])
        self.assertNotIn("platform_product_id", projections[0])


if __name__ == "__main__":
    unittest.main()

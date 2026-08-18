"""v26 — products 结构化价格边界（floor_price / max_discount_percent / promotions）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shopping_cli.core.catalog import create_merchant, create_product, product_summary, update_product
from shopping_cli.core.errors import ValidationError
from shopping_cli.db.session import db_session


class ProductPricingBoundariesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self._tmp.name) / "marketplace.sqlite"
        with db_session(self.db_file) as conn:
            create_merchant(conn, merchant_id="seller-a", name="Shop")
            create_product(
                conn,
                merchant_id="seller-a",
                sku="sku-1",
                title="保温杯",
                price=100.0,
                stock=10,
            )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_migration_adds_pricing_boundary_columns(self):
        with db_session(self.db_file) as conn:
            cols = {row["name"] for row in conn.execute("pragma table_info(products)")}
        for col in ("floor_price", "max_discount_percent", "promotions_json"):
            self.assertIn(col, cols)

    def test_defaults_are_zero_and_empty(self):
        with db_session(self.db_file) as conn:
            p = product_summary(conn, "sku-1")
        self.assertEqual(p["floor_price"], 0.0)
        self.assertEqual(p["max_discount_percent"], 0.0)
        self.assertEqual(p["promotions"], [])

    def test_create_persists_pricing_boundaries(self):
        with db_session(self.db_file) as conn:
            create_product(
                conn,
                merchant_id="seller-a",
                sku="sku-2",
                title="充电器",
                price=89.0,
                stock=5,
                floor_price=70.0,
                max_discount_percent=12.5,
                promotions=[{"title": "限时特价", "type": "price_cut"}],
            )
            p = product_summary(conn, "sku-2")
        self.assertEqual(p["floor_price"], 70.0)
        self.assertEqual(p["max_discount_percent"], 12.5)
        self.assertEqual(p["promotions"], [{"title": "限时特价", "type": "price_cut"}])

    def test_update_pricing_boundaries(self):
        with db_session(self.db_file) as conn:
            update_product(
                conn,
                "sku-1",
                merchant_id="seller-a",
                floor_price=80.0,
                max_discount_percent=10.0,
                promotions=[{"title": "x"}],
            )
            p = product_summary(conn, "sku-1")
        self.assertEqual(p["floor_price"], 80.0)
        self.assertEqual(p["max_discount_percent"], 10.0)
        self.assertEqual(p["promotions"], [{"title": "x"}])

    def test_invalid_floor_price_rejected(self):
        with db_session(self.db_file) as conn:
            with self.assertRaises(ValidationError):
                create_product(
                    conn, merchant_id="seller-a", sku="bad-1", title="x",
                    price=10.0, stock=1, floor_price=-1.0,
                )

    def test_invalid_discount_rejected(self):
        with db_session(self.db_file) as conn:
            with self.assertRaises(ValidationError):
                create_product(
                    conn, merchant_id="seller-a", sku="bad-2", title="x",
                    price=10.0, stock=1, max_discount_percent=101.0,
                )

    def test_invalid_promotions_rejected(self):
        with db_session(self.db_file) as conn:
            with self.assertRaises(ValidationError):
                create_product(
                    conn, merchant_id="seller-a", sku="bad-3", title="x",
                    price=10.0, stock=1, promotions="not-a-list",
                )


if __name__ == "__main__":
    unittest.main()

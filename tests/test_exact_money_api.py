from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shopping_cli.api.handlers import catalog as catalog_handlers
from shopping_cli.core.catalog import create_merchant, create_product
from shopping_cli.core.errors import AuthError, ConflictError
from shopping_cli.core.money_authority import (
    activate_exact_money,
    begin_money_migration,
    stage_delivery_fee,
    stage_product_money,
)
from shopping_cli.db.session import db_session
from shopping_cli.services import tokens as token_service


class ExactMoneyApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "shopping.sqlite"
        self.token = "shopping-merchant-exact-test"
        with db_session(self.db_path) as conn:
            create_merchant(conn, "m1", "Merchant", delivery_fee=12.34)
            create_product(conn, "m1", "sku-a", "A", 99.99, 5, floor_price=88.88)
            token_service.ensure_merchant_token(conn, self.token, "m1")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def require_token(conn, merchant_id: str, payload: dict) -> None:
        token_service.require_merchant_token(conn, merchant_id, str(payload.get("_auth_token") or ""))

    def payload(self, **values):
        return {"_auth_token": self.token, **values}

    def activate(self) -> None:
        with db_session(self.db_path) as conn:
            begin_money_migration(conn, "m1")
            stage_product_money(
                conn,
                merchant_id="m1",
                sku="sku-a",
                currency="CNY",
                price_minor="9999",
                floor_price_minor="8888",
            )
            stage_delivery_fee(conn, merchant_id="m1", currency="CNY", fee_minor="1234")
            activate_exact_money(conn, "m1")

    def test_list_and_detail_fail_closed_before_activation(self) -> None:
        with self.assertRaisesRegex(ConflictError, "not authoritative"):
            catalog_handlers.list_products_exact(
                self.db_path,
                {"merchant_id": "m1"},
                self.payload(),
                self.require_token,
            )

    def test_exact_list_create_update_and_detail(self) -> None:
        self.activate()
        listed = catalog_handlers.list_products_exact(
            self.db_path,
            {"merchant_id": "m1", "limit": 50, "offset": 0},
            self.payload(),
            self.require_token,
        )
        self.assertEqual(listed["money_mode"], "EXACT_MINOR")
        self.assertEqual(listed["authority_version"], 1)
        self.assertEqual(listed["items"][0]["price_minor"], "9999")
        self.assertNotIn("floor_price_minor", listed["items"][0])

        created = catalog_handlers.create_product_exact_api(
            self.db_path,
            self.payload(
                merchant_id="m1",
                sku="sku-b",
                title="B",
                price_minor="1234",
                floor_price_minor="1000",
                stock=3,
                currency="CNY",
                currency_table_version="kiwi-workbench-currency-v1-2026-09-21",
                expected_authority_version=1,
            ),
            self.require_token,
        )
        self.assertEqual(created["product"]["price_minor"], "1234")
        updated = catalog_handlers.update_product_money_exact_api(
            self.db_path,
            "sku-b",
            self.payload(
                merchant_id="m1",
                price_minor="1200",
                currency_table_version="kiwi-workbench-currency-v1-2026-09-21",
                expected_authority_version=1,
            ),
            self.require_token,
        )
        self.assertEqual(updated["product"]["price_minor"], "1200")
        detail = catalog_handlers.get_product_exact(
            self.db_path,
            "sku-b",
            self.payload(merchant_id="m1"),
            self.require_token,
        )
        self.assertEqual(detail["product"]["authority_version"], 1)

    def test_wrong_or_missing_token_is_rejected(self) -> None:
        self.activate()
        with self.assertRaises(AuthError):
            catalog_handlers.list_products_exact(
                self.db_path,
                {"merchant_id": "m1"},
                {"_auth_token": "wrong"},
                self.require_token,
            )


if __name__ == "__main__":
    unittest.main()

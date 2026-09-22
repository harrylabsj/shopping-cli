from __future__ import annotations

import sqlite3
import unittest

from shopping_cli.core.catalog import (
    create_merchant,
    create_product,
    create_product_exact,
    update_merchant,
    update_product,
    upsert_delivery_rule,
)
from shopping_cli.core.errors import ConflictError, ValidationError
from shopping_cli.core.money_authority import (
    abort_money_migration,
    activate_exact_money,
    begin_money_migration,
    decimal_token_to_minor,
    exact_delivery_fee,
    exact_product_money,
    money_migration_report,
    money_mode,
    stage_delivery_fee,
    stage_product_money,
    update_delivery_fee_exact,
    update_product_money_exact,
)
from shopping_cli.commerce.adapters import LocalCommerceDataSource
from shopping_cli.commerce.data_source import AuthorityConflictError
from shopping_cli.data_sources.adapter import upsert_product_row
from shopping_cli.db.migrations import CURRENT_SCHEMA_VERSION
from shopping_cli.db.session import init_db


class ExactMoneyAuthorityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma foreign_keys=on")
        init_db(self.conn)
        create_merchant(self.conn, "m1", "Merchant", delivery_fee=12.34)
        create_product(self.conn, "m1", "sku-a", "A", 99.99, 5, floor_price=88.88)
        create_product(self.conn, "m1", "sku-b", "B", 100, 6, floor_price=0)

    def tearDown(self) -> None:
        self.conn.close()

    def test_schema_v28_adds_nullable_exact_columns_without_guessing(self) -> None:
        self.assertEqual(CURRENT_SCHEMA_VERSION, 30)
        row = self.conn.execute(
            "select price_minor_text, floor_price_minor_text, money_currency_table_version "
            "from products where sku='sku-a'"
        ).fetchone()
        self.assertIsNone(row["price_minor_text"])
        self.assertIsNone(row["floor_price_minor_text"])
        self.assertEqual(row["money_currency_table_version"], "")

    def test_freeze_stage_activate_and_exact_updates_have_one_authority(self) -> None:
        version = begin_money_migration(self.conn, "m1")
        self.assertEqual(version, 1)
        self.assertEqual(money_mode(self.conn, "m1"), ("FROZEN", 1))
        with self.assertRaises(AuthorityConflictError):
            LocalCommerceDataSource(self.conn).get_price("sku-a")

        with self.assertRaises(ConflictError):
            update_product(self.conn, "sku-a", merchant_id="m1", price=101)
        with self.assertRaises(ConflictError):
            create_product(self.conn, "m1", "sku-c", "C", 10, 1)
        with self.assertRaises(ConflictError):
            upsert_delivery_rule(self.conn, "m1", fee=20)

        stage_product_money(
            self.conn,
            merchant_id="m1",
            sku="sku-a",
            currency="CNY",
            price_minor="9999",
            floor_price_minor="8888",
        )
        with self.assertRaisesRegex(ConflictError, "sku-b"):
            activate_exact_money(self.conn, "m1")
        stage_product_money(
            self.conn,
            merchant_id="m1",
            sku="sku-b",
            currency="CNY",
            price_minor="10000",
            floor_price_minor="0",
        )
        stage_delivery_fee(self.conn, merchant_id="m1", currency="CNY", fee_minor="1234")
        report = money_migration_report(self.conn, "m1")
        self.assertEqual(report["staged_count"], 2)
        self.assertTrue(report["delivery_fee_staged"])
        self.assertEqual(activate_exact_money(self.conn, "m1"), 1)
        self.assertEqual(money_mode(self.conn, "m1"), ("EXACT_MINOR", 1))

        current = exact_product_money(self.conn, "m1", "sku-a")
        self.assertEqual(current.price_minor, "9999")
        self.assertEqual(current.floor_price_minor, "8888")
        updated = update_product_money_exact(
            self.conn,
            merchant_id="m1",
            sku="sku-a",
            expected_authority_version=1,
            price_minor="9250",
            floor_price_minor="8800",
        )
        self.assertEqual(updated.price_minor, "9250")
        self.assertEqual(LocalCommerceDataSource(self.conn).get_price("sku-a").value, 9250)
        legacy = self.conn.execute("select price, floor_price from products where sku='sku-a'").fetchone()
        self.assertEqual(legacy["price"], 92.5)
        self.assertEqual(legacy["floor_price"], 88.0)
        self.assertEqual(
            update_delivery_fee_exact(
                self.conn,
                merchant_id="m1",
                expected_authority_version=1,
                fee_minor="1500",
            ),
            "1500",
        )
        self.assertEqual(exact_delivery_fee(self.conn, "m1"), "1500")
        created = create_product_exact(
            self.conn,
            "m1",
            "sku-c",
            "C",
            "1234",
            3,
            expected_authority_version=1,
            floor_price_minor="1200",
        )
        self.assertEqual(created["price"], 12.34)
        self.assertEqual(LocalCommerceDataSource(self.conn).get_price("sku-c").value, 1234)

        with self.assertRaises(ConflictError):
            update_product(self.conn, "sku-a", merchant_id="m1", floor_price=89)
        with self.assertRaises(ConflictError):
            upsert_product_row(
                self.conn,
                sku="sku-a",
                merchant_id="m1",
                title="A",
                description="",
                category="",
                price=95,
                currency="CNY",
                stock=5,
                source="erp",
                revision="r2",
                now_ts="2026-09-21T00:00:00+00:00",
                fresh_until="2026-09-22T00:00:00+00:00",
            )

        # Non-money delivery edits may carry the unchanged derived projection.
        update_merchant(self.conn, "m1", service_area="new area")
        with self.assertRaises(ConflictError):
            update_merchant(self.conn, "m1", delivery_fee=16)

    def test_abort_clears_staging_and_restores_legacy_writes(self) -> None:
        begin_money_migration(self.conn, "m1")
        stage_product_money(
            self.conn,
            merchant_id="m1",
            sku="sku-a",
            currency="CNY",
            price_minor="9999",
            floor_price_minor="8888",
        )
        stage_delivery_fee(self.conn, merchant_id="m1", currency="CNY", fee_minor="1234")
        abort_money_migration(self.conn, "m1")
        self.assertEqual(money_mode(self.conn, "m1"), ("LEGACY_REAL", 1))
        row = self.conn.execute(
            "select price_minor_text, floor_price_minor_text from products where sku='sku-a'"
        ).fetchone()
        self.assertIsNone(row["price_minor_text"])
        self.assertIsNone(row["floor_price_minor_text"])
        update_product(self.conn, "sku-a", merchant_id="m1", price=101)

    def test_authoritative_decimal_tokens_are_exact_and_pollution_is_rejected(self) -> None:
        self.assertEqual(decimal_token_to_minor("CNY", "99.99"), "9999")
        self.assertEqual(decimal_token_to_minor("CNY", "0"), "0")
        with self.assertRaises(ValidationError):
            decimal_token_to_minor("CNY", "1.001")
        with self.assertRaises(ValidationError):
            decimal_token_to_minor("CNY", "1e2")
        with self.assertRaises(ValidationError):
            decimal_token_to_minor("JPY", "100")
        begin_money_migration(self.conn, "m1")
        with self.assertRaises(ValidationError):
            stage_product_money(
                self.conn,
                merchant_id="m1",
                sku="sku-a",
                currency="CNY",
                price_minor="01",
                floor_price_minor="0",
            )


if __name__ == "__main__":
    unittest.main()

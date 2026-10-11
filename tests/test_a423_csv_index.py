from __future__ import annotations
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from shopping_cli.core import catalog
from shopping_cli.db.session import open_connection
from shopping_cli.data_sources import csv_excel_source as csv

ROWS = [
    {"sku": "NEW", "title": "Fresh orchid", "price": "12.50", "stock": "3"},
    {"sku": "OLD", "title": "Fresh daisy", "price": "10", "stock": "3"},
]


class CsvIndexTransactionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "shop.sqlite"
        self.c = open_connection(self.path)
        self.addCleanup(self.c.close)
        catalog.create_merchant(self.c, merchant_id="m", name="Synthetic")
        catalog.create_product(self.c, "m", "OLD", "Old tulip", 10, 3)
        self.c.execute("UPDATE products SET source='csv_excel' WHERE sku='OLD'")
        self.c.execute("CREATE TABLE caller_scratch(v text)")
        self.c.commit()
        self.config = csv.CsvExcelSyncConfig(path="unused synthetic", default_merchant_id="m")

    def test_nonempty_index_updates_new_and_renamed_products(self):
        self.assertGreater(
            self.c.execute(f"SELECT count(*) FROM {catalog.PRODUCT_SEARCH_INDEX_TABLE}").fetchone()[0], 0
        )
        result = csv.sync_csv_excel(self.c, self.config, rows=ROWS)
        self.assertEqual(result.upserted, 2)
        self.assertEqual({r["sku"] for r in catalog.search_products(self.c, query="orchid")}, {"NEW"})
        self.assertEqual({r["sku"] for r in catalog.search_products(self.c, query="daisy")}, {"OLD"})
        self.assertEqual(catalog.search_products(self.c, query="tulip"), [])

    def test_success_outer_caller_transaction_remains_uncommitted_and_allows_caller_rollback(self):
        observer = open_connection(self.path)
        try:
            self.c.execute("INSERT INTO caller_scratch VALUES ('caller')")
            csv.sync_csv_excel(self.c, self.config, rows=ROWS)
            self.assertTrue(self.c.in_transaction)
            self.assertEqual(observer.execute("SELECT count(*) FROM caller_scratch").fetchone()[0], 0)
            self.assertEqual(observer.execute("SELECT count(*) FROM products WHERE sku='NEW'").fetchone()[0], 0)
            self.c.rollback()
            self.assertEqual(self.c.execute("SELECT count(*) FROM caller_scratch").fetchone()[0], 0)
            self.assertEqual(self.c.execute("SELECT count(*) FROM products WHERE sku='NEW'").fetchone()[0], 0)
            self.assertEqual({r["sku"] for r in catalog.search_products(self.c, query="tulip")}, {"OLD"})
        finally:
            observer.close()

    def test_second_index_fault_rolls_back_only_batch_with_or_without_outer_transaction(self):
        for outer in (False, True):
            with self.subTest(outer=outer):
                if outer:
                    self.c.execute("INSERT INTO caller_scratch VALUES ('caller')")
                original = catalog.sync_product_search_index
                calls = []

                def fault(conn, sku=""):
                    original(conn, sku=sku)
                    calls.append(sku)
                    if len(calls) == 2:
                        raise RuntimeError("synthetic second index failure")

                with mock.patch.object(csv, "sync_product_search_index", fault, create=True):
                    with self.assertRaisesRegex(RuntimeError, "synthetic second index failure"):
                        csv.sync_csv_excel(self.c, self.config, rows=ROWS)
                self.assertEqual(self.c.in_transaction, outer)
                self.assertEqual(self.c.execute("SELECT count(*) FROM products WHERE sku='NEW'").fetchone()[0], 0)
                self.assertEqual(
                    self.c.execute("SELECT title FROM products WHERE sku='OLD'").fetchone()[0], "Old tulip"
                )
                self.assertEqual({r["sku"] for r in catalog.search_products(self.c, query="tulip")}, {"OLD"})
                self.assertEqual(self.c.execute("SELECT count(*) FROM caller_scratch").fetchone()[0], int(outer))
                self.c.rollback()

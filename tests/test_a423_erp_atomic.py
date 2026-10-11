from __future__ import annotations
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from shopping_cli.api.handlers import erp as erp_api
from shopping_cli.core import catalog
from shopping_cli.data_sources.erp_source import ErpSourceError, ErpSyncConfig, sync_erp_products
from shopping_cli.db.session import open_connection

ROW = {"sku": "ERP-NEW", "title": "Fresh searchable orchid", "price": 12.5, "stock": 3}


class ErpAtomicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "shopping.sqlite"
        c = open_connection(self.path)
        catalog.create_merchant(
            c,
            merchant_id="m",
            name="Synthetic",
            city="Test",
            service_area="Test",
            tags=[],
            contact="synthetic@example.test",
            automation_boundaries="full-auto",
        )
        c.execute("CREATE TABLE caller_scratch(v text)")
        c.commit()
        c.close()
        resolver = mock.patch("shopping_cli.data_sources.erp_source._resolve_verified_host", return_value="203.0.113.1")
        resolver.start()
        self.addCleanup(resolver.stop)
        erp_api._erp_sync_buckets.clear()

    def test_api_second_page_network_failure_leaves_zero_products_and_index_rows(self):
        calls = []

        def fetch(url, *args, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return 200, json.dumps({"results": [ROW]}).encode()
            raise ErpSourceError("synthetic second page failed")

        with (
            mock.patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": "synthetic-admin"}),
            mock.patch("shopping_cli.data_sources.erp_source._default_fetch", side_effect=fetch),
        ):
            result = erp_api.sync_erp(
                self.path,
                {
                    "base_url": "https://erp.example",
                    "admin_token": "synthetic-admin",
                    "default_merchant_id": "m",
                    "page_size": 1,
                },
            )
        self.assertFalse(result["ok"])
        self.assertEqual(len(calls), 2)
        c = open_connection(self.path)
        try:
            self.assertEqual(c.execute("SELECT count(*) FROM products").fetchone()[0], 0)
            self.assertEqual(c.execute(f"SELECT count(*) FROM {catalog.PRODUCT_SEARCH_INDEX_TABLE}").fetchone()[0], 0)
        finally:
            c.close()

    def test_real_second_connection_can_write_during_second_page_fetch(self):
        entered = threading.Event()
        release = threading.Event()
        errors = []
        transactions = []

        def worker():
            c = open_connection(self.path)
            calls = []

            def fetch(url):
                calls.append(url)
                if len(calls) == 1:
                    return 200, json.dumps({"results": [ROW]}).encode()
                transactions.append(c.in_transaction)
                entered.set()
                if not release.wait(3):
                    raise AssertionError("synthetic barrier not released")
                return 200, b'{"results":[]}'

            try:
                sync_erp_products(
                    c, ErpSyncConfig(base_url="https://erp.example", default_merchant_id="m", page_size=1), fetch=fetch
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                c.rollback()
                c.close()

        t = threading.Thread(target=worker)
        t.start()
        try:
            self.assertTrue(entered.wait(2), errors)
            c = open_connection(self.path)
            c.execute("PRAGMA busy_timeout=30")
            try:
                c.execute("INSERT INTO caller_scratch VALUES ('independent writer')")
                c.commit()
            finally:
                c.rollback()
                c.close()
            self.assertEqual(transactions, [False])
        finally:
            release.set()
            t.join(4)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [])

    def test_outer_caller_transaction_is_rejected_before_fetch_and_preserved(self):
        c = open_connection(self.path)
        observer = open_connection(self.path)
        calls = []
        c.execute("INSERT INTO caller_scratch VALUES ('caller uncommitted')")
        try:
            with self.assertRaises(ErpSourceError):
                sync_erp_products(
                    c,
                    ErpSyncConfig(base_url="https://erp.example", default_merchant_id="m"),
                    fetch=lambda url: calls.append(url) or (200, b'{"results":[]}'),
                )
            self.assertTrue(c.in_transaction)
            self.assertEqual(calls, [])
            self.assertEqual(c.execute("SELECT count(*) FROM caller_scratch").fetchone()[0], 1)
            self.assertEqual(observer.execute("SELECT count(*) FROM caller_scratch").fetchone()[0], 0)
            c.rollback()
            self.assertEqual(c.execute("SELECT count(*) FROM caller_scratch").fetchone()[0], 0)
        finally:
            c.rollback()
            c.close()
            observer.close()

    def test_second_index_failure_rolls_back_all_owned_products_and_indexes(self):
        import shopping_cli.data_sources.erp_source as source

        c = open_connection(self.path)
        try:
            catalog.create_product(c, "m", "SEED", "Existing searchable seed", 5, 2)
            c.commit()
            before = [tuple(row) for row in c.execute(f"SELECT * FROM {catalog.PRODUCT_SEARCH_INDEX_TABLE}")]
            real_index = catalog.sync_product_search_index
            indexed = []

            def index_then_fault(conn, sku=""):
                real_index(conn, sku=sku)
                indexed.append(sku)
                if len(indexed) == 2:
                    raise RuntimeError("synthetic index write failure")

            feed = {"results": [ROW, {**ROW, "sku": "ERP-SECOND"}]}
            with mock.patch.object(source, "sync_product_search_index", index_then_fault):
                with self.assertRaisesRegex(RuntimeError, "synthetic index write failure"):
                    sync_erp_products(
                        c,
                        ErpSyncConfig(base_url="https://erp.example", default_merchant_id="m"),
                        fetch=lambda url: (200, json.dumps(feed).encode()),
                    )
            self.assertFalse(c.in_transaction)
            self.assertEqual([row[0] for row in c.execute("SELECT sku FROM products")], ["SEED"])
            self.assertEqual(
                [tuple(row) for row in c.execute(f"SELECT * FROM {catalog.PRODUCT_SEARCH_INDEX_TABLE}")], before
            )
            names = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertFalse(names & {"orders", "payments", "reservations"})
        finally:
            c.rollback()
            c.close()

    def test_overlarge_page_is_structural_error_with_zero_writes(self):
        c = open_connection(self.path)
        try:
            with self.assertRaisesRegex(ErpSourceError, "page_size"):
                sync_erp_products(
                    c,
                    ErpSyncConfig(base_url="https://erp.example", default_merchant_id="m", page_size=1),
                    fetch=lambda url: (200, json.dumps({"results": [ROW, {**ROW, "sku": "SECOND"}]}).encode()),
                )
            self.assertEqual(c.execute("SELECT count(*) FROM products").fetchone()[0], 0)
            self.assertFalse(c.in_transaction)
        finally:
            c.rollback()
            c.close()

    def test_token_revoked_during_fetch_is_rechecked_before_any_product_write(self):
        from shopping_cli.services import tokens
        from shopping_cli.core.errors import AuthError

        c = open_connection(self.path)
        token = tokens.issue_merchant_token(c, "m")
        c.commit()
        c.close()
        calls = []

        def fetch(url, *args, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return 200, json.dumps({"results": [ROW]}).encode()
            other = open_connection(self.path)
            try:
                tokens.revoke_merchant_tokens(other, "m")
                other.commit()
            finally:
                other.close()
            return 200, b'{"results":[]}'

        with mock.patch("shopping_cli.data_sources.erp_source._default_fetch", side_effect=fetch):
            with self.assertRaises(AuthError):
                erp_api.sync_erp(
                    self.path,
                    {"base_url": "https://erp.example", "merchant_id": "m", "merchant_token": token, "page_size": 1},
                )
        self.assertEqual(len(calls), 2)
        c = open_connection(self.path)
        try:
            self.assertEqual(c.execute("SELECT count(*) FROM products").fetchone()[0], 0)
        finally:
            c.close()

    def test_money_freeze_during_fetch_keeps_original_row_skip_report_and_zero_writes(self):
        from shopping_cli.core.money_authority import begin_money_migration

        c = open_connection(self.path)
        calls = []

        def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                return 200, json.dumps({"results": [ROW]}).encode()
            other = open_connection(self.path)
            try:
                begin_money_migration(other, "m")
                other.commit()
            finally:
                other.close()
            return 200, b'{"results":[]}'

        try:
            report = sync_erp_products(
                c, ErpSyncConfig(base_url="https://erp.example", default_merchant_id="m", page_size=1), fetch=fetch
            )
            self.assertEqual((report.fetched, report.upserted, report.skipped), (1, 0, 1))
            self.assertTrue(report.errors)
            self.assertEqual(c.execute("SELECT count(*) FROM products").fetchone()[0], 0)
        finally:
            c.rollback()
            c.close()

    def test_second_page_structure_failure_and_ten_full_pages_leave_zero_writes(self):
        for shape in ("bad-shape", "ten-full"):
            with self.subTest(shape=shape):
                calls = []

                def fetch(url, *args, **kwargs):
                    calls.append(url)
                    if shape == "bad-shape" and len(calls) == 2:
                        return 200, b'{"unexpected":[]}'
                    return 200, json.dumps({"results": [ROW]}).encode()

                erp_api._erp_sync_buckets.clear()
                with (
                    mock.patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": "synthetic-admin"}),
                    mock.patch("shopping_cli.data_sources.erp_source._default_fetch", side_effect=fetch),
                ):
                    result = erp_api.sync_erp(
                        self.path,
                        {
                            "base_url": "https://erp.example",
                            "admin_token": "synthetic-admin",
                            "default_merchant_id": "m",
                            "page_size": 1,
                        },
                    )
                self.assertFalse(result["ok"])
                self.assertEqual(len(calls), 2 if shape == "bad-shape" else 10)
                c = open_connection(self.path)
                try:
                    self.assertEqual(c.execute("SELECT count(*) FROM products").fetchone()[0], 0)
                    self.assertEqual(
                        c.execute(f"SELECT count(*) FROM {catalog.PRODUCT_SEARCH_INDEX_TABLE}").fetchone()[0], 0
                    )
                finally:
                    c.close()

    def test_import_updates_nonempty_index_for_new_and_renamed_products(self):
        c = open_connection(self.path)
        try:
            catalog.create_product(c, "m", "ERP-OLD", "Old searchable tulip", 10, 3)
            c.execute("UPDATE products SET source='erp' WHERE sku='ERP-OLD'")
            c.commit()
            self.assertTrue(catalog.product_search_index_available(c))
            self.assertGreater(c.execute(f"SELECT count(*) FROM {catalog.PRODUCT_SEARCH_INDEX_TABLE}").fetchone()[0], 0)
            feed = {"results": [ROW, {**ROW, "sku": "ERP-OLD", "title": "Fresh searchable daisy"}]}
            report = sync_erp_products(
                c,
                ErpSyncConfig(base_url="https://erp.example", default_merchant_id="m"),
                fetch=lambda url: (200, json.dumps(feed).encode()),
            )
            self.assertEqual(report.upserted, 2)
            self.assertEqual({row["sku"] for row in catalog.search_products(c, query="orchid")}, {"ERP-NEW"})
            self.assertEqual({row["sku"] for row in catalog.search_products(c, query="daisy")}, {"ERP-OLD"})
            self.assertEqual(catalog.search_products(c, query="tulip"), [])
        finally:
            c.rollback()
            c.close()

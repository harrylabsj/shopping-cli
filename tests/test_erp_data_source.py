"""ERP 数据源测试（shopping-cli data hub v0.2.1 §3/#7）。

覆盖：
- 分页拉取 → upsert 本地 products 表 + source='erp' 标注（UPSTREAM_PROXY 缓存）；
- ERP 覆盖自身此前同步的行（source='erp' → source='erp'）；
- 本地手改行（source='local'）同 SKU 冲突 → 跳过并记入 conflicts
  （绝不静默合并冲突权威源）；
- 网络失败 / 结构错误 / 非 2xx → ErpSourceError（fail-closed）；
- 缺 merchant_id 且无默认 → 记 errors 跳过。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from shopping_cli.data_sources.erp_source import (
    AUTHORITY_ERP,
    SOURCE_ERP,
    ErpSourceError,
    ErpSyncConfig,
    sync_erp_products,
)
from shopping_cli.db.session import open_connection

PAGE1 = {"results": [{"sku": "SKU-001", "title": "Coffee", "price": 99.0, "stock": 12, "currency": "CNY"}]}
PAGE2 = {"results": [{"sku": "SKU-002", "title": "Tea", "price": 42.0, "stock": 5}]}


def fake_fetch(pages: list[dict]) -> object:
    calls: list[str] = []

    def fetch(url: str) -> tuple[int, bytes]:
        calls.append(url)
        for page in pages:
            if "limit" in url and str(pages.index(page)) in url:
                return (200, json.dumps(page).encode())
        # 单页：所有请求都回第一页
        if len(pages) == 1:
            return (200, json.dumps(pages[0]).encode())
        return (200, json.dumps({"results": []}).encode())

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


class ErpDataSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db_path = str(Path(self.tmp) / "shop.sqlite")
        self.conn = open_connection(self.db_path)
        # products.merchant_id 弱引用 merchants(id)——测试预置商家影子行。
        self.conn.execute(
            "insert into merchants(id, name, created_at, updated_at) values ('merchant-1', 'Acme', 't', 't')"
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_sync_upserts_with_erp_source_label(self) -> None:
        fetch = fake_fetch([PAGE1])
        report = sync_erp_products(
            self.conn,
            ErpSyncConfig(base_url="https://erp.example", default_merchant_id="merchant-1"),
            fetch=fetch,
        )
        self.assertEqual(report.fetched, 1)
        self.assertEqual(report.upserted, 1)
        self.assertEqual(report.conflicts, [])
        row = self.conn.execute(
            "select sku, merchant_id, title, price, stock, source from products where sku = 'SKU-001'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "merchant-1")
        self.assertEqual(row[2], "Coffee")
        self.assertEqual(row[3], 99.0)
        self.assertEqual(row[4], 12)
        self.assertEqual(row[5], SOURCE_ERP)
        self.assertEqual(report.as_dict()["authority"], AUTHORITY_ERP)

    def test_erp_sync_overwrites_previous_erp_rows(self) -> None:
        fetch = fake_fetch([PAGE1])
        sync_erp_products(self.conn, ErpSyncConfig(base_url="https://erp.example", default_merchant_id="merchant-1"), fetch=fetch)
        fetch2 = fake_fetch([{"results": [{"sku": "SKU-001", "title": "Coffee v2", "price": 88.0, "stock": 3}]}])
        report = sync_erp_products(self.conn, ErpSyncConfig(base_url="https://erp.example", default_merchant_id="merchant-1"), fetch=fetch2)
        self.assertEqual(report.upserted, 1)
        row = self.conn.execute("select title, price, source from products where sku='SKU-001'").fetchone()
        self.assertEqual(row[0], "Coffee v2")
        self.assertEqual(row[1], 88.0)
        self.assertEqual(row[2], SOURCE_ERP)

    def test_local_authoritative_row_conflict_is_skipped(self) -> None:
        self.conn.execute(
            """insert into products(sku, merchant_id, title, description, category, tags_json,
               price, currency, stock, delivery_attributes_json, active, source, created_at, updated_at)
               values ('SKU-001','merchant-1','Local Title','','','[]',55.0,'CNY',1,'[]',1,'local','t','t')"""
        )
        fetch = fake_fetch([PAGE1])
        report = sync_erp_products(self.conn, ErpSyncConfig(base_url="https://erp.example", default_merchant_id="m"), fetch=fetch)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(report.conflicts[0]["sku"], "SKU-001")
        # 本地行未被覆盖
        row = self.conn.execute("select title, price, source from products where sku='SKU-001'").fetchone()
        self.assertEqual(row[0], "Local Title")
        self.assertEqual(row[1], 55.0)
        self.assertEqual(row[2], "local")

    def test_network_failure_fail_closed(self) -> None:
        def boom(_url: str) -> tuple[int, bytes]:
            raise OSError("connection refused")

        with self.assertRaises(ErpSourceError):
            sync_erp_products(
                self.conn,
                ErpSyncConfig(base_url="https://erp.example", default_merchant_id="m"),
                fetch=boom,
            )

    def test_non_2xx_and_bad_shape_fail_closed(self) -> None:
        def http500(_url: str) -> tuple[int, bytes]:
            return (500, b"boom")

        with self.assertRaises(ErpSourceError):
            sync_erp_products(
                self.conn, ErpSyncConfig(base_url="https://erp.example", default_merchant_id="m"), fetch=http500
            )

        def bad_shape(_url: str) -> tuple[int, bytes]:
            return (200, b'{"foo": 1}')

        with self.assertRaises(ErpSourceError):
            sync_erp_products(
                self.conn, ErpSyncConfig(base_url="https://erp.example", default_merchant_id="m"), fetch=bad_shape
            )

    def test_missing_merchant_id_recorded_as_error(self) -> None:
        fetch = fake_fetch([{"results": [{"sku": "SKU-001", "title": "Coffee", "price": 1.0, "stock": 1}]}])
        report = sync_erp_products(
            self.conn, ErpSyncConfig(base_url="https://erp.example"), fetch=fetch
        )
        self.assertEqual(report.errors, ["sku SKU-001: no merchant_id (and no default_merchant_id)"])
        self.assertEqual(report.upserted, 0)

    def test_invalid_base_url_fail_closed(self) -> None:
        with self.assertRaises(ErpSourceError):
            sync_erp_products(
                self.conn, ErpSyncConfig(base_url="ftp://erp.example"), fetch=fake_fetch([PAGE1])
            )

    def test_sync_backfills_provenance_columns_v17(self) -> None:
        fetch = fake_fetch([PAGE1])
        sync_erp_products(
            self.conn, ErpSyncConfig(base_url="https://erp.example", default_merchant_id="merchant-1"),
            fetch=fetch,
        )
        row = self.conn.execute(
            "select source, source_revision, observed_at, fresh_until from products where sku = 'SKU-001'"
        ).fetchone()
        self.assertEqual(row["source"], "erp")
        self.assertTrue(row["source_revision"].startswith("erp-sync:"))
        self.assertTrue(row["observed_at"])
        # fresh_until 在未来（默认 TTL 24h）
        from datetime import datetime

        fresh = datetime.fromisoformat(row["fresh_until"])
        self.assertGreater(fresh, datetime.fromisoformat(row["observed_at"]))


if __name__ == "__main__":
    unittest.main()

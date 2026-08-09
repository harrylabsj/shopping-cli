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
        # 注入 fetch 的测试不经过真实 DNS；SSRF 解析校验路径在
        # test_private_ip_base_url_rejected_before_fetch 独立覆盖。
        from unittest import mock
        self.resolver = mock.patch(
            "shopping_cli.data_sources.erp_source._resolve_verified_host",
            return_value="203.0.113.1",
        )
        self.resolver.start()
        self.addCleanup(self.resolver.stop)
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
        report = sync_erp_products(self.conn, ErpSyncConfig(base_url="https://erp.example", default_merchant_id="merchant-1"), fetch=fetch)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(report.conflicts[0]["sku"], "SKU-001")
        # 本地行未被覆盖
        row = self.conn.execute("select title, price, source from products where sku='SKU-001'").fetchone()
        self.assertEqual(row[0], "Local Title")
        self.assertEqual(row[1], 55.0)
        self.assertEqual(row[2], "local")

    def test_cross_merchant_sku_refuses_reassignment(self) -> None:
        """SKU 已属于其他 merchant 的行不能被 feed 改划归属（admin/CLI 路径也拦）。"""
        self.conn.execute(
            """insert into products(sku, merchant_id, title, description, category, tags_json,
               price, currency, stock, delivery_attributes_json, active, source, created_at, updated_at)
               values ('SKU-001','merchant-1','Other Tenant','','','[]',55.0,'CNY',1,'[]',1,'local','t','t')"""
        )
        fetch = fake_fetch([PAGE1])
        report = sync_erp_products(self.conn, ErpSyncConfig(base_url="https://erp.example", default_merchant_id="merchant-2"), fetch=fetch)
        self.assertEqual(report.skipped, 1)
        self.assertIn("already owned by another merchant", report.errors[0])
        self.assertEqual(report.conflicts, [])
        row = self.conn.execute("select merchant_id, source from products where sku='SKU-001'").fetchone()
        self.assertEqual(row[0], "merchant-1")
        self.assertEqual(row[1], "local")

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

    # ── v3.0 SSRF 加固（H1）──

    def test_private_ip_base_url_rejected_before_fetch(self) -> None:
        # 本测试需要真实 DNS/IP 校验——临时停用 setUp 的解析 mock。
        self.resolver.stop()
        try:
            for url in (
                "http://127.0.0.1:8765",
                "http://localhost:8765",
                "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.5/",
                "http://192.168.1.1/",
            ):
                with self.subTest(url=url), self.assertRaises(ErpSourceError):
                    sync_erp_products(
                        self.conn, ErpSyncConfig(base_url=url), fetch=fake_fetch([PAGE1])
                    )
        finally:
            self.resolver.start()

    def test_non_standard_port_rejected(self) -> None:
        with self.assertRaises(ErpSourceError):
            sync_erp_products(
                self.conn, ErpSyncConfig(base_url="https://erp.example:8443"), fetch=fake_fetch([PAGE1])
            )

    def test_feed_beyond_page_cap_aborts(self) -> None:
        """恶意/异常 feed 永远返回满页 → 超过页数硬上限必须中止。"""
        def always_full(_url):
            # 每页返回满页（page_size=100）→ 循环永不自然终止
            return (200, json.dumps({"results": [PAGE1["results"][0]] * 100}).encode("utf-8"))
        from unittest.mock import patch
        with patch("shopping_cli.data_sources.erp_source._MAX_PAGES", 3):
            with self.assertRaises(ErpSourceError) as raised:
                sync_erp_products(
                    self.conn,
                    ErpSyncConfig(base_url="https://erp.example", page_size=100),
                    fetch=always_full,
                )
        self.assertIn("more than 3 full pages", str(raised.exception))

    def test_non_finite_price_rejected(self) -> None:
        """price=inf 在解析期被拒（此前会写进投影并让 get_price OverflowError）。"""
        page = {"results": [{"sku": "INF-1", "title": "Bad", "price": float("inf"), "stock": 1}]}
        def fetch_inf(_url):
            return (200, json.dumps(page).encode("utf-8"))
        with self.assertRaises(ErpSourceError):
            sync_erp_products(
                self.conn, ErpSyncConfig(base_url="https://erp.example"), fetch=fetch_inf
            )

    def test_local_edit_promotes_erp_row_to_authoritative(self) -> None:
        """update_product/set_stock 编辑 ERP 行 → source 提升为 local（H3）。"""
        sync_erp_products(
            self.conn, ErpSyncConfig(base_url="https://erp.example", default_merchant_id="merchant-1"),
            fetch=fake_fetch([PAGE1]),
        )
        from shopping_cli.core.catalog import update_product
        update_product(self.conn, "SKU-001", title="手工改的标题")
        row = self.conn.execute(
            "select source from products where sku = 'SKU-001'"
        ).fetchone()
        self.assertEqual(row["source"], "local")
        # 提升后：再次同步同一 SKU 必须跳过并记入 conflicts（不再被覆盖）。
        fetch = fake_fetch([PAGE1])
        report = sync_erp_products(
            self.conn, ErpSyncConfig(base_url="https://erp.example", default_merchant_id="merchant-1"),
            fetch=fetch,
        )
        self.assertIn("local authoritative row", report.conflicts[0]["reason"])

    def test_set_stock_promotes_erp_row_to_authoritative(self) -> None:
        from shopping_cli.core.catalog import set_stock
        sync_erp_products(
            self.conn, ErpSyncConfig(base_url="https://erp.example", default_merchant_id="merchant-1"),
            fetch=fake_fetch([PAGE1]),
        )
        set_stock(self.conn, "SKU-001", 42)
        row = self.conn.execute(
            "select source from products where sku = 'SKU-001'"
        ).fetchone()
        self.assertEqual(row["source"], "local")


if __name__ == "__main__":
    unittest.main()

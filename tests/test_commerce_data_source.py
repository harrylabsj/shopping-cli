"""CommerceDataSource 本地适配器测试（shopping-cli v0.3 §4/§5；测试计划 v0.3 §3）。

覆盖：
- LocalCommerceDataSource（LOCAL_AUTHORITATIVE）/ ErpCommerceDataSource
  （UPSTREAM_PROXY_WRITE）read + write_mode 标注 + provenance 透传；
- resolve_field 双源冲突 → AuthorityConflictError（fail-closed）；
- draft/apply 写意图 fail-closed（NotImplementedError）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shopping_cli.commerce.adapters import (
    ErpCommerceDataSource,
    LocalCommerceDataSource,
    resolve_field,
)
from shopping_cli.commerce.data_source import (
    LOCAL_AUTHORITATIVE,
    UPSTREAM_PROXY_WRITE,
    AuthorityConflictError,
    CommerceField,
)
from shopping_cli.db.session import open_connection

MERCHANT = "merchant-1"


class CommerceDataSourceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db_path = str(Path(self.tmp) / "shop.sqlite")
        self.conn = open_connection(self.db_path)
        self.conn.execute(
            f"insert into merchants(id, name, created_at, updated_at) values ('{MERCHANT}', 'Acme', 't', 't')"
        )
        for sku, source in (("LOCAL-1", "local"), ("ERP-1", "erp")):
            self.conn.execute(
                """
                insert into products(
                    sku, merchant_id, title, price, currency, stock, active, source,
                    source_revision, observed_at, fresh_until, created_at, updated_at
                ) values (?, ?, 'Item', 99.0, 'CNY', 5, 1, ?, 'rev-1',
                          '2026-08-07T00:00:00Z', '2026-08-08T00:00:00Z', 't', 't')
                """,
                (sku, MERCHANT, source),
            )
        self.conn.commit()
        self.local = LocalCommerceDataSource(self.conn)
        self.erp = ErpCommerceDataSource(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_local_source_marks_local_authoritative(self) -> None:
        fact = self.local.get_product("LOCAL-1")
        self.assertEqual(fact.sku, "LOCAL-1")
        self.assertEqual(fact.price_minor, 9900)
        field = self.local.get_price("LOCAL-1")
        self.assertEqual(field.write_mode, LOCAL_AUTHORITATIVE)
        self.assertEqual(field.value, 9900)
        self.assertEqual(field.source_revision, "rev-1")
        self.assertEqual(field.fresh_until, "2026-08-08T00:00:00Z")

    def test_erp_source_marks_upstream_proxy_write(self) -> None:
        fact = self.erp.get_product("ERP-1")
        self.assertEqual(fact.sku, "ERP-1")
        field = self.erp.get_inventory("ERP-1")
        self.assertEqual(field.write_mode, UPSTREAM_PROXY_WRITE)
        self.assertEqual(field.value, 5)

    def test_sources_do_not_cross_read(self) -> None:
        self.assertIsNone(self.local.get_product("ERP-1"))
        self.assertIsNone(self.erp.get_product("LOCAL-1"))

    def test_search_products_scoped_to_source(self) -> None:
        self.assertEqual([p.sku for p in self.local.search_products()], ["LOCAL-1"])
        self.assertEqual([p.sku for p in self.erp.search_products()], ["ERP-1"])

    def test_resolve_field_single_source(self) -> None:
        field = resolve_field(
            "stock",
            {"local": self.local.get_inventory("LOCAL-1"), "erp": None},
        )
        self.assertEqual(field.write_mode, LOCAL_AUTHORITATIVE)

    def test_resolve_field_conflict_fails_closed(self) -> None:
        # 同一 sku 双源都声明权威且无优先级 → AuthorityConflictError
        local_field = CommerceField(
            value=1, authority_source="local", write_mode=LOCAL_AUTHORITATIVE
        )
        erp_field = CommerceField(
            value=2, authority_source="erp", write_mode=UPSTREAM_PROXY_WRITE
        )
        with self.assertRaises(AuthorityConflictError):
            resolve_field("stock", {"local": local_field, "erp": erp_field})

    def test_write_intent_fails_closed(self) -> None:
        with self.assertRaises(NotImplementedError):
            self.local.draft_product_change("LOCAL-1", {"price": 1})
        with self.assertRaises(NotImplementedError):
            self.erp.apply_inventory_change("ERP-1", {"stock": 0})
        with self.assertRaises(NotImplementedError):
            self.local.apply_product_change("LOCAL-1", {"title": "x"})


if __name__ == "__main__":
    unittest.main()

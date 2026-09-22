"""v1 库存写入的 operation-level receipt（B 线）。

## 为什么有这条测试

库存写入此前只走 legacy ``PATCH /products/{sku}``，**没有可对账的回执**。后果不是
"少个日志"，而是：外部写一旦落进 UNKNOWN，**无法查明副作用是否真的发生过**——
只能拿**当前**库存值去猜**历史**操作的结果，或者把候选永远挂在 UNKNOWN 上升级人工。
（两份 WB run 的 ``not_verified`` 点名的就是这个阻塞。）

本端点把库存写入提升到与 exact 商品创建/改价同一口径：

1. **回执与效果同事务**——要么都提交，要么都不提交；
2. **相同 operation_id 幂等重放**（同请求回原回执，不产生第二次效果）；
3. **同 operation_id 不同请求 → 冲突拒绝**（不静默复用）；
4. **商家鉴权**——回执只向所属商家开放；
5. **并发单效果**——同 operation_id 并发提交只落一次效果；
6. **失败不留回执**——效果没提交就绝不会有"成功"回执（否则对账会把没发生的
   副作用当成已发生）。

第 6 条是本文件里最重要的一条：它是"有回执 ⟺ 效果已提交"这个等价关系的守门断言。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import tempfile
import unittest
from pathlib import Path

from shopping_cli.api.handlers import catalog as catalog_handlers
from shopping_cli.core.catalog import create_merchant, create_product
from shopping_cli.core.errors import (
    AuthError,
    IdempotencyConflict,
    NotFoundError,
    ValidationError,
)
from shopping_cli.core.money_authority import (
    activate_exact_money,
    begin_money_migration,
    stage_delivery_fee,
    stage_product_money,
)
from shopping_cli.db.session import db_session
from shopping_cli.services import tokens as token_service

CURRENCY_TABLE = "kiwi-workbench-currency-v1-2026-09-21"


class InventoryOperationReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "shopping.sqlite"
        self.token = "shopping-merchant-inventory-test"
        self.other_token = "shopping-merchant-inventory-other"
        with db_session(self.db_path) as conn:
            create_merchant(conn, "m1", "Merchant One", delivery_fee=12.34)
            create_merchant(conn, "m2", "Merchant Two", delivery_fee=0.0)
            create_product(conn, "m1", "sku-a", "A", 99.99, 5, floor_price=88.88)
            create_product(conn, "m2", "sku-m2", "M2", 10.0, 1, floor_price=9.0)
            token_service.ensure_merchant_token(conn, self.token, "m1")
            token_service.ensure_merchant_token(conn, self.other_token, "m2")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def require_token(conn, merchant_id: str, payload: dict) -> None:
        token_service.require_merchant_token(
            conn, merchant_id, str(payload.get("_auth_token") or "")
        )

    def payload(self, **values):
        return {"_auth_token": self.token, **values}

    def activate(self) -> None:
        """把 m1 推到 exact 线——v1 面的投影需要金额权威。"""
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

    def stock_of(self, sku: str) -> int:
        with db_session(self.db_path) as conn:
            row = conn.execute("select stock from products where sku=?", (sku,)).fetchone()
            return int(row["stock"])

    def receipt_row_count(self) -> int:
        with db_session(self.db_path) as conn:
            return int(
                conn.execute("select count(*) as c from merchant_product_operations").fetchone()["c"]
            )

    def call(self, sku: str = "sku-a", **values):
        return catalog_handlers.update_product_inventory_exact_api(
            self.db_path,
            sku,
            self.payload(merchant_id="m1", **values),
            self.require_token,
        )

    # ── ① 回执与效果 ────────────────────────────────────────────────
    def test_write_updates_stock_and_records_receipt(self) -> None:
        self.activate()
        result = self.call(
            stock=42,
            currency_table_version=CURRENCY_TABLE,
            operation_id="op-inv-1",
        )
        self.assertEqual(result["product"]["stock"], 42)
        self.assertFalse(result["idempotent"])
        self.assertEqual(self.stock_of("sku-a"), 42)

        # 回执可经既有查询端点取回，且 kind 与环境正确
        receipt = catalog_handlers.get_product_operation_exact(
            self.db_path,
            "op-inv-1",
            self.payload(merchant_id="m1"),
            self.require_token,
        )
        self.assertEqual(receipt["operation"]["status"], "succeeded")
        self.assertEqual(receipt["operation"]["operation_kind"], "product_inventory_update")
        self.assertEqual(receipt["operation"]["sku"], "sku-a")

    # ── ② 幂等重放 ──────────────────────────────────────────────────
    def test_same_operation_and_request_replays_without_second_effect(self) -> None:
        self.activate()
        first = self.call(
            stock=7, currency_table_version=CURRENCY_TABLE, operation_id="op-replay"
        )
        # 中间改一次库存，再重放原请求——重放必须回原回执，且**不覆盖**中间那次改动
        self.call(stock=99, currency_table_version=CURRENCY_TABLE, operation_id="op-other")
        self.assertEqual(self.stock_of("sku-a"), 99)

        replay = self.call(
            stock=7, currency_table_version=CURRENCY_TABLE, operation_id="op-replay"
        )
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["product"]["stock"], 7)
        # 重放没有产生第二次效果：库存仍是 op-other 造成的 99
        self.assertEqual(self.stock_of("sku-a"), 99)

    # ── ③ 同 ID 不同请求 → 冲突 ─────────────────────────────────────
    def test_same_operation_id_with_different_request_conflicts(self) -> None:
        self.activate()
        self.call(stock=7, currency_table_version=CURRENCY_TABLE, operation_id="op-conflict")
        with self.assertRaises(IdempotencyConflict):
            self.call(
                stock=8, currency_table_version=CURRENCY_TABLE, operation_id="op-conflict"
            )
        self.assertEqual(self.stock_of("sku-a"), 7)

    # ── ④ 鉴权 ──────────────────────────────────────────────────────
    def test_rejects_missing_or_foreign_token(self) -> None:
        self.activate()
        with self.assertRaises(AuthError):
            catalog_handlers.update_product_inventory_exact_api(
                self.db_path,
                "sku-a",
                {"merchant_id": "m1", "stock": 5, "currency_table_version": CURRENCY_TABLE,
                 "operation_id": "op-noauth"},
                self.require_token,
            )
        # 拿 m2 的 token 写 m1 的商家 → 拒
        with self.assertRaises(AuthError):
            catalog_handlers.update_product_inventory_exact_api(
                self.db_path,
                "sku-a",
                {"_auth_token": self.other_token, "merchant_id": "m1", "stock": 5,
                 "currency_table_version": CURRENCY_TABLE, "operation_id": "op-foreign"},
                self.require_token,
            )
        self.assertEqual(self.stock_of("sku-a"), 5)
        self.assertEqual(self.receipt_row_count(), 0)

    def test_rejects_writing_another_merchants_product(self) -> None:
        self.activate()
        # m1 的合法凭据，但 sku 属于 m2 → set_stock 的归属校验必须拦住
        with self.assertRaises(ValidationError):
            self.call(
                sku="sku-m2",
                stock=5,
                currency_table_version=CURRENCY_TABLE,
                operation_id="op-cross",
            )
        self.assertEqual(self.stock_of("sku-m2"), 1)

    # ── ⑤ 前置条件 ──────────────────────────────────────────────────
    def test_requires_exact_currency_table_version(self) -> None:
        self.activate()
        with self.assertRaises(ValidationError):
            self.call(stock=5, operation_id="op-noct")
        with self.assertRaises(ValidationError):
            self.call(stock=5, currency_table_version="wrong", operation_id="op-badct")

    # ── ⑥ 失败不留回执（本文件最重要的一条）────────────────────────
    def test_no_receipt_when_effect_fails(self) -> None:
        """效果没提交 → 绝不能留下"成功"回执。

        否则对账会把一个**没发生**的副作用当成已发生（或反之把已发生的当成没发生），
        正是本端点在修的那个问题。
        """
        self.activate()
        with self.assertRaises((NotFoundError, ValidationError)):
            self.call(
                sku="sku-does-not-exist",
                stock=5,
                currency_table_version=CURRENCY_TABLE,
                operation_id="op-ghost",
            )
        self.assertEqual(self.receipt_row_count(), 0)
        # 该 operation_id 不可被查询到
        with self.assertRaises(NotFoundError):
            catalog_handlers.get_product_operation_exact(
                self.db_path,
                "op-ghost",
                self.payload(merchant_id="m1"),
                self.require_token,
            )

    def test_receipt_is_scoped_to_owning_merchant(self) -> None:
        self.activate()
        self.call(stock=11, currency_table_version=CURRENCY_TABLE, operation_id="op-scoped")
        # m2 的凭据查 m1 的回执 → 查不到（不泄漏存在性之外的信息）
        with self.assertRaises(NotFoundError):
            catalog_handlers.get_product_operation_exact(
                self.db_path,
                "op-scoped",
                {"_auth_token": self.other_token, "merchant_id": "m2"},
                self.require_token,
            )

    # ── ⑦ 并发单效果 ────────────────────────────────────────────────
    def test_concurrent_same_operation_produces_single_effect(self) -> None:
        self.activate()

        def submit(_: int):
            return self.call(
                stock=33,
                currency_table_version=CURRENCY_TABLE,
                operation_id="op-concurrent",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(8)))

        # 恰好一条非重放，其余全部走幂等重放
        non_replay = [r for r in results if not r["idempotent"]]
        self.assertEqual(len(non_replay), 1)
        self.assertEqual(self.stock_of("sku-a"), 33)
        self.assertEqual(self.receipt_row_count(), 1)


if __name__ == "__main__":
    unittest.main()

"""v1 上下架写入的 operation-level receipt（B 线：上下架）。

## 为什么有这条测试

上下架此前在数据模型里**根本不存在**——``listings/*`` 全是从 products 派生的
只读投影，没有任何 paused 状态可写；kiwi 侧工具只能 fail-closed 报「不可得」
（刻意不用库存写零伪装下架，语义不同且会污染库存事实）。没有真实状态可写，
就没有可对账的回执——外部写一旦落进 UNKNOWN 就无法查明副作用是否真的发生过。

本端点把上下架提升到与库存写入同一口径：

1. **回执与效果同事务**——要么都提交，要么都不提交；
2. **相同 operation_id 幂等重放**（同请求回原回执，不产生第二次效果）；
3. **同 operation_id 不同请求 → 冲突拒绝**（不静默复用）；
4. **商家鉴权**——回执只向所属商家开放；
5. **并发单效果**——同 operation_id 并发提交只落一次效果；
6. **失败不留回执**——效果没提交就绝不会有"成功"回执。

另覆盖 v31 的语义面：paused 后商品退出可发布清单（「下架」的投影语义）、单条
投影携带 paused 标记、operation kind 枚举表对未播种 kind 的拒绝（词表从 CHECK
改枚举表后，拒绝强度不得下降）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
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
from shopping_cli.listings.projection import (
    list_publishable_listings,
    project_product_listing,
)
from shopping_cli.services import tokens as token_service

CURRENCY_TABLE = "kiwi-workbench-currency-v1-2026-09-21"


class ListingOperationReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "shopping.sqlite"
        self.token = "shopping-merchant-listing-test"
        self.other_token = "shopping-merchant-listing-other"
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

    def paused_of(self, sku: str) -> bool:
        with db_session(self.db_path) as conn:
            row = conn.execute(
                "select listing_paused from products where sku=?", (sku,)
            ).fetchone()
            return bool(row["listing_paused"])

    def receipt_row_count(self) -> int:
        with db_session(self.db_path) as conn:
            return int(
                conn.execute("select count(*) as c from merchant_product_operations").fetchone()["c"]
            )

    def call(self, sku: str = "sku-a", **values):
        return catalog_handlers.update_product_listing_exact_api(
            self.db_path,
            sku,
            self.payload(merchant_id="m1", **values),
            self.require_token,
        )

    # ── ① 回执与效果 ────────────────────────────────────────────────
    def test_write_pauses_listing_and_records_receipt(self) -> None:
        self.activate()
        result = self.call(
            paused=True,
            currency_table_version=CURRENCY_TABLE,
            operation_id="op-lst-1",
        )
        self.assertTrue(result["product"]["listing_paused"])
        self.assertFalse(result["idempotent"])
        self.assertTrue(self.paused_of("sku-a"))

        # 回执可经既有查询端点取回，且 kind 与环境正确
        receipt = catalog_handlers.get_product_operation_exact(
            self.db_path,
            "op-lst-1",
            self.payload(merchant_id="m1"),
            self.require_token,
        )
        self.assertEqual(receipt["operation"]["status"], "succeeded")
        self.assertEqual(receipt["operation"]["operation_kind"], "product_listing_change")
        self.assertEqual(receipt["operation"]["sku"], "sku-a")
        # 回执的响应体携带 paused 目标态——下游对账核对的是语义，不只是状态
        self.assertTrue(receipt["result"]["product"]["listing_paused"])

        # 恢复销售同样是一条可回执的写
        resume = self.call(
            paused=False,
            currency_table_version=CURRENCY_TABLE,
            operation_id="op-lst-2",
        )
        self.assertFalse(resume["product"]["listing_paused"])
        self.assertFalse(self.paused_of("sku-a"))

    # ── ② 幂等重放 ──────────────────────────────────────────────────
    def test_same_operation_and_request_replays_without_second_effect(self) -> None:
        self.activate()
        first = self.call(
            paused=True, currency_table_version=CURRENCY_TABLE, operation_id="op-replay"
        )
        # 中间改一次状态，再重放原请求——重放必须回原回执，且**不覆盖**中间那次改动
        self.call(paused=False, currency_table_version=CURRENCY_TABLE, operation_id="op-other")
        self.assertFalse(self.paused_of("sku-a"))

        replay = self.call(
            paused=True, currency_table_version=CURRENCY_TABLE, operation_id="op-replay"
        )
        self.assertTrue(replay["idempotent"])
        self.assertTrue(replay["product"]["listing_paused"])
        # 重放没有产生第二次效果：状态仍是 op-other 造成的未暂停
        self.assertFalse(self.paused_of("sku-a"))

    # ── ③ 同 ID 不同请求 → 冲突 ─────────────────────────────────────
    def test_same_operation_id_with_different_request_conflicts(self) -> None:
        self.activate()
        self.call(paused=True, currency_table_version=CURRENCY_TABLE, operation_id="op-conflict")
        with self.assertRaises(IdempotencyConflict):
            self.call(
                paused=False, currency_table_version=CURRENCY_TABLE, operation_id="op-conflict"
            )
        self.assertTrue(self.paused_of("sku-a"))

    # ── ④ 鉴权 ──────────────────────────────────────────────────────
    def test_rejects_missing_or_foreign_token(self) -> None:
        self.activate()
        with self.assertRaises(AuthError):
            catalog_handlers.update_product_listing_exact_api(
                self.db_path,
                "sku-a",
                {"merchant_id": "m1", "paused": True, "currency_table_version": CURRENCY_TABLE,
                 "operation_id": "op-noauth"},
                self.require_token,
            )
        # 拿 m2 的 token 写 m1 的商家 → 拒
        with self.assertRaises(AuthError):
            catalog_handlers.update_product_listing_exact_api(
                self.db_path,
                "sku-a",
                {"_auth_token": self.other_token, "merchant_id": "m1", "paused": True,
                 "currency_table_version": CURRENCY_TABLE, "operation_id": "op-foreign"},
                self.require_token,
            )
        self.assertFalse(self.paused_of("sku-a"))
        self.assertEqual(self.receipt_row_count(), 0)

    def test_rejects_writing_another_merchants_product(self) -> None:
        self.activate()
        # m1 的合法凭据，但 sku 属于 m2 → set_listing_paused 的归属校验必须拦住
        with self.assertRaises(ValidationError):
            self.call(
                sku="sku-m2",
                paused=True,
                currency_table_version=CURRENCY_TABLE,
                operation_id="op-cross",
            )
        self.assertFalse(self.paused_of("sku-m2"))

    # ── ⑤ 前置条件 ──────────────────────────────────────────────────
    def test_requires_exact_currency_table_version(self) -> None:
        self.activate()
        with self.assertRaises(ValidationError):
            self.call(paused=True, operation_id="op-noct")
        with self.assertRaises(ValidationError):
            self.call(paused=True, currency_table_version="wrong", operation_id="op-badct")

    def test_paused_must_be_a_strict_boolean(self) -> None:
        """paused 是方向性语义字段：宽松 truthy 转换会把「恢复」静默执行成「暂停」。"""
        self.activate()
        with self.assertRaises(ValidationError):
            self.call(
                paused="false",
                currency_table_version=CURRENCY_TABLE,
                operation_id="op-strbool",
            )
        self.assertFalse(self.paused_of("sku-a"))
        self.assertEqual(self.receipt_row_count(), 0)

    # ── ⑥ 失败不留回执 ──────────────────────────────────────────────
    def test_no_receipt_when_effect_fails(self) -> None:
        """效果没提交 → 绝不能留下"成功"回执。"""
        self.activate()
        with self.assertRaises((NotFoundError, ValidationError)):
            self.call(
                sku="sku-does-not-exist",
                paused=True,
                currency_table_version=CURRENCY_TABLE,
                operation_id="op-ghost",
            )
        self.assertEqual(self.receipt_row_count(), 0)
        with self.assertRaises(NotFoundError):
            catalog_handlers.get_product_operation_exact(
                self.db_path,
                "op-ghost",
                self.payload(merchant_id="m1"),
                self.require_token,
            )

    def test_receipt_is_scoped_to_owning_merchant(self) -> None:
        self.activate()
        self.call(paused=True, currency_table_version=CURRENCY_TABLE, operation_id="op-scoped")
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
                paused=True,
                currency_table_version=CURRENCY_TABLE,
                operation_id="op-concurrent",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(8)))

        # 恰好一条非重放，其余全部走幂等重放
        non_replay = [r for r in results if not r["idempotent"]]
        self.assertEqual(len(non_replay), 1)
        self.assertTrue(self.paused_of("sku-a"))
        self.assertEqual(self.receipt_row_count(), 1)

    # ── ⑧ 投影语义：暂停 = 不可发布 ─────────────────────────────────
    def test_paused_product_leaves_publishable_projection(self) -> None:
        self.activate()
        with db_session(self.db_path) as conn:
            before = list_publishable_listings(conn, merchant_id="m1")
            self.assertIn("sku-a", [p["source_product_ref"] for p in before])
            self.assertFalse(
                project_product_listing(conn, "sku-a", merchant_id="m1")["_provenance"][
                    "listing_paused"
                ]
            )

        self.call(paused=True, currency_table_version=CURRENCY_TABLE, operation_id="op-proj")

        with db_session(self.db_path) as conn:
            # 下架语义：暂停商品退出可发布清单，但仍在 products 里、可恢复
            after = list_publishable_listings(conn, merchant_id="m1")
            self.assertNotIn("sku-a", [p["source_product_ref"] for p in after])
            # 单条投影仍可取，且携带 paused 标记（owner 核对销售状态用）
            projection = project_product_listing(conn, "sku-a", merchant_id="m1")
            self.assertTrue(projection["_provenance"]["listing_paused"])

    # ── ⑨ operation kind 枚举表：拒绝强度不低于旧 CHECK ─────────────
    def test_unseeded_operation_kind_is_rejected_by_enum_fk(self) -> None:
        """v31 把词表从 CHECK 改为枚举表 + FK（session 层 pragma foreign_keys=on，
        故 FK 真实强制）。未播种的 kind 必须仍被 IntegrityError 拒绝——否则词表
        收敛是以放松校验为代价的。
        """
        with self.assertRaises(sqlite3.IntegrityError):
            with db_session(self.db_path) as conn:
                conn.execute(
                    """
                    insert into merchant_product_operations(
                        operation_id, merchant_id, operation_kind, sku, request_hash,
                        status, response_json, created_at
                    ) values('op-bad-kind', 'm1', 'kind_not_in_enum', 'sku-a',
                             'h', 'succeeded', '{}', 't')
                    """
                )


if __name__ == "__main__":
    unittest.main()

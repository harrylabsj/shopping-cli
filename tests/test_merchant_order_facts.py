"""商家私有订单/退款/成本快照读取面（Kiwi 商家持续经营 v0.2 §11–§13）。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shopping_cli.api.handlers import orders
from shopping_cli.core.errors import ConflictError, NotFoundError, ValidationError
from shopping_cli.db.session import db_session


def _auth(_conn, _merchant_id: str, _payload: dict) -> None:
    return None


class MerchantOrderFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "shopping.sqlite"
        with db_session(self.db):
            pass

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _record(self, **overrides):
        payload = {
            "merchant_id": "m1",
            "source_namespace": "jd",
            "source_account_id": "acc-1",
            "external_order_id": "order-1",
            "evidence_source": "manual_report",
            "observed_at": "2026-10-29T10:00:00Z",
            "payment_status": "paid",
            "currency": "CNY",
            "total_minor": 1000,
            "lines": [
                {"line_id": "l1", "sku": "SKU-1", "quantity": 2, "amount_minor": 1000},
            ],
        }
        payload.update(overrides)
        return orders.record_order(self.db, payload, _auth)

    def test_records_and_reads_manual_order_without_external_verification(self) -> None:
        created = self._record()
        order = created["order"]
        self.assertEqual(order["evidence_source"], "manual_report")
        self.assertEqual(order["lines"][0]["sku"], "SKU-1")

        listed = orders.list_orders(self.db, {"merchant_id": "m1"}, {}, _auth)
        self.assertEqual(listed["total"], 1)
        fetched = orders.get_order(self.db, "order-1", {"merchant_id": "m1"}, {}, _auth)
        self.assertEqual(fetched["order"]["record_id"], order["record_id"])
        self.assertEqual(orders.list_orders(self.db, {"merchant_id": "m2"}, {}, _auth)["total"], 0)

        with self.assertRaises(NotFoundError):
            orders.get_order(self.db, "missing", {"merchant_id": "m1"}, {}, _auth)

    def test_rejects_merchant_self_verification(self) -> None:
        with self.assertRaises(ValidationError):
            self._record(evidence_source="external_verified")

    def test_duplicate_order_updates_lines_instead_of_duplicating(self) -> None:
        first = self._record()["order"]
        second = self._record(
            lines=[
                {"line_id": "l1", "sku": "SKU-1", "quantity": 3, "amount_minor": 1500},
                {"line_id": "l2", "sku": "SKU-2", "quantity": 1, "amount_minor": 500},
            ]
        )["order"]
        self.assertEqual(second["record_id"], first["record_id"])
        self.assertEqual(len(second["lines"]), 2)
        self.assertEqual(orders.list_orders(self.db, {"merchant_id": "m1"}, {}, _auth)["total"], 1)

    def test_refund_correction_and_replay(self) -> None:
        self._record()
        first = orders.record_refund(
            self.db,
            "order-1",
            {
                "merchant_id": "m1",
                "external_refund_id": "r1",
                "amount_minor": 200,
                "version": 1,
            },
            _auth,
        )
        self.assertEqual(first["refund_total_minor"], 200)

        replay = orders.record_refund(
            self.db,
            "order-1",
            {
                "merchant_id": "m1",
                "external_refund_id": "r1",
                "amount_minor": 200,
                "version": 1,
            },
            _auth,
        )
        self.assertTrue(replay["deduplicated"])

        corrected = orders.record_refund(
            self.db,
            "order-1",
            {
                "merchant_id": "m1",
                "external_refund_id": "r1",
                "amount_minor": 150,
                "version": 2,
            },
            _auth,
        )
        self.assertEqual(corrected["refund_total_minor"], 150)

        with self.assertRaises(ConflictError):
            orders.record_refund(
                self.db,
                "order-1",
                {
                    "merchant_id": "m1",
                    "external_refund_id": "r1",
                    "amount_minor": 999,
                    "version": 2,
                },
                _auth,
            )

    def test_cost_snapshot_roundtrip_and_private_scope(self) -> None:
        saved = orders.save_cost_snapshot(
            self.db,
            {
                "merchant_id": "m1",
                "ref_kind": "order",
                "ref_id": "order-1",
                "currency": "CNY",
                "completeness": "COMPLETE",
                "costs": {"goods_cost_minor": 600},
                "missing_items": [],
                "version": 1,
            },
            _auth,
        )
        snapshot = saved["snapshot"]
        self.assertEqual(snapshot["costs"]["goods_cost_minor"], 600)
        fetched = orders.get_cost_snapshot(
            self.db, snapshot["snapshot_id"], {"merchant_id": "m1"}, {}, _auth
        )
        self.assertEqual(fetched["snapshot"]["snapshot_id"], snapshot["snapshot_id"])
        with self.assertRaises(NotFoundError):
            orders.get_cost_snapshot(
                self.db, snapshot["snapshot_id"], {"merchant_id": "m2"}, {}, _auth
            )

    def test_source_contract_revision_and_readback(self) -> None:
        first = orders.register_source_contract(
            self.db,
            {
                "merchant_id": "m1",
                "source_namespace": "jd",
                "source_account_id": "acc-1",
                "ordering": "monotonic",
                "version_scope": "payment",
                "event_id_scope": "order",
                "supports_watermark": True,
            },
            _auth,
        )
        self.assertEqual(first["contract"]["revision"], 1)
        second = orders.register_source_contract(
            self.db,
            {
                "merchant_id": "m1",
                "source_namespace": "jd",
                "source_account_id": "acc-1",
                "ordering": "unorderable",
                "version_scope": "payment",
                "event_id_scope": "order",
                "supports_watermark": False,
            },
            _auth,
        )
        self.assertEqual(second["contract"]["revision"], 2)
        read = orders.get_source_contract(
            self.db, "jd", "acc-1", {"merchant_id": "m1"}, {}, _auth
        )
        self.assertEqual(read["contract"]["ordering"], "unorderable")

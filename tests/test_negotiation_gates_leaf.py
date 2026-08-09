"""Characterization tests for the negotiation_gates leaf-module extraction.

The truly-pure negotiation gate decision strategies — the shared proposal fact
checks and the buyer non-binding gate — moved move-only from
``shopping_cli.services.negotiation`` into the leaf module
``shopping_cli.services.negotiation_gates``. The facade re-imports them under
the same private ``_``-prefixed aliases, so call order, error messages, the
state machine, the public API and the contract semantics are byte-identical.

The merchant gate stays in the facade: it reads the merchant automation
boundary floor through ``merchant_agent._authorized_bargain_amount`` — a
cross-module private access that a pure leaf must not own. These tests pin
that boundary, the identity re-exports and the unchanged decision outcomes.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shopping_cli.core import negotiation as protocol
from shopping_cli.core.catalog import create_merchant, create_product
from shopping_cli.core.conversations import ensure_conversation
from shopping_cli.core.policies import create_policy
from shopping_cli.db.session import db_session
from shopping_cli.services import negotiation as negotiation_service
from shopping_cli.services import negotiation_gates as gates

# Names that physically moved into the leaf and must be re-exported by
# ``services.negotiation`` as the *identical* objects.
MOVED_NAMES = ("check_proposal_facts", "buyer_gate")

FLOOR_BOUNDARIES = "手写陶瓷杯最低可成交价 80 元"


def rfc3339(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def make_proposal(
    *,
    sku: str = "cup-1",
    quantity: int = 2,
    unit_price: float = 89.0,
    currency: str = "CNY",
    stock_quantity: int = 12,
    stock_status: str = "available",
    observed_at: datetime | None = None,
    valid_until: datetime | None = None,
    eta_start: datetime | None = None,
    eta_end: datetime | None = None,
    policy_refs: list[str] | None = None,
) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "sku": sku,
        "quantity": quantity,
        "unit_price": unit_price,
        "currency": currency,
        "stock": {
            "status": stock_status,
            "quantity": stock_quantity,
            "observed_at": rfc3339(observed_at or now),
            "reserved": False,
        },
        "delivery": {
            "eta_start": rfc3339(eta_start or now + timedelta(hours=20)),
            "eta_end": rfc3339(eta_end or now + timedelta(hours=24)),
            "fee": 0,
        },
        "after_sales_policy_refs": policy_refs or ["policy:return-7d"],
        "valid_until": rfc3339(valid_until or now + timedelta(minutes=5)),
    }


def make_decision(
    conversation_id: str,
    message_id: int,
    *,
    action: str = "counter",
    proposal: dict | None,
    public_message: str = "如果购买 2 件，单价可调整为 89 元，明天下午送达。",
) -> dict:
    decision = {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "conversation_id": conversation_id,
        "in_reply_to_message_id": message_id,
        "action": action,
        "open_issues": [],
        "public_message": public_message,
        "reason_codes": ["within_policy"],
        "request_human_review": False,
    }
    if proposal is not None:
        decision["proposal"] = proposal
    return decision


class NegotiationGatesLeafTest(unittest.TestCase):
    """Boundary + behavior characterization for the leaf-module split."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self._tmp.name) / "marketplace.sqlite"
        with db_session(self.db_file) as conn:
            create_merchant(
                conn,
                merchant_id="seller-a",
                name="West Lake Tea",
                automation_boundaries=FLOOR_BOUNDARIES,
                delivery_eta_minutes=60,
            )
            create_merchant(conn, merchant_id="seller-b", name="Other Shop")
            create_product(
                conn,
                merchant_id="seller-a",
                sku="cup-1",
                title="手写陶瓷杯",
                price=99.0,
                stock=12,
                description="景德镇手工陶瓷杯 350ml",
            )
            create_policy(
                conn,
                merchant_id="seller-a",
                code="return-7d",
                title="签收后 7 天内支持无理由退货（不影响二次销售）。",
                body="签收后 7 天内支持无理由退货。",
            )
            conversation = ensure_conversation(conn, buyer_id="buyer-001", merchant_id="seller-a", sku="cup-1")
            self.conversation_id = conversation["id"]

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _conversation_row(self, conn: sqlite3.Connection) -> sqlite3.Row:
        return conn.execute(
            "select * from conversations where id = ?", (self.conversation_id,)
        ).fetchone()

    # -- module boundary ----------------------------------------------------

    def test_moved_helpers_live_in_leaf_module(self) -> None:
        for name in MOVED_NAMES:
            self.assertTrue(hasattr(gates, name), f"leaf module missing {name}")

    def test_negotiation_re_exports_identical_objects(self) -> None:
        for name in MOVED_NAMES:
            private_name = f"_{name}"
            self.assertTrue(
                hasattr(negotiation_service, private_name),
                f"services.negotiation no longer exposes {private_name}",
            )
            self.assertIs(
                getattr(negotiation_service, private_name),
                getattr(gates, name),
                f"{private_name} is not re-exported by identity",
            )

    def test_merchant_gate_stays_in_facade(self) -> None:
        # The merchant gate reaches merchant_agent._authorized_bargain_amount,
        # a cross-module private access; it must keep living in the facade.
        self.assertTrue(hasattr(negotiation_service, "_merchant_gate"))
        self.assertFalse(hasattr(gates, "merchant_gate"))

    # -- check_proposal_facts behavior --------------------------------------

    def test_facts_accepts_valid_proposal(self) -> None:
        with db_session(self.db_file) as conn:
            outcome = gates.check_proposal_facts(conn, self._conversation_row(conn), make_proposal())
        self.assertEqual(outcome.result, "accepted")
        self.assertEqual(outcome.reason_codes, ())
        self.assertEqual(outcome.public_reason, "")

    def test_facts_rejects_wrong_sku(self) -> None:
        with db_session(self.db_file) as conn:
            outcome = gates.check_proposal_facts(
                conn, self._conversation_row(conn), make_proposal(sku="other-sku")
            )
        self.assertEqual(outcome.result, "rejected_retryable")
        self.assertEqual(outcome.reason_codes, ("unknown_product",))
        self.assertEqual(outcome.public_reason, "磋商商品与会话商品不一致，请基于最新快照中的商品报价。")

    def test_facts_rejects_quantity_above_stock(self) -> None:
        with db_session(self.db_file) as conn:
            outcome = gates.check_proposal_facts(
                conn, self._conversation_row(conn), make_proposal(quantity=13)
            )
        self.assertEqual(outcome.reason_codes, ("insufficient_stock",))
        self.assertEqual(outcome.public_reason, "当前可售库存为 12 件，请调整数量。")

    def test_facts_rejects_stale_inventory_observation(self) -> None:
        # Server stock is 12/available; a forged observation of 11 must never
        # be written into a public structured message.
        with db_session(self.db_file) as conn:
            outcome = gates.check_proposal_facts(
                conn, self._conversation_row(conn), make_proposal(stock_quantity=11)
            )
        self.assertEqual(outcome.reason_codes, ("stale_inventory",))
        self.assertEqual(
            outcome.public_reason, "库存观察与服务端最新库存（12 件，available）不一致，请重新获取快照后再报价。"
        )

    def test_facts_rejects_expired_quote(self) -> None:
        with db_session(self.db_file) as conn:
            outcome = gates.check_proposal_facts(
                conn,
                self._conversation_row(conn),
                make_proposal(valid_until=datetime.now(timezone.utc) - timedelta(minutes=1)),
            )
        self.assertEqual(outcome.reason_codes, ("quote_expired",))
        self.assertEqual(outcome.public_reason, "报价有效期已过期，请重新获取快照后再报价。")

    def test_facts_rejects_currency_mismatch(self) -> None:
        with db_session(self.db_file) as conn:
            outcome = gates.check_proposal_facts(
                conn, self._conversation_row(conn), make_proposal(currency="USD")
            )
        self.assertEqual(outcome.reason_codes, ("currency_mismatch",))
        self.assertEqual(outcome.public_reason, "币种必须为 CNY。")

    # -- buyer_gate behavior ------------------------------------------------

    def test_buyer_gate_rejects_missing_proposal_for_counter(self) -> None:
        with db_session(self.db_file) as conn:
            decision = make_decision(self.conversation_id, 1, action="counter", proposal=None)
            outcome = negotiation_service._buyer_gate(conn, self._conversation_row(conn), decision)
        self.assertEqual(outcome.result, "rejected_retryable")
        self.assertEqual(outcome.reason_codes, ("missing_proposal",))
        self.assertEqual(outcome.public_reason, "propose/counter 必须携带结构化 proposal。")

    def test_buyer_gate_accepts_ask_without_proposal(self) -> None:
        with db_session(self.db_file) as conn:
            decision = make_decision(self.conversation_id, 1, action="ask", proposal=None)
            outcome = negotiation_service._buyer_gate(conn, self._conversation_row(conn), decision)
        self.assertEqual(outcome.result, "accepted")
        self.assertEqual(outcome.reason_codes, ())

    def test_buyer_gate_delegates_to_facts_check(self) -> None:
        with db_session(self.db_file) as conn:
            valid = negotiation_service._buyer_gate(
                conn, self._conversation_row(conn), make_decision(self.conversation_id, 1, proposal=make_proposal())
            )
            stale = negotiation_service._buyer_gate(
                conn,
                self._conversation_row(conn),
                make_decision(self.conversation_id, 1, proposal=make_proposal(stock_quantity=11)),
            )
        self.assertEqual(valid.result, "accepted")
        self.assertEqual(stale.reason_codes, ("stale_inventory",))

    # -- merchant_gate behavior (stays in facade) ---------------------------

    def test_merchant_gate_accepts_price_within_floor(self) -> None:
        with db_session(self.db_file) as conn:
            outcome = negotiation_service._merchant_gate(
                conn, self._conversation_row(conn), make_decision(self.conversation_id, 1, proposal=make_proposal())
            )
        self.assertEqual(outcome.result, "accepted")
        self.assertEqual(outcome.reason_codes, ())

    def test_merchant_gate_escalates_below_floor(self) -> None:
        with db_session(self.db_file) as conn:
            outcome = negotiation_service._merchant_gate(
                conn,
                self._conversation_row(conn),
                make_decision(
                    self.conversation_id, 1, proposal=make_proposal(unit_price=79.0)
                ),
            )
        self.assertEqual(outcome.result, "human_required")
        self.assertEqual(outcome.reason_codes, ("below_floor",))
        self.assertEqual(outcome.public_reason, "报价低于商家授权的自动磋商范围，需要人工处理。")

    def test_merchant_gate_rejects_stale_inventory_before_price_check(self) -> None:
        # The facts check runs before the floor/price gate and must return the
        # same stale_inventory outcome whether called from the leaf or facade.
        with db_session(self.db_file) as conn:
            row = self._conversation_row(conn)
            decision = make_decision(
                self.conversation_id, 1, proposal=make_proposal(stock_quantity=11)
            )
            leaf = gates.check_proposal_facts(conn, row, decision["proposal"])
            facade = negotiation_service._merchant_gate(conn, row, decision)
        self.assertEqual(leaf.reason_codes, ("stale_inventory",))
        self.assertEqual(facade.reason_codes, ("stale_inventory",))
        self.assertEqual(facade.public_reason, leaf.public_reason)


if __name__ == "__main__":
    unittest.main()

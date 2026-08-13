"""Characterization tests for the negotiation decision-message leaf.

The pure decision-message write-intent projection — sender / status /
structured_payload shaping that runs right before an accepted decision is
appended — moved move-only from ``shopping_cli.services.negotiation`` into the
leaf module ``shopping_cli.services.negotiation_message``. The facade
re-imports the used ones under the same private ``_``-prefixed aliases, so
call order, error messages, the state machine, the public API and the contract
semantics are byte-identical.

These tests pin the leaf's public surface, the facade identity re-exports, the
exact projection outputs across merchant/buyer and decline/non-decline, the
idempotency fields and protocol version carried by the structured payload, and
that ``submit_decision`` writes exactly what the leaf projects.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from shopping_cli.api.app import handle_request
from shopping_cli.core import negotiation as protocol
from shopping_cli.core.catalog import create_merchant, create_product
from shopping_cli.core.conversations import append_message, conversation_messages, ensure_conversation
from shopping_cli.core.policies import create_policy
from shopping_cli.db.session import db_session
from shopping_cli.services import negotiation as negotiation_service
from shopping_cli.services import negotiation_message as message_leaf
from shopping_cli.services import tokens as token_service

# Names that physically moved into the leaf module.
MOVED_NAMES = ("decision_sender", "decision_status", "decision_structured_payload")

# The facade must re-export these under the identical private aliases.
FACADE_RE_EXPORTS = (
    ("decision_sender", "_decision_sender"),
    ("decision_status", "_decision_status"),
    ("decision_structured_payload", "_decision_structured_payload"),
)

FLOOR_BOUNDARIES = "手写陶瓷杯最低可成交价 80 元"


def rfc3339(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def make_decision(
    conversation_id: str,
    message_id: int,
    *,
    action: str = "counter",
    unit_price: float = 89.0,
    quantity: int = 2,
    public_message: str = "如果购买 2 件，单价可调整为 89 元，明天下午送达。",
) -> dict:
    now = datetime.now(timezone.utc)
    decision: dict[str, Any] = {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "conversation_id": conversation_id,
        "in_reply_to_message_id": message_id,
        "action": action,
        "open_issues": [],
        "public_message": public_message,
        "reason_codes": ["within_policy"],
        "request_human_review": False,
    }
    if action in {"propose", "counter", "accept_nonbinding"}:
        decision["proposal"] = {
            "sku": "cup-1",
            "quantity": quantity,
            "unit_price": unit_price,
            "currency": "CNY",
            "stock": {
                "status": "available",
                "quantity": 12,
                "observed_at": rfc3339(now),
                "reserved": False,
            },
            "delivery": {
                "eta_start": rfc3339(now + timedelta(hours=20)),
                "eta_end": rfc3339(now + timedelta(hours=24)),
                "fee": 0,
            },
            "after_sales_policy_refs": ["policy:return-7d"],
            "valid_until": rfc3339(now + timedelta(minutes=5)),
        }
    return decision


# -- module boundary --------------------------------------------------------


@pytest.mark.parametrize("name", MOVED_NAMES)
def test_moved_helpers_live_in_leaf_module(name: str) -> None:
    assert hasattr(message_leaf, name), f"leaf module missing {name}"


@pytest.mark.parametrize(("public_name", "private_alias"), FACADE_RE_EXPORTS)
def test_negotiation_re_exports_identical_objects(public_name: str, private_alias: str) -> None:
    assert hasattr(negotiation_service, private_alias), f"services.negotiation no longer exposes {private_alias}"
    assert getattr(negotiation_service, private_alias) is getattr(message_leaf, public_name)


# -- decision_sender --------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("merchant", "merchant_agent"),
        ("buyer", "buyer"),
    ],
)
def test_decision_sender_maps_role(role: str, expected: str) -> None:
    assert message_leaf.decision_sender(role) == expected


# -- decision_status --------------------------------------------------------


@pytest.mark.parametrize("role", ["merchant", "buyer"])
def test_decision_status_decline_closes_for_both_roles(role: str) -> None:
    assert message_leaf.decision_status(role, "decline") == "closed"


@pytest.mark.parametrize("action", [action for action in protocol.DECISION_ACTIONS if action != "decline"])
def test_decision_status_non_decline_hands_turn_to_counterpart(action: str) -> None:
    assert message_leaf.decision_status("merchant", action) == "waiting_buyer"
    assert message_leaf.decision_status("buyer", action) == "waiting_merchant"


# -- decision_structured_payload --------------------------------------------


def test_structured_payload_carries_protocol_version_and_role() -> None:
    payload = message_leaf.decision_structured_payload(
        "shopping-cli-merchant-agent:seller-a", "merchant", {"action": "counter"}, "key-1"
    )
    assert payload["protocol_version"] == protocol.PROTOCOL_VERSION
    assert payload["role"] == "merchant"


def test_structured_payload_records_idempotency_key_and_agent_identity() -> None:
    # These are the fields _find_decision_replay matches on: an idempotent
    # retry with the same key + same agent_id returns the already-written
    # decision instead of producing a fresh write.
    payload = message_leaf.decision_structured_payload(
        "shopping-cli-buyer-agent:buyer-001", "buyer", {"action": "ask"}, "buyer:7:shopping.negotiation/0.1"
    )
    assert payload["idempotency_key"] == "buyer:7:shopping.negotiation/0.1"
    assert payload["agent_id"] == "shopping-cli-buyer-agent:buyer-001"
    assert payload["source_id"] == "shopping-cli-buyer-agent:buyer-001"


def test_structured_payload_embeds_decision_by_reference() -> None:
    decision = {"action": "decline", "public_message": "不卖"}
    payload = message_leaf.decision_structured_payload("agent-1", "merchant", decision, "key-1")
    assert payload["decision"] is decision


# -- submit_decision delegation (DB-backed) ---------------------------------


class SubmitDecisionDelegationTest(unittest.TestCase):
    """``submit_decision`` must write exactly what the leaf projects."""

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
            self.merchant_token = token_service.issue_merchant_token(conn, "seller-a")
            conversation = ensure_conversation(conn, buyer_id="buyer-001", merchant_id="seller-a", sku="cup-1")
            self.conversation_id = conversation["id"]
            self.buyer_token = token_service.issue_buyer_token(conn, "buyer-001", self.conversation_id)
            message = append_message(
                conn, self.conversation_id, "buyer", "ask_price", "买 2 件可以便宜一点吗？明天能送到吗？"
            )
            self.buyer_message_id = int(message["id"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None, query: dict | None = None, token: str = ""):
        body = dict(payload or {})
        if token:
            body["_auth_token"] = token
        return handle_request(self.db_file, method, path, body, query or {})

    def claim(self, token: str, message_id: int, key: str):
        return self.call(
            "POST",
            "/negotiation/claims",
            {
                "conversation_id": self.conversation_id,
                "message_id": message_id,
                "idempotency_key": key,
            },
            token=token,
        )

    def submit(self, token: str, decision: dict, key: str):
        return self.call(
            "POST",
            "/negotiation/decisions",
            {"idempotency_key": key, "decision": decision},
            token=token,
        )

    def _written_message(self) -> dict:
        with db_session(self.db_file) as conn:
            return conversation_messages(conn, self.conversation_id)[-1]

    def _conversation_status(self) -> str:
        with db_session(self.db_file) as conn:
            row = conn.execute("select status from conversations where id = ?", (self.conversation_id,)).fetchone()
        return str(row["status"])

    def _assert_written_matches_leaf(self, role: str, decision: dict, idempotency_key: str) -> None:
        agent_id = (
            token_service.default_merchant_agent_id("seller-a")
            if role == "merchant"
            else protocol.buyer_agent_identity("buyer-001")
        )
        written = self._written_message()
        self.assertEqual(written["sender"], message_leaf.decision_sender(role))
        self.assertEqual(
            self._conversation_status(),
            message_leaf.decision_status(role, decision["action"]),
        )
        self.assertEqual(
            written["structured_payload"],
            message_leaf.decision_structured_payload(agent_id, role, decision, idempotency_key),
        )

    def test_merchant_decision_rate_limit_per_owner(self) -> None:
        """审查 S-M2：磋商决策提交加 per-owner 固定窗口限流（此前无限流）。"""
        # 自包含 fresh merchant（限流桶按 owner_id 键，隔离其他测试）。
        with db_session(self.db_file) as conn:
            create_merchant(
                conn,
                merchant_id="seller-ratelimit",
                name="Rate Limited Tea",
                automation_boundaries=FLOOR_BOUNDARIES,
                delivery_eta_minutes=60,
            )
            create_product(
                conn,
                merchant_id="seller-ratelimit",
                sku="cup-rl",
                title="茶杯",
                price=88.0,
                stock=5,
            )
            rl_token = token_service.issue_merchant_token(conn, "seller-ratelimit")
            rl_conversation = ensure_conversation(
                conn, buyer_id="buyer-rl", merchant_id="seller-ratelimit", sku="cup-rl"
            )
            msg = append_message(
                conn, rl_conversation["id"], "buyer", "ask_price", "便宜点？"
            )
        rl_message_id = int(msg["id"])
        with patch.dict(
            os.environ, {"SHOPPING_NEGOTIATION_DECISION_RATE_LIMIT_PER_MINUTE": "1"}, clear=False
        ):
            status, payload = self.call(
                "POST",
                "/negotiation/claims",
                {
                    "conversation_id": rl_conversation["id"],
                    "message_id": rl_message_id,
                    "idempotency_key": "rl:claim",
                },
                token=rl_token,
            )
            self.assertEqual(status, 200, payload)
            status, first = self.call(
                "POST",
                "/negotiation/decisions",
                {
                    "idempotency_key": "rl:1",
                    "decision": make_decision(rl_conversation["id"], rl_message_id, action="counter"),
                },
                token=rl_token,
            )
            self.assertEqual(status, 200, first)
            # 第 2 个决策提交超出 per-owner 1/min 限流（此前无限流可任意刷）
            status, limited = self.call(
                "POST",
                "/negotiation/decisions",
                {
                    "idempotency_key": "rl:2",
                    "decision": make_decision(rl_conversation["id"], rl_message_id, action="counter"),
                },
                token=rl_token,
            )
            self.assertEqual(status, 429)
            self.assertIn("rate limit", limited["error"])

    def test_merchant_counter_writes_leaf_projected_message(self) -> None:
        decision = make_decision(self.conversation_id, self.buyer_message_id, action="counter")
        status, payload = self.claim(self.merchant_token, self.buyer_message_id, "merchant:1")
        self.assertEqual(status, 200, payload)
        status, payload = self.submit(self.merchant_token, decision, "merchant:1")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["policy_result"]["result"], "accepted")
        self._assert_written_matches_leaf("merchant", decision, "merchant:1")
        self.assertEqual(self._conversation_status(), "waiting_buyer")

    def test_buyer_counter_writes_leaf_projected_message(self) -> None:
        merchant_decision = make_decision(self.conversation_id, self.buyer_message_id, action="counter")
        status, payload = self.claim(self.merchant_token, self.buyer_message_id, "merchant:1")
        self.assertEqual(status, 200, payload)
        status, payload = self.submit(self.merchant_token, merchant_decision, "merchant:1")
        self.assertEqual(status, 200, payload)
        merchant_message_id = payload["policy_result"]["message_id"]
        self.assertIsInstance(merchant_message_id, int)

        buyer_decision = make_decision(self.conversation_id, merchant_message_id, action="counter", unit_price=88.0)
        status, payload = self.claim(self.buyer_token, merchant_message_id, "buyer:1")
        self.assertEqual(status, 200, payload)
        status, payload = self.submit(self.buyer_token, buyer_decision, "buyer:1")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["policy_result"]["result"], "accepted")
        self._assert_written_matches_leaf("buyer", buyer_decision, "buyer:1")
        self.assertEqual(self._conversation_status(), "waiting_merchant")

    def test_merchant_decline_writes_closed_leaf_projected_message(self) -> None:
        merchant_decision = make_decision(self.conversation_id, self.buyer_message_id, action="counter")
        status, payload = self.claim(self.merchant_token, self.buyer_message_id, "merchant:1")
        self.assertEqual(status, 200, payload)
        status, payload = self.submit(self.merchant_token, merchant_decision, "merchant:1")
        self.assertEqual(status, 200, payload)
        merchant_message_id = payload["policy_result"]["message_id"]

        buyer_decision = make_decision(self.conversation_id, merchant_message_id, action="counter", unit_price=88.0)
        status, payload = self.claim(self.buyer_token, merchant_message_id, "buyer:1")
        self.assertEqual(status, 200, payload)
        status, payload = self.submit(self.buyer_token, buyer_decision, "buyer:1")
        self.assertEqual(status, 200, payload)
        buyer_message_id = payload["policy_result"]["message_id"]

        decline = make_decision(self.conversation_id, buyer_message_id, action="decline", public_message="这个价格接受不了。")
        status, payload = self.claim(self.merchant_token, buyer_message_id, "merchant:2")
        self.assertEqual(status, 200, payload)
        status, payload = self.submit(self.merchant_token, decline, "merchant:2")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["policy_result"]["result"], "accepted")
        self._assert_written_matches_leaf("merchant", decline, "merchant:2")
        self.assertEqual(self._conversation_status(), "closed")


if __name__ == "__main__":
    unittest.main()

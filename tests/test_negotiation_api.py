"""End-to-end tests for the shopping.negotiation/0.1 Commerce API.

Tests go through app.handle_request (the fallback ASGI dispatch path shared
with the FastAPI stack) so route registration, the error envelope and the
policy gate are all exercised. Both roles are covered: merchant and buyer.
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shopping_cli.api.app import handle_request
from shopping_cli.core import negotiation as protocol
from shopping_cli.core.catalog import create_merchant, create_product
from shopping_cli.core.conversations import append_message, conversation_messages, ensure_conversation
from shopping_cli.core.policies import create_policy
from shopping_cli.db.session import db_session
from shopping_cli.services import tokens as token_service

FLOOR_BOUNDARIES = "手写陶瓷杯最低可成交价 80 元"
NO_BOUNDARIES = ""


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
    observed_at: str | None = None,
    valid_until: str | None = None,
    request_human_review: bool = False,
    with_proposal: bool = True,
) -> dict:
    now = datetime.now(timezone.utc)
    decision = {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "conversation_id": conversation_id,
        "in_reply_to_message_id": message_id,
        "action": action,
        "open_issues": [],
        "public_message": public_message,
        "reason_codes": ["within_policy"],
        "request_human_review": request_human_review,
    }
    if with_proposal:
        decision["proposal"] = {
            "sku": "cup-1",
            "quantity": quantity,
            "unit_price": unit_price,
            "currency": "CNY",
            "stock": {
                "status": "available",
                "quantity": 12,
                "observed_at": observed_at or rfc3339(now),
                "reserved": False,
            },
            "delivery": {
                "eta_start": rfc3339(now + timedelta(hours=20)),
                "eta_end": rfc3339(now + timedelta(hours=24)),
                "fee": 0,
            },
            "after_sales_policy_refs": ["policy:return-7d"],
            "valid_until": valid_until or rfc3339(now + timedelta(minutes=5)),
        }
    return decision


class NegotiationApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self._tmp.name) / "marketplace.sqlite"
        self.seed(boundaries=FLOOR_BOUNDARIES)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def seed(self, boundaries: str = FLOOR_BOUNDARIES, stock: int = 12) -> None:
        with db_session(self.db_file) as conn:
            create_merchant(
                conn,
                merchant_id="seller-a",
                name="West Lake Tea",
                automation_boundaries=boundaries,
                delivery_eta_minutes=60,
            )
            create_merchant(conn, merchant_id="seller-b", name="Other Shop")
            create_product(
                conn,
                merchant_id="seller-a",
                sku="cup-1",
                title="手写陶瓷杯",
                price=99.0,
                stock=stock,
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
            self.other_merchant_token = token_service.issue_merchant_token(conn, "seller-b")
            conversation = ensure_conversation(conn, buyer_id="buyer-001", merchant_id="seller-a", sku="cup-1")
            self.conversation_id = conversation["id"]
            self.buyer_token = token_service.issue_buyer_token(conn, "buyer-001", self.conversation_id)
            other = ensure_conversation(conn, buyer_id="buyer-002", merchant_id="seller-a", sku="cup-1")
            self.other_buyer_token = token_service.issue_buyer_token(conn, "buyer-002", other["id"])
            message = append_message(
                conn, self.conversation_id, "buyer", "ask_price", "买 2 件可以便宜一点吗？明天能送到吗？"
            )
            self.buyer_message_id = int(message["id"])

    # -- helpers -----------------------------------------------------------

    def call(self, method: str, path: str, payload: dict | None = None, query: dict | None = None, token: str = ""):
        body = dict(payload or {})
        if token:
            body["_auth_token"] = token
        return handle_request(self.db_file, method, path, body, query or {})

    def claim(self, token: str, message_id: int | None = None, key: str = "agent-1:1:shopping.negotiation/0.1"):
        return self.call(
            "POST",
            "/negotiation/claims",
            {
                "conversation_id": self.conversation_id,
                "message_id": message_id if message_id is not None else self.buyer_message_id,
                "idempotency_key": key,
            },
            token=token,
        )

    def snapshot(self, token: str, message_id: int | None = None):
        return self.call(
            "GET",
            "/negotiation/snapshot",
            query={
                "conversation_id": self.conversation_id,
                "message_id": str(message_id if message_id is not None else self.buyer_message_id),
            },
            token=token,
        )

    def submit(self, token: str, decision: dict, key: str = "agent-1:1:shopping.negotiation/0.1"):
        return self.call(
            "POST",
            "/negotiation/decisions",
            {"idempotency_key": key, "decision": decision},
            token=token,
        )

    def merchant_accept(self, key: str = "agent-1:1:shopping.negotiation/0.1", **overrides):
        status, payload = self.claim(self.merchant_token)
        assert status == 200 and payload["claim"]["claimed"] is True, payload
        decision = make_decision(self.conversation_id, self.buyer_message_id, **overrides)
        return self.submit(self.merchant_token, decision, key=key)

    def message_count(self) -> int:
        with db_session(self.db_file) as conn:
            return len(conversation_messages(conn, self.conversation_id))

    # -- capabilities ------------------------------------------------------

    def test_capabilities_advertises_protocol_and_no_orders(self):
        status, payload = self.call("GET", "/capabilities")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        report = payload["capabilities"]
        protocol.validate_contract("capabilities", report)
        self.assertIn("shopping.negotiation/0.1", report["protocol_versions"])
        self.assertEqual(report["backend"], "local_marketplace")
        self.assertFalse(report["capabilities"]["orders"])

    # -- pending messages --------------------------------------------------

    def test_merchant_sees_pending_buyer_message(self):
        status, payload = self.call("GET", "/negotiation/pending-messages", token=self.merchant_token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["role"], "merchant")
        self.assertEqual(len(payload["pending"]), 1)
        entry = payload["pending"][0]
        self.assertEqual(entry["conversation_id"], self.conversation_id)
        self.assertEqual(entry["message_id"], self.buyer_message_id)
        self.assertEqual(entry["sender_role"], "buyer")
        self.assertEqual(entry["conversation_status"], "waiting_merchant")

    def test_buyer_has_no_pending_when_waiting_merchant(self):
        status, payload = self.call("GET", "/negotiation/pending-messages", token=self.buyer_token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["role"], "buyer")
        self.assertEqual(payload["pending"], [])

    def test_pending_requires_token_fail_closed(self):
        status, payload = self.call("GET", "/negotiation/pending-messages")
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])

    def test_pending_after_claim_hides_processing_message(self):
        self.claim(self.merchant_token)
        status, payload = self.call("GET", "/negotiation/pending-messages", token=self.merchant_token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["pending"], [])

    def test_other_merchant_sees_no_pending(self):
        status, payload = self.call("GET", "/negotiation/pending-messages", token=self.other_merchant_token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["pending"], [])

    # -- claim --------------------------------------------------------------

    def test_claim_and_reclaim_idempotent(self):
        status, payload = self.claim(self.merchant_token)
        self.assertEqual(status, 200)
        self.assertTrue(payload["claim"]["claimed"])
        status, payload = self.claim(self.merchant_token)
        self.assertEqual(status, 200)
        self.assertFalse(payload["claim"]["claimed"])
        self.assertEqual(payload["claim"]["status"], "processing")

    def test_buyer_cannot_claim_buyer_message(self):
        status, _payload = self.claim(self.buyer_token)
        self.assertEqual(status, 409)  # conversation waits for merchant, not buyer

    def test_other_merchant_cannot_claim(self):
        status, _payload = self.claim(self.other_merchant_token)
        self.assertEqual(status, 403)

    def test_claim_without_token_fails_closed(self):
        status, _payload = self.call(
            "POST",
            "/negotiation/claims",
            {
                "conversation_id": self.conversation_id,
                "message_id": self.buyer_message_id,
                "idempotency_key": "k",
            },
        )
        self.assertEqual(status, 403)

    def test_client_declared_merchant_id_does_not_escalate_buyer(self):
        status, _payload = self.call(
            "POST",
            "/negotiation/claims",
            {
                "merchant_id": "seller-a",
                "role": "merchant",
                "conversation_id": self.conversation_id,
                "message_id": self.buyer_message_id,
                "idempotency_key": "k",
            },
            token=self.buyer_token,
        )
        self.assertEqual(status, 409)  # still a buyer: not the buyer's turn, no escalation

    def test_concurrent_claims_exactly_one_wins(self):
        results: list[bool] = []

        def worker(index: int) -> None:
            status, payload = self.claim(self.merchant_token, key=f"agent-{index}:1:v1")
            results.append(status == 200 and payload["claim"]["claimed"])

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [False, False, False, False, False, True])

    # -- snapshot -----------------------------------------------------------

    def test_merchant_snapshot_is_schema_valid_and_role_trimmed(self):
        self.claim(self.merchant_token)
        status, payload = self.snapshot(self.merchant_token)
        self.assertEqual(status, 200)
        snapshot = payload["snapshot"]
        protocol.validate_contract("snapshot", snapshot)
        self.assertEqual(snapshot["role"], "merchant")
        self.assertEqual(snapshot["conversation"]["next_actor"], "merchant")
        self.assertEqual(snapshot["in_reply_to_message_id"], self.buyer_message_id)
        self.assertEqual(snapshot["product"]["sku"], "cup-1")
        self.assertEqual(snapshot["product"]["list_price"], 99.0)
        self.assertEqual(snapshot["stock"]["quantity"], 12)
        self.assertFalse(snapshot["stock"]["reserved"])
        self.assertEqual(snapshot["stock"]["source"]["backend"], "local_marketplace")
        self.assertEqual(snapshot["after_sales_policies"][0]["ref"], "policy:return-7d")
        self.assertEqual(snapshot["messages"][-1]["sender_role"], "buyer")
        raw = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("automation_boundaries", raw)
        self.assertNotIn("最低可成交价", raw)

    def test_snapshot_requires_active_claim(self):
        status, _payload = self.snapshot(self.merchant_token)
        self.assertEqual(status, 409)

    def test_snapshot_message_timestamps_carry_timezone_offset(self):
        # DB rows store naive local time; snapshot must re-emit explicit-offset
        # RFC 3339 so the strict (Ajv-compatible) contract validator accepts it.
        status, payload = self.merchant_accept()
        self.assertEqual(status, 200)
        merchant_message_id = payload["policy_result"]["message_id"]
        status, payload = self.claim(self.buyer_token, message_id=merchant_message_id, key="buyer-agent:2:v1")
        self.assertEqual(status, 200)
        status, payload = self.snapshot(self.buyer_token, message_id=merchant_message_id)
        self.assertEqual(status, 200)
        snapshot = payload["snapshot"]
        protocol.validate_contract("snapshot", snapshot)  # strict date-time validation
        self.assertEqual(len(snapshot["messages"]), 2)
        for message in snapshot["messages"]:
            self.assertRegex(message["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(Z|[+-]\d\d:\d\d)$")

    def test_snapshot_fail_closed_for_other_identities(self):
        self.claim(self.merchant_token)
        status, _payload = self.snapshot(self.other_merchant_token)
        self.assertEqual(status, 403)
        status, _payload = self.snapshot(self.other_buyer_token)
        self.assertEqual(status, 403)

    def test_snapshot_rejects_wrong_turn(self):
        # Buyer token while conversation waits for merchant: no own turn.
        status, _payload = self.snapshot(self.buyer_token)
        self.assertEqual(status, 409)

    # -- decision: happy path ----------------------------------------------

    def test_merchant_counter_accepted_writes_once_and_advances(self):
        status, payload = self.merchant_accept()
        self.assertEqual(status, 200)
        result = payload["policy_result"]
        protocol.validate_contract("policy-result", result)
        self.assertEqual(result["result"], "accepted")
        self.assertEqual(result["next_actor"], "buyer")
        self.assertIn("message_id", result)
        with db_session(self.db_file) as conn:
            messages = conversation_messages(conn, self.conversation_id)
            self.assertEqual(len(messages), 2)
            written = messages[-1]
            self.assertEqual(written["sender"], "merchant_agent")
            self.assertEqual(written["intent"], "negotiate")
            structured = written["structured_payload"]
            self.assertEqual(structured["protocol_version"], protocol.PROTOCOL_VERSION)
            self.assertEqual(structured["decision"]["action"], "counter")
            row = conn.execute(
                "select status, next_actor from conversations where id = ?", (self.conversation_id,)
            ).fetchone()
            self.assertEqual(row["status"], "waiting_buyer")
            events = {
                row["event"]
                for row in conn.execute(
                    "select event from audit_events where conversation_id = ?", (self.conversation_id,)
                ).fetchall()
            }
            self.assertIn("negotiation_decision_submitted", events)
            self.assertIn("negotiation_policy_accepted", events)

    def test_idempotent_replay_does_not_duplicate_message(self):
        # Lost-response retry: same key, same decision, no fresh claim needed.
        status, payload = self.claim(self.merchant_token)
        self.assertEqual(status, 200)
        decision = make_decision(self.conversation_id, self.buyer_message_id)
        status, first = self.submit(self.merchant_token, decision)
        self.assertEqual(status, 200)
        self.assertEqual(first["policy_result"]["result"], "accepted")
        count = self.message_count()
        status, second = self.submit(self.merchant_token, decision)
        self.assertEqual(status, 200)
        self.assertEqual(second["policy_result"]["result"], "accepted")
        self.assertEqual(second["policy_result"]["message_id"], first["policy_result"]["message_id"])
        self.assertEqual(self.message_count(), count)

    def test_conflicting_payload_same_idempotency_key_fails_closed(self):
        status, payload = self.claim(self.merchant_token)
        self.assertEqual(status, 200)
        decision = make_decision(self.conversation_id, self.buyer_message_id)
        status, _payload = self.submit(self.merchant_token, decision)
        self.assertEqual(status, 200)
        mutated = make_decision(self.conversation_id, self.buyer_message_id, unit_price=85.0)
        status, _payload = self.submit(self.merchant_token, mutated)
        self.assertEqual(status, 409)
        self.assertEqual(self.message_count(), 2)

    def test_replay_is_scoped_to_actor_and_conversation(self):
        # Idempotent replay only returns an already-written decision of the
        # same actor + conversation; other tokens/owners can never read the
        # accepted result by reusing the key.
        status, payload = self.claim(self.merchant_token)
        self.assertEqual(status, 200)
        decision = make_decision(self.conversation_id, self.buyer_message_id)
        status, first = self.submit(self.merchant_token, decision)
        self.assertEqual(status, 200)
        self.assertEqual(first["policy_result"]["result"], "accepted")
        count = self.message_count()
        # A different merchant reusing the key cannot even see the conversation.
        status, _payload = self.submit(self.other_merchant_token, decision)
        self.assertEqual(status, 403)
        # The buyer of the same conversation has a different agent identity:
        # no replay hit, and the decision fails the normal checks instead.
        status, payload = self.submit(self.buyer_token, decision)
        self.assertEqual(status, 400)  # in_reply_to is the buyer's own message
        self.assertNotIn("policy_result", payload)
        self.assertEqual(self.message_count(), count)  # replay produced no write

    # -- decision: policy gate ----------------------------------------------

    def test_below_floor_requires_human(self):
        status, payload = self.merchant_accept(unit_price=75.0)
        self.assertEqual(status, 200)
        result = payload["policy_result"]
        protocol.validate_contract("policy-result", result)
        self.assertEqual(result["result"], "human_required")
        self.assertEqual(result["reason_codes"], ["below_floor"])
        self.assertEqual(result["next_actor"], "none")
        self.assertNotIn("80", result["public_reason"])
        with db_session(self.db_file) as conn:
            row = conn.execute("select status from conversations where id = ?", (self.conversation_id,)).fetchone()
            self.assertEqual(row["status"], "human_required")
        self.assertEqual(self.message_count(), 1)  # no message written

    def test_unit_price_at_floor_with_plain_quote_is_accepted(self):
        # Exactly the authorized floor (80), quoted as a normal price with no
        # threshold semantics: not a leak, not below the floor -> accepted.
        status, payload = self.merchant_accept(
            unit_price=80.0,
            public_message="如果购买 2 件，单价可调整为 80 元，明天下午送达。",
        )
        self.assertEqual(status, 200)
        result = payload["policy_result"]
        self.assertEqual(result["result"], "accepted")
        self.assertEqual(self.message_count(), 2)

    def test_floor_price_with_threshold_semantics_requires_human(self):
        # Same number, but framed as 底价/最低价: semantic + numeric leak.
        status, payload = self.merchant_accept(
            unit_price=80.0,
            public_message="这是底价 80 元，最低价了。",
        )
        self.assertEqual(status, 200)
        result = payload["policy_result"]
        self.assertEqual(result["result"], "human_required")
        self.assertEqual(result["reason_codes"], ["private_threshold_leak"])
        self.assertEqual(self.message_count(), 1)  # no message written

    def test_public_message_leaking_floor_requires_human(self):
        status, payload = self.merchant_accept(
            unit_price=85.0,
            public_message="最低可成交价 80 元，给你 85 元。",
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["policy_result"]["result"], "human_required")
        self.assertEqual(payload["policy_result"]["reason_codes"], ["private_threshold_leak"])

    def test_unauthorized_discount_requires_human_without_boundaries(self):
        with db_session(self.db_file) as conn:
            conn.execute("update merchants set automation_boundaries = '' where id = 'seller-a'")
        status, payload = self.merchant_accept(unit_price=89.0)
        self.assertEqual(status, 200)
        self.assertEqual(payload["policy_result"]["result"], "human_required")
        self.assertEqual(payload["policy_result"]["reason_codes"], ["unauthorized_discount"])

    def test_structured_floor_price_triggers_below_floor(self):
        # v26：结构化 floor_price（非 automation_boundaries）同样触发 below_floor。
        with db_session(self.db_file) as conn:
            conn.execute("update products set floor_price = 80 where sku = 'cup-1'")
            conn.execute("update merchants set automation_boundaries = '' where id = 'seller-a'")
        status, payload = self.merchant_accept(unit_price=75.0)
        self.assertEqual(status, 200)
        self.assertEqual(payload["policy_result"]["result"], "human_required")
        self.assertEqual(payload["policy_result"]["reason_codes"], ["below_floor"])

    def test_structured_floor_at_boundary_is_accepted(self):
        # v26：结构化 floor_price=80，报价恰为 80（无阈值语义）→ 接受。
        with db_session(self.db_file) as conn:
            conn.execute("update products set floor_price = 80 where sku = 'cup-1'")
            conn.execute("update merchants set automation_boundaries = '' where id = 'seller-a'")
        status, payload = self.merchant_accept(unit_price=80.0, public_message="单价 80 元。")
        self.assertEqual(status, 200)
        self.assertEqual(payload["policy_result"]["result"], "accepted")

    def test_structured_max_discount_percent_triggers_below_floor(self):
        # v26：max_discount_percent=10 → 有效底价 99*(1-0.1)=89.1；报价 89 低于它 → below_floor。
        with db_session(self.db_file) as conn:
            conn.execute("update products set max_discount_percent = 10 where sku = 'cup-1'")
            conn.execute("update merchants set automation_boundaries = '' where id = 'seller-a'")
        status, payload = self.merchant_accept(unit_price=89.0)
        self.assertEqual(status, 200)
        self.assertEqual(payload["policy_result"]["result"], "human_required")
        self.assertEqual(payload["policy_result"]["reason_codes"], ["below_floor"])

    def test_insufficient_stock_is_retryable(self):
        status, payload = self.merchant_accept(quantity=99)
        self.assertEqual(status, 200)
        result = payload["policy_result"]
        self.assertEqual(result["result"], "rejected_retryable")
        self.assertEqual(result["reason_codes"], ["insufficient_stock"])
        self.assertEqual(result["next_actor"], "merchant")
        self.assertEqual(result["retries_remaining"], 2)
        self.assertEqual(self.message_count(), 1)
        with db_session(self.db_file) as conn:
            events = [
                row["event"]
                for row in conn.execute(
                    "select event from audit_events where conversation_id = ?", (self.conversation_id,)
                ).fetchall()
            ]
            self.assertIn("negotiation_policy_denied", events)

    def test_stale_inventory_observation_is_retryable(self):
        stale = rfc3339(datetime.now(timezone.utc) - timedelta(hours=2))
        status, payload = self.merchant_accept(observed_at=stale)
        self.assertEqual(status, 200)
        self.assertEqual(payload["policy_result"]["result"], "rejected_retryable")
        self.assertEqual(payload["policy_result"]["reason_codes"], ["stale_inventory"])

    def test_forged_stock_quantity_is_rejected_without_write(self):
        # Proposal claims a stock observation that does not match the
        # authoritative server-side stock (12): rejected, no message written.
        status, payload = self.claim(self.merchant_token)
        self.assertEqual(status, 200)
        decision = make_decision(self.conversation_id, self.buyer_message_id)
        decision["proposal"]["stock"]["quantity"] = 7
        status, payload = self.submit(self.merchant_token, decision)
        self.assertEqual(status, 200)
        result = payload["policy_result"]
        self.assertEqual(result["result"], "rejected_retryable")
        self.assertEqual(result["reason_codes"], ["stale_inventory"])
        self.assertEqual(self.message_count(), 1)

    def test_forged_stock_status_is_rejected_without_write(self):
        status, payload = self.claim(self.merchant_token)
        self.assertEqual(status, 200)
        decision = make_decision(self.conversation_id, self.buyer_message_id)
        decision["proposal"]["stock"]["status"] = "low"  # server maps 12 -> available
        status, payload = self.submit(self.merchant_token, decision)
        self.assertEqual(status, 200)
        result = payload["policy_result"]
        self.assertEqual(result["result"], "rejected_retryable")
        self.assertEqual(result["reason_codes"], ["stale_inventory"])
        self.assertEqual(self.message_count(), 1)

    def test_proposal_stale_after_live_stock_change_is_rejected(self):
        # Stock changed (12 -> 5) after the agent's snapshot: the old
        # observation must be rejected even though the purchase quantity
        # itself (2) would still be feasible.
        with db_session(self.db_file) as conn:
            conn.execute("update products set stock = 5 where sku = 'cup-1'")
        status, payload = self.claim(self.merchant_token)
        self.assertEqual(status, 200)
        decision = make_decision(self.conversation_id, self.buyer_message_id)
        status, payload = self.submit(self.merchant_token, decision)
        self.assertEqual(status, 200)
        result = payload["policy_result"]
        self.assertEqual(result["result"], "rejected_retryable")
        self.assertEqual(result["reason_codes"], ["stale_inventory"])
        self.assertEqual(self.message_count(), 1)
        # A fresh observation matching the new stock is accepted.
        decision["proposal"]["stock"]["quantity"] = 5
        status, payload = self.submit(self.merchant_token, decision, key="agent-1:1:retry")
        self.assertEqual(status, 200)
        self.assertEqual(payload["policy_result"]["result"], "accepted")

    def test_buyer_proposal_stock_observation_also_enforced(self):
        # Buyer proposals are non-binding intent, but an accepted one is still
        # written into the public structured message, so the same stock
        # observation consistency is enforced for the buyer role.
        status, payload = self.merchant_accept()
        self.assertEqual(status, 200)
        merchant_message_id = payload["policy_result"]["message_id"]
        status, payload = self.claim(self.buyer_token, message_id=merchant_message_id, key="buyer-agent:2:v1")
        self.assertEqual(status, 200)
        decision = make_decision(
            self.conversation_id,
            merchant_message_id,
            action="propose",
            public_message="我按 89 元每件要 2 件（非约束性意向）。",
        )
        decision["proposal"]["stock"]["quantity"] = 99
        status, payload = self.submit(self.buyer_token, decision, key="buyer-agent:2:v1")
        self.assertEqual(status, 200)
        result = payload["policy_result"]
        self.assertEqual(result["result"], "rejected_retryable")
        self.assertEqual(result["reason_codes"], ["stale_inventory"])
        self.assertEqual(self.message_count(), 2)

    def test_naive_timestamp_rejected_at_schema_stage(self):
        status, payload = self.claim(self.merchant_token)
        self.assertEqual(status, 200)
        for field, value in (
            ("valid_until", "2026-08-04T00:37:20"),
            ("observed_at", "2026-08-04T00:37:20"),
        ):
            decision = make_decision(self.conversation_id, self.buyer_message_id)
            if field == "valid_until":
                decision["proposal"]["valid_until"] = value
            else:
                decision["proposal"]["stock"]["observed_at"] = value
            status, payload = self.submit(self.merchant_token, decision)
            self.assertEqual(status, 400, field)
            self.assertFalse(payload["ok"])
        self.assertEqual(self.message_count(), 1)

    def test_expired_quote_is_retryable(self):
        expired = rfc3339(datetime.now(timezone.utc) - timedelta(minutes=1))
        status, payload = self.merchant_accept(valid_until=expired)
        self.assertEqual(status, 200)
        self.assertEqual(payload["policy_result"]["reason_codes"], ["quote_expired"])

    def test_unknown_policy_ref_is_retryable(self):
        status, payload = self.claim(self.merchant_token)
        self.assertEqual(status, 200)
        decision = make_decision(self.conversation_id, self.buyer_message_id)
        decision["proposal"]["after_sales_policy_refs"] = ["policy:lifetime-warranty"]
        status, payload = self.submit(self.merchant_token, decision)
        self.assertEqual(status, 200)
        self.assertEqual(payload["policy_result"]["reason_codes"], ["unknown_policy_ref"])

    def test_missing_proposal_for_counter_is_retryable(self):
        status, payload = self.merchant_accept(with_proposal=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["policy_result"]["result"], "rejected_retryable")
        self.assertEqual(payload["policy_result"]["reason_codes"], ["missing_proposal"])

    def test_reserved_true_fails_schema_validation(self):
        status, payload = self.claim(self.merchant_token)
        self.assertEqual(status, 200)
        decision = make_decision(self.conversation_id, self.buyer_message_id)
        decision["proposal"]["stock"]["reserved"] = True
        status, _payload = self.submit(self.merchant_token, decision)
        self.assertEqual(status, 400)

    def test_extra_property_fails_schema_validation(self):
        status, payload = self.claim(self.merchant_token)
        self.assertEqual(status, 200)
        decision = make_decision(self.conversation_id, self.buyer_message_id)
        decision["private_floor_price"] = 80.0
        status, _payload = self.submit(self.merchant_token, decision)
        self.assertEqual(status, 400)

    def test_wrong_protocol_version_fails(self):
        status, payload = self.claim(self.merchant_token)
        self.assertEqual(status, 200)
        decision = make_decision(self.conversation_id, self.buyer_message_id)
        decision["protocol_version"] = "shopping.negotiation/0.2"
        status, _payload = self.submit(self.merchant_token, decision)
        self.assertEqual(status, 400)

    def test_decision_without_claim_fails_closed(self):
        decision = make_decision(self.conversation_id, self.buyer_message_id)
        status, _payload = self.submit(self.merchant_token, decision)
        self.assertEqual(status, 409)

    def test_decision_reply_to_wrong_message_fails(self):
        self.claim(self.merchant_token)
        decision = make_decision(self.conversation_id, self.buyer_message_id + 100)
        status, _payload = self.submit(self.merchant_token, decision)
        self.assertEqual(status, 404)

    def test_escalate_requires_human_and_flags_conversation(self):
        status, payload = self.merchant_accept(action="escalate", with_proposal=False, request_human_review=True)
        self.assertEqual(status, 200)
        result = payload["policy_result"]
        self.assertEqual(result["result"], "human_required")
        self.assertIn("agent_requested_human_review", result["reason_codes"])
        with db_session(self.db_file) as conn:
            row = conn.execute("select status from conversations where id = ?", (self.conversation_id,)).fetchone()
            self.assertEqual(row["status"], "human_required")
            flag = conn.execute(
                "select reason from moderation_flags where conversation_id = ?", (self.conversation_id,)
            ).fetchone()
            self.assertIsNotNone(flag)
        self.assertEqual(self.message_count(), 1)

    def test_decline_closes_conversation_and_revokes_buyer_token(self):
        status, payload = self.merchant_accept(
            action="decline", with_proposal=False, public_message="抱歉，无法继续磋商。"
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["policy_result"]["result"], "accepted")
        self.assertEqual(payload["policy_result"]["next_actor"], "none")
        with db_session(self.db_file) as conn:
            row = conn.execute("select status from conversations where id = ?", (self.conversation_id,)).fetchone()
            self.assertEqual(row["status"], "closed")
            token_row = conn.execute(
                "select revoked_at from api_tokens where buyer_id = 'buyer-001' and conversation_id = ?",
                (self.conversation_id,),
            ).fetchone()
            self.assertTrue(token_row["revoked_at"])

    # -- claim lifecycle ------------------------------------------------------

    def test_complete_fail_abandon_lifecycle(self):
        self.claim(self.merchant_token)
        status, payload = self.call(
            "POST", "/negotiation/claims/complete", {"message_id": self.buyer_message_id}, token=self.merchant_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["process"]["status"], "processed")
        # completed claims are not retryable through complete again——
        # 已 settle 的 claim 再操作返回 409（不再静默报成功）
        status, payload = self.call(
            "POST", "/negotiation/claims/fail", {"message_id": self.buyer_message_id}, token=self.merchant_token
        )
        self.assertEqual(status, 409)
        self.assertIn("claim already settled", payload["error"])

    def test_fail_then_abandon(self):
        self.claim(self.merchant_token)
        status, payload = self.call(
            "POST",
            "/negotiation/claims/fail",
            {"message_id": self.buyer_message_id, "error": "model timeout"},
            token=self.merchant_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["process"]["status"], "failed")
        status, payload = self.claim(self.merchant_token, key="retry-key")
        self.assertEqual(status, 200)
        self.assertTrue(payload["claim"]["claimed"])
        self.assertEqual(payload["claim"]["attempts"], 2)
        status, payload = self.call(
            "POST",
            "/negotiation/claims/abandon",
            {"message_id": self.buyer_message_id, "error": "shutdown"},
            token=self.merchant_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["process"]["status"], "abandoned")

    def test_other_merchant_cannot_complete_claim(self):
        self.claim(self.merchant_token)
        status, _payload = self.call(
            "POST",
            "/negotiation/claims/complete",
            {"message_id": self.buyer_message_id},
            token=self.other_merchant_token,
        )
        self.assertEqual(status, 404)  # different agent identity: no such process

    # -- heartbeat and stale recovery (M3) --------------------------------

    def _merchant_agent_id(self) -> str:
        return token_service.default_merchant_agent_id("seller-a")

    def _buyer_agent_id(self) -> str:
        return protocol.buyer_agent_identity("buyer-001")

    def _claim_updated_at(self, agent_id: str, message_id: int) -> str:
        with db_session(self.db_file) as conn:
            row = conn.execute(
                "select updated_at from agent_message_processes where agent_id = ? and message_id = ?",
                (agent_id, message_id),
            ).fetchone()
            return str(row["updated_at"])

    def _claim_status(self, agent_id: str, message_id: int) -> str:
        with db_session(self.db_file) as conn:
            row = conn.execute(
                "select status from agent_message_processes where agent_id = ? and message_id = ?",
                (agent_id, message_id),
            ).fetchone()
            return str(row["status"])

    def _backdate_claim(self, agent_id: str, message_id: int, seconds: float) -> None:
        old = (datetime.now().replace(microsecond=0) - timedelta(seconds=seconds)).isoformat()
        with db_session(self.db_file) as conn:
            conn.execute(
                "update agent_message_processes set updated_at = ? where agent_id = ? and message_id = ?",
                (old, agent_id, message_id),
            )

    def _audit_events(self, event: str) -> list[dict]:
        with db_session(self.db_file) as conn:
            rows = conn.execute("select details_json from audit_events where event = ?", (event,)).fetchall()
            return [json.loads(row["details_json"]) for row in rows]

    def test_heartbeat_refreshes_only_own_processing_claim(self):
        self.claim(self.merchant_token)
        before = self._claim_updated_at(self._merchant_agent_id(), self.buyer_message_id)
        status, payload = self.call(
            "POST",
            "/negotiation/claims/heartbeat",
            {"message_id": self.buyer_message_id},
            token=self.merchant_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["heartbeat"]["status"], "ok")
        self.assertEqual(payload["heartbeat"]["refreshed"], 1)
        self.assertIsInstance(payload["heartbeat"]["at"], str)
        self.assertNotIn("token", json.dumps(payload).lower())
        # Minimal, non-secret surface only.
        self.assertEqual(set(payload["heartbeat"]), {"status", "refreshed", "at"})
        # Settled claims are never refreshed.
        self.call(
            "POST", "/negotiation/claims/complete", {"message_id": self.buyer_message_id}, token=self.merchant_token
        )
        status, payload = self.call(
            "POST",
            "/negotiation/claims/heartbeat",
            {"message_id": self.buyer_message_id},
            token=self.merchant_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["heartbeat"]["refreshed"], 0)
        after = self._claim_updated_at(self._merchant_agent_id(), self.buyer_message_id)
        self.assertGreaterEqual(after, before)
        # Audited.
        events = self._audit_events("agent_message_heartbeat")
        self.assertTrue(any(e.get("message_id") == self.buyer_message_id for e in events))

    def test_heartbeat_all_processing_claims_scoped_to_actor(self):
        self.claim(self.merchant_token)
        status, payload = self.call("POST", "/negotiation/claims/heartbeat", {}, token=self.merchant_token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["heartbeat"]["refreshed"], 1)
        # Another identity has nothing to refresh.
        status, payload = self.call("POST", "/negotiation/claims/heartbeat", {}, token=self.other_buyer_token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["heartbeat"]["refreshed"], 0)

    def test_heartbeat_requires_token_fail_closed(self):
        status, payload = self.call("POST", "/negotiation/claims/heartbeat", {"message_id": self.buyer_message_id})
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])
        status, payload = self.call("POST", "/negotiation/claims/abandon-stale", {})
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])

    def test_heartbeat_cannot_touch_other_identities(self):
        self.claim(self.merchant_token)
        # Other merchant: different agent identity -> no such process.
        status, _ = self.call(
            "POST",
            "/negotiation/claims/heartbeat",
            {"message_id": self.buyer_message_id},
            token=self.other_merchant_token,
        )
        self.assertEqual(status, 404)
        # Buyer token: the merchant's claim is not the buyer's process.
        status, _ = self.call(
            "POST",
            "/negotiation/claims/heartbeat",
            {"message_id": self.buyer_message_id},
            token=self.buyer_token,
        )
        self.assertEqual(status, 404)
        self.assertEqual(self._claim_status(self._merchant_agent_id(), self.buyer_message_id), "processing")

    def test_abandon_stale_recovers_only_own_claims_and_allows_reclaim(self):
        self.claim(self.merchant_token)
        self._backdate_claim(self._merchant_agent_id(), self.buyer_message_id, 400)
        # Other identities cannot abandon the merchant's stale claim.
        for token in (self.other_merchant_token, self.other_buyer_token, self.buyer_token):
            status, payload = self.call("POST", "/negotiation/claims/abandon-stale", {}, token=token)
            self.assertEqual(status, 200)
            self.assertEqual(payload["stale"]["abandoned"], 0)
            self.assertEqual(self._claim_status(self._merchant_agent_id(), self.buyer_message_id), "processing")
        # The owner recovers its own stale claim.
        status, payload = self.call("POST", "/negotiation/claims/abandon-stale", {}, token=self.merchant_token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["stale"]["abandoned"], 1)
        self.assertEqual(payload["stale"]["message_ids"], [self.buyer_message_id])
        self.assertEqual(payload["stale"]["ttl_seconds"], 300)
        self.assertEqual(self._claim_status(self._merchant_agent_id(), self.buyer_message_id), "abandoned")
        # Audited with the stale reason; reclaimable afterwards.
        events = self._audit_events("agent_message_abandoned")
        self.assertTrue(any(e.get("reason") == "stale_processing_claim" for e in events))
        status, payload = self.claim(self.merchant_token, key="retry-after-stale")
        self.assertEqual(status, 200)
        self.assertTrue(payload["claim"]["claimed"])
        self.assertEqual(payload["claim"]["attempts"], 2)

    def test_abandon_stale_ignores_fresh_claims(self):
        self.claim(self.merchant_token)
        status, payload = self.call("POST", "/negotiation/claims/abandon-stale", {}, token=self.merchant_token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["stale"]["abandoned"], 0)
        self.assertEqual(self._claim_status(self._merchant_agent_id(), self.buyer_message_id), "processing")

    def test_heartbeat_prevents_stale_recovery(self):
        self.claim(self.merchant_token)
        self._backdate_claim(self._merchant_agent_id(), self.buyer_message_id, 400)
        status, payload = self.call(
            "POST",
            "/negotiation/claims/heartbeat",
            {"message_id": self.buyer_message_id},
            token=self.merchant_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["heartbeat"]["refreshed"], 1)
        status, payload = self.call("POST", "/negotiation/claims/abandon-stale", {}, token=self.merchant_token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["stale"]["abandoned"], 0)
        self.assertEqual(self._claim_status(self._merchant_agent_id(), self.buyer_message_id), "processing")

    def test_abandon_stale_ttl_validation_fail_closed(self):
        self.claim(self.merchant_token)
        for bad in (1.5, True, 0, -5, 86_401, "abc", "1.5", "", float("nan")):
            status, payload = self.call(
                "POST", "/negotiation/claims/abandon-stale", {"ttl_seconds": bad}, token=self.merchant_token
            )
            self.assertEqual(status, 400, f"ttl_seconds={bad!r} must be rejected: {payload}")
            self.assertFalse(payload["ok"])
        # Whole numbers (int, integral float, decimal string) are accepted.
        for good in (60, 60.0, "60"):
            status, payload = self.call(
                "POST", "/negotiation/claims/abandon-stale", {"ttl_seconds": good}, token=self.merchant_token
            )
            self.assertEqual(status, 200, f"ttl_seconds={good!r}: {payload}")
            self.assertEqual(payload["stale"]["ttl_seconds"], 60)
        # Fresh claim was never abandoned by any of the above.
        self.assertEqual(self._claim_status(self._merchant_agent_id(), self.buyer_message_id), "processing")

    def test_buyer_stale_recovery_is_own_and_conversation_scoped(self):
        # Drive to waiting_buyer, then the buyer claims the merchant counter.
        status, payload = self.merchant_accept()
        self.assertEqual(status, 200)
        merchant_message_id = payload["policy_result"]["message_id"]
        status, payload = self.claim(self.buyer_token, message_id=merchant_message_id, key="buyer:2:v1")
        self.assertEqual(status, 200)
        self._backdate_claim(self._buyer_agent_id(), merchant_message_id, 400)
        # Another buyer's token cannot touch it.
        status, payload = self.call("POST", "/negotiation/claims/abandon-stale", {}, token=self.other_buyer_token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["stale"]["abandoned"], 0)
        self.assertEqual(self._claim_status(self._buyer_agent_id(), merchant_message_id), "processing")
        # The buyer recovers its own stale claim and can reclaim it.
        status, payload = self.call("POST", "/negotiation/claims/abandon-stale", {}, token=self.buyer_token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["stale"]["abandoned"], 1)
        self.assertEqual(payload["stale"]["message_ids"], [merchant_message_id])
        status, payload = self.claim(self.buyer_token, message_id=merchant_message_id, key="buyer:2:v1:retry")
        self.assertEqual(status, 200)
        self.assertTrue(payload["claim"]["claimed"])
        self.assertEqual(payload["claim"]["attempts"], 2)

    # -- buyer role -----------------------------------------------------------

    def test_buyer_full_turn_after_merchant_counter(self):
        status, payload = self.merchant_accept()
        self.assertEqual(status, 200)
        merchant_message_id = payload["policy_result"]["message_id"]

        status, payload = self.call("GET", "/negotiation/pending-messages", token=self.buyer_token)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["pending"]), 1)
        self.assertEqual(payload["pending"][0]["message_id"], merchant_message_id)
        self.assertEqual(payload["pending"][0]["sender_role"], "merchant")

        status, payload = self.claim(self.buyer_token, message_id=merchant_message_id, key="buyer-agent:2:v1")
        self.assertEqual(status, 200)
        self.assertTrue(payload["claim"]["claimed"])

        status, payload = self.snapshot(self.buyer_token, message_id=merchant_message_id)
        self.assertEqual(status, 200)
        snapshot = payload["snapshot"]
        protocol.validate_contract("snapshot", snapshot)
        self.assertEqual(snapshot["role"], "buyer")
        self.assertEqual(snapshot["current_proposal"]["unit_price"], 89.0)
        raw = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("automation_boundaries", raw)
        self.assertNotIn("最低可成交价", raw)

        decision = make_decision(
            self.conversation_id,
            merchant_message_id,
            action="accept_nonbinding",
            with_proposal=False,
            public_message="好的，就按 89 元每件、2 件，我这边确认（非约束性共识）。",
        )
        status, payload = self.submit(self.buyer_token, decision, key="buyer-agent:2:v1")
        self.assertEqual(status, 200)
        result = payload["policy_result"]
        protocol.validate_contract("policy-result", result)
        self.assertEqual(result["result"], "accepted")
        self.assertEqual(result["next_actor"], "merchant")

        status, payload = self.call(
            "POST", "/negotiation/claims/complete", {"message_id": merchant_message_id}, token=self.buyer_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["process"]["status"], "processed")

        with db_session(self.db_file) as conn:
            tables = {row["name"] for row in conn.execute("select name from sqlite_master where type='table'")}
            self.assertNotIn("orders", tables)
            self.assertNotIn("payments", tables)
            row = conn.execute(
                "select status, next_actor from conversations where id = ?", (self.conversation_id,)
            ).fetchone()
            self.assertEqual(row["status"], "waiting_merchant")

    def test_buyer_cannot_act_on_other_buyers_conversation(self):
        status, payload = self.merchant_accept()
        self.assertEqual(status, 200)
        merchant_message_id = payload["policy_result"]["message_id"]
        status, _payload = self.call(
            "POST",
            "/negotiation/claims",
            {
                "conversation_id": self.conversation_id,
                "message_id": merchant_message_id,
                "idempotency_key": "k",
            },
            token=self.other_buyer_token,
        )
        self.assertEqual(status, 403)

    def test_no_order_side_effects_on_any_decision(self):
        self.merchant_accept()
        with db_session(self.db_file) as conn:
            row = conn.execute("select stock from products where sku = 'cup-1'").fetchone()
            self.assertEqual(row["stock"], 12)  # negotiation never reserves or decrements stock


class NegotiationRouteTest(unittest.TestCase):
    def test_routes_registered_for_both_stacks(self):
        from shopping_cli.api.route_registry import routes_for_group

        agent_paths = {route.path for route in routes_for_group("agents")}
        for path in (
            "/capabilities",
            "/negotiation/pending-messages",
            "/negotiation/claims",
            "/negotiation/claims/complete",
            "/negotiation/claims/fail",
            "/negotiation/claims/abandon",
            "/negotiation/claims/heartbeat",
            "/negotiation/claims/abandon-stale",
            "/negotiation/snapshot",
            "/negotiation/decisions",
        ):
            self.assertIn(path, {route.path for route in routes_for_group("marketplace")} | agent_paths)

    def test_unknown_negotiation_route_is_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, payload = handle_request(Path(tmp) / "x.sqlite", "GET", "/negotiation/unknown", {}, {})
            self.assertEqual(status, 404)
            self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()

"""HostedNegotiationCompatibilityAdapter — KNP/1.0 → shopping.negotiation/0.1
mapping (v2.4-W2).

The adapter is a pure service layer: it never writes, it returns a
description object the v2.4-W3 JSON-RPC endpoint will execute.  ``conn`` is
used read-only to locate the hosted conversation.

Coverage:

* the full 11-action classification matrix matches Kiwi's
  ``KNP_TO_LEGACY_ACTION`` exactly;
* lossless translations produce a legacy ``negotiate`` intent, a structured
  payload whose ``decision`` passes the frozen decision contract, and the
  correct target conversation;
* protected-semantics downgrades to ``fail_closed``: conditional semantics in
  an offer, an offer without an expressible expiry, negotiation-scoped
  decline, and unmappable identity references;
* ``requires_human`` routing description for rfq / clarification_response /
  withdraw / cancel;
* negotiation_id ↔ conversation_id deterministic reversible encoding;
* the idempotency triple ``(sender_identity, message_id, digest)`` is carried
  on every TranslationResult.

Binding: docs/a2a/shopping-cli-a2a-binding-1.0-rc1.md §3.6, §4, §6
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shopping_cli.a2a.binding import (
    HOSTED_NEGOTIATION_PREFIX,
    HostedNegotiationCompatibilityAdapter,
    IdempotencyKey,
    KNP_TO_LEGACY_ACTION,
    LEGACY_NEGOTIATION_INTENT,
    TranslationResult,
    classify_action,
    conversation_id_from_negotiation_id,
    negotiation_id_for_conversation,
)
from shopping_cli.a2a.knp import finalize_envelope, validate_envelope
from shopping_cli.core.conversations import ensure_conversation
from shopping_cli.core.negotiation import validate_contract
from shopping_cli.db.session import db_session, now_iso

CAPABILITY = "com.harrylabsj.kiwi.shopping.negotiation"
EXCHANGE_ID = "ex_01H5V8KXZqJ7Qp3mN2B6A"
TIMESTAMP = "2026-08-05T12:00:00Z"

MERCHANT_ID = "merchant-1"
BUYER_ID = "buyer-1"
SKU = "SKU-001"

ADAPTER = HostedNegotiationCompatibilityAdapter


# Kiwi mapping.ts matrix — the authoritative classification source.
EXPECTED_CLASSIFICATION = {
    "inquiry": "lossless",
    "rfq": "requires_human",
    "offer": "lossless",
    "counter_offer": "lossless",
    "conditional_offer": "fail_closed",
    "clarification": "lossless",
    "clarification_response": "requires_human",
    "accept_nonbinding": "lossless",
    "withdraw": "requires_human",
    "decline": "lossless",
    "cancel": "requires_human",
}

# Legacy decision actions that lossless KNP actions must land on.
LEGACY_ACTION_BY_KNP = {
    "inquiry": "ask",
    "offer": "propose",
    "counter_offer": "counter",
    "clarification": "ask",
    "accept_nonbinding": "accept_nonbinding",
    "decline": "decline",
}


def _seed_conversation(db_file: Path) -> str:
    """Create merchant + product + conversation; return the conversation id."""
    with db_session(db_file):
        pass  # initialize schema
    with db_session(db_file) as conn:
        ts = now_iso()
        conn.execute(
            """
            insert into merchants(id, name, city, service_area, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (MERCHANT_ID, "Tea Shop", "Hangzhou", "Xihu", ts, ts),
        )
        conn.execute(
            """
            insert into products(sku, merchant_id, title, price, currency, stock,
                                 created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (SKU, MERCHANT_ID, "Longjing", 850.0, "CNY", 500, ts, ts),
        )
        conn.commit()
    with db_session(db_file) as conn:
        conversation = ensure_conversation(conn, BUYER_ID, MERCHANT_ID, SKU)
        return str(conversation["id"])


def _envelope(conversation_id: str, **overrides: object) -> dict:
    """Build a finalized KNP envelope addressed at a hosted conversation."""
    fields: dict[str, object] = {
        "capability": CAPABILITY,
        "protocol_version": "1.0",
        "negotiation_id": negotiation_id_for_conversation(conversation_id),
        "exchange_id": EXCHANGE_ID,
        "message_id": "msg_03H5V8KXZqJ7Qp3mN2B6A",
        "actor": "buyer",
        "action": "inquiry",
        "created_at": TIMESTAMP,
        "payload": {"type": "inquiry", "subject": {"sku": SKU}},
    }
    fields.update(overrides)
    return finalize_envelope(fields)


def _validated(fields: dict) -> object:
    return validate_envelope(fields)


class ClassificationMatrixTest(unittest.TestCase):
    """All 11 KNP actions classify exactly as in Kiwi's mapping matrix."""

    def test_classification_matches_kiwi_matrix(self):
        self.assertEqual(
            KNP_TO_LEGACY_ACTION,
            {
                "inquiry": "ask",
                "rfq": "requires_human",
                "offer": "propose",
                "counter_offer": "counter",
                "conditional_offer": "fail_closed",
                "clarification": "ask",
                "clarification_response": "requires_human",
                "accept_nonbinding": "accept_nonbinding",
                "withdraw": "requires_human",
                "decline": "decline",
                "cancel": "requires_human",
            },
        )

    def test_classify_action_all_actions(self):
        for action, expected in EXPECTED_CLASSIFICATION.items():
            with self.subTest(action=action):
                self.assertEqual(classify_action(action), expected)
                self.assertEqual(ADAPTER.classify_action(action), expected)


class ConversationMappingTest(unittest.TestCase):
    """Deterministic reversible negotiation_id ↔ conversation_id encoding."""

    def test_round_trip_encoding(self):
        self.assertEqual(
            negotiation_id_for_conversation("CONV-0001"),
            f"{HOSTED_NEGOTIATION_PREFIX}CONV-0001",
        )
        self.assertEqual(
            conversation_id_from_negotiation_id("neg_CONV-0001"),
            "CONV-0001",
        )

    def test_non_hosted_negotiation_id_has_no_conversation(self):
        # A foreign kiwi-style id carries the prefix but names a conversation
        # that does not exist; the adapter rejects it at the DB lookup.
        self.assertEqual(
            conversation_id_from_negotiation_id("neg_01H5V8KXZqJ7Qp3mN2B6A"),
            "01H5V8KXZqJ7Qp3mN2B6A",
        )
        # No hosted prefix at all → no conversation.
        self.assertIsNone(conversation_id_from_negotiation_id("ex_01"))

    def test_negotiation_id_without_suffix_is_not_mappable(self):
        self.assertIsNone(conversation_id_from_negotiation_id("neg_"))


class LosslessTranslationTest(unittest.TestCase):
    """Lossless actions → legacy ``negotiate`` intent + valid decision."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self._tmp.name) / "test.sqlite"
        self.conversation_id = _seed_conversation(self.db_file)

    def tearDown(self):
        self._tmp.cleanup()

    def _translate(self, fields: dict, sender_identity: str = BUYER_ID) -> TranslationResult:
        with db_session(self.db_file) as conn:
            return ADAPTER.translate_envelope(
                conn,
                _validated(fields),
                sender_identity=sender_identity,
            )

    def test_inquiry_maps_to_legacy_ask(self):
        result = self._translate(
            _envelope(
                self.conversation_id,
                in_reply_to="msg_legacy_1",
                payload={
                    "type": "inquiry",
                    "subject": {"sku": SKU},
                    "questions": [{"code": "delivery.estimated_date"}],
                },
            )
        )
        self.assertEqual(result.classification, "lossless")
        self.assertEqual(result.target_conversation_id, self.conversation_id)
        self.assertEqual(result.legacy_intent, LEGACY_NEGOTIATION_INTENT)
        decision = result.legacy_structured_payload["decision"]
        self.assertEqual(decision["action"], "ask")
        self.assertEqual(decision["conversation_id"], self.conversation_id)
        self.assertEqual(decision["in_reply_to_message_id"], 1)
        self.assertEqual(decision["open_issues"], ["delivery.estimated_date"])
        validate_contract("decision", decision)

    def test_clarification_maps_to_legacy_ask_with_open_issues(self):
        result = self._translate(
            _envelope(
                self.conversation_id,
                in_reply_to="msg_legacy_1",
                action="clarification",
                payload={
                    "type": "clarification",
                    "questions": [{"field": "fulfillment.delivery_before", "reason": "missing"}],
                },
            )
        )
        self.assertEqual(result.classification, "lossless")
        self.assertEqual(result.legacy_structured_payload["decision"]["action"], "ask")
        self.assertEqual(
            result.legacy_structured_payload["decision"]["open_issues"],
            ["fulfillment.delivery_before: missing"],
        )

    def test_offer_maps_to_legacy_propose_with_proposal(self):
        result = self._translate(
            _envelope(
                self.conversation_id,
                in_reply_to="msg_legacy_1",
                actor="merchant",
                action="offer",
                payload=_offer_payload(),
            ),
            sender_identity="merchant-1",
        )
        self.assertEqual(result.classification, "lossless")
        decision = result.legacy_structured_payload["decision"]
        self.assertEqual(decision["action"], "propose")
        self.assertEqual(decision["in_reply_to_message_id"], 1)
        proposal = decision["proposal"]
        self.assertEqual(proposal["sku"], SKU)
        self.assertEqual(proposal["quantity"], 200)
        self.assertEqual(proposal["unit_price"], 850.0)
        self.assertEqual(proposal["currency"], "CNY")
        self.assertEqual(proposal["valid_until"], "2026-08-06T12:00:00Z")
        self.assertEqual(proposal["stock"]["reserved"], False)
        self.assertEqual(proposal["delivery"]["fee"], 5.0)
        validate_contract("decision", decision)

    def test_counter_offer_maps_to_legacy_counter(self):
        result = self._translate(
            _envelope(
                self.conversation_id,
                in_reply_to="msg_legacy_1",
                actor="buyer",
                action="counter_offer",
                payload={
                    "type": "counter_offer",
                    "offer_id": "off_02H5V8KXZqJ7Qp3mN2B6A",
                    "responding_to_offer_id": "off_01H5V8KXZqJ7Qp3mN2B6A",
                    "proposed_terms": _offer_payload()["terms"],
                },
            )
        )
        self.assertEqual(result.classification, "lossless")
        self.assertEqual(result.legacy_structured_payload["decision"]["action"], "counter")
        validate_contract("decision", result.legacy_structured_payload["decision"])

    def test_accept_nonbinding_maps_to_legacy_accept_nonbinding(self):
        result = self._translate(
            _envelope(
                self.conversation_id,
                in_reply_to="msg_legacy_2",
                action="accept_nonbinding",
                payload={
                    "type": "accept_nonbinding",
                    "offer_id": "off_01H5V8KXZqJ7Qp3mN2B6A",
                    "terms_digest": f"sha256:{'a' * 64}",
                },
            )
        )
        self.assertEqual(result.classification, "lossless")
        decision = result.legacy_structured_payload["decision"]
        self.assertEqual(decision["action"], "accept_nonbinding")
        self.assertNotIn("proposal", decision)
        # consultation-only invariant: the legacy accept is non-binding.
        validate_contract("decision", decision)

    def test_decline_maps_to_legacy_decline(self):
        result = self._translate(
            _envelope(
                self.conversation_id,
                action="decline",
                payload={
                    "type": "decline",
                    "scope": "offer",
                    "target_message_id": "msg_legacy_2",
                    "reason_code": "terms_unacceptable",
                },
            )
        )
        self.assertEqual(result.classification, "lossless")
        decision = result.legacy_structured_payload["decision"]
        self.assertEqual(decision["action"], "decline")
        self.assertEqual(decision["in_reply_to_message_id"], 2)
        validate_contract("decision", decision)

    def test_legacy_structured_payload_shape(self):
        result = self._translate(
            _envelope(self.conversation_id, in_reply_to="msg_legacy_1")
        )
        payload = result.legacy_structured_payload
        self.assertEqual(payload["protocol_version"], "shopping.negotiation/0.1")
        self.assertEqual(payload["agent_id"], BUYER_ID)
        self.assertEqual(payload["role"], "buyer")
        self.assertEqual(payload["source_id"], BUYER_ID)
        self.assertTrue(payload["idempotency_key"].startswith("knp:"))

    def test_every_lossless_action_lands_on_its_legacy_action(self):
        for action, legacy_action in LEGACY_ACTION_BY_KNP.items():
            with self.subTest(action=action):
                fields = _envelope(self.conversation_id, action=action)
                if action in ("offer", "counter_offer"):
                    fields = _envelope(
                        self.conversation_id,
                        action=action,
                        actor="merchant" if action == "offer" else "buyer",
                        in_reply_to="msg_legacy_1",
                        payload=_offer_payload() if action == "offer" else {
                            "type": "counter_offer",
                            "offer_id": "off_02H5V8KXZqJ7Qp3mN2B6A",
                            "responding_to_offer_id": "off_01H5V8KXZqJ7Qp3mN2B6A",
                            "proposed_terms": _offer_payload()["terms"],
                        },
                    )
                elif action == "decline":
                    fields = _envelope(
                        self.conversation_id,
                        action="decline",
                        payload={
                            "type": "decline",
                            "scope": "offer",
                            "target_message_id": "msg_legacy_1",
                        },
                    )
                elif action in ("inquiry", "clarification"):
                    fields = _envelope(
                        self.conversation_id,
                        action=action,
                        in_reply_to="msg_legacy_1",
                        payload={
                            "type": action,
                            "questions": (
                                [{"code": "delivery.estimated_date"}]
                                if action == "inquiry"
                                else [{"field": "fulfillment.delivery_before"}]
                            ),
                        },
                    )
                elif action == "accept_nonbinding":
                    fields = _envelope(
                        self.conversation_id,
                        action="accept_nonbinding",
                        in_reply_to="msg_legacy_1",
                        payload={
                            "type": "accept_nonbinding",
                            "offer_id": "off_01H5V8KXZqJ7Qp3mN2B6A",
                            "terms_digest": f"sha256:{'a' * 64}",
                        },
                    )
                result = self._translate(fields)
                self.assertEqual(result.classification, "lossless")
                self.assertEqual(
                    result.legacy_structured_payload["decision"]["action"],
                    legacy_action,
                )


class ProtectedSemanticsTest(unittest.TestCase):
    """Protected semantics are never silently dropped (binding rc1 §4)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self._tmp.name) / "test.sqlite"
        self.conversation_id = _seed_conversation(self.db_file)

    def tearDown(self):
        self._tmp.cleanup()

    def _translate(self, fields: dict) -> TranslationResult:
        with db_session(self.db_file) as conn:
            return ADAPTER.translate_envelope(
                conn,
                _validated(fields),
                sender_identity=BUYER_ID,
            )

    def test_conditional_offer_is_fail_closed(self):
        result = self._translate(
            _envelope(
                self.conversation_id,
                action="conditional_offer",
                payload={
                    "type": "conditional_offer",
                    "offer_id": "off_03H5V8KXZqJ7Qp3mN2B6A",
                    "base_terms": {},
                    "conditions": [],
                },
            )
        )
        self.assertEqual(result.classification, "fail_closed")
        self.assertIn("conditions", result.reason)

    def test_lossless_offer_with_conditions_downgrades_to_fail_closed(self):
        terms = _offer_payload()["terms"]
        terms["conditions"] = [
            {"when": {"all": [{"field": "aggregate.total_quantity", "op": "gte", "value": 500}]}, "then_terms": {"items": []}}
        ]
        result = self._translate(
            _envelope(
                self.conversation_id,
                in_reply_to="msg_legacy_1",
                actor="merchant",
                action="offer",
                payload=_offer_payload(terms=terms),
            )
        )
        self.assertEqual(result.classification, "fail_closed")
        self.assertIn("conditional semantics", result.reason)

    def test_offer_without_expressible_expiry_fails_closed(self):
        terms = dict(_offer_payload()["terms"])
        del terms["valid_until"]
        result = self._translate(
            _envelope(
                self.conversation_id,
                in_reply_to="msg_legacy_1",
                actor="merchant",
                action="offer",
                payload=_offer_payload(terms=terms),
            )
        )
        self.assertEqual(result.classification, "fail_closed")
        self.assertIn("valid_until", result.reason)

    def test_offer_missing_delivery_facts_fails_closed(self):
        terms = dict(_offer_payload()["terms"])
        del terms["fulfillment_terms"]
        result = self._translate(
            _envelope(
                self.conversation_id,
                in_reply_to="msg_legacy_1",
                actor="merchant",
                action="offer",
                payload=_offer_payload(terms=terms),
            )
        )
        self.assertEqual(result.classification, "fail_closed")
        self.assertIn("delivery", result.reason)

    def test_decline_scope_negotiation_fails_closed(self):
        result = self._translate(
            _envelope(
                self.conversation_id,
                action="decline",
                payload={"type": "decline", "scope": "negotiation"},
            )
        )
        self.assertEqual(result.classification, "fail_closed")
        self.assertIn("negotiation", result.reason)

    def test_decline_with_unmappable_target_fails_closed(self):
        result = self._translate(
            _envelope(
                self.conversation_id,
                action="decline",
                payload={
                    "type": "decline",
                    "scope": "offer",
                    "target_message_id": "msg_02H5V8KXZqJ7Qp3mN2B6A",
                },
            )
        )
        self.assertEqual(result.classification, "fail_closed")
        self.assertIn("identity reference", result.reason)

    def test_lossless_action_without_in_reply_to_fails_closed(self):
        result = self._translate(_envelope(self.conversation_id))
        self.assertEqual(result.classification, "fail_closed")
        self.assertIn("in_reply_to", result.reason)

    def test_lossless_action_with_unmappable_in_reply_to_fails_closed(self):
        result = self._translate(
            _envelope(self.conversation_id, in_reply_to="msg_01H5V8KXZqJ7Qp3mN2B6A")
        )
        self.assertEqual(result.classification, "fail_closed")
        self.assertIn("identity reference", result.reason)

    def test_fail_closed_result_has_no_legacy_write_description(self):
        result = self._translate(
            _envelope(
                self.conversation_id,
                action="conditional_offer",
                payload={"type": "conditional_offer", "offer_id": "o1", "base_terms": {}, "conditions": []},
            )
        )
        self.assertIsNone(result.legacy_structured_payload)
        self.assertIsNone(result.legacy_intent)
        self.assertIsNone(result.human_review)


class RequiresHumanRoutingTest(unittest.TestCase):
    """Unsupported actions route to human review, not auto-execution."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self._tmp.name) / "test.sqlite"
        self.conversation_id = _seed_conversation(self.db_file)

    def tearDown(self):
        self._tmp.cleanup()

    def test_unsupported_actions_route_to_human_review(self):
        for action in ("rfq", "clarification_response", "withdraw", "cancel"):
            with self.subTest(action=action):
                fields = _envelope(
                    self.conversation_id,
                    action=action,
                    in_reply_to="msg_legacy_1",
                )
                if action == "clarification_response":
                    fields["payload"] = {
                        "type": "clarification_response",
                        "answer": "delivery_before = 2026-08-20",
                    }
                elif action == "rfq":
                    fields["payload"] = {
                        "type": "rfq",
                        "items": [{"sku": SKU, "quantity": {"value": 200, "unit": "piece"}}],
                    }
                elif action == "withdraw":
                    fields["payload"] = {
                        "type": "withdraw",
                        "scope": "offer",
                        "target_message_id": "msg_legacy_1",
                    }
                else:
                    fields["payload"] = {"type": "cancel"}

                with db_session(self.db_file) as conn:
                    result = ADAPTER.translate_envelope(
                        conn,
                        _validated(fields),
                        sender_identity=BUYER_ID,
                    )

                self.assertEqual(result.classification, "requires_human")
                self.assertEqual(result.target_conversation_id, self.conversation_id)
                self.assertEqual(result.human_review["reason"], f"knp_action_unsupported:{action}")
                self.assertEqual(result.human_review["severity"], "review")
                self.assertIsNone(result.legacy_structured_payload)
                self.assertIsNone(result.legacy_intent)

    def test_human_review_routing_aligned_with_moderation_flag_semantics(self):
        """The human_review description carries the add_flag carrier fields
        (reason + severity) used by core.conversations.add_flag."""
        with db_session(self.db_file) as conn:
            result = ADAPTER.translate_envelope(
                conn,
                _validated(
                    _envelope(
                        self.conversation_id,
                        action="withdraw",
                        payload={
                            "type": "withdraw",
                            "scope": "offer",
                            "target_message_id": "msg_legacy_1",
                        },
                    )
                ),
                sender_identity=BUYER_ID,
            )
        review = result.human_review
        self.assertEqual(set(review), {"reason", "severity"})
        self.assertTrue(review["reason"])
        self.assertIsInstance(review["severity"], str)


class UnknownNegotiationTest(unittest.TestCase):
    """Foreign / unknown negotiation ids fail closed (no existence oracle)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self._tmp.name) / "test.sqlite"
        self.conversation_id = _seed_conversation(self.db_file)

    def tearDown(self):
        self._tmp.cleanup()

    def test_foreign_negotiation_id_fails_closed(self):
        fields = _envelope(self.conversation_id)
        fields["negotiation_id"] = "neg_01H5V8KXZqJ7Qp3mN2B6A"
        with db_session(self.db_file) as conn:
            result = ADAPTER.translate_envelope(
                conn,
                _validated(fields),
                sender_identity=BUYER_ID,
            )
        self.assertEqual(result.classification, "fail_closed")
        self.assertIn("hosted conversation", result.reason)
        self.assertIsNone(result.legacy_structured_payload)

    def test_non_negotiation_prefix_fails_closed(self):
        fields = _envelope(self.conversation_id)
        fields["negotiation_id"] = "ex_01"
        with db_session(self.db_file) as conn:
            result = ADAPTER.translate_envelope(
                conn,
                _validated(fields),
                sender_identity=BUYER_ID,
            )
        self.assertEqual(result.classification, "fail_closed")
        self.assertIn("prefix", result.reason)


class IdempotencyCarrierTest(unittest.TestCase):
    """Every TranslationResult carries the §3.6 idempotency triple."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self._tmp.name) / "test.sqlite"
        self.conversation_id = _seed_conversation(self.db_file)

    def tearDown(self):
        self._tmp.cleanup()

    def test_idempotency_triple_is_carried_on_all_classifications(self):
        cases = [
            _envelope(self.conversation_id, in_reply_to="msg_legacy_1"),  # lossless
            _envelope(
                self.conversation_id,
                action="conditional_offer",
                payload={"type": "conditional_offer", "offer_id": "o1", "base_terms": {}, "conditions": []},
            ),  # fail_closed
            _envelope(
                self.conversation_id,
                action="rfq",
                payload={"type": "rfq", "items": [{"sku": SKU, "quantity": {"value": 200, "unit": "piece"}}]},
            ),  # requires_human
        ]
        with db_session(self.db_file) as conn:
            for fields in cases:
                env = _validated(fields)
                result = ADAPTER.translate_envelope(conn, env, sender_identity=BUYER_ID)
                self.assertIsInstance(result.idempotency, IdempotencyKey)
                self.assertEqual(result.idempotency.sender_identity, BUYER_ID)
                self.assertEqual(result.idempotency.message_id, env.message_id)
                self.assertEqual(result.idempotency.digest, env.digest)


def _offer_payload(terms: dict | None = None) -> dict:
    """A KNP offer payload whose terms are losslessly expressible as a legacy
    proposal (single SKU + delivery eta / fee / stock / valid_until)."""
    base_terms: dict[str, object] = {
        "items": [
            {
                "sku": SKU,
                "quantity": {"value": 200, "unit": "piece"},
                "unit_price": {"currency": "CNY", "amount_minor": 85000},
            }
        ],
        "fulfillment_terms": {
            "eta_start": "2026-08-06T12:00:00Z",
            "eta_end": "2026-08-07T12:00:00Z",
            "delivery_fee": {"currency": "CNY", "amount_minor": 500},
            "legacy_stock": {
                "status": "available",
                "quantity": 500,
                "observed_at": "2026-08-05T12:00:00Z",
                "reserved": False,
            },
        },
        "valid_until": "2026-08-06T12:00:00Z",
    }
    return {
        "type": "offer",
        "offer_id": "off_01H5V8KXZqJ7Qp3mN2B6A",
        "terms": terms if terms is not None else base_terms,
    }


if __name__ == "__main__":
    unittest.main()

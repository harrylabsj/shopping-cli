"""KNP/1.0 envelope model, validation and content digest (v2.4-W2).

The digest golden vectors are generated from the Kiwi runtime's own
implementation (``kiwi/src/negotiation/domain/envelope.ts`` + ``jcs.ts``,
run through the built ``dist/`` module with Node).  They are the correctness
anchor for the Python JCS port: the Python ``compute_envelope_digest`` must
produce byte-identical canonical JSON and identical ``sha256:`` digests.

Coverage:

* 4 golden vectors (counter_offer / offer / inquiry / accept_nonbinding);
* digest stability: key order irrelevant, transport signature fields excluded,
  digest itself excluded, tampering detected;
* fail-closed validation: unknown protocol_version, unknown action, actor
  outside buyer|merchant, missing fields, non-RFC3339 created_at, non-object
  payload, malformed digest, clarification_response without in_reply_to;
* JCS canonical bytes match the Kiwi canonical JSON exactly.

Binding: docs/a2a/shopping-cli-a2a-binding-1.0-rc1.md §3.6, §4
"""

from __future__ import annotations

import unittest

from shopping_cli.a2a.knp import (
    KNP_ACTIONS,
    KNP_PROTOCOL_VERSION,
    KnpValidationError,
    NegotiationEnvelope,
    TRANSPORT_SIGNATURE_FIELDS,
    compute_envelope_digest,
    finalize_envelope,
    jcs_canonicalize,
    validate_envelope,
    verify_envelope_digest,
)

CAPABILITY = "example.kiwi.shopping.negotiation"
NEGOTIATION_ID = "neg_01H5V8KXZqJ7Qp3mN2B6A"
EXCHANGE_ID = "ex_01H5V8KXZqJ7Qp3mN2B6A"
MESSAGE_ID = "msg_01H5V8KXZqJ7Qp3mN2B6A"
IN_REPLY_TO = "msg_00H5V8KXZqJ7Qp3mN2B6A"
OFFER_ID_1 = "off_01H5V8KXZqJ7Qp3mN2B6A"
OFFER_ID_2 = "off_02H5V8KXZqJ7Qp3mN2B6A"
SKU = "SKU-001"
TIMESTAMP = "2026-08-05T12:00:00Z"
DELIVERY_BEFORE = "2026-08-20T18:00:00Z"
QUANTITY = {"value": 200, "unit": "piece"}


# ---------------------------------------------------------------------------
# Golden digest vectors — generated from the Kiwi dist implementation:
#
#   node --input-type=module -e "import { finalizeEnvelope } from
#   './dist/negotiation/domain/envelope.js'; ... "
#
# Each entry is (label, envelope content WITHOUT digest, expected digest).
# ---------------------------------------------------------------------------

COUNTER_OFFER_FIELDS = {
    "capability": CAPABILITY,
    "protocol_version": "1.0",
    "negotiation_id": NEGOTIATION_ID,
    "exchange_id": EXCHANGE_ID,
    "message_id": MESSAGE_ID,
    "in_reply_to": IN_REPLY_TO,
    "actor": "buyer",
    "action": "counter_offer",
    "created_at": TIMESTAMP,
    "payload": {
        "type": "counter_offer",
        "offer_id": OFFER_ID_2,
        "responding_to_offer_id": OFFER_ID_1,
        "proposed_terms": {
            "items": [
                {
                    "sku": SKU,
                    "quantity": QUANTITY,
                    "unit_price": {"currency": "CNY", "amount_minor": 83500},
                }
            ],
        },
    },
    "public_message": "If we order 200 units, we propose CNY 835.00 per unit.",
}

OFFER_FIELDS = {
    "capability": CAPABILITY,
    "protocol_version": "1.0",
    "negotiation_id": NEGOTIATION_ID,
    "exchange_id": EXCHANGE_ID,
    "message_id": "msg_02H5V8KXZqJ7Qp3mN2B6A",
    "in_reply_to": IN_REPLY_TO,
    "actor": "merchant",
    "action": "offer",
    "created_at": TIMESTAMP,
    "payload": {
        "type": "offer",
        "offer_id": OFFER_ID_1,
        "terms": {
            "items": [
                {
                    "sku": SKU,
                    "quantity": QUANTITY,
                    "unit_price": {"currency": "CNY", "amount_minor": 85000},
                }
            ],
            "fulfillment_terms": {"delivery_before": DELIVERY_BEFORE},
            "valid_until": "2026-08-06T12:00:00Z",
        },
    },
    "public_message": "We offer CNY 850.00 per unit, delivery before 2026-08-20.",
}

INQUIRY_FIELDS = {
    "capability": CAPABILITY,
    "protocol_version": "1.0",
    "negotiation_id": NEGOTIATION_ID,
    "exchange_id": EXCHANGE_ID,
    "message_id": "msg_03H5V8KXZqJ7Qp3mN2B6A",
    "actor": "buyer",
    "action": "inquiry",
    "created_at": TIMESTAMP,
    "payload": {
        "type": "inquiry",
        "subject": {"sku": SKU},
        "questions": [{"code": "delivery.estimated_date"}],
    },
}

ACCEPT_NONBINDING_FIELDS = {
    "capability": CAPABILITY,
    "protocol_version": "1.0",
    "negotiation_id": NEGOTIATION_ID,
    "exchange_id": EXCHANGE_ID,
    "message_id": "msg_04H5V8KXZqJ7Qp3mN2B6A",
    "in_reply_to": MESSAGE_ID,
    "actor": "buyer",
    "action": "accept_nonbinding",
    "created_at": TIMESTAMP,
    "payload": {
        "type": "accept_nonbinding",
        "offer_id": OFFER_ID_1,
        "terms_digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    },
}

GOLDEN_VECTORS = (
    ("counter_offer", COUNTER_OFFER_FIELDS, "sha256:68a18d9a93d4bc4a55d7c68f22640042f533064be723f20f0f9c25b50e7c15c8"),
    ("offer", OFFER_FIELDS, "sha256:d8dccd8c45ba4227652750cba9c46d3f121a46d48e71cf7d3a21d96137a18b58"),
    ("inquiry", INQUIRY_FIELDS, "sha256:088874f6cfdd69ebc33f84c5393a135de2c8b21acd2c37cce8ec2d20d12a7217"),
    ("accept_nonbinding", ACCEPT_NONBINDING_FIELDS, "sha256:b7e938fc706f6b26891ad1843a126c52511733efeeaaf898922a0cf44c9c8f52"),
)

# The canonical JSON captured from Kiwi's ``canonicalize`` for the
# counter_offer envelope — a byte-level cross-language anchor for the JCS port.
COUNTER_OFFER_CANONICAL_JSON = (
    '{"action":"counter_offer","actor":"buyer",'
    '"capability":"example.kiwi.shopping.negotiation",'
    '"created_at":"2026-08-05T12:00:00Z",'
    '"exchange_id":"ex_01H5V8KXZqJ7Qp3mN2B6A",'
    '"in_reply_to":"msg_00H5V8KXZqJ7Qp3mN2B6A",'
    '"message_id":"msg_01H5V8KXZqJ7Qp3mN2B6A",'
    '"negotiation_id":"neg_01H5V8KXZqJ7Qp3mN2B6A",'
    '"payload":{"offer_id":"off_02H5V8KXZqJ7Qp3mN2B6A",'
    '"proposed_terms":{"items":[{"quantity":{"unit":"piece","value":200},'
    '"sku":"SKU-001","unit_price":{"amount_minor":83500,"currency":"CNY"}}]},'
    '"responding_to_offer_id":"off_01H5V8KXZqJ7Qp3mN2B6A",'
    '"type":"counter_offer"},'
    '"protocol_version":"1.0",'
    '"public_message":"If we order 200 units, we propose CNY 835.00 per unit."}'
)


def _error_code(fn: object) -> str | None:
    """Return the KnpValidationError code raised by ``fn``, or None."""
    try:
        fn()  # type: ignore[operator]
    except KnpValidationError as exc:
        return exc.code
    return None


class KnpDigestTest(unittest.TestCase):
    """Content digest: JCS canonicalization + SHA-256, §19.2."""

    def test_golden_vectors_match_kiwi(self):
        for label, fields, expected in GOLDEN_VECTORS:
            with self.subTest(vector=label):
                self.assertEqual(compute_envelope_digest(fields), expected)
                self.assertEqual(finalize_envelope(fields)["digest"], expected)

    def test_jcs_canonical_bytes_match_kiwi(self):
        self.assertEqual(jcs_canonicalize(COUNTER_OFFER_FIELDS), COUNTER_OFFER_CANONICAL_JSON)

    def test_digest_is_stable_across_key_order(self):
        fields = COUNTER_OFFER_FIELDS
        scrambled = {
            "payload": fields["payload"],
            "public_message": fields["public_message"],
            "action": fields["action"],
            "actor": fields["actor"],
            "created_at": fields["created_at"],
            "message_id": fields["message_id"],
            "negotiation_id": fields["negotiation_id"],
            "exchange_id": fields["exchange_id"],
            "in_reply_to": fields["in_reply_to"],
            "protocol_version": fields["protocol_version"],
            "capability": fields["capability"],
        }
        self.assertEqual(compute_envelope_digest(scrambled), compute_envelope_digest(fields))

    def test_digest_excludes_digest_itself_and_transport_signatures(self):
        fields = COUNTER_OFFER_FIELDS
        base = compute_envelope_digest(fields)
        with_signatures = dict(fields)
        with_signatures["signature"] = {"alg": "hmac-sha256", "value": "0000"}
        with_signatures["transport_signature"] = "sig-t"
        with_signatures["http_message_signature"] = "sig-h"
        with_signatures["x_message_signature"] = "sig-x"
        self.assertEqual(compute_envelope_digest(with_signatures), base)

        envelope = finalize_envelope(fields)
        self.assertEqual(
            compute_envelope_digest({**envelope, "digest": f"sha256:{'f' * 64}"}),
            base,
        )

    def test_digest_changes_when_bound_fields_change(self):
        fields = COUNTER_OFFER_FIELDS
        base = compute_envelope_digest(fields)
        self.assertNotEqual(
            compute_envelope_digest({**fields, "message_id": "msg_09..."}),
            base,
        )
        tampered_payload = dict(fields)
        tampered_payload["payload"] = dict(fields["payload"])
        tampered_payload["payload"]["proposed_terms"] = {
            "items": [{"sku": "SKU-OTHER", "quantity": QUANTITY}]
        }
        self.assertNotEqual(compute_envelope_digest(tampered_payload), base)

    def test_verify_detects_tampered_digest(self):
        envelope = finalize_envelope(COUNTER_OFFER_FIELDS)
        self.assertTrue(verify_envelope_digest(envelope))
        tampered = dict(envelope)
        tampered["payload"] = dict(envelope["payload"])
        tampered["payload"]["type"] = "offer"
        self.assertFalse(verify_envelope_digest(tampered))
        tampered_actor = {**envelope, "actor": "merchant"}
        self.assertFalse(verify_envelope_digest(tampered_actor))

    def test_optional_absent_fields_do_not_change_digest_across_round_trip(self):
        # An envelope finalized without in_reply_to/public_message verifies
        # after validation because the dataclass wire form omits them.
        envelope = finalize_envelope(INQUIRY_FIELDS)
        validated = validate_envelope(envelope)
        self.assertTrue(verify_envelope_digest(validated))


class KnpEnvelopeValidationTest(unittest.TestCase):
    """Envelope schema validation, fail-closed (§8 / §33)."""

    def test_accepts_the_sub_spec_envelope_example(self):
        envelope = finalize_envelope(COUNTER_OFFER_FIELDS)
        validated = validate_envelope(envelope)
        self.assertIsInstance(validated, NegotiationEnvelope)
        self.assertEqual(validated.capability, CAPABILITY)
        self.assertEqual(validated.protocol_version, KNP_PROTOCOL_VERSION)
        self.assertEqual(validated.negotiation_id, NEGOTIATION_ID)
        self.assertEqual(validated.exchange_id, EXCHANGE_ID)
        self.assertEqual(validated.message_id, MESSAGE_ID)
        self.assertEqual(validated.in_reply_to, IN_REPLY_TO)
        self.assertEqual(validated.action, "counter_offer")
        self.assertEqual(validated.as_dict()["digest"], envelope["digest"])

    def test_protocol_version_must_be_1_0(self):
        envelope = finalize_envelope(COUNTER_OFFER_FIELDS)
        for bad in ("2.0", "banana", "0.9"):
            with self.subTest(version=bad):
                self.assertEqual(
                    _error_code(lambda b=bad: validate_envelope({**envelope, "protocol_version": b})),
                    "protocol_version_unsupported",
                )

    def test_actor_only_buyer_or_merchant(self):
        envelope = finalize_envelope(COUNTER_OFFER_FIELDS)
        self.assertEqual(_error_code(lambda: validate_envelope({**envelope, "actor": "system"})), "schema_invalid")

    def test_unknown_action_is_rejected(self):
        envelope = finalize_envelope(COUNTER_OFFER_FIELDS)
        self.assertEqual(_error_code(lambda: validate_envelope({**envelope, "action": "purchase"})), "schema_invalid")

    def test_missing_required_fields_are_rejected(self):
        envelope = finalize_envelope(COUNTER_OFFER_FIELDS)
        without_negotiation = {k: v for k, v in envelope.items() if k != "negotiation_id"}
        self.assertEqual(_error_code(lambda: validate_envelope(without_negotiation)), "schema_invalid")
        without_digest = {k: v for k, v in envelope.items() if k != "digest"}
        self.assertEqual(_error_code(lambda: validate_envelope(without_digest)), "schema_invalid")
        self.assertEqual(_error_code(lambda: validate_envelope({**envelope, "capability": ""})), "schema_invalid")

    def test_non_rfc3339_created_at_is_rejected(self):
        envelope = finalize_envelope(COUNTER_OFFER_FIELDS)
        self.assertEqual(
            _error_code(lambda: validate_envelope({**envelope, "created_at": "2026-08-05 12:00:00"})),
            "schema_invalid",
        )

    def test_non_object_payload_is_rejected(self):
        envelope = finalize_envelope(COUNTER_OFFER_FIELDS)
        self.assertEqual(_error_code(lambda: validate_envelope({**envelope, "payload": []})), "schema_invalid")
        self.assertEqual(_error_code(lambda: validate_envelope({**envelope, "payload": "offer"})), "schema_invalid")

    def test_malformed_digest_is_rejected(self):
        envelope = finalize_envelope(COUNTER_OFFER_FIELDS)
        self.assertEqual(_error_code(lambda: validate_envelope({**envelope, "digest": "sha256:zzzz"})), "schema_invalid")
        self.assertEqual(_error_code(lambda: validate_envelope({**envelope, "digest": "md5:abc"})), "schema_invalid")

    def test_clarification_response_requires_in_reply_to(self):
        fields = dict(INQUIRY_FIELDS)
        fields["action"] = "clarification_response"
        fields["payload"] = {"type": "clarification_response", "answer": "delivery_before = 2026-08-20"}
        without_reply = {k: v for k, v in fields.items() if k != "in_reply_to"}
        self.assertEqual(_error_code(lambda: validate_envelope(finalize_envelope(without_reply))), "schema_invalid")
        with_reply = {**without_reply, "in_reply_to": MESSAGE_ID}
        validated = validate_envelope(finalize_envelope(with_reply))
        self.assertEqual(validated.action, "clarification_response")

    def test_knp_actions_vocabulary_is_frozen(self):
        self.assertEqual(
            KNP_ACTIONS,
            (
                "inquiry",
                "rfq",
                "offer",
                "counter_offer",
                "conditional_offer",
                "clarification",
                "clarification_response",
                "accept_nonbinding",
                "withdraw",
                "decline",
                "cancel",
            ),
        )

    def test_transport_signature_field_names(self):
        self.assertEqual(
            TRANSPORT_SIGNATURE_FIELDS,
            frozenset(
                {
                    "signature",
                    "transport_signature",
                    "http_message_signature",
                    "x_message_signature",
                }
            ),
        )


if __name__ == "__main__":
    unittest.main()

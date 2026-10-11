from __future__ import annotations
import unittest
from shopping_cli.core import negotiation as protocol
from shopping_cli.core.errors import ConflictError, ValidationError
from shopping_cli.core.money_authority import CURRENCY_TABLE_VERSION, decimal_token_to_minor
from shopping_cli.db.session import db_session
from shopping_cli.services import negotiation
import test_negotiation_gates_leaf as setup_fixture


class MoneyBoundariesTest(unittest.TestCase):
    def setUp(self):
        self.fixture = setup_fixture.NegotiationGatesLeafTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def gate(self, conn, price):
        decision = setup_fixture.make_decision(
            self.fixture.conversation_id,
            1,
            proposal=setup_fixture.make_proposal(unit_price=price),
            public_message="A consultation quote is available.",
        )
        return negotiation._merchant_gate(conn, self.fixture._conversation_row(conn), decision)

    def test_exact_decimal_discount_floor_equal_is_accepted_one_minor_below_is_not(self):
        with db_session(self.fixture.db_file) as c:
            c.execute("UPDATE products SET floor_price=0,max_discount_percent=10 WHERE sku='cup-1'")
            self.assertEqual(self.gate(c, 89.10).result, "accepted")
            self.assertEqual(self.gate(c, 89.09).reason_codes, ("below_floor",))

    def test_fractional_floor_is_not_rounded_down_to_permit_below_authorized_price(self):
        with db_session(self.fixture.db_file) as c:
            c.execute("UPDATE products SET floor_price=89.105,max_discount_percent=0 WHERE sku='cup-1'")
            self.assertEqual(self.gate(c, 89.10).reason_codes, ("below_floor",))
            self.assertEqual(self.gate(c, 89.11).result, "accepted")

    def test_exact_minor_authority_wins_over_stale_real_projection_and_preserves_sku_binding(self):
        with db_session(self.fixture.db_file) as c:
            c.execute("INSERT INTO merchant_money_authority VALUES ('seller-a','EXACT_MINOR',1,'t','t','t')")
            c.execute(
                "UPDATE products SET floor_price=5,price=99,price_minor_text='9900',floor_price_minor_text='8910',money_currency_table_version=? WHERE sku='cup-1'",
                (CURRENCY_TABLE_VERSION,),
            )
            self.assertEqual(self.gate(c, 89.09).reason_codes, ("below_floor",))
            self.assertEqual(self.gate(c, 89.10).result, "accepted")
            c.execute("UPDATE conversations SET merchant_id='seller-b' WHERE id=?", (self.fixture.conversation_id,))
            with self.assertRaises(ConflictError):
                self.gate(c, 100)

    def test_frozen_money_cannot_emit_automatic_price_acceptance(self):
        with db_session(self.fixture.db_file) as c:
            c.execute("INSERT INTO merchant_money_authority VALUES ('seller-a','FROZEN',1,'t',NULL,'t')")
            self.assertEqual(self.gate(c, 99).reason_codes, ("money_authority_frozen",))
            decision = setup_fixture.make_decision(
                self.fixture.conversation_id,
                1,
                action="decline",
                proposal=None,
                public_message="Cannot offer this consultation quote.",
            )
            self.assertEqual(
                negotiation._merchant_gate(c, self.fixture._conversation_row(c), decision).result, "accepted"
            )

    def test_huge_json_number_is_typed_validation_failure_not_overflow(self):
        proposal = setup_fixture.make_proposal(unit_price=10**400)
        decision = setup_fixture.make_decision(self.fixture.conversation_id, 1, proposal=proposal)
        with self.assertRaises(ValidationError):
            protocol.validate_contract("decision", decision)
        proposal["unit_price"] = 89.1
        protocol.validate_contract("decision", decision)

    def test_nonfinite_decimal_tokens_are_typed_invalid_and_normal_minor_contract_is_kept(self):
        for token in ("nan", "NaN", "sNaN", "Infinity", "-Infinity"):
            with self.subTest(token=token), self.assertRaises(ValidationError):
                decimal_token_to_minor("CNY", token)
        self.assertEqual(decimal_token_to_minor("CNY", "89.10"), "8910")
        with self.assertRaises(ValidationError):
            decimal_token_to_minor("CNY", "89.105")

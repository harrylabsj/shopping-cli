"""Cross-language fixture tests for the frozen shopping.negotiation/0.1 contracts.

The schemas under shopping_cli/contracts and the fixtures under
fixtures/negotiation are verbatim copies of the Kiwi contract set. Kiwi
validates the same fixtures with Ajv (TypeScript); here they are validated
with the self-contained Python validator so both languages agree on every
valid and invalid fixture without any runtime dependency on the Kiwi repo.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from shopping_cli.core import negotiation as protocol
from shopping_cli.core.errors import ValidationError

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT / "fixtures" / "negotiation"
SCHEMAS_DIR = ROOT / "shopping_cli" / "contracts" / "shopping.negotiation" / "0.1"

SCHEMA_NAMES = ("capabilities", "decision", "policy-result", "snapshot")


def load_fixture(name: str) -> object:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


class FrozenContractShapeTest(unittest.TestCase):
    def test_all_frozen_schemas_are_present_and_strict(self):
        for name in SCHEMA_NAMES:
            schema = protocol.load_contract_schema(name)
            self.assertEqual(schema.get("additionalProperties"), False, name)
            self.assertIn(f"shopping.negotiation/0.1/{name}.schema.json", str(schema.get("$id")))

    def test_schemas_on_disk_match_packaged_copies(self):
        for name in SCHEMA_NAMES:
            on_disk = json.loads((SCHEMAS_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk, protocol.load_contract_schema(name))

    def test_every_fixture_targets_a_known_schema(self):
        fixtures = sorted(FIXTURES_DIR.glob("*.json"))
        self.assertGreaterEqual(len(fixtures), 13)
        for fixture in fixtures:
            schema_name = fixture.name.split(".")[0]
            self.assertIn(schema_name, SCHEMA_NAMES, fixture.name)

    def test_valid_fixtures_pass_and_invalid_fixtures_fail(self):
        for fixture in sorted(FIXTURES_DIR.glob("*.json")):
            schema_name = fixture.name.split(".")[0]
            expected_valid = ".valid." in fixture.name
            with self.subTest(fixture=fixture.name):
                try:
                    protocol.validate_contract(schema_name, load_fixture(fixture.name))
                except ValidationError:
                    self.assertFalse(expected_valid, f"{fixture.name} should be valid")
                else:
                    self.assertTrue(expected_valid, f"{fixture.name} should be invalid")

    def test_capabilities_report_matches_valid_fixture(self):
        report = protocol.capabilities_report()
        protocol.validate_contract("capabilities", report)
        self.assertEqual(report, load_fixture("capabilities.local_marketplace.valid.json"))
        self.assertFalse(report["capabilities"]["orders"])


class SchemaValidatorTest(unittest.TestCase):
    def validate(self, value: object) -> None:
        protocol.validate_contract("decision", value)

    def valid_decision(self) -> dict:
        return dict(load_fixture("decision.counter.valid.json"))

    def test_type_list_allows_null_current_proposal(self):
        snapshot = load_fixture("snapshot.merchant.valid.json")
        self.assertIsInstance(snapshot, dict)
        protocol.validate_contract("snapshot", snapshot)

    def test_string_length_limits(self):
        decision = self.valid_decision()
        decision["conversation_id"] = ""
        with self.assertRaises(ValidationError):
            self.validate(decision)

    def test_numeric_bounds(self):
        decision = self.valid_decision()
        decision["proposal"]["quantity"] = 0
        with self.assertRaises(ValidationError):
            self.validate(decision)
        decision = self.valid_decision()
        decision["confidence"] = 1.5
        with self.assertRaises(ValidationError):
            self.validate(decision)

    def test_boolean_is_not_a_number(self):
        decision = self.valid_decision()
        decision["proposal"]["unit_price"] = True
        with self.assertRaises(ValidationError):
            self.validate(decision)

    def test_nested_additional_properties(self):
        decision = self.valid_decision()
        decision["proposal"]["stock"]["extra"] = 1
        with self.assertRaises(ValidationError):
            self.validate(decision)

    def test_max_items(self):
        decision = self.valid_decision()
        decision["open_issues"] = [f"issue-{index}" for index in range(33)]
        with self.assertRaises(ValidationError):
            self.validate(decision)

    def test_missing_required(self):
        decision = self.valid_decision()
        del decision["proposal"]["valid_until"]
        with self.assertRaises(ValidationError):
            self.validate(decision)

    def test_protocol_version_const(self):
        decision = self.valid_decision()
        decision["protocol_version"] = "shopping.negotiation/0.2"
        with self.assertRaises(ValidationError):
            self.validate(decision)


class StrictDateTimeFormatTest(unittest.TestCase):
    """format: date-time is enforced strictly, compatible with Kiwi's Ajv +
    ajv-formats: an explicit offset (Z or ±HH:MM) is required and naive or
    impossible timestamps are rejected at the schema stage."""

    def valid_decision(self) -> dict:
        return dict(load_fixture("decision.counter.valid.json"))

    def test_helper_accepts_offset_and_z(self):
        self.assertTrue(protocol.is_rfc3339_datetime("2026-08-03T15:00:00+08:00"))
        self.assertTrue(protocol.is_rfc3339_datetime("2026-08-03T07:00:00Z"))
        self.assertTrue(protocol.is_rfc3339_datetime("2026-08-03T07:00:00.123z"))

    def test_helper_rejects_naive_and_invalid(self):
        for value in (
            "2026-08-04T00:37:20",  # naive: no offset
            "2026-08-04 00:37:20",  # naive with space separator
            "2026-13-04T00:37:20Z",  # month 13
            "2026-02-30T00:37:20Z",  # impossible day
            "2026-08-04T25:37:20Z",  # hour 25
            "2026-08-04T00:37:20+0800",  # offset without colon
            "2026-08-04",
            "",
            123,
        ):
            self.assertFalse(protocol.is_rfc3339_datetime(value), value)

    def test_naive_valid_until_rejected(self):
        decision = self.valid_decision()
        decision["proposal"]["valid_until"] = "2026-08-04T00:37:20"
        with self.assertRaises(ValidationError):
            protocol.validate_contract("decision", decision)

    def test_naive_stock_observed_at_rejected(self):
        decision = self.valid_decision()
        decision["proposal"]["stock"]["observed_at"] = "2026-08-04T00:37:20"
        with self.assertRaises(ValidationError):
            protocol.validate_contract("decision", decision)

    def test_naive_delivery_eta_rejected(self):
        decision = self.valid_decision()
        decision["proposal"]["delivery"]["eta_start"] = "2026-08-04T00:37:20"
        with self.assertRaises(ValidationError):
            protocol.validate_contract("decision", decision)

    def test_impossible_date_rejected(self):
        decision = self.valid_decision()
        decision["proposal"]["valid_until"] = "2026-02-30T00:37:20Z"
        with self.assertRaises(ValidationError):
            protocol.validate_contract("decision", decision)

    def test_naive_snapshot_message_created_at_rejected(self):
        snapshot = load_fixture("snapshot.merchant.valid.json")
        self.assertIsInstance(snapshot, dict)
        snapshot["messages"][0]["created_at"] = "2026-08-04T00:37:20"
        with self.assertRaises(ValidationError):
            protocol.validate_contract("snapshot", snapshot)
        snapshot["messages"][0]["created_at"] = "2026-08-04T00:37:20+08:00"
        protocol.validate_contract("snapshot", snapshot)

    def test_normalize_db_timestamp_adds_offset_to_naive_local_time(self):
        normalized = protocol.normalize_db_timestamp("2026-08-04T00:37:20")
        self.assertTrue(protocol.is_rfc3339_datetime(normalized), normalized)
        self.assertRegex(normalized, r"(Z|[+-]\d\d:\d\d)$")
        self.assertTrue(normalized.startswith("2026-08-04T00:37:20"))

    def test_normalize_db_timestamp_keeps_existing_offset(self):
        self.assertEqual(
            protocol.normalize_db_timestamp("2026-08-04T00:37:20+08:00"),
            "2026-08-04T00:37:20+08:00",
        )

    def test_normalize_db_timestamp_falls_back_for_garbage(self):
        self.assertTrue(protocol.is_rfc3339_datetime(protocol.normalize_db_timestamp("not-a-date")))


if __name__ == "__main__":
    unittest.main()

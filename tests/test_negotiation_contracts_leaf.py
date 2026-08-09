"""Characterization tests for the negotiation_contracts leaf-module extraction.

The pure contract-validation / time-normalization / JSON-canonicalization
helpers moved move-only from ``shopping_cli.core.negotiation`` into the leaf
module ``shopping_cli.core.negotiation_contracts``. ``core.negotiation``
re-exports the same objects so every existing ``protocol.*`` access keeps its
exact surface, error types/messages, schema-loader cache behavior and call
signatures. These tests pin that contract of the split.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from shopping_cli.core import negotiation as protocol
from shopping_cli.core import negotiation_contracts as contracts
from shopping_cli.core.errors import ValidationError

# Names that physically moved into the leaf module and must be re-exported
# by ``core.negotiation`` as the *identical* objects.
MOVED_NAMES = (
    "PROTOCOL_VERSION",
    "CONTRACTS_DIR",
    "is_rfc3339_datetime",
    "parse_rfc3339",
    "now_rfc3339",
    "normalize_db_timestamp",
    "canonical_json",
    "load_contract_schema",
    "validate_contract",
    "capabilities_report",
)

# Orchestration-facing surface that must stay defined in ``core.negotiation``.
KEPT_NAMES = (
    "DECISION_ACTIONS",
    "POLICY_RESULTS",
    "STOCK_STATUSES",
    "MERCHANT_NEXT_ACTORS",
    "BUYER_NEXT_ACTORS",
    "BUYER_SENDERS",
    "MERCHANT_SENDERS",
    "MAX_DECISION_ATTEMPTS",
    "STOCK_OBSERVATION_MAX_AGE_SECONDS",
    "AUDIT_DECISION_SUBMITTED",
    "AUDIT_POLICY_ACCEPTED",
    "AUDIT_POLICY_DENIED",
    "AUDIT_HUMAN_REQUIRED",
    "snapshot_next_actor",
    "role_for_next_actor",
    "buyer_agent_identity",
    "truncate_text",
)


def _valid_decision() -> dict[str, Any]:
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    fixture = root / "fixtures" / "negotiation" / "decision.counter.valid.json"
    return json.loads(fixture.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", MOVED_NAMES)
def test_moved_helpers_live_in_leaf_module(name: str) -> None:
    assert hasattr(contracts, name), f"leaf module missing {name}"


@pytest.mark.parametrize("name", MOVED_NAMES)
def test_negotiation_re_exports_identical_objects(name: str) -> None:
    assert hasattr(protocol, name), f"core.negotiation no longer exposes {name}"
    assert getattr(protocol, name) is getattr(contracts, name), f"{name} is not re-exported by identity"


@pytest.mark.parametrize("name", KEPT_NAMES)
def test_orchestration_surface_stays_in_negotiation(name: str) -> None:
    assert hasattr(protocol, name), f"core.negotiation lost orchestration name {name}"
    assert not hasattr(contracts, name), f"leaf module unexpectedly owns {name}"


def test_validate_contract_call_signature_and_error_type() -> None:
    with pytest.raises(ValidationError):
        protocol.validate_contract("decision", {"protocol_version": protocol.PROTOCOL_VERSION})


@pytest.mark.parametrize(
    ("mutate", "expected_message"),
    [
        (lambda d: d.__setitem__("protocol_version", 123), "decision.protocol_version must be string"),
        (lambda d: d.__setitem__("extra_field", 1), "decision contains unsupported field: extra_field"),
        (
            lambda d: d["proposal"].__setitem__("valid_until", "2026-08-04T00:37:20"),
            "decision.proposal.valid_until must be an RFC 3339 date-time with an explicit offset (Z or ±HH:MM)",
        ),
        (
            lambda d: d.pop("conversation_id"),
            "decision.conversation_id is required",
        ),
    ],
)
def test_error_messages_preserved(mutate: Any, expected_message: str) -> None:
    decision = _valid_decision()
    mutate(decision)
    with pytest.raises(ValidationError) as excinfo:
        protocol.validate_contract("decision", decision)
    assert str(excinfo.value) == expected_message


def test_schema_loader_cache_behavior_preserved() -> None:
    first = protocol.load_contract_schema("decision")
    second = protocol.load_contract_schema("decision")
    assert first is second  # lru_cache returns the same cached dict object
    # The re-export is the same function object, so both import paths share
    # one cache (no double-loading, same hits/misses counters).
    assert protocol.load_contract_schema("snapshot") is contracts.load_contract_schema("snapshot")
    info = protocol.load_contract_schema.cache_info()
    assert info.currsize >= 1
    assert info.misses >= 1
    assert info.maxsize is None


def test_schema_loader_error_type_and_message_preserved() -> None:
    with pytest.raises(RuntimeError) as excinfo:
        protocol.load_contract_schema("does-not-exist")
    assert "frozen negotiation contract schema is missing:" in str(excinfo.value)


def test_canonical_json_is_deterministic_and_compact() -> None:
    value = {"b": 1, "a": "中"}
    expected = '{"a":"中","b":1}'
    assert protocol.canonical_json(value) == expected
    assert protocol.canonical_json(value) == contracts.canonical_json(value)
    assert protocol.canonical_json({"x": [2, 1]}) == '{"x":[2,1]}'


def test_parse_rfc3339_behavior() -> None:
    naive = protocol.parse_rfc3339("2026-08-04T00:37:20")
    assert naive is not None and naive.tzinfo is not None
    assert naive.utcoffset().total_seconds() == 0  # naive -> UTC
    z = protocol.parse_rfc3339("2026-08-04T00:37:20Z")
    assert z is not None and z.isoformat() == "2026-08-04T00:37:20+00:00"
    offset = protocol.parse_rfc3339("2026-08-04T00:37:20+08:00")
    assert offset is not None and offset.isoformat() == "2026-08-04T00:37:20+08:00"
    assert protocol.parse_rfc3339("not-a-date") is None
    assert protocol.parse_rfc3339("") is None
    assert protocol.parse_rfc3339(None) is None


def test_now_rfc3339_shape() -> None:
    value = protocol.now_rfc3339()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", value) is not None
    assert protocol.is_rfc3339_datetime(value)


def test_normalize_db_timestamp_keeps_existing_offset() -> None:
    assert protocol.normalize_db_timestamp("2026-08-04T00:37:20+08:00") == "2026-08-04T00:37:20+08:00"


def test_normalize_db_timestamp_adds_offset_to_naive_local_time() -> None:
    normalized = protocol.normalize_db_timestamp("2026-08-04T00:37:20")
    assert protocol.is_rfc3339_datetime(normalized)
    assert normalized.startswith("2026-08-04T00:37:20")
    assert re.search(r"(Z|[+-]\d\d:\d\d)$", normalized) is not None


def test_normalize_db_timestamp_falls_back_for_garbage() -> None:
    assert protocol.is_rfc3339_datetime(protocol.normalize_db_timestamp("not-a-date"))
    assert protocol.is_rfc3339_datetime(protocol.normalize_db_timestamp(None))


def test_capabilities_report_validates_and_stays_orderless() -> None:
    report = protocol.capabilities_report()
    protocol.validate_contract("capabilities", report)
    assert report["capabilities"]["orders"] is False
    assert report is not contracts.capabilities_report  # fresh dict each call


def test_role_and_identity_helpers_preserved() -> None:
    assert protocol.snapshot_next_actor("merchant_agent") == "merchant"
    assert protocol.snapshot_next_actor("buyer") == "buyer"
    assert protocol.snapshot_next_actor("operator") == "none"
    assert protocol.role_for_next_actor("merchant_agent") == "merchant"
    assert protocol.role_for_next_actor("buyer") == "buyer"
    assert protocol.role_for_next_actor("") == ""
    assert protocol.buyer_agent_identity("b-1") == "shopping-cli-buyer-agent:b-1"
    assert protocol.truncate_text("abcdef", 3) == "abc"
    assert protocol.truncate_text(None, 3) == ""

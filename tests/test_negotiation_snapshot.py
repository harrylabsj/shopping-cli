"""Characterization tests for the pure negotiation snapshot message projection."""

from __future__ import annotations

import re
from typing import Any

import pytest

from shopping_cli.core import negotiation as protocol
from shopping_cli.services.negotiation import _snapshot_message as negotiation_alias
from shopping_cli.services.negotiation_snapshot import snapshot_message


def _plain_message(**overrides: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": 7,
        "sender": "buyer",
        "created_at": "2026-08-09T10:00:00+08:00",
        "text": "hello",
    }
    message.update(overrides)
    return message


def test_plain_message_projects_public_fields() -> None:
    entry = snapshot_message(_plain_message())
    assert entry == {
        "id": 7,
        "sender_role": "buyer",
        "created_at": "2026-08-09T10:00:00+08:00",
        "public_message": "hello",
        "proposal": None,
    }
    assert "action" not in entry


def test_id_is_coerced_to_int() -> None:
    entry = snapshot_message(_plain_message(id="42"))
    assert entry["id"] == 42
    assert isinstance(entry["id"], int)


def test_sender_mapping_buyer_senders() -> None:
    for sender in protocol.BUYER_SENDERS:
        entry = snapshot_message(_plain_message(sender=sender))
        assert entry["sender_role"] == "buyer"


def test_sender_mapping_merchant_senders() -> None:
    for sender in ("merchant", "merchant_agent", "operator", "unknown"):
        entry = snapshot_message(_plain_message(sender=sender))
        assert entry["sender_role"] == "merchant"


def test_created_at_naive_local_time_is_normalized_with_offset() -> None:
    entry = snapshot_message(_plain_message(created_at="2026-08-09T10:00:00"))
    assert re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(Z|[+-]\d\d:\d\d)$", entry["created_at"])
    assert entry["created_at"].startswith("2026-08-09T10:00:00")


def test_valid_structured_decision_and_proposal() -> None:
    proposal = {"item_sku": "cup-1", "quantity": 2, "unit_price": 95.0}
    message = _plain_message(
        structured_payload={
            "protocol_version": protocol.PROTOCOL_VERSION,
            "decision": {"action": "counter", "proposal": proposal},
        }
    )
    entry = snapshot_message(message)
    assert entry["action"] == "counter"
    assert entry["proposal"] == proposal


def test_valid_structured_decision_without_proposal_sets_none() -> None:
    message = _plain_message(
        structured_payload={
            "protocol_version": protocol.PROTOCOL_VERSION,
            "decision": {"action": "ask"},
        }
    )
    entry = snapshot_message(message)
    assert entry["action"] == "ask"
    assert entry["proposal"] is None


def test_wrong_protocol_version_gates_structured_decision() -> None:
    message = _plain_message(
        structured_payload={
            "protocol_version": "shopping.negotiation/0.0",
            "decision": {"action": "counter", "proposal": {"item_sku": "cup-1"}},
        }
    )
    entry = snapshot_message(message)
    assert "action" not in entry
    assert entry["proposal"] is None


def test_invalid_action_is_omitted_but_proposal_is_kept() -> None:
    message = _plain_message(
        structured_payload={
            "protocol_version": protocol.PROTOCOL_VERSION,
            "decision": {"action": "not-a-real-action", "proposal": {"item_sku": "cup-1"}},
        }
    )
    entry = snapshot_message(message)
    assert "action" not in entry
    assert entry["proposal"] == {"item_sku": "cup-1"}


def test_non_dict_decision_sets_proposal_none_and_no_action() -> None:
    message = _plain_message(
        structured_payload={
            "protocol_version": protocol.PROTOCOL_VERSION,
            "decision": "counter",
        }
    )
    entry = snapshot_message(message)
    assert "action" not in entry
    assert entry["proposal"] is None


def test_non_dict_proposal_is_coerced_to_none() -> None:
    message = _plain_message(
        structured_payload={
            "protocol_version": protocol.PROTOCOL_VERSION,
            "decision": {"action": "counter", "proposal": "not-a-dict"},
        }
    )
    entry = snapshot_message(message)
    assert entry["action"] == "counter"
    assert entry["proposal"] is None


def test_text_is_truncated_to_2000() -> None:
    long_text = "x" * 5000
    entry = snapshot_message(_plain_message(text=long_text))
    assert entry["public_message"] == "x" * 2000
    assert len(entry["public_message"]) == 2000


def test_empty_or_none_text_projects_empty_public_message() -> None:
    for text in (None, ""):
        entry = snapshot_message(_plain_message(text=text))
        assert entry["public_message"] == ""


def test_backward_compatible_alias_matches_public_helper() -> None:
    assert negotiation_alias is snapshot_message
    message = _plain_message()
    assert negotiation_alias(message) == snapshot_message(message)


@pytest.mark.parametrize("missing", ["id", "sender", "created_at", "text"])
def test_required_public_fields_raise_key_error(missing: str) -> None:
    message = _plain_message()
    del message[missing]
    with pytest.raises(KeyError):
        snapshot_message(message)

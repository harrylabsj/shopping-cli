from dataclasses import FrozenInstanceError

import pytest

from shopping_cli.core.errors import ValidationError
from shopping_cli.services.negotiation_policy_result import (
    ACCEPTED_OUTCOME,
    GateOutcome,
    build_policy_result,
    human_required,
    rejected,
)


def test_accepted_outcome_is_accepted_with_empty_reason_codes() -> None:
    assert ACCEPTED_OUTCOME == GateOutcome("accepted", (), "")


def test_rejected_constructs_retryable_outcome_with_single_reason_code() -> None:
    outcome = rejected("unknown_product", "商品当前不可用，请重新获取快照。")
    assert outcome == GateOutcome("rejected_retryable", ("unknown_product",), "商品当前不可用，请重新获取快照。")


def test_human_required_constructs_human_outcome_with_single_reason_code() -> None:
    outcome = human_required("below_floor", "报价低于商家授权的自动磋商范围，需要人工处理。")
    assert outcome == GateOutcome("human_required", ("below_floor",), "报价低于商家授权的自动磋商范围，需要人工处理。")


def test_gate_outcome_is_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        ACCEPTED_OUTCOME.reason_codes = ("changed",)  # type: ignore[misc]


def test_build_policy_result_truncates_reason_codes_to_32() -> None:
    reason_codes = [f"code-{i}" for i in range(64)]
    payload = build_policy_result(
        "conv-1",
        "rejected_retryable",
        "merchant",
        reason_codes,
        "请调整数量。",
        2,
    )
    assert payload["reason_codes"] == [f"code-{i}" for i in range(32)]
    assert len(payload["reason_codes"]) == 32


def test_build_policy_result_truncates_public_reason_to_1000() -> None:
    payload = build_policy_result(
        "conv-1",
        "rejected_retryable",
        "merchant",
        [],
        "x" * 2000,
        2,
    )
    assert payload["public_reason"] == "x" * 1000
    assert len(payload["public_reason"]) == 1000


def test_build_policy_result_never_emits_negative_retries() -> None:
    payload = build_policy_result("conv-1", "rejected_retryable", "merchant", [], "再试一次。", -5)
    assert payload["retries_remaining"] == 0


def test_build_policy_result_omits_message_id_when_absent() -> None:
    payload = build_policy_result("conv-1", "accepted", "buyer", [], "决策已接受。", 0)
    assert "message_id" not in payload


def test_build_policy_result_includes_message_id_when_provided() -> None:
    payload = build_policy_result("conv-1", "accepted", "buyer", [], "决策已接受。", 0, message_id=42)
    assert payload["message_id"] == 42


def test_build_policy_result_sets_frozen_protocol_version() -> None:
    payload = build_policy_result("conv-1", "accepted", "none", [], "决策已接受。", 0)
    assert payload["protocol_version"] == "shopping.negotiation/0.1"


def test_build_policy_result_validates_against_frozen_contract() -> None:
    with pytest.raises(ValidationError):
        build_policy_result("conv-1", "not-a-result", "merchant", [], "", 0)

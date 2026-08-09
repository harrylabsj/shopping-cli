"""Characterization tests for the negotiation snapshot block projection leaf.

The pure snapshot projections — product / stock / delivery block shaping, the
stock-status bucket derivation, the newest proposal and the newest decision's
open issues — moved move-only from ``shopping_cli.services.negotiation`` into
the leaf module ``shopping_cli.services.negotiation_snapshot_projection``. The
facade re-imports the used ones under the same private ``_``-prefixed aliases,
so call order, error messages, the state machine, the public API and the
contract semantics are byte-identical.

These tests pin the leaf's public surface, the facade identity re-exports,
the exact projection outputs (including truncation and fail-closed shapes),
and that ``build_snapshot`` delegates to the leaf projections.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from shopping_cli.api.app import handle_request
from shopping_cli.core import negotiation as protocol
from shopping_cli.core.catalog import create_merchant, create_product, product_summary
from shopping_cli.core.conversations import append_message, ensure_conversation
from shopping_cli.core.policies import create_policy
from shopping_cli.db.session import db_session
from shopping_cli.services import negotiation as negotiation_service
from shopping_cli.services import negotiation_snapshot_projection as projection
from shopping_cli.services import tokens as token_service

# Names that physically moved into the leaf module.
MOVED_NAMES = (
    "stock_status_for",
    "project_product",
    "project_stock",
    "project_delivery",
    "latest_proposal",
    "latest_open_issues",
)

# The facade must re-export these under the identical private aliases. The
# leaf's ``stock_status_for`` is intentionally leaf-internal: ``project_stock``
# owns the only call site inside ``build_snapshot``.
FACADE_RE_EXPORTS = (
    ("project_product", "_project_product"),
    ("project_stock", "_project_stock"),
    ("project_delivery", "_project_delivery"),
    ("latest_proposal", "_latest_proposal"),
    ("latest_open_issues", "_latest_open_issues"),
)

FIXED_NOW = datetime(2026, 8, 9, 10, 0, 0, tzinfo=timezone.utc)


def _plain_product(**overrides: Any) -> dict[str, Any]:
    product: dict[str, Any] = {
        "sku": "cup-1",
        "title": "手写陶瓷杯",
        "currency": "CNY",
        "price": 99.0,
        "description": "景德镇手工陶瓷杯 350ml",
    }
    product.update(overrides)
    return product


def _decision_message(
    *,
    open_issues: Any = None,
    version: str | None = None,
) -> dict[str, Any]:
    return {
        "structured_payload": {
            "protocol_version": version or protocol.PROTOCOL_VERSION,
            "decision": {"action": "counter", "open_issues": open_issues},
        }
    }


# -- module boundary --------------------------------------------------------


@pytest.mark.parametrize("name", MOVED_NAMES)
def test_moved_helpers_live_in_leaf_module(name: str) -> None:
    assert hasattr(projection, name), f"leaf module missing {name}"


@pytest.mark.parametrize(("public_name", "private_alias"), FACADE_RE_EXPORTS)
def test_negotiation_re_exports_identical_objects(public_name: str, private_alias: str) -> None:
    assert hasattr(negotiation_service, private_alias), f"services.negotiation no longer exposes {private_alias}"
    assert getattr(negotiation_service, private_alias) is getattr(projection, public_name)


def test_stock_status_for_is_leaf_internal() -> None:
    # project_stock owns the only stock-status derivation inside build_snapshot;
    # the facade must not expose it as its own name.
    assert not hasattr(negotiation_service, "stock_status_for")
    assert not hasattr(negotiation_service, "_stock_status_for")


# -- stock_status_for -------------------------------------------------------


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [
        (0, "out_of_stock"),
        (1, "low"),
        (2, "low"),
        (3, "available"),
        (100, "available"),
    ],
)
def test_stock_status_buckets(quantity: int, expected: str) -> None:
    assert projection.stock_status_for(quantity) == expected


# -- project_product --------------------------------------------------------


def test_project_product_shapes_block() -> None:
    assert projection.project_product(_plain_product()) == {
        "sku": "cup-1",
        "title": "手写陶瓷杯",
        "currency": "CNY",
        "list_price": 99.0,
        "description": "景德镇手工陶瓷杯 350ml",
    }


def test_project_product_omits_absent_description() -> None:
    product = _plain_product()
    del product["description"]
    block = projection.project_product(product)
    assert block == {
        "sku": "cup-1",
        "title": "手写陶瓷杯",
        "currency": "CNY",
        "list_price": 99.0,
    }


def test_project_product_omits_empty_description() -> None:
    block = projection.project_product(_plain_product(description=""))
    assert "description" not in block


def test_project_product_truncates_to_schema_limits() -> None:
    block = projection.project_product(
        _plain_product(sku="x" * 200, title="y" * 600, currency="z" * 20, description="d" * 3000)
    )
    assert len(block["sku"]) == 128
    assert len(block["title"]) == 500
    assert len(block["currency"]) == 8
    assert len(block["description"]) == 2000


def test_project_product_coerces_price_to_float() -> None:
    block = projection.project_product(_plain_product(price="99"))
    assert block["list_price"] == 99.0
    assert isinstance(block["list_price"], float)


# -- project_stock ----------------------------------------------------------


def test_project_stock_shapes_block_and_derives_status() -> None:
    block = projection.project_stock(12, "2026-08-09T10:00:00+00:00")
    assert block == {
        "status": "available",
        "quantity": 12,
        "observed_at": "2026-08-09T10:00:00+00:00",
        "reserved": False,
        "source": {"backend": "local_marketplace", "observed_at": "2026-08-09T10:00:00+00:00"},
    }


def test_project_stock_low_bucket() -> None:
    assert projection.project_stock(2, "2026-08-09T10:00:00+00:00")["status"] == "low"
    assert projection.project_stock(0, "2026-08-09T10:00:00+00:00")["status"] == "out_of_stock"


# -- project_delivery -------------------------------------------------------


def test_project_delivery_deterministic_with_injected_now() -> None:
    block = projection.project_delivery({}, now=FIXED_NOW)
    assert block == {
        "eta_start": "2026-08-09T11:00:00+00:00",
        "eta_end": "2026-08-09T13:00:00+00:00",
        "fee": 0.0,
    }


def test_project_delivery_uses_rule_eta_minutes_and_fee() -> None:
    block = projection.project_delivery({"eta_minutes": 90, "fee": 12.5}, now=FIXED_NOW)
    assert block["eta_start"] == "2026-08-09T11:30:00+00:00"
    assert block["eta_end"] == "2026-08-09T13:30:00+00:00"
    assert block["fee"] == 12.5


def test_project_delivery_includes_notes_when_present() -> None:
    block = projection.project_delivery({"notes": "周六周日不发货"}, now=FIXED_NOW)
    assert block["notes"] == "周六周日不发货"


def test_project_delivery_omits_absent_or_empty_notes() -> None:
    assert "notes" not in projection.project_delivery({}, now=FIXED_NOW)
    assert "notes" not in projection.project_delivery({"notes": ""}, now=FIXED_NOW)


def test_project_delivery_truncates_notes_to_500() -> None:
    block = projection.project_delivery({"notes": "x" * 900}, now=FIXED_NOW)
    assert block["notes"] == "x" * 500


def test_project_delivery_accepts_none_rule() -> None:
    block = projection.project_delivery(None, now=FIXED_NOW)
    assert block["fee"] == 0.0
    assert "eta_start" in block


def test_project_delivery_uses_single_now_for_both_eta_bounds() -> None:
    # Both bounds must derive from the same injected ``now`` so the window is
    # exactly the configured span (eta_minutes + 120).
    block = projection.project_delivery({"eta_minutes": 60}, now=FIXED_NOW)
    start = datetime.fromisoformat(block["eta_start"])
    end = datetime.fromisoformat(block["eta_end"])
    assert (end - start).total_seconds() == 7200.0


# -- latest_proposal --------------------------------------------------------


def test_latest_proposal_returns_newest_dict() -> None:
    messages = [{"proposal": None}, {"proposal": {"unit_price": 89.0}}, {"proposal": {"unit_price": 88.0}}]
    assert projection.latest_proposal(messages) == {"unit_price": 88.0}


def test_latest_proposal_skips_none_and_missing() -> None:
    messages = [{"proposal": None}, {}, {"proposal": {"unit_price": 89.0}}]
    assert projection.latest_proposal(messages) == {"unit_price": 89.0}


def test_latest_proposal_empty_or_all_none() -> None:
    assert projection.latest_proposal([]) is None
    assert projection.latest_proposal([{"proposal": None}, {}]) is None


def test_latest_proposal_ignores_non_dict_proposal() -> None:
    messages = [{"proposal": "not-a-dict"}, {"proposal": {"unit_price": 89.0}}]
    assert projection.latest_proposal(messages) == {"unit_price": 89.0}


# -- latest_open_issues -----------------------------------------------------


def test_latest_open_issues_returns_newest_decision_list() -> None:
    messages = [
        _decision_message(open_issues=["old"]),
        {"structured_payload": {}},
        _decision_message(open_issues=["x", "y"]),
    ]
    assert projection.latest_open_issues(messages) == ["x", "y"]


def test_latest_open_issues_wrong_protocol_version_is_ignored() -> None:
    messages = [_decision_message(open_issues=["ignored"], version="shopping.negotiation/0.0")]
    assert projection.latest_open_issues(messages) == []


def test_latest_open_issues_non_list_is_fail_closed() -> None:
    assert projection.latest_open_issues([_decision_message(open_issues="not-a-list")]) == []
    assert projection.latest_open_issues([_decision_message(open_issues=None)]) == []
    assert projection.latest_open_issues([{"structured_payload": {"decision": "ask"}}]) == []


def test_latest_open_issues_empty_input() -> None:
    assert projection.latest_open_issues([]) == []


def test_latest_open_issues_truncates_and_filters() -> None:
    issues = ["", "  ", "keep", "x" * 600]
    assert projection.latest_open_issues([_decision_message(open_issues=issues)]) == ["keep", "x" * 500]


def test_latest_open_issues_caps_at_32() -> None:
    issues = [f"issue-{i}" for i in range(64)]
    assert projection.latest_open_issues([_decision_message(open_issues=issues)]) == [f"issue-{i}" for i in range(32)]


# -- build_snapshot delegation (DB-backed) ----------------------------------


class BuildSnapshotDelegationTest(unittest.TestCase):
    """``build_snapshot`` must compose the leaf projections around its DB reads."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self._tmp.name) / "marketplace.sqlite"
        with db_session(self.db_file) as conn:
            create_merchant(
                conn,
                merchant_id="seller-a",
                name="West Lake Tea",
                automation_boundaries="手写陶瓷杯最低可成交价 80 元",
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
            message = append_message(conn, self.conversation_id, "buyer", "ask_price", "买 2 件可以便宜一点吗？")
            self.buyer_message_id = int(message["id"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None, query: dict | None = None, token: str = ""):
        body = dict(payload or {})
        if token:
            body["_auth_token"] = token
        return handle_request(self.db_file, method, path, body, query or {})

    def test_snapshot_blocks_are_produced_by_the_leaf_projections(self) -> None:
        status, payload = self.call(
            "POST",
            "/negotiation/claims",
            {
                "conversation_id": self.conversation_id,
                "message_id": self.buyer_message_id,
                "idempotency_key": "agent-1:1:shopping.negotiation/0.1",
            },
            token=self.merchant_token,
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["claim"]["claimed"])

        status, payload = self.call(
            "GET",
            "/negotiation/snapshot",
            query={"conversation_id": self.conversation_id, "message_id": str(self.buyer_message_id)},
            token=self.merchant_token,
        )
        self.assertEqual(status, 200, payload)
        snapshot = payload["snapshot"]
        protocol.validate_contract("snapshot", snapshot)

        # product block is byte-identical to the leaf projection of the same
        # catalog row, proving build_snapshot delegates rather than re-shapes.
        with db_session(self.db_file) as conn:
            product = product_summary(conn, "cup-1")
        self.assertEqual(snapshot["product"], projection.project_product(product))
        # stock block is exactly reconstructible from its own quantity.
        self.assertEqual(
            snapshot["stock"],
            projection.project_stock(snapshot["stock"]["quantity"], snapshot["stock"]["observed_at"]),
        )
        self.assertEqual(
            snapshot["stock"]["status"], projection.stock_status_for(snapshot["stock"]["quantity"])
        )
        # delivery window is exactly the 120-minute span around eta_minutes.
        start = datetime.fromisoformat(snapshot["delivery"]["eta_start"])
        end = datetime.fromisoformat(snapshot["delivery"]["eta_end"])
        self.assertEqual((end - start).total_seconds(), 7200.0)
        self.assertEqual(snapshot["delivery"]["fee"], 0.0)
        raw = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("automation_boundaries", raw)
        self.assertNotIn("最低可成交价", raw)


if __name__ == "__main__":
    unittest.main()

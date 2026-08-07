"""Hosted A2A JSON-RPC endpoint (v2.4-W3) tests.

Covers the framework-agnostic core (`shopping_cli.a2a.hosted_server`), the
thin HTTP handler, the shared-host POST route on both ASGI stacks, the v14
``a2a_inbound_idempotency`` migration, and the KNP → shopping.negotiation/0.1
binding execution.

Invariants exercised:

* JSON-RPC frame negatives map to -32600 / -32601 / -32602;
* KNP envelope negatives (no Data Part, bad digest, bad protocol_version)
  map to -32050 with a §18 protocol_code;
* lossless actions execute through the authoritative ``submit_decision`` write
  path (policy gate) and return an A2A Message whose Data Part carries a KNP
  result envelope with a verifiable digest and ``in_reply_to`` = inbound
  message_id;
* idempotency (rc1 §3.6): same (sender_identity, message_id, digest) replays
  with no second business effect; same id + different digest fails closed as
  ``idempotency_conflict``;
* fail_closed (conditional_offer) never advances negotiation state;
* requires_human (rfq) lands a human-review flag and returns human_required;
* 404 gate for non-hosted / inactive / unknown agents (no existence oracle);
* auth missing → 401, invalid → 403;
* every inbound processing writes an audit event (actor system/a2a).

Binding: docs/a2a/shopping-cli-a2a-binding-1.0-rc1.md §3.1–§3.6, §4–§6
"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shopping_cli.agents.tools import record_heartbeat
from shopping_cli.api.app import create_app, handle_request
from shopping_cli.api.fallback_asgi import MarketplaceASGIApp
from shopping_cli.a2a.binding import negotiation_id_for_conversation
from shopping_cli.a2a.knp import finalize_envelope, verify_envelope_digest
from shopping_cli.core.catalog import create_merchant, create_product
from shopping_cli.core.conversations import append_message, conversation_messages, ensure_conversation
from shopping_cli.core.policies import create_policy
from shopping_cli.db.session import db_session
from shopping_cli.services import tokens as token_service

A2A_PATH = "/a2a/agents/{catalog_agent_id}"

MERCHANT_ID = "mrc-host"
BUYER_ID = "buyer-1"
SKU = "SKU-001"
CAPABILITY = "com.harrylabsj.kiwi.shopping.negotiation"
EXCHANGE_ID = "ex_01H5V8KXZqJ7Qp3mN2B6A"
TIMESTAMP = "2026-08-05T12:00:00Z"

AGENT_ID = f"shopping-cli-merchant-agent:{MERCHANT_ID}"
CAGT_ID = f"cagt_{AGENT_ID}"


def _rfc3339(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _future(seconds: int = 3600) -> str:
    return _rfc3339(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def _past(seconds: int = 60) -> str:
    return _rfc3339(datetime.now(timezone.utc) - timedelta(seconds=seconds))


def _offer_terms(quantity: int = 2, stock_quantity: int = 12) -> dict:
    """A KNP TermSet that is losslessly expressible as a legacy proposal."""
    return {
        "items": [
            {
                "sku": SKU,
                "quantity": {"value": quantity, "unit": "piece"},
                "unit_price": {"currency": "CNY", "amount_minor": 85000},
            }
        ],
        "fulfillment_terms": {
            "eta_start": _future(1800),
            "eta_end": _future(7200),
            "delivery_fee": {"currency": "CNY", "amount_minor": 500},
            "legacy_stock": {
                "status": "available",
                "quantity": stock_quantity,
                "observed_at": _past(30),
                "reserved": False,
            },
        },
        "valid_until": _future(3600),
    }


def _counter_offer_payload(terms: dict | None = None) -> dict:
    return {
        "type": "counter_offer",
        "offer_id": "off_02H5V8KXZqJ7Qp3mN2B6A",
        "responding_to_offer_id": "off_01H5V8KXZqJ7Qp3mN2B6A",
        "proposed_terms": terms if terms is not None else _offer_terms(),
    }


# ── seeding ───────────────────────────────────────────────────────────────────


def _seed(
    db_file: Path,
    *,
    stock: int = 12,
    boundaries: str = "",
) -> dict:
    """Create merchant + product + policy + hosted catalog agent + a
    conversation with one merchant message (so a buyer can reply)."""
    with db_session(db_file):
        pass  # initialize schema
    with db_session(db_file) as conn:
        create_merchant(
            conn,
            merchant_id=MERCHANT_ID,
            name="Hosted Tea Shop",
            automation_boundaries=boundaries,
            delivery_eta_minutes=60,
        )
        create_product(
            conn,
            merchant_id=MERCHANT_ID,
            sku=SKU,
            title="Longjing Tea",
            price=850.0,
            stock=stock,
            description="西湖龙井 250g",
        )
        create_policy(
            conn,
            merchant_id=MERCHANT_ID,
            code="return-7d",
            title="签收后 7 天内支持无理由退货。",
            body="签收后 7 天内支持无理由退货。",
        )
        record_heartbeat(conn, MERCHANT_ID, capabilities=["catalog"])
        conversation = ensure_conversation(conn, BUYER_ID, MERCHANT_ID, SKU)
        conversation_id = str(conversation["id"])
        buyer_token = token_service.issue_buyer_token(conn, BUYER_ID, conversation_id)
        merchant_token = token_service.issue_merchant_token(conn, MERCHANT_ID)
        message = append_message(
            conn,
            conversation_id,
            "merchant_agent",
            "negotiate",
            "请问您对这款商品有什么问题？",
        )
        merchant_message_id = int(message["id"])
        conn.commit()
    return {
        "conversation_id": conversation_id,
        "buyer_token": buyer_token,
        "merchant_token": merchant_token,
        "merchant_message_id": merchant_message_id,
    }


def _seed_non_publishable_agents(db_file: Path) -> None:
    """Add catalog agents that must NOT be addressable (404 / NotFoundError)."""
    from shopping_cli.agent_catalog.sqlite_repository import upsert_catalog_agent

    with db_session(db_file) as conn:
        upsert_catalog_agent(
            conn,
            catalog_agent_id="cagt_direct",
            merchant_id=MERCHANT_ID,
            display_name="Direct Agent",
            source_type="self_registered",
            lifecycle_status="active",
            verification_status="domain_verified",
            hosting_mode="direct",
        )
        upsert_catalog_agent(
            conn,
            catalog_agent_id="cagt_inactive",
            merchant_id=MERCHANT_ID,
            display_name="Inactive Agent",
            source_type="hosted",
            lifecycle_status="inactive",
            verification_status="commerce_verified",
            hosting_mode="hosted",
        )


# ── envelope / request builders ───────────────────────────────────────────────


def _envelope(
    conversation_id: str,
    *,
    action: str = "inquiry",
    actor: str = "buyer",
    message_id: str = "msg_01H5V8KXZqJ7Qp3mN2B6A",
    in_reply_to: str | None = None,
    payload: dict | None = None,
    public_message: str = "请告诉我交付时间。",
    negotiation_id: str | None = None,
    **overrides: object,
) -> dict:
    fields: dict[str, object] = {
        "capability": CAPABILITY,
        "protocol_version": "1.0",
        "negotiation_id": negotiation_id or negotiation_id_for_conversation(conversation_id),
        "exchange_id": EXCHANGE_ID,
        "message_id": message_id,
        "actor": actor,
        "action": action,
        "created_at": TIMESTAMP,
        "payload": payload if payload is not None else {"type": action, "subject": {"sku": SKU}},
        "public_message": public_message,
    }
    if in_reply_to is not None:
        fields["in_reply_to"] = in_reply_to
    fields.update(overrides)
    return finalize_envelope(fields)


def _message_send_body(
    envelope: dict,
    *,
    request_id: str = "req_1",
    context_id: str | None = None,
) -> dict:
    body: dict = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": str(envelope["message_id"]),
                "parts": [{"kind": "data", "data": {"knp_envelope": envelope}}],
            }
        },
    }
    if context_id is not None:
        body["params"]["contextId"] = context_id
    return body


def _result_envelope(body: dict) -> dict:
    """Extract the KNP result envelope from a JSON-RPC success response."""
    message = body["result"]["message"]
    data_part = next(part for part in message["parts"] if part.get("kind") == "data")
    return data_part["data"]["knp_envelope"]


# ── Fake FastAPI harness (fastapi is an optional dependency) ──────────────────


class FakeFastAPI:
    def __init__(self, *args, **kwargs):
        self.state = SimpleNamespace()
        self.routes = []
        self.exception_handlers = {}

    def exception_handler(self, exc_type):
        def decorator(func):
            self.exception_handlers[exc_type] = func
            return func

        return decorator

    def get(self, path):
        return self._route("GET", path)

    def post(self, path):
        return self._route("POST", path)

    def patch(self, path):
        return self._route("PATCH", path)

    def _route(self, method, path):
        def decorator(func):
            self.routes.append(SimpleNamespace(methods={method}, path=path, endpoint=func))
            return func

        return decorator


class _HostedA2aTestCase(unittest.TestCase):
    """Shared env patching + dual-stack request helpers."""

    def setUp(self):
        self._base_env = patch.dict(
            "os.environ",
            {
                "SHOPPING_HOSTED_A2A_BASE_URL": "https://shopping.example",
                "SHOPPING_ADMIN_TOKEN": "test-admin-token-a2a",
            },
            clear=False,
        )
        self._base_env.start()

    def tearDown(self):
        self._base_env.stop()

    def _new_db(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name) / "test.sqlite"

    # ── direct handler (fallback dispatch path) ───────────────────────────

    def call_jsonrpc(
        self,
        db_file: Path,
        body: dict,
        *,
        token: str = "",
        catalog_agent_id: str = CAGT_ID,
    ) -> tuple[int, dict]:
        payload = dict(body)
        if token:
            payload["_auth_token"] = token
        return handle_request(
            db_file,
            "POST",
            A2A_PATH.format(catalog_agent_id=catalog_agent_id),
            payload,
            {},
        )

    # ── fallback ASGI ─────────────────────────────────────────────────────

    async def _asgi_post(self, app, path, body, headers=None):
        sent = []
        received = False
        body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")

        async def receive():
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        async def send(message):
            sent.append(message)

        req_headers = [(b"content-type", b"application/json")]
        for key, value in (headers or {}).items():
            req_headers.append((str(key).lower().encode("latin1"), str(value).encode("latin1")))

        await app(
            {
                "type": "http",
                "method": "POST",
                "path": path,
                "query_string": b"",
                "headers": req_headers,
            },
            receive,
            send,
        )
        status = next(m["status"] for m in sent if m["type"] == "http.response.start")
        resp_body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        parsed = json.loads(resp_body.decode("utf-8")) if resp_body else {}
        return status, parsed

    def _fallback_post(self, app, path, body, headers=None):
        return asyncio.run(self._asgi_post(app, path, body, headers=headers))

    # ── FastAPI harness ───────────────────────────────────────────────────

    def _fastapi_app(self, db_file):
        with patch("shopping_cli.api.app.FastAPI", FakeFastAPI):
            return create_app(db_file)

    def _fastapi_post(self, app, path, catalog_agent_id, body, authorization=""):
        endpoint = next(
            (
                route.endpoint
                for route in app.routes
                if route.path == path and "POST" in route.methods
            ),
            None,
        )
        if endpoint is None:
            raise AssertionError(f"No POST route for {path}")
        try:
            result = endpoint(catalog_agent_id, body, authorization)
        except Exception as exc:  # route raised → exception_handler mapping
            for exc_type, handler in app.exception_handlers.items():
                if isinstance(exc, exc_type):
                    response = handler(None, exc)
                    body_bytes = getattr(response, "body", b"")
                    if isinstance(body_bytes, str):
                        body_bytes = body_bytes.encode("utf-8")
                    return response.status_code, json.loads(body_bytes.decode("utf-8") or "{}")
            raise

        status = result.status_code
        body_bytes = getattr(result, "body", b"")
        if isinstance(body_bytes, str):
            body_bytes = body_bytes.encode("utf-8")
        parsed = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        return status, parsed

    # ── assertions ────────────────────────────────────────────────────────

    def assert_result_outcome(self, body: dict, outcome: str) -> None:
        """Assert the response is a JSON-RPC success carrying a KNP result
        envelope with the given outcome and a verifiable digest."""
        self.assertEqual(body["jsonrpc"], "2.0")
        self.assertIn("result", body)
        envelope = _result_envelope(body)
        self.assertEqual(envelope["payload"]["outcome"], outcome)
        self.assertTrue(
            verify_envelope_digest(envelope),
            "result envelope digest must be verifiable",
        )
        return envelope

    def assert_protocol_error(self, body: dict, protocol_code: str) -> None:
        self.assertEqual(body["jsonrpc"], "2.0")
        error = body["error"]
        self.assertEqual(error["code"], -32050)
        self.assertEqual(error["data"]["protocol_code"], protocol_code)


# ── JSON-RPC frame negatives ──────────────────────────────────────────────────


class A2AJsonRpcFrameTest(_HostedA2aTestCase):
    """Frame-level negatives map to -32600 / -32601 / -32602."""

    def setUp(self):
        super().setUp()
        self.db_file = self._new_db()
        self.seed = _seed(self.db_file)

    def _post(self, body, token=None):
        return self.call_jsonrpc(self.db_file, body, token=token or self.seed["buyer_token"])

    def test_bad_jsonrpc_version_is_invalid_request(self):
        body = _message_send_body(_envelope(self.seed["conversation_id"]))
        body["jsonrpc"] = "1.0"
        status, resp = self._post(body)
        self.assertEqual(status, 400)
        self.assertEqual(resp["error"]["code"], -32600)

    def test_missing_method_is_invalid_request(self):
        body = _message_send_body(_envelope(self.seed["conversation_id"]))
        del body["method"]
        status, resp = self._post(body)
        self.assertEqual(status, 400)
        self.assertEqual(resp["error"]["code"], -32600)

    def test_non_string_id_is_invalid_request(self):
        body = _message_send_body(_envelope(self.seed["conversation_id"]))
        body["id"] = 42
        status, resp = self._post(body)
        self.assertEqual(status, 400)
        self.assertEqual(resp["error"]["code"], -32600)

    def test_unknown_method_is_method_not_found(self):
        body = _message_send_body(_envelope(self.seed["conversation_id"]))
        body["method"] = "tasks/get"
        status, resp = self._post(body)
        self.assertEqual(status, 200)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_params_not_object_is_invalid_params(self):
        body = _message_send_body(_envelope(self.seed["conversation_id"]))
        body["params"] = [1, 2, 3]
        status, resp = self._post(body)
        self.assertEqual(status, 200)
        self.assertEqual(resp["error"]["code"], -32602)

    def test_missing_message_is_invalid_params(self):
        body = _message_send_body(_envelope(self.seed["conversation_id"]))
        del body["params"]["message"]
        status, resp = self._post(body)
        self.assertEqual(status, 200)
        self.assertEqual(resp["error"]["code"], -32602)

    def test_echoes_request_id(self):
        body = _message_send_body(
            _envelope(
                self.seed["conversation_id"],
                in_reply_to=f"msg_legacy_{self.seed['merchant_message_id']}",
                action="accept_nonbinding",
                payload={
                    "type": "accept_nonbinding",
                    "offer_id": "off_01",
                    "terms_digest": f"sha256:{'a' * 64}",
                },
            ),
            request_id="req-echo-77",
        )
        status, resp = self._post(body)
        self.assertEqual(status, 200)
        self.assertEqual(resp["id"], "req-echo-77")


# ── KNP envelope negatives ────────────────────────────────────────────────────


class A2AEnvelopeTest(_HostedA2aTestCase):
    """KNP envelope negatives map to -32050 with a §18 protocol_code."""

    def setUp(self):
        super().setUp()
        self.db_file = self._new_db()
        self.seed = _seed(self.db_file)

    def _post(self, body):
        return self.call_jsonrpc(self.db_file, body, token=self.seed["buyer_token"])

    def test_no_data_part_is_schema_invalid(self):
        envelope = _envelope(self.seed["conversation_id"])
        body = _message_send_body(envelope)
        body["params"]["message"]["parts"] = [{"kind": "text", "text": "hello"}]
        status, resp = self._post(body)
        self.assertEqual(status, 200)
        self.assert_protocol_error(resp, "schema_invalid")

    def test_multiple_data_parts_are_schema_invalid(self):
        envelope = _envelope(self.seed["conversation_id"])
        body = _message_send_body(envelope)
        body["params"]["message"]["parts"] = [
            {"kind": "data", "data": {"knp_envelope": envelope}},
            {"kind": "data", "data": {"knp_envelope": dict(envelope)}},
        ]
        status, resp = self._post(body)
        self.assertEqual(status, 200)
        self.assert_protocol_error(resp, "schema_invalid")

    def test_data_part_non_object_is_schema_invalid(self):
        envelope = _envelope(self.seed["conversation_id"])
        body = _message_send_body(envelope)
        body["params"]["message"]["parts"] = [{"kind": "data", "data": "not-an-object"}]
        status, resp = self._post(body)
        self.assertEqual(status, 200)
        self.assert_protocol_error(resp, "schema_invalid")

    def test_bad_protocol_version_is_protocol_version_unsupported(self):
        envelope = _envelope(self.seed["conversation_id"], protocol_version="0.9")
        body = _message_send_body(envelope)
        status, resp = self._post(body)
        self.assertEqual(status, 200)
        self.assert_protocol_error(resp, "protocol_version_unsupported")

    def test_tampered_digest_is_schema_invalid(self):
        envelope = _envelope(self.seed["conversation_id"])
        envelope["digest"] = f"sha256:{'b' * 64}"
        body = _message_send_body(envelope)
        status, resp = self._post(body)
        self.assertEqual(status, 200)
        self.assert_protocol_error(resp, "schema_invalid")

    def test_unknown_action_is_schema_invalid(self):
        envelope = _envelope(
            self.seed["conversation_id"],
            action="not_a_knp_action",
            payload={"type": "not_a_knp_action"},
        )
        body = _message_send_body(envelope)
        status, resp = self._post(body)
        self.assertEqual(status, 200)
        self.assert_protocol_error(resp, "schema_invalid")

    def test_merchant_actor_is_rejected_for_buyer_token(self):
        envelope = _envelope(
            self.seed["conversation_id"],
            actor="merchant",
            action="offer",
            payload={"type": "offer", "offer_id": "off_1", "terms": _offer_terms()},
        )
        body = _message_send_body(envelope)
        status, resp = self._post(body)
        self.assertEqual(status, 200)
        self.assert_protocol_error(resp, "identity_rejected")


# ── lossless execution ────────────────────────────────────────────────────────


class A2ALosslessTest(_HostedA2aTestCase):
    """Lossless actions execute through the policy-gated write path."""

    def setUp(self):
        super().setUp()
        self.db_file = self._new_db()
        self.seed = _seed(self.db_file, stock=12, boundaries="")

    def _send(self, envelope, request_id="req_1"):
        body = _message_send_body(envelope, request_id=request_id)
        return self.call_jsonrpc(self.db_file, body, token=self.seed["buyer_token"])

    def _message_count(self) -> int:
        with db_session(self.db_file) as conn:
            return len(conversation_messages(conn, self.seed["conversation_id"]))

    def test_inquiry_is_written_and_returns_result_envelope(self):
        envelope = _envelope(
            self.seed["conversation_id"],
            action="inquiry",
            in_reply_to=f"msg_legacy_{self.seed['merchant_message_id']}",
            payload={
                "type": "inquiry",
                "subject": {"sku": SKU},
                "questions": [{"code": "delivery.estimated_date"}],
            },
            public_message="请问明天能送到吗？",
        )
        status, body = self._send(envelope)
        self.assertEqual(status, 200)
        result = self.assert_result_outcome(body, "accepted")
        self.assertEqual(result["in_reply_to"], envelope["message_id"])
        self.assertEqual(result["actor"], "merchant")
        self.assertTrue(result["message_id"].startswith("msg_legacy_"))
        # The legacy message was written as the buyer's ask decision.
        with db_session(self.db_file) as conn:
            messages = conversation_messages(conn, self.seed["conversation_id"])
        self.assertEqual(len(messages), 2)
        written = messages[1]
        self.assertEqual(written["sender"], "buyer")
        decision = written["structured_payload"]["decision"]
        self.assertEqual(decision["action"], "ask")
        self.assertEqual(decision["open_issues"], ["delivery.estimated_date"])
        # Conversation advanced to the merchant.
        with db_session(self.db_file) as conn:
            row = conn.execute(
                "select status, next_actor from conversations where id = ?",
                (self.seed["conversation_id"],),
            ).fetchone()
        self.assertEqual(row["status"], "waiting_merchant")
        self.assertEqual(row["next_actor"], "merchant_agent")

    def test_accept_nonbinding_is_accepted_and_non_binding(self):
        envelope = _envelope(
            self.seed["conversation_id"],
            action="accept_nonbinding",
            in_reply_to=f"msg_legacy_{self.seed['merchant_message_id']}",
            payload={
                "type": "accept_nonbinding",
                "offer_id": "off_01H5V8KXZqJ7Qp3mN2B6A",
                "terms_digest": f"sha256:{'a' * 64}",
            },
            public_message="我接受这个报价（非约束性）。",
        )
        status, body = self._send(envelope)
        self.assertEqual(status, 200)
        result = self.assert_result_outcome(body, "accepted")
        self.assertEqual(result["payload"]["legacy_message_id"], self.seed["merchant_message_id"] + 1)
        with db_session(self.db_file) as conn:
            messages = conversation_messages(conn, self.seed["conversation_id"])
        written = messages[-1]
        self.assertEqual(written["structured_payload"]["decision"]["action"], "accept_nonbinding")
        # Consultation-only invariant: no order/payment/reservation semantics.
        self.assertNotIn("order", json.dumps(written["structured_payload"]).lower())

    def test_policy_gate_rejects_counter_offer_over_stock(self):
        envelope = _envelope(
            self.seed["conversation_id"],
            action="counter_offer",
            in_reply_to=f"msg_legacy_{self.seed['merchant_message_id']}",
            payload=_counter_offer_payload(_offer_terms(quantity=99, stock_quantity=12)),
            public_message="单价 850 元，购买 99 件。",
        )
        status, body = self._send(envelope)
        self.assertEqual(status, 200)
        result = self.assert_result_outcome(body, "rejected_retryable")
        self.assertEqual(result["payload"]["outcome"], "rejected_retryable")
        # No message written; the claim stays processing for a retry.
        self.assertEqual(self._message_count(), 1)
        with db_session(self.db_file) as conn:
            row = conn.execute(
                "select status from conversations where id = ?",
                (self.seed["conversation_id"],),
            ).fetchone()
        self.assertEqual(row["status"], "waiting_buyer")

    def test_policy_gate_accepts_counter_offer_within_stock(self):
        envelope = _envelope(
            self.seed["conversation_id"],
            action="counter_offer",
            in_reply_to=f"msg_legacy_{self.seed['merchant_message_id']}",
            payload=_counter_offer_payload(_offer_terms(quantity=2, stock_quantity=12)),
            public_message="单价 850 元，购买 2 件。",
        )
        status, body = self._send(envelope)
        self.assertEqual(status, 200)
        self.assert_result_outcome(body, "accepted")
        self.assertEqual(self._message_count(), 2)


# ── idempotency ───────────────────────────────────────────────────────────────


class A2AIdempotencyTest(_HostedA2aTestCase):
    """rc1 §3.6 — same id + same digest replays; same id + different digest
    fails closed as idempotency_conflict."""

    def setUp(self):
        super().setUp()
        self.db_file = self._new_db()
        self.seed = _seed(self.db_file)

    def _accept_nonbinding_envelope(self, message_id="msg_idem_01", public_message="接受报价。"):
        return _envelope(
            self.seed["conversation_id"],
            action="accept_nonbinding",
            in_reply_to=f"msg_legacy_{self.seed['merchant_message_id']}",
            message_id=message_id,
            payload={
                "type": "accept_nonbinding",
                "offer_id": "off_idem",
                "terms_digest": f"sha256:{'c' * 64}",
            },
            public_message=public_message,
        )

    def test_replay_same_digest_returns_same_result_no_second_write(self):
        envelope = self._accept_nonbinding_envelope()
        body = _message_send_body(envelope, request_id="req_1")
        status, first = self.call_jsonrpc(self.db_file, body, token=self.seed["buyer_token"])
        self.assertEqual(status, 200)
        self.assertEqual(first["result"]["message"]["messageId"], first["result"]["message"]["messageId"])

        with db_session(self.db_file) as conn:
            count_after_first = len(conversation_messages(conn, self.seed["conversation_id"]))

        status, second = self.call_jsonrpc(self.db_file, body, token=self.seed["buyer_token"])
        self.assertEqual(status, 200)
        # Byte-identical result, no duplicate business effect.
        self.assertEqual(second["result"], first["result"])

        with db_session(self.db_file) as conn:
            count_after_second = len(conversation_messages(conn, self.seed["conversation_id"]))
        self.assertEqual(count_after_second, count_after_first)

    def test_replay_is_system_a2a_audited(self):
        envelope = self._accept_nonbinding_envelope()
        body = _message_send_body(envelope, request_id="req_1")
        self.call_jsonrpc(self.db_file, body, token=self.seed["buyer_token"])
        self.call_jsonrpc(self.db_file, body, token=self.seed["buyer_token"])
        with db_session(self.db_file) as conn:
            rows = conn.execute(
                "select details_json from audit_events where actor = 'system/a2a' order by id",
            ).fetchall()
        replayed = [
            json.loads(row["details_json"])
            for row in rows
            if json.loads(row["details_json"]).get("replayed") is True
        ]
        self.assertEqual(len(replayed), 1)

    def test_same_id_different_digest_is_idempotency_conflict(self):
        envelope = self._accept_nonbinding_envelope()
        body = _message_send_body(envelope, request_id="req_1")
        status, _ = self.call_jsonrpc(self.db_file, body, token=self.seed["buyer_token"])
        self.assertEqual(status, 200)

        # Same message_id, changed content → different digest → conflict.
        conflicting = self._accept_nonbinding_envelope(public_message="不同内容的报价。")
        self.assertNotEqual(conflicting["digest"], envelope["digest"])
        body["params"]["message"]["parts"] = [
            {"kind": "data", "data": {"knp_envelope": conflicting}}
        ]
        status, resp = self.call_jsonrpc(self.db_file, body, token=self.seed["buyer_token"])
        self.assertEqual(status, 200)
        self.assert_protocol_error(resp, "idempotency_conflict")

        # No additional legacy message was written.
        with db_session(self.db_file) as conn:
            count = len(conversation_messages(conn, self.seed["conversation_id"]))
        self.assertEqual(count, 2)

    def test_second_reply_to_advanced_conversation_is_state_conflict_and_released(self):
        """A new KNP message replying to the same merchant message after the
        conversation advanced fails closed as state_conflict, is audited, and
        the idempotency claim is released so a later retry can re-process."""
        first = self._accept_nonbinding_envelope(message_id="msg_step_1")
        status, _ = self.call_jsonrpc(
            self.db_file, _message_send_body(first, request_id="req_1"), token=self.seed["buyer_token"]
        )
        self.assertEqual(status, 200)

        # Conversation advanced to the merchant; a new buyer reply to the same
        # merchant message is no longer this buyer's turn.
        second = self._accept_nonbinding_envelope(message_id="msg_step_2")
        status, resp = self.call_jsonrpc(
            self.db_file, _message_send_body(second, request_id="req_2"), token=self.seed["buyer_token"]
        )
        self.assertEqual(status, 200)
        self.assert_protocol_error(resp, "state_conflict")

        with db_session(self.db_file) as conn:
            # The failed attempt was audited with the protocol error code.
            row = conn.execute(
                "select details_json from audit_events where actor = 'system/a2a' and details_json like '%state_conflict%' order by id desc limit 1",
            ).fetchone()
            # The idempotency claim for the failed message was released, so a
            # fresh retry (after the merchant advances the turn back) can
            # re-process the message id.
            claim = conn.execute(
                "select 1 from a2a_inbound_idempotency where sender_identity = ? and message_id = ?",
                (BUYER_ID, second["message_id"]),
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row["details_json"])["error_code"], "state_conflict")
        self.assertIsNone(claim)


# ── fail_closed / requires_human ──────────────────────────────────────────────


class A2AFailClosedTest(_HostedA2aTestCase):
    """fail_closed classification returns a structured KNP error and never
    advances negotiation state."""

    def setUp(self):
        super().setUp()
        self.db_file = self._new_db()
        self.seed = _seed(self.db_file)

    def test_conditional_offer_is_capability_incompatible(self):
        envelope = _envelope(
            self.seed["conversation_id"],
            action="conditional_offer",
            payload={
                "type": "conditional_offer",
                "offer_id": "off_cond",
                "base_terms": {},
                "conditions": [],
            },
            public_message="条件报价。",
        )
        body = _message_send_body(envelope)
        status, resp = self.call_jsonrpc(self.db_file, body, token=self.seed["buyer_token"])
        self.assertEqual(status, 200)
        self.assert_protocol_error(resp, "capability_incompatible")

        # State not advanced: no message written, conversation unchanged.
        with db_session(self.db_file) as conn:
            count = len(conversation_messages(conn, self.seed["conversation_id"]))
            row = conn.execute(
                "select status from conversations where id = ?",
                (self.seed["conversation_id"],),
            ).fetchone()
        self.assertEqual(count, 1)
        self.assertEqual(row["status"], "waiting_buyer")

    def test_fail_closed_is_idempotently_replayable(self):
        envelope = _envelope(
            self.seed["conversation_id"],
            action="conditional_offer",
            payload={
                "type": "conditional_offer",
                "offer_id": "off_cond",
                "base_terms": {},
                "conditions": [],
            },
            public_message="条件报价。",
        )
        body = _message_send_body(envelope, request_id="req_1")
        status, first = self.call_jsonrpc(self.db_file, body, token=self.seed["buyer_token"])
        self.assertEqual(status, 200)
        status, second = self.call_jsonrpc(self.db_file, body, token=self.seed["buyer_token"])
        self.assertEqual(status, 200)
        self.assertEqual(second["error"], first["error"])


class A2ARequiresHumanTest(_HostedA2aTestCase):
    """requires_human classification lands a human-review flag."""

    def setUp(self):
        super().setUp()
        self.db_file = self._new_db()
        self.seed = _seed(self.db_file)

    def test_rfq_routes_to_human_review(self):
        envelope = _envelope(
            self.seed["conversation_id"],
            action="rfq",
            in_reply_to=f"msg_legacy_{self.seed['merchant_message_id']}",
            payload={"type": "rfq", "items": [{"sku": SKU, "quantity": {"value": 200, "unit": "piece"}}]},
            public_message="请提供 200 件的报价。",
        )
        body = _message_send_body(envelope)
        status, resp = self.call_jsonrpc(self.db_file, body, token=self.seed["buyer_token"])
        self.assertEqual(status, 200)
        result = self.assert_result_outcome(resp, "human_required")

        with db_session(self.db_file) as conn:
            flags = conn.execute(
                "select reason, severity, conversation_id from moderation_flags",
            ).fetchall()
            row = conn.execute(
                "select status, next_actor from conversations where id = ?",
                (self.seed["conversation_id"],),
            ).fetchone()
        self.assertEqual(len(flags), 1)
        self.assertEqual(flags[0]["reason"], "knp_action_unsupported:rfq")
        self.assertEqual(flags[0]["severity"], "review")
        self.assertEqual(flags[0]["conversation_id"], self.seed["conversation_id"])
        self.assertEqual(row["status"], "human_required")
        self.assertEqual(result["in_reply_to"], envelope["message_id"])


# ── route gate: 404 + auth ────────────────────────────────────────────────────


class A2ARouteGateTest(_HostedA2aTestCase):
    """404 gate for non-hosted/inactive/unknown agents and auth 401/403."""

    def setUp(self):
        super().setUp()
        self.db_file = self._new_db()
        self.seed = _seed(self.db_file)
        _seed_non_publishable_agents(self.db_file)

    def _body(self):
        envelope = _envelope(
            self.seed["conversation_id"],
            in_reply_to=f"msg_legacy_{self.seed['merchant_message_id']}",
        )
        return _message_send_body(envelope)

    def test_unknown_agent_is_404(self):
        status, body = self.call_jsonrpc(
            self.db_file, self._body(), token=self.seed["buyer_token"], catalog_agent_id="cagt_unknown"
        )
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])
        self.assertEqual(body["error"], "Unknown catalog agent: cagt_unknown")

    def test_non_hosted_agent_is_404(self):
        status, body = self.call_jsonrpc(
            self.db_file, self._body(), token=self.seed["buyer_token"], catalog_agent_id="cagt_direct"
        )
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_inactive_agent_is_404(self):
        status, body = self.call_jsonrpc(
            self.db_file, self._body(), token=self.seed["buyer_token"], catalog_agent_id="cagt_inactive"
        )
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_404_reveals_no_existence_details(self):
        """Non-hosted, inactive, and unknown ids are indistinguishable."""
        for cagt_id in ("cagt_direct", "cagt_inactive", "cagt_unknown"):
            with self.subTest(catalog_agent_id=cagt_id):
                status, body = self.call_jsonrpc(
                    self.db_file, self._body(), token=self.seed["buyer_token"], catalog_agent_id=cagt_id
                )
                self.assertEqual(status, 404)
                self.assertEqual(body["error"], f"Unknown catalog agent: {cagt_id}")

    def test_missing_auth_is_401(self):
        status, body = self.call_jsonrpc(self.db_file, self._body())
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], -32051)
        self.assertEqual(body["error"]["data"]["protocol_code"], "authentication_required")

    def test_invalid_auth_is_403(self):
        status, body = self.call_jsonrpc(self.db_file, self._body(), token="shopping_buyer_bogus_token")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], -32051)
        self.assertEqual(body["error"]["data"]["protocol_code"], "authorization_failed")

    def test_merchant_token_is_403(self):
        status, body = self.call_jsonrpc(self.db_file, self._body(), token=self.seed["merchant_token"])
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["data"]["protocol_code"], "authorization_failed")

    def test_auth_missing_on_unknown_agent_is_401(self):
        """Auth precedes the 404 gate (transport before domain)."""
        status, body = self.call_jsonrpc(self.db_file, self._body(), catalog_agent_id="cagt_unknown")
        self.assertEqual(status, 401)


# ── audit ─────────────────────────────────────────────────────────────────────


class A2AAuditTest(_HostedA2aTestCase):
    """Every inbound processing writes a system/a2a audit event."""

    def setUp(self):
        super().setUp()
        self.db_file = self._new_db()
        self.seed = _seed(self.db_file)

    def _audit_rows(self):
        with db_session(self.db_file) as conn:
            rows = conn.execute(
                "select actor, event, details_json from audit_events order by id",
            ).fetchall()
        return [
            {
                "actor": row["actor"],
                "event": row["event"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    def test_lossless_processing_is_audited_without_payload_content(self):
        envelope = _envelope(
            self.seed["conversation_id"],
            in_reply_to=f"msg_legacy_{self.seed['merchant_message_id']}",
            payload={
                "type": "inquiry",
                "subject": {"sku": SKU},
                "questions": [{"code": "delivery.estimated_date"}],
            },
            public_message="请问明天能送到吗？",
        )
        body = _message_send_body(envelope)
        self.call_jsonrpc(self.db_file, body, token=self.seed["buyer_token"])

        events = self._audit_rows()
        a2a = [row for row in events if row["actor"] == "system/a2a" and row["event"] == "a2a_inbound_message"]
        self.assertEqual(len(a2a), 1)
        details = a2a[0]["details"]
        self.assertEqual(details["message_id"], envelope["message_id"])
        self.assertEqual(details["classification"], "lossless")
        self.assertEqual(details["outcome"], "accepted")
        self.assertIsNotNone(details.get("legacy_message_id"))
        # Payload content must not leak into the audit trail.
        serialized = json.dumps(details)
        self.assertNotIn("请问明天能送到吗", serialized)
        self.assertNotIn("delivery.estimated_date", serialized)

    def test_protocol_error_is_audited_with_error_code(self):
        envelope = _envelope(
            self.seed["conversation_id"],
            action="conditional_offer",
            payload={"type": "conditional_offer", "offer_id": "off_1", "base_terms": {}, "conditions": []},
            public_message="条件报价。",
        )
        body = _message_send_body(envelope)
        self.call_jsonrpc(self.db_file, body, token=self.seed["buyer_token"])

        events = self._audit_rows()
        a2a = [row for row in events if row["actor"] == "system/a2a" and row["event"] == "a2a_inbound_message"]
        self.assertEqual(len(a2a), 1)
        self.assertEqual(a2a[0]["details"]["classification"], "fail_closed")
        self.assertEqual(a2a[0]["details"]["error_code"], "capability_incompatible")


# ── schema migration v13 → v14 ────────────────────────────────────────────────


class A2AMigrationTest(_HostedA2aTestCase):
    """The v14 migration creates the a2a_inbound_idempotency ledger."""

    def test_v13_to_v14_upgrade_creates_idempotency_table(self):
        db_file = self._new_db()
        with db_session(db_file):
            pass  # init at v14
        with closing(sqlite3.connect(db_file)) as raw:
            raw.execute("drop table if exists a2a_inbound_idempotency")
            raw.execute("pragma user_version = 13")
            raw.commit()

        with db_session(db_file) as conn:
            columns = {
                row["name"] for row in conn.execute("pragma table_info(a2a_inbound_idempotency)").fetchall()
            }
            user_version = conn.execute("pragma user_version").fetchone()[0]

        self.assertEqual(user_version, 16)
        self.assertEqual(
            columns,
            {"sender_identity", "message_id", "digest", "status", "response_json", "created_at", "updated_at"},
        )

    def test_fresh_init_has_idempotency_table(self):
        db_file = self._new_db()
        with db_session(db_file) as conn:
            columns = {
                row["name"] for row in conn.execute("pragma table_info(a2a_inbound_idempotency)").fetchall()
            }
        self.assertEqual(
            columns,
            {"sender_identity", "message_id", "digest", "status", "response_json", "created_at", "updated_at"},
        )

    def test_idempotency_primary_key_prevents_duplicate_rows(self):
        db_file = self._new_db()
        with db_session(db_file) as conn:
            conn.execute(
                """
                insert into a2a_inbound_idempotency(
                    sender_identity, message_id, digest, status, response_json, created_at, updated_at
                ) values ('buyer-1', 'msg_1', 'sha256:aaaa', 'completed', '{}', '2026-08-06T00:00:00', '2026-08-06T00:00:00')
                """
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    insert into a2a_inbound_idempotency(
                        sender_identity, message_id, digest, status, response_json, created_at, updated_at
                    ) values ('buyer-1', 'msg_1', 'sha256:bbbb', 'processing', '{}', '2026-08-06T00:00:00', '2026-08-06T00:00:00')
                    """
                )


# ── dual-stack routing ────────────────────────────────────────────────────────


class A2ADualStackRoutingTest(_HostedA2aTestCase):
    """The POST route is registered and behaves identically on both stacks."""

    def setUp(self):
        super().setUp()
        self.db_file = self._new_db()
        self.seed = _seed(self.db_file)
        _seed_non_publishable_agents(self.db_file)

    def _envelope(self):
        return _envelope(
            self.seed["conversation_id"],
            in_reply_to=f"msg_legacy_{self.seed['merchant_message_id']}",
            payload={
                "type": "inquiry",
                "subject": {"sku": SKU},
                "questions": [{"code": "delivery.estimated_date"}],
            },
            public_message="请问明天能送到吗？",
        )

    def _body(self):
        return _message_send_body(self._envelope())

    # ── fallback ASGI ─────────────────────────────────────────────────────

    def test_fallback_route_processes_message_send(self):
        app = MarketplaceASGIApp(self.db_file)
        status, body = self._fallback_post(
            app,
            A2A_PATH.format(catalog_agent_id=CAGT_ID),
            self._body(),
            headers={"Authorization": f"Bearer {self.seed['buyer_token']}"},
        )
        self.assertEqual(status, 200)
        self.assert_result_outcome(body, "accepted")

    def test_fallback_route_404_unknown_agent(self):
        app = MarketplaceASGIApp(self.db_file)
        status, body = self._fallback_post(
            app,
            A2A_PATH.format(catalog_agent_id="cagt_unknown"),
            self._body(),
            headers={"Authorization": f"Bearer {self.seed['buyer_token']}"},
        )
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_fallback_route_401_missing_auth(self):
        app = MarketplaceASGIApp(self.db_file)
        status, body = self._fallback_post(app, A2A_PATH.format(catalog_agent_id=CAGT_ID), self._body())
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["data"]["protocol_code"], "authentication_required")

    def test_fallback_route_403_invalid_auth(self):
        app = MarketplaceASGIApp(self.db_file)
        status, body = self._fallback_post(
            app,
            A2A_PATH.format(catalog_agent_id=CAGT_ID),
            self._body(),
            headers={"Authorization": "Bearer shopping_buyer_bogus"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["data"]["protocol_code"], "authorization_failed")

    # ── FastAPI harness ───────────────────────────────────────────────────

    def test_fastapi_route_processes_message_send(self):
        app = self._fastapi_app(self.db_file)
        status, body = self._fastapi_post(
            app,
            A2A_PATH,
            CAGT_ID,
            self._body(),
            authorization=f"Bearer {self.seed['buyer_token']}",
        )
        self.assertEqual(status, 200)
        self.assert_result_outcome(body, "accepted")

    def test_fastapi_route_404_unknown_agent(self):
        app = self._fastapi_app(self.db_file)
        status, body = self._fastapi_post(
            app,
            A2A_PATH,
            "cagt_unknown",
            self._body(),
            authorization=f"Bearer {self.seed['buyer_token']}",
        )
        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])

    def test_fastapi_route_401_missing_auth(self):
        app = self._fastapi_app(self.db_file)
        status, body = self._fastapi_post(app, A2A_PATH, CAGT_ID, self._body())
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["data"]["protocol_code"], "authentication_required")

    # ── registry consistency ─────────────────────────────────────────────

    def test_route_in_registry(self):
        from shopping_cli.api.route_registry import route_info, routes_for_group

        paths = {route.path: route.methods for route in route_info()}
        self.assertIn(A2A_PATH, paths)
        self.assertEqual(paths[A2A_PATH], {"POST"})
        self.assertIn(A2A_PATH, {route.path for route in routes_for_group("a2a")})
        self.assertIn(A2A_PATH, {route.path for route in routes_for_group("marketplace")})

    def test_fastapi_app_registers_route(self):
        app = self._fastapi_app(self.db_file)
        route_paths = {
            route.path for route in getattr(app, "routes", []) if hasattr(route, "path")
        }
        self.assertIn(A2A_PATH, route_paths)

    def test_context_id_is_echoed(self):
        envelope = self._envelope()
        body = _message_send_body(envelope, context_id="ctx-negotiation-1")
        status, resp = self.call_jsonrpc(self.db_file, body, token=self.seed["buyer_token"])
        self.assertEqual(status, 200)
        self.assertEqual(resp["result"]["message"].get("contextId"), "ctx-negotiation-1")


if __name__ == "__main__":
    unittest.main()

"""Dual-stack tests for Agent Catalog write routes (§10.2–§10.4, §6.2).

Covers register/refresh/verify/claim across the fallback ASGI and FastAPI
routes, real idempotency claim/replay, per-actor + per-domain rate limits
(§17.4), §10.3 permission gates, the §6.2 HTTPS domain-control claim
challenge, and §23 audit events.  All network I/O is mocked — the bounded
verification queue, the verification service, and the identity verifier are
replaced by fakes, so no test ever touches the wire.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shopping_cli.agent_catalog.sqlite_repository import (
    require_catalog_agent,
    set_verification_status,
    upsert_catalog_agent,
)
from shopping_cli.api.app import create_app
from shopping_cli.api.fallback_asgi import MarketplaceASGIApp
from shopping_cli.api.route_registry import route_info
from shopping_cli.core import catalog
from shopping_cli.db.session import db_session
from shopping_cli.services import tokens as token_service

TEST_ADMIN_TOKEN = "test-admin-token-catalog-writes"
TEST_WORKER_TOKEN = "test-verification-worker-token"

DOMAIN = "merchant.example"
CARD_URL = f"https://{DOMAIN}/agent-card.json"


# ── Fakes (no wire) ──────────────────────────────────────────────────────────


class FakeQueue:
    def __init__(self) -> None:
        self.tasks: list[tuple[str, str, str]] = []

    def enqueue(self, catalog_agent_id: str, *, kind: str, actor: str, wait: bool = False):
        self.tasks.append((catalog_agent_id, kind, actor))
        return SimpleNamespace(task_id=f"vt-test-{len(self.tasks)}")


def _fake_stage(stage: str = "profile", outcome: str = "passed", target: str = "profile_valid") -> SimpleNamespace:
    return SimpleNamespace(
        stage=stage,
        outcome=outcome,
        target_status=target,
        reason="",
        verification_id=1,
        snapshot_ids=(1,),
    )


def _fake_verify_result(
    catalog_agent_id: str,
    status: str = "commerce_verified",
    previous: str = "discovered",
    stages: list | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        catalog_agent_id=catalog_agent_id,
        previous_status=previous,
        status=status,
        stages=stages or [_fake_stage()],
    )


class FakeVerificationService:
    def __init__(self, result: SimpleNamespace) -> None:
        self.result = result

    def verify(self, catalog_agent_id: str, actor: str = "verification_worker") -> SimpleNamespace:
        return self.result


class FakeIdentityVerifier:
    def __init__(self, passed: bool = True) -> None:
        self.passed = passed

    def verify_domain_control(self, canonical_domain: str, declared: dict | None = None) -> SimpleNamespace:
        if self.passed:
            return SimpleNamespace(passed=True, reason="https domain control verified")
        return SimpleNamespace(passed=False, reason="domain control challenge failed")


def _audit_events(db_file: Path) -> list[tuple[str, str, dict]]:
    with db_session(db_file) as conn:
        rows = conn.execute(
            "select event, actor, details_json from audit_events order by id"
        ).fetchall()
        return [(r["event"], r["actor"], json.loads(r["details_json"] or "{}")) for r in rows]


class AgentCatalogWritesApiTest(unittest.TestCase):
    def setUp(self):
        self._env_patcher = patch.dict(
            os.environ,
            {
                "SHOPPING_ADMIN_TOKEN": TEST_ADMIN_TOKEN,
                "SHOPPING_VERIFICATION_WORKER_TOKEN": TEST_WORKER_TOKEN,
            },
            clear=False,
        )
        self._env_patcher.start()
        self._fake_queue = FakeQueue()
        self._queue_patcher = patch(
            "shopping_cli.api.handlers.agent_catalog._verification_queue",
            return_value=self._fake_queue,
        )
        self._queue_patcher.start()

    def tearDown(self):
        self._queue_patcher.stop()
        self._env_patcher.stop()

    # ── seed helpers ──────────────────────────────────────────────────────────

    def _seed_merchant(self, db_file: Path, merchant_id: str = "mrc-writes") -> str:
        with db_session(db_file) as conn:
            catalog.create_merchant(
                conn,
                merchant_id=merchant_id,
                name="Write Merchant",
                city="Hangzhou",
                service_area="Xihu",
                tags=["electronics"],
                contact="writes@example.com",
                automation_boundaries="full-auto",
            )
            token = token_service.issue_merchant_token(conn, merchant_id)
            return token

    def _seed_catalog_agent(
        self,
        db_file: Path,
        catalog_agent_id: str = "cagt_write_001",
        *,
        merchant_id: str = "",
        source_type: str = "self_registered",
        hosting_mode: str = "direct",
        verification_status: str = "discovered",
        canonical_domain: str = DOMAIN,
    ) -> None:
        with db_session(db_file) as conn:
            upsert_catalog_agent(
                conn,
                catalog_agent_id=catalog_agent_id,
                merchant_id=merchant_id,
                display_name=canonical_domain,
                canonical_domain=canonical_domain,
                agent_type="commerce",
                source_type=source_type,
                lifecycle_status="active",
                verification_status=verification_status,
                hosting_mode=hosting_mode,
            )

    # ── ASGI infra ────────────────────────────────────────────────────────────

    async def _asgi(self, app, method, path, body=None, headers=None, query_string=""):
        sent = []
        body_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
        received = False

        async def receive():
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        async def send(message):
            sent.append(message)

        hdrs = [(b"content-type", b"application/json")]
        if headers:
            hdrs += [(k.lower().encode("latin1"), v.encode("latin1")) for k, v in headers.items()]
        await app(
            {
                "type": "http",
                "method": method,
                "path": path,
                "query_string": query_string.encode("utf-8"),
                "headers": hdrs,
            },
            receive,
            send,
        )
        status = next(
            message["status"] for message in sent if message["type"] == "http.response.start"
        )
        body_out = b"".join(
            message.get("body", b"") for message in sent if message["type"] == "http.response.body"
        )
        return status, json.loads(body_out.decode("utf-8") or "{}")

    def _request(self, app, method, path, body=None, headers=None, query_string=""):
        return asyncio.run(self._asgi(app, method, path, body, headers, query_string))

    def _post(self, db_file, path, body, token="", idempotency_key=""):
        headers = {}
        if token:
            headers["authorization"] = f"Bearer {token}"
        if idempotency_key:
            headers["idempotency-key"] = idempotency_key
        return self._request(MarketplaceASGIApp(db_file), "POST", path, body, headers)

    # ── FastAPI helper ────────────────────────────────────────────────────────

    def _fastapi_post(self, app, path, payload, authorization="", idempotency_key="", **path_kwargs):
        from shopping_cli.api import app as app_module
        if app_module.FastAPI is None:
            return None
        endpoint = next(
            (route.endpoint for route in app.routes if route.path == path and "POST" in route.methods),
            None,
        )
        if endpoint is None:
            raise AssertionError(f"No POST route found for {path}")
        try:
            return 200, endpoint(
                payload=payload,
                authorization=authorization,
                idempotency_key=idempotency_key,
                **path_kwargs,
            )
        except Exception as exc:
            for exc_type, handler in app.exception_handlers.items():
                if isinstance(exc, exc_type):
                    response = handler(None, exc)
                    return response.status_code, json.loads(response.body.decode("utf-8"))
            raise

    # ═══════════════════════════════════════════════════════════════════════════
    # 1. POST /v1/agent-catalog/agents/register
    # ═══════════════════════════════════════════════════════════════════════════

    def test_register_creates_discovered_agent_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            status, body = self._post(
                db_file,
                "/v1/agent-catalog/agents/register",
                {"domain": DOMAIN, "agent_card_url": CARD_URL, "idempotency_key": "reg-1"},
            )

            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertTrue(body["verification_enqueued"])
            agent = body["catalog_agent"]
            self.assertEqual(agent["canonical_domain"], DOMAIN)
            self.assertEqual(agent["source_type"], "self_registered")
            self.assertEqual(agent["verification"]["status"], "discovered")
            self.assertEqual(agent["hosting"]["mode"], "direct")

            # §23 audit — catalog_agent_registered
            events = [e for e in _audit_events(db_file) if e[0] == "catalog_agent_registered"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][1], "cli")
            self.assertEqual(events[0][2]["canonical_domain"], DOMAIN)
            self.assertEqual(self._fake_queue.tasks[-1][1], "verify")

    def test_register_missing_domain_validation_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            status, body = self._post(db_file, "/v1/agent-catalog/agents/register", {})
            self.assertEqual(status, 400)
            self.assertFalse(body["ok"])

    def test_register_rejects_url_domain_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            status, body = self._post(
                db_file, "/v1/agent-catalog/agents/register", {"domain": "https://merchant.example"}
            )
            self.assertEqual(status, 400)
            self.assertIn("invalid canonical domain", body["error"])

    def test_register_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            payload = {"domain": DOMAIN, "idempotency_key": "reg-idem"}
            status1, body1 = self._post(db_file, "/v1/agent-catalog/agents/register", payload)
            status2, body2 = self._post(db_file, "/v1/agent-catalog/agents/register", payload)

            self.assertEqual(status1, 200)
            self.assertEqual(status2, 200)
            self.assertTrue(body2["idempotent"])
            self.assertEqual(
                body2["catalog_agent"]["catalog_agent_id"],
                body1["catalog_agent"]["catalog_agent_id"],
            )
            # Only one record + one audit event.
            events = [e for e in _audit_events(db_file) if e[0] == "catalog_agent_registered"]
            self.assertEqual(len(events), 1)

    def test_register_replay_with_different_request_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._post(
                db_file,
                "/v1/agent-catalog/agents/register",
                {"domain": DOMAIN, "idempotency_key": "reg-same"},
            )
            status, body = self._post(
                db_file,
                "/v1/agent-catalog/agents/register",
                {"domain": "other.example", "idempotency_key": "reg-same"},
            )
            self.assertEqual(status, 409)
            self.assertIn("reused with a different request", body["error"])

    def test_register_actor_rate_limit_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            with patch.dict(
                os.environ,
                {"SHOPPING_AGENT_CATALOG_WRITE_RATE_LIMIT_PER_MINUTE": "3"},
                clear=False,
            ):
                for i in range(3):
                    status, _ = self._post(
                        db_file,
                        "/v1/agent-catalog/agents/register",
                        {"domain": f"r{i}.example", "idempotency_key": f"rate-{i}"},
                        token=TEST_ADMIN_TOKEN,
                    )
                    self.assertEqual(status, 200, f"request {i} should succeed")
                status, body = self._post(
                    db_file,
                    "/v1/agent-catalog/agents/register",
                    {"domain": "r3.example", "idempotency_key": "rate-3"},
                    token=TEST_ADMIN_TOKEN,
                )
            self.assertEqual(status, 429)
            self.assertIn("rate limit", body["error"])

    def test_register_per_domain_limit_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            with patch.dict(
                os.environ,
                {"SHOPPING_AGENT_CATALOG_REGISTER_DOMAIN_LIMIT_PER_HOUR": "1"},
                clear=False,
            ):
                status, body = self._post(
                    db_file,
                    "/v1/agent-catalog/agents/register",
                    {"domain": DOMAIN, "idempotency_key": "pd-1"},
                )
                self.assertEqual(status, 200)
                # Make the live record re-registerable (terminal state) so the
                # second request reaches the per-domain budget check.
                cid = body["catalog_agent"]["catalog_agent_id"]
                with db_session(db_file) as conn:
                    set_verification_status(conn, cid, "rejected")
                status2, body2 = self._post(
                    db_file,
                    "/v1/agent-catalog/agents/register",
                    {"domain": DOMAIN, "idempotency_key": "pd-2"},
                )
            self.assertEqual(status2, 429)
            self.assertIn("registration rate limit", body2["error"])

    def test_register_live_domain_conflicts_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._post(
                db_file,
                "/v1/agent-catalog/agents/register",
                {"domain": DOMAIN, "idempotency_key": "conf-1"},
            )
            status, body = self._post(
                db_file,
                "/v1/agent-catalog/agents/register",
                {"domain": DOMAIN, "idempotency_key": "conf-2"},
            )
            self.assertEqual(status, 409)
            self.assertIn("already registered", body["error"])

    def test_register_with_merchant_binding_admin_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file, "mrc-reg")
            status, body = self._post(
                db_file,
                "/v1/agent-catalog/agents/register",
                {"domain": DOMAIN, "merchant_id": "mrc-reg", "idempotency_key": "mreg-1"},
                token=TEST_ADMIN_TOKEN,
            )
            self.assertEqual(status, 200)
            agent = body["catalog_agent"]
            self.assertEqual(agent["merchant"]["id"], "mrc-reg")
            events = [e for e in _audit_events(db_file) if e[0] == "catalog_agent_registered"]
            self.assertEqual(events[-1][1], "admin")

    def test_register_fastapi(self):
        from shopping_cli.api import app as app_module
        if app_module.FastAPI is None:
            self.skipTest("FastAPI not installed")

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            app = create_app(db_file)
            result = self._fastapi_post(
                app,
                "/v1/agent-catalog/agents/register",
                {"domain": DOMAIN, "idempotency_key": "fast-reg"},
            )
            self.assertIsNotNone(result)
            status, body = result
            self.assertEqual(status, 200)
            self.assertTrue(body["ok"])
            self.assertEqual(body["catalog_agent"]["canonical_domain"], DOMAIN)
            self.assertEqual(body["catalog_agent"]["verification"]["status"], "discovered")

    # ═══════════════════════════════════════════════════════════════════════════
    # 2. POST /v1/agent-catalog/agents/{id}/refresh
    # ═══════════════════════════════════════════════════════════════════════════

    def test_refresh_requires_authorization_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_catalog_agent(db_file, "cagt_refresh")
            status, body = self._post(
                db_file, "/v1/agent-catalog/agents/cagt_refresh/refresh", {}
            )
            self.assertEqual(status, 403)
            self.assertIn("authorization required", body["error"])

    def test_refresh_admin_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_catalog_agent(db_file, "cagt_refresh")
            status, body = self._post(
                db_file,
                "/v1/agent-catalog/agents/cagt_refresh/refresh",
                {"idempotency_key": "rf-1"},
                token=TEST_ADMIN_TOKEN,
            )
            self.assertEqual(status, 200)
            self.assertTrue(body["refresh_enqueued"])
            self.assertEqual(body["verification_status"], "discovered")
            self.assertEqual(self._fake_queue.tasks[-1][0], "cagt_refresh")
            self.assertEqual(self._fake_queue.tasks[-1][1], "refresh")

    def test_refresh_owner_merchant_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            merchant_token = self._seed_merchant(db_file, "mrc-owner")
            self._seed_catalog_agent(db_file, "cagt_refresh", merchant_id="mrc-owner")
            status, body = self._post(
                db_file,
                "/v1/agent-catalog/agents/cagt_refresh/refresh",
                {"idempotency_key": "rf-owner"},
                token=merchant_token,
            )
            self.assertEqual(status, 200)
            self.assertTrue(body["refresh_enqueued"])
            # The refresh audit (catalog_agent_refreshed) is written by the
            # verification worker when it executes the task; at enqueue time
            # the handler records the resolved owner-merchant actor on the task.
            self.assertEqual(self._fake_queue.tasks[-1][0], "cagt_refresh")
            self.assertEqual(self._fake_queue.tasks[-1][1], "refresh")
            self.assertEqual(self._fake_queue.tasks[-1][2], "merchant:mrc-owner")

    def test_refresh_verification_worker_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_catalog_agent(db_file, "cagt_refresh")
            status, body = self._post(
                db_file,
                "/v1/agent-catalog/agents/cagt_refresh/refresh",
                {"idempotency_key": "rf-worker"},
                token=TEST_WORKER_TOKEN,
            )
            self.assertEqual(status, 200)
            self.assertTrue(body["refresh_enqueued"])

    def test_refresh_wrong_merchant_denied_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            token = self._seed_merchant(db_file, "mrc-a")
            self._seed_merchant(db_file, "mrc-b")
            self._seed_catalog_agent(db_file, "cagt_refresh", merchant_id="mrc-b")
            status, body = self._post(
                db_file,
                "/v1/agent-catalog/agents/cagt_refresh/refresh",
                {"idempotency_key": "rf-wrong"},
                token=token,
            )
            self.assertEqual(status, 403)

    def test_refresh_unknown_agent_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            status, body = self._post(
                db_file,
                "/v1/agent-catalog/agents/cagt_missing/refresh",
                {},
                token=TEST_ADMIN_TOKEN,
            )
            self.assertEqual(status, 404)

    def test_refresh_fastapi(self):
        from shopping_cli.api import app as app_module
        if app_module.FastAPI is None:
            self.skipTest("FastAPI not installed")

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_catalog_agent(db_file, "cagt_refresh")
            app = create_app(db_file)
            result = self._fastapi_post(
                app,
                "/v1/agent-catalog/agents/{catalog_agent_id}/refresh",
                {"idempotency_key": "fast-rf"},
                authorization=f"Bearer {TEST_ADMIN_TOKEN}",
                catalog_agent_id="cagt_refresh",
            )
            self.assertIsNotNone(result)
            status, body = result
            self.assertEqual(status, 200)
            self.assertTrue(body["refresh_enqueued"])

    # ═══════════════════════════════════════════════════════════════════════════
    # 3. POST /v1/agent-catalog/agents/{id}/verify
    # ═══════════════════════════════════════════════════════════════════════════

    def _patch_verify_service(self, result):
        return patch(
            "shopping_cli.api.handlers.agent_catalog._verification_service",
            return_value=FakeVerificationService(result),
        )

    def test_verify_runs_synchronously_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_catalog_agent(db_file, "cagt_verify")
            result = _fake_verify_result(
                "cagt_verify",
                status="commerce_verified",
                previous="discovered",
                stages=[
                    _fake_stage("profile", "passed", "profile_valid"),
                    _fake_stage("domain_control", "passed", "domain_verified"),
                    _fake_stage("agent_identity", "passed", "agent_verified"),
                    _fake_stage("commerce_capability", "passed", "commerce_verified"),
                ],
            )
            with self._patch_verify_service(result):
                status, body = self._post(
                    db_file,
                    "/v1/agent-catalog/agents/cagt_verify/verify",
                    {"idempotency_key": "vf-1"},
                    token=TEST_ADMIN_TOKEN,
                )

            self.assertEqual(status, 200)
            self.assertEqual(body["verification_status"], "commerce_verified")
            self.assertEqual(body["previous_status"], "discovered")
            self.assertEqual(len(body["stages"]), 4)
            self.assertEqual(body["stages"][-1]["stage"], "commerce_capability")
            self.assertEqual(body["stages"][-1]["outcome"], "passed")

    def test_verify_requires_authorization_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_catalog_agent(db_file, "cagt_verify")
            status, body = self._post(db_file, "/v1/agent-catalog/agents/cagt_verify/verify", {})
            self.assertEqual(status, 403)

    def test_verify_idempotent_replay_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_catalog_agent(db_file, "cagt_verify")
            result = _fake_verify_result("cagt_verify", status="domain_verified", previous="discovered")
            with self._patch_verify_service(result):
                s1, b1 = self._post(
                    db_file,
                    "/v1/agent-catalog/agents/cagt_verify/verify",
                    {"idempotency_key": "vf-idem"},
                    token=TEST_ADMIN_TOKEN,
                )
                s2, b2 = self._post(
                    db_file,
                    "/v1/agent-catalog/agents/cagt_verify/verify",
                    {"idempotency_key": "vf-idem"},
                    token=TEST_ADMIN_TOKEN,
                )
            self.assertEqual(s1, 200)
            self.assertEqual(s2, 200)
            self.assertTrue(b2["idempotent"])
            self.assertEqual(b2["verification_status"], b1["verification_status"])

    def test_verify_fastapi(self):
        from shopping_cli.api import app as app_module
        if app_module.FastAPI is None:
            self.skipTest("FastAPI not installed")

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_catalog_agent(db_file, "cagt_verify")
            app = create_app(db_file)
            with self._patch_verify_service(
                _fake_verify_result("cagt_verify", status="domain_verified")
            ):
                result = self._fastapi_post(
                    app,
                    "/v1/agent-catalog/agents/{catalog_agent_id}/verify",
                    {"idempotency_key": "fast-vf"},
                    authorization=f"Bearer {TEST_ADMIN_TOKEN}",
                    catalog_agent_id="cagt_verify",
                )
            self.assertIsNotNone(result)
            status, body = result
            self.assertEqual(status, 200)
            self.assertEqual(body["verification_status"], "domain_verified")

    # ═══════════════════════════════════════════════════════════════════════════
    # 4. POST /v1/agent-catalog/agents/{id}/claim
    # ═══════════════════════════════════════════════════════════════════════════

    def _patch_identity_verifier(self, passed=True):
        return patch(
            "shopping_cli.api.handlers.agent_catalog._identity_verifier",
            return_value=FakeIdentityVerifier(passed),
        )

    def test_claim_self_registered_challenge_passes_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            token = self._seed_merchant(db_file, "mrc-claim")
            self._seed_catalog_agent(db_file, "cagt_claim", canonical_domain=DOMAIN)
            with self._patch_identity_verifier(passed=True):
                status, body = self._post(
                    db_file,
                    "/v1/agent-catalog/agents/cagt_claim/claim",
                    {"merchant_id": "mrc-claim", "idempotency_key": "claim-1"},
                    token=token,
                )

            self.assertEqual(status, 200)
            agent = body["catalog_agent"]
            self.assertEqual(agent["merchant"]["id"], "mrc-claim")
            events = [e for e in _audit_events(db_file) if e[0] == "catalog_agent_claimed"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][2]["claim_method"], "https_domain_control")
            self.assertEqual(events[0][2]["merchant_id"], "mrc-claim")

    def test_claim_challenge_failure_is_not_proof_fallback(self):
        """Knowing the Agent Card URL is never proof — a failed challenge denies."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            token = self._seed_merchant(db_file, "mrc-claim")
            self._seed_catalog_agent(db_file, "cagt_claim", canonical_domain=DOMAIN)
            with self._patch_identity_verifier(passed=False):
                status, body = self._post(
                    db_file,
                    "/v1/agent-catalog/agents/cagt_claim/claim",
                    {"merchant_id": "mrc-claim", "idempotency_key": "claim-2"},
                    token=token,
                )

            self.assertEqual(status, 403)
            self.assertIn("claim denied", body["error"])
            # The merchant binding must not have changed.
            with db_session(db_file) as conn:
                agent = require_catalog_agent(conn, "cagt_claim")
            self.assertEqual(agent["merchant_id"] or "", "")

    def test_claim_hosted_agent_uses_identity_proof_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            token = self._seed_merchant(db_file, "mrc-claim")
            self._seed_catalog_agent(
                db_file,
                "cagt_hosted",
                merchant_id="mrc-claim",
                source_type="hosted",
                hosting_mode="hosted",
            )
            # No identity-verifier patch: hosted proof needs no challenge.
            status, body = self._post(
                db_file,
                "/v1/agent-catalog/agents/cagt_hosted/claim",
                {"merchant_id": "mrc-claim", "idempotency_key": "claim-3"},
                token=token,
            )

            self.assertEqual(status, 200)
            events = [e for e in _audit_events(db_file) if e[0] == "catalog_agent_claimed"]
            self.assertEqual(events[-1][2]["claim_method"], "hosted_identity")

    def test_claim_requires_merchant_or_admin_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_catalog_agent(db_file, "cagt_claim")
            # Unauthenticated claim is rejected before any challenge.
            status, body = self._post(
                db_file,
                "/v1/agent-catalog/agents/cagt_claim/claim",
                {"merchant_id": "mrc-claim", "idempotency_key": "claim-4"},
            )
            self.assertEqual(status, 403)
            self.assertIn("merchant token required", body["error"])

    def test_claim_admin_may_claim_for_merchant_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file, "mrc-admin")
            self._seed_catalog_agent(db_file, "cagt_claim", canonical_domain=DOMAIN)
            with self._patch_identity_verifier(passed=True):
                status, body = self._post(
                    db_file,
                    "/v1/agent-catalog/agents/cagt_claim/claim",
                    {"merchant_id": "mrc-admin", "idempotency_key": "claim-5"},
                    token=TEST_ADMIN_TOKEN,
                )
            self.assertEqual(status, 200)
            self.assertEqual(body["catalog_agent"]["merchant"]["id"], "mrc-admin")
            events = [e for e in _audit_events(db_file) if e[0] == "catalog_agent_claimed"]
            self.assertEqual(events[-1][1], "admin")

    def test_claim_fastapi(self):
        from shopping_cli.api import app as app_module
        if app_module.FastAPI is None:
            self.skipTest("FastAPI not installed")

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            token = self._seed_merchant(db_file, "mrc-claim")
            self._seed_catalog_agent(db_file, "cagt_claim", canonical_domain=DOMAIN)
            app = create_app(db_file)
            with self._patch_identity_verifier(passed=True):
                result = self._fastapi_post(
                    app,
                    "/v1/agent-catalog/agents/{catalog_agent_id}/claim",
                    {"merchant_id": "mrc-claim", "idempotency_key": "fast-claim"},
                    authorization=f"Bearer {token}",
                    catalog_agent_id="cagt_claim",
                )
            self.assertIsNotNone(result)
            status, body = result
            self.assertEqual(status, 200)
            self.assertEqual(body["catalog_agent"]["merchant"]["id"], "mrc-claim")

    # ═══════════════════════════════════════════════════════════════════════════
    # 5. Route registry consistency + FastAPI route presence
    # ═══════════════════════════════════════════════════════════════════════════

    def test_write_routes_in_registry(self):
        paths = {route.path: route.methods for route in route_info()}
        self.assertEqual(paths["/v1/agent-catalog/agents/register"], {"POST"})
        self.assertEqual(paths["/v1/agent-catalog/agents/{catalog_agent_id}/refresh"], {"POST"})
        self.assertEqual(paths["/v1/agent-catalog/agents/{catalog_agent_id}/verify"], {"POST"})
        self.assertEqual(paths["/v1/agent-catalog/agents/{catalog_agent_id}/claim"], {"POST"})

    def test_write_routes_in_agent_catalog_group(self):
        from shopping_cli.api.route_registry import routes_for_group

        catalog_paths = {route.path for route in routes_for_group("agent_catalog")}
        for path in (
            "/v1/agent-catalog/agents/register",
            "/v1/agent-catalog/agents/{catalog_agent_id}/refresh",
            "/v1/agent-catalog/agents/{catalog_agent_id}/verify",
            "/v1/agent-catalog/agents/{catalog_agent_id}/claim",
        ):
            self.assertIn(path, catalog_paths)

    def test_fastapi_app_registers_write_routes(self):
        from shopping_cli.api import app as app_module
        if app_module.FastAPI is None:
            self.skipTest("FastAPI not installed")

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            app = create_app(db_file)
            route_paths = {
                route.path for route in getattr(app, "routes", []) if hasattr(route, "path")
            }
            for path in (
                "/v1/agent-catalog/agents/register",
                "/v1/agent-catalog/agents/{catalog_agent_id}/refresh",
                "/v1/agent-catalog/agents/{catalog_agent_id}/verify",
                "/v1/agent-catalog/agents/{catalog_agent_id}/claim",
            ):
                self.assertIn(path, route_paths, f"FastAPI missing route: {path}")

    # ═══════════════════════════════════════════════════════════════════════════
    # 6. Read routes unchanged
    # ═══════════════════════════════════════════════════════════════════════════

    def test_read_routes_still_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_catalog_agent(db_file, "cagt_read", canonical_domain="read.example")
            app = MarketplaceASGIApp(db_file)
            status, body = self._request(app, "GET", "/v1/agent-catalog/agents/cagt_read")
            self.assertEqual(status, 200)
            self.assertEqual(body["catalog_agent"]["catalog_agent_id"], "cagt_read")


if __name__ == "__main__":
    unittest.main()

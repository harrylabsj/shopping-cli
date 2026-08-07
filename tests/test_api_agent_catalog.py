"""Dual-stack tests for Agent Catalog public read API (§10.1).

Tests cover FastAPI and fallback ASGI for all 4 routes, route registry
consistency, and private-field leak prevention.
"""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shopping_cli.agent_catalog.sqlite_repository import upsert_catalog_agent
from shopping_cli.api.app import create_app, create_catalog_app
from shopping_cli.api.fallback_asgi import MarketplaceASGIApp
from shopping_cli.api.route_registry import route_info
from shopping_cli.core import catalog
from shopping_cli.db.session import db_session


# ── Private fields that must never appear in public responses (§3.4) ──────────
_PRIVATE_FIELDS = frozenset({
    "automation_boundaries",
    "floor_price",
    "cost",
    "discount_policy",
    "agent_token",
    "merchant_token",
    "private_contact",
    "llm_prompt",
    "internal_strategy",
    "private_reputation_evidence",
    "delivery_fee",
    "delivery_currency",
    "delivery_eta_minutes",
    "delivery_radius_km",
    "delivery_notes",
    "contact",
    "hours",
    "first_seen_at",
    "last_seen_at",
    "created_at",
    "updated_at",
    "provider_name",
})


def _collect_keys(obj):
    """Recursively collect all string keys in a JSON-serializable object."""
    seen: set[str] = set()
    _walk_keys(obj, seen)
    return seen


def _walk_keys(obj, seen):
    if isinstance(obj, dict):
        for key in obj:
            seen.add(key)
            _walk_keys(obj[key], seen)
    elif isinstance(obj, list):
        for item in obj:
            _walk_keys(item, seen)


class FakeFastAPI:
    def __init__(self, *, title, version, docs_url="/docs", redoc_url="/redoc", openapi_url="/openapi.json"):
        self.title = title
        self.version = version
        self.state = SimpleNamespace()
        self.routes = []
        self.exception_handlers = {}
        for path in (openapi_url, docs_url, redoc_url):
            if path is not None:
                self.routes.append(SimpleNamespace(methods={"GET"}, path=path, endpoint=lambda: None))

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


class AgentCatalogApiTest(unittest.TestCase):
    TEST_ADMIN_TOKEN = "test-admin-token-catalog"

    def setUp(self):
        self._env_patcher = patch.dict(
            os.environ,
            {"SHOPPING_ADMIN_TOKEN": self.TEST_ADMIN_TOKEN},
            clear=False,
        )
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    # ── seeded helpers ────────────────────────────────────────────────────────

    def _seed_merchant(self, db_file):
        with db_session(db_file) as conn:
            catalog.create_merchant(
                conn,
                merchant_id="mrc_seed",
                name="Seed Merchant",
                city="Hangzhou",
                service_area="Xihu",
                tags=["electronics", "display"],
                contact="test@example.com",
                automation_boundaries="full-auto",
            )

    def _seed_catalog_agents(self, db_file):
        """Insert two catalog agents with merchants.  Returns the ids."""
        with db_session(db_file) as conn:
            upsert_catalog_agent(
                conn,
                catalog_agent_id="cagt_001",
                merchant_id="mrc_seed",
                display_name="Seed Agent Alpha",
                canonical_domain="alpha.example.com",
                agent_type="commerce",
                source_type="hosted",
                lifecycle_status="active",
                verification_status="commerce_verified",
                hosting_mode="hosted",
            )
            upsert_catalog_agent(
                conn,
                catalog_agent_id="cagt_002",
                merchant_id="mrc_seed",
                display_name="Seed Agent Beta",
                canonical_domain="beta.example.com",
                agent_type="commerce",
                source_type="hosted",
                lifecycle_status="active",
                verification_status="domain_verified",
                hosting_mode="direct",
            )
            return ["cagt_001", "cagt_002"]

    # ── ASGI test infra ───────────────────────────────────────────────────────

    async def asgi_request(self, app, method, path, query_string=""):
        return await self._asgi(app, method, path, query_string)

    async def _asgi(self, app, method, path, query_string=""):
        sent = []
        received = False

        async def receive():
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        qs = query_string if isinstance(query_string, bytes) else query_string.encode("utf-8")
        await app(
            {
                "type": "http",
                "method": method,
                "path": path,
                "query_string": qs,
                "headers": [(b"content-type", b"application/json")],
            },
            receive,
            send,
        )
        status = next(
            message["status"] for message in sent if message["type"] == "http.response.start"
        )
        body = b"".join(
            message.get("body", b"") for message in sent if message["type"] == "http.response.body"
        )
        return status, json.loads(body.decode("utf-8") or "{}")

    def _request(self, app, method, path, query_string=""):
        return asyncio.run(self.asgi_request(app, method, path, query_string))

    # ── FastAPI helper ────────────────────────────────────────────────────────

    def _fastapi_call(self, app, path, **kwargs):
        """Call a FastAPI route by path, extracting endpoint and args."""
        from shopping_cli.api import app as app_module
        if app_module.FastAPI is None:
            return None
        endpoint = next(
            (route.endpoint for route in app.routes if route.path == path and "GET" in route.methods),
            None,
        )
        if endpoint is None:
            raise AssertionError(f"No GET route found for {path}")
        try:
            return 200, endpoint(**kwargs)
        except Exception as exc:
            for exc_type, handler in app.exception_handlers.items():
                if isinstance(exc, exc_type):
                    response = handler(None, exc)
                    return response.status_code, json.loads(response.body.decode("utf-8"))
            raise

    # ═══════════════════════════════════════════════════════════════════════════
    # 1. GET /v1/agent-catalog/agents
    # ═══════════════════════════════════════════════════════════════════════════

    def test_list_agents_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            self._seed_catalog_agents(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(app, "GET", "/v1/agent-catalog/agents")

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["results"], list)
        self.assertEqual(len(body["results"]), 2)
        self.assertIsNone(body["next_cursor"])


    def test_list_agents_pagination_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            self._seed_catalog_agents(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(
                app, "GET", "/v1/agent-catalog/agents", query_string="limit=1"
            )

        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 1)
        self.assertIsNotNone(body["next_cursor"])

    def test_list_agents_invalid_limit_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(
                app, "GET", "/v1/agent-catalog/agents", query_string="limit=abc"
            )

        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    def test_list_agents_empty_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            # Don't seed any agents
            app = create_catalog_app(db_file)

            status, body = self._request(app, "GET", "/v1/agent-catalog/agents")

        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 0)
        self.assertIsNone(body["next_cursor"])

    def test_list_agents_method_not_allowed_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            app = create_catalog_app(db_file)

            status, body = self._request(app, "POST", "/v1/agent-catalog/agents")

        self.assertEqual(status, 405)
        self.assertFalse(body["ok"])

    # ═══════════════════════════════════════════════════════════════════════════
    # 2. GET /v1/agent-catalog/agents/{catalog_agent_id}
    # ═══════════════════════════════════════════════════════════════════════════

    def test_get_agent_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            self._seed_catalog_agents(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(app, "GET", "/v1/agent-catalog/agents/cagt_001")

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("catalog_agent", body)
        self.assertEqual(body["catalog_agent"]["catalog_agent_id"], "cagt_001")


    def test_get_agent_404_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            app = create_catalog_app(db_file)

            status, body = self._request(app, "GET", "/v1/agent-catalog/agents/cagt_nonexistent")

        self.assertEqual(status, 404)
        self.assertFalse(body["ok"])


    def test_search_agents_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            self._seed_catalog_agents(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(app, "GET", "/v1/agent-catalog/agents/search")

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["results"], list)
        self.assertEqual(len(body["results"]), 2)


    def test_search_agents_with_q_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            self._seed_catalog_agents(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(
                app, "GET", "/v1/agent-catalog/agents/search", query_string="q=Alpha"
            )

        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 1)
        # "Alpha" is in the display_name of the catalog agent
        self.assertEqual(body["results"][0]["catalog_agent_id"], "cagt_001")

    def test_search_agents_with_hosting_mode_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            self._seed_catalog_agents(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(
                app, "GET", "/v1/agent-catalog/agents/search", query_string="hosting_mode=direct"
            )

        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["hosting"]["mode"], "direct")

    def test_search_agents_with_verification_status_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            self._seed_catalog_agents(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(
                app, "GET", "/v1/agent-catalog/agents/search",
                query_string="verification_status=commerce_verified",
            )

        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["verification"]["status"], "commerce_verified")

    def test_search_agents_no_results_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            app = create_catalog_app(db_file)

            status, body = self._request(
                app, "GET", "/v1/agent-catalog/agents/search", query_string="q=nosuchagent"
            )

        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 0)

    def test_search_agents_invalid_limit_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            app = create_catalog_app(db_file)

            status, body = self._request(
                app, "GET", "/v1/agent-catalog/agents/search", query_string="limit=bad"
            )

        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])

    # ═══════════════════════════════════════════════════════════════════════════
    # 4. GET /v1/agent-catalog/merchants/{merchant_id}/agents
    # ═══════════════════════════════════════════════════════════════════════════

    def test_list_merchant_agents_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            self._seed_catalog_agents(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(
                app, "GET", "/v1/agent-catalog/merchants/mrc_seed/agents"
            )

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["results"], list)
        self.assertEqual(len(body["results"]), 2)


    def test_list_merchant_agents_unknown_merchant_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            app = create_catalog_app(db_file)

            status, body = self._request(
                app, "GET", "/v1/agent-catalog/merchants/unknown_merchant/agents"
            )

        # Unknown merchant returns 200 with empty results (not 404 — the
        # merchant itself may not exist, but the route is valid)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 0)

    def test_list_merchant_agents_pagination_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            self._seed_catalog_agents(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(
                app, "GET", "/v1/agent-catalog/merchants/mrc_seed/agents",
                query_string="limit=1",
            )

        self.assertEqual(status, 200)
        self.assertEqual(len(body["results"]), 1)
        self.assertIsNotNone(body["next_cursor"])

    # ═══════════════════════════════════════════════════════════════════════════
    # 5. Route registry consistency
    # ═══════════════════════════════════════════════════════════════════════════

    def test_agent_catalog_routes_in_registry(self):
        """All 4 new routes must appear in route_info() with consistent methods."""
        paths = {route.path: route.methods for route in route_info()}

        # List
        self.assertIn("/v1/agent-catalog/agents", paths)
        self.assertEqual(paths["/v1/agent-catalog/agents"], {"GET"})
        # Search
        self.assertIn("/v1/agent-catalog/agents/search", paths)
        self.assertEqual(paths["/v1/agent-catalog/agents/search"], {"GET"})
        # Detail
        self.assertIn("/v1/agent-catalog/agents/{catalog_agent_id}", paths)
        self.assertEqual(paths["/v1/agent-catalog/agents/{catalog_agent_id}"], {"GET"})
        # Merchant agents
        self.assertIn("/v1/agent-catalog/merchants/{merchant_id}/agents", paths)
        self.assertEqual(
            paths["/v1/agent-catalog/merchants/{merchant_id}/agents"], {"GET"}
        )

    def test_agent_catalog_routes_have_marketplace_group(self):
        """All 4 routes must be in the marketplace group."""
        from shopping_cli.api.route_registry import routes_for_group

        marketplace_paths = {route.path for route in routes_for_group("marketplace")}
        for path in (
            "/v1/agent-catalog/agents",
            "/v1/agent-catalog/agents/search",
            "/v1/agent-catalog/agents/{catalog_agent_id}",
            "/v1/agent-catalog/merchants/{merchant_id}/agents",
        ):
            self.assertIn(path, marketplace_paths, f"{path} not in marketplace group")

    def test_agent_catalog_routes_have_agent_catalog_group(self):
        """All 4 routes must be in the agent_catalog group."""
        from shopping_cli.api.route_registry import routes_for_group

        catalog_paths = {route.path for route in routes_for_group("agent_catalog")}
        for path in (
            "/v1/agent-catalog/agents",
            "/v1/agent-catalog/agents/search",
            "/v1/agent-catalog/agents/{catalog_agent_id}",
            "/v1/agent-catalog/merchants/{merchant_id}/agents",
        ):
            self.assertIn(path, catalog_paths, f"{path} not in agent_catalog group")

    # ═══════════════════════════════════════════════════════════════════════════
    # 6. Private field leak prevention (§3.4)
    # ═══════════════════════════════════════════════════════════════════════════

    def test_no_private_fields_in_list_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            self._seed_catalog_agents(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(app, "GET", "/v1/agent-catalog/agents")

        self.assertEqual(status, 200)
        keys = _collect_keys(body)
        leaked = _PRIVATE_FIELDS & keys
        self.assertEqual(leaked, set(), f"Private fields leaked: {leaked}")

    def test_no_private_fields_in_get_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            self._seed_catalog_agents(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(app, "GET", "/v1/agent-catalog/agents/cagt_001")

        self.assertEqual(status, 200)
        keys = _collect_keys(body)
        leaked = _PRIVATE_FIELDS & keys
        self.assertEqual(leaked, set(), f"Private fields leaked: {leaked}")

    def test_no_private_fields_in_search_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            self._seed_catalog_agents(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(app, "GET", "/v1/agent-catalog/agents/search")

        self.assertEqual(status, 200)
        keys = _collect_keys(body)
        leaked = _PRIVATE_FIELDS & keys
        self.assertEqual(leaked, set(), f"Private fields leaked: {leaked}")

    def test_no_private_fields_in_merchant_agents_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            self._seed_catalog_agents(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(
                app, "GET", "/v1/agent-catalog/merchants/mrc_seed/agents"
            )

        self.assertEqual(status, 200)
        keys = _collect_keys(body)
        leaked = _PRIVATE_FIELDS & keys
        self.assertEqual(leaked, set(), f"Private fields leaked: {leaked}")

    # ═══════════════════════════════════════════════════════════════════════════
    # 7. Response shape assertions
    # ═══════════════════════════════════════════════════════════════════════════

    def test_list_response_has_correct_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            self._seed_catalog_agents(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(app, "GET", "/v1/agent-catalog/agents")

        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        result = body["results"][0]
        # Must include catalog_agent_id
        self.assertIn("catalog_agent_id", result)
        # Must include merchant block
        self.assertIn("merchant", result)
        self.assertIn("name", result["merchant"])
        # Must include verification/hosting blocks
        self.assertIn("verification", result)
        self.assertIn("status", result["verification"])
        self.assertIn("hosting", result)
        self.assertIn("mode", result["hosting"])

    def test_detail_response_has_correct_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed_merchant(db_file)
            self._seed_catalog_agents(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(app, "GET", "/v1/agent-catalog/agents/cagt_001")

        self.assertEqual(status, 200)
        agent = body["catalog_agent"]
        self.assertEqual(agent["catalog_agent_id"], "cagt_001")
        self.assertIn("merchant", agent)
        self.assertIn("verification", agent)
        self.assertIn("hosting", agent)

    def test_marketplace_app_removes_agent_catalog_routes_mvp8(self):
        """MVP #8（v0.3 §12）：主 API 不再承担 Agent Catalog 面。

        - FastAPI 分支：注册后移除 /v1/agent-catalog/* 路由；
        - fallback 分支：路由视图过滤；请求返回 404；
        - 共享路由（/v1/hosted/*、/a2a/*）保留。
        """
        from shopping_cli.api import app as app_module

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            app = create_app(db_file)

            if app_module.FastAPI is not None:
                route_paths = {
                    route.path for route in getattr(app, "routes", []) if hasattr(route, "path")
                }
                for path in (
                    "/v1/agent-catalog/agents",
                    "/v1/agent-catalog/agents/search",
                    "/v1/agent-catalog/agents/{catalog_agent_id}",
                    "/v1/agent-catalog/agents/register",
                ):
                    self.assertNotIn(path, route_paths, f"主 API 不应暴露 Agent Catalog 路由: {path}")
                # 共享路由保留
                self.assertIn("/v1/hosted/agents/{catalog_agent_id}/agent-card.json", route_paths)
                self.assertIn("/a2a/agents/{catalog_agent_id}", route_paths)

            status, body = self._request(app, "GET", "/v1/agent-catalog/agents")
            self.assertEqual(status, 404, "主 API fallback 不应路由 agent-catalog 请求")


class AgentCatalogTrustObservationPrivacyTest(unittest.TestCase):
    """§5.7 / §3.4 — private trust observations never leak through public reads.

    Seeds the private ``agent_trust_observations`` table with distinctive
    marker content and asserts NONE of the 4 public read routes (list / get /
    search / merchant-agents) surface that content in any key or string value.
    """

    _LEAK_EVIDENCE = "EVID-LEAK-7f4aa2"
    _LEAK_SOURCE = "SRC-LEAK-7f4aa2"

    def _collect_strings(self, obj):
        """Recursively collect every string key and string value in obj."""
        found = []

        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    found.append(str(key))
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, str):
                found.append(value)

        walk(obj)
        return found

    def _seed(self, db_file):
        from shopping_cli.agent_catalog.sqlite_repository import insert_trust_observation

        with db_session(db_file) as conn:
            catalog.create_merchant(
                conn,
                merchant_id="mrc_seed",
                name="Seed Merchant",
                city="Hangzhou",
                service_area="Xihu",
                tags=["electronics"],
                contact="test@example.com",
                automation_boundaries="full-auto",
            )
            upsert_catalog_agent(
                conn,
                catalog_agent_id="cagt_001",
                merchant_id="mrc_seed",
                display_name="Seed Agent Alpha",
                canonical_domain="alpha.example.com",
                agent_type="commerce",
                source_type="hosted",
                lifecycle_status="active",
                verification_status="commerce_verified",
                hosting_mode="hosted",
            )
            insert_trust_observation(
                conn,
                catalog_agent_id="cagt_001",
                kind="local_asserted_dispute",
                value=2.0,
                source=self._LEAK_SOURCE,
                evidence_ref=self._LEAK_EVIDENCE,
            )
            insert_trust_observation(
                conn,
                catalog_agent_id="cagt_001",
                kind="timeout_rate",
                value=0.3,
                source=self._LEAK_SOURCE,
                evidence_ref=self._LEAK_EVIDENCE,
            )

    async def _asgi_request(self, app, method, path, query_string=""):
        sent = []
        received = False

        async def receive():
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        qs = query_string if isinstance(query_string, bytes) else query_string.encode("utf-8")
        await app(
            {
                "type": "http",
                "method": method,
                "path": path,
                "query_string": qs,
                "headers": [(b"content-type", b"application/json")],
            },
            receive,
            send,
        )
        status = next(
            message["status"] for message in sent if message["type"] == "http.response.start"
        )
        body = b"".join(
            message.get("body", b"") for message in sent if message["type"] == "http.response.body"
        )
        return status, json.loads(body.decode("utf-8") or "{}")

    def _request(self, app, method, path, query_string=""):
        return asyncio.run(self._asgi_request(app, method, path, query_string))

    def _assert_no_observation_leak(self, body):
        strings = self._collect_strings(body)
        self.assertNotIn(self._LEAK_EVIDENCE, strings)
        self.assertNotIn(self._LEAK_SOURCE, strings)
        keys = set(strings)
        self.assertNotIn("evidence_ref", keys)
        self.assertNotIn("observation_id", keys)

    def test_list_route_does_not_leak_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(app, "GET", "/v1/agent-catalog/agents")

        self.assertEqual(status, 200)
        self._assert_no_observation_leak(body)

    def test_get_route_does_not_leak_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(app, "GET", "/v1/agent-catalog/agents/cagt_001")

        self.assertEqual(status, 200)
        self._assert_no_observation_leak(body)

    def test_search_route_does_not_leak_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(app, "GET", "/v1/agent-catalog/agents/search")

        self.assertEqual(status, 200)
        self._assert_no_observation_leak(body)

    def test_merchant_agents_route_does_not_leak_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed(db_file)
            app = create_catalog_app(db_file)

            status, body = self._request(
                app, "GET", "/v1/agent-catalog/merchants/mrc_seed/agents"
            )

        self.assertEqual(status, 200)
        self._assert_no_observation_leak(body)


if __name__ == "__main__":
    unittest.main()

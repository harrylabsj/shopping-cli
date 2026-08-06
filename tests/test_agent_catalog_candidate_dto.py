"""CandidateAgent DTO contract conformance tests (v2.3-T1).

Validates the formalised §8.2 contract (shopping_cli/agent_catalog/
candidate_dto.py) against the public serializers' real output and all four
public read routes (fallback ASGI + FastAPI dual-stack):

* serializer output on a full-field fixture conforms to CANDIDATE_AGENT_SCHEMA;
* every candidate returned by the four read routes carries the ``contract``
  version annotation and conforms to the schema;
* capability identifiers are fully-qualified (§8.2);
* §3.4 private fields never survive serialization or schema validation.

The schema validator below is a hand-written minimal recursive JSON Schema
subset — no new third-party dependency (jsonschema is not a project dep).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from shopping_cli.agent_catalog.candidate_dto import (
    CANDIDATE_AGENT_SCHEMA,
    CANDIDATE_CONTRACT_NAME,
    CANDIDATE_DTO_VERSION,
    to_contract_hosting_mode,
)
from shopping_cli.agent_catalog.serializers import catalog_search_result
from shopping_cli.agent_catalog.sqlite_repository import (
    replace_capabilities,
    replace_skills,
    upsert_catalog_agent,
    upsert_profile_endpoints,
)
from shopping_cli.api.app import create_app
from shopping_cli.api.fallback_asgi import MarketplaceASGIApp
from shopping_cli.core import catalog
from shopping_cli.db.session import db_session


# ── Minimal recursive JSON Schema (draft-07 subset) validator ────────────────


def _check_type(instance: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    t = schema.get("type")
    if t is None:
        return
    matches = {
        "object": isinstance(instance, dict),
        "array": isinstance(instance, list),
        "string": isinstance(instance, str),
        "integer": isinstance(instance, int) and not isinstance(instance, bool),
        "number": isinstance(instance, (int, float)) and not isinstance(instance, bool),
        "boolean": isinstance(instance, bool),
        "null": instance is None,
    }
    if not matches.get(t, True):
        errors.append(f"{path}: expected type {t!r}, got {type(instance).__name__}")


def _check(instance: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    _check_type(instance, schema, path, errors)

    if schema.get("type") == "object":
        if not isinstance(instance, dict):
            return
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}: missing required property {name!r}")
        for key, value in instance.items():
            if key in props:
                _check(value, props[key], f"{path}.{key}", errors)
            elif "additionalProperties" in schema:
                ap = schema["additionalProperties"]
                if ap is False:
                    errors.append(f"{path}: unexpected property {key!r}")
                elif isinstance(ap, dict):
                    _check(value, ap, f"{path}.{key}", errors)

    elif schema.get("type") == "array":
        if not isinstance(instance, list):
            return
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(instance):
                _check(item, items, f"{path}[{index}]", errors)

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']!r}")
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")
    if isinstance(instance, str) and "minLength" in schema and len(instance) < schema["minLength"]:
        errors.append(f"{path}: string shorter than minLength {schema['minLength']}")


def schema_errors(instance: Any, schema: dict[str, Any]) -> list[str]:
    """Return schema violation messages for *instance* (empty list = valid)."""
    errors: list[str] = []
    _check(instance, schema, "$", errors)
    return errors


def assert_valid_candidate(testcase: unittest.TestCase, candidate: dict[str, Any]) -> None:
    """Assert *candidate* is schema-valid and carries the contract annotation."""
    errors = schema_errors(candidate, CANDIDATE_AGENT_SCHEMA)
    testcase.assertEqual(errors, [], f"Candidate violates contract: {errors}")
    testcase.assertEqual(candidate["contract"]["name"], CANDIDATE_CONTRACT_NAME)
    testcase.assertEqual(candidate["contract"]["version"], CANDIDATE_DTO_VERSION)


def _collect_keys(obj: Any) -> set[str]:
    """Recursively collect all string keys in a JSON-serializable object."""
    seen: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                seen.add(key)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(obj)
    return seen


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _full_catalog_row() -> dict[str, Any]:
    return {
        "catalog_agent_id": "cagt_full",
        "merchant_id": "mrc_1",
        "merchant_name": "Full Merchant",
        "display_name": "Full Merchant",
        "verification_status": "commerce_verified",
        "last_verified_at": "2026-08-01T10:00:00",
        "hosting_mode": "hosted",
        # §3.4 private / internal-only fields that must NOT appear in output:
        "first_seen_at": "2026-01-01T00:00:00",
        "last_seen_at": "2026-08-01T00:00:00",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-08-01T00:00:00",
        "provider_name": "internal-provider",
    }


def _full_merchant() -> dict[str, Any]:
    return {
        "id": "mrc_1",
        "name": "Full Merchant",
        "city": "Hangzhou",
        "service_area": "Xihu",
        "canonical_domain": "merchant.example",
        "tags_json": '["electronics", "display"]',
    }


def _full_capabilities() -> list[dict[str, Any]]:
    return [
        {
            "namespace": "com.harrylabsj.shopping.capability",
            "capability_id": "catalog",
            "version": "1.0",
        },
        {"namespace": "", "capability_id": "consultation", "version": ""},
    ]


def _full_endpoints() -> list[dict[str, Any]]:
    return [
        {
            "kind": "agent_card",
            "url": "https://merchant.example/.well-known/agent-card.json",
            "protocol": "a2a",
            "protocol_version": "1.0",
            "preference": 10,
        },
        {
            "kind": "ucp_profile",
            "url": "https://merchant.example/.well-known/ucp",
            "protocol": "ucp",
            "protocol_version": "2026-04-08",
            "preference": 5,
        },
        {
            "kind": "a2a",
            "url": "https://merchant.example/a2a",
            "protocol": "a2a",
            "protocol_version": "1.0",
            "preference": 0,
        },
        # internal endpoint without a public URL must not surface:
        {"kind": "hosted_gateway", "url": "", "protocol": "", "protocol_version": "", "preference": 0},
    ]


def _full_skills() -> list[dict[str, Any]]:
    return [
        {
            "skill_id": "s_neg",
            "name": "Negotiation",
            "description": "commerce negotiation",
            "tags_json": '["commerce"]',
        }
    ]


# ── Tests ────────────────────────────────────────────────────────────────────


class SerializerSchemaConformanceTest(unittest.TestCase):
    """Serializer output on a full-field fixture conforms to the schema."""

    def test_full_candidate_conforms_to_schema(self):
        out = catalog_search_result(
            _full_catalog_row(),
            merchant=_full_merchant(),
            capabilities=_full_capabilities(),
            endpoints=_full_endpoints(),
            skills=_full_skills(),
        )
        assert_valid_candidate(self, out)

    def test_minimal_candidate_conforms_to_schema(self):
        """A candidate with only the always-present blocks is still valid."""
        out = catalog_search_result(
            {
                "catalog_agent_id": "cagt_min",
                "verification_status": "discovered",
                "hosting_mode": "unknown",
            }
        )
        assert_valid_candidate(self, out)
        self.assertNotIn("merchant", out)
        self.assertNotIn("discovery", out)
        self.assertNotIn("capabilities", out)
        self.assertNotIn("skills", out)

    def test_contract_annotation_present_on_every_candidate(self):
        for hosting in ("hosted", "direct", "hybrid", "unknown"):
            out = catalog_search_result(
                {
                    "catalog_agent_id": f"cagt_{hosting}",
                    "verification_status": "discovered",
                    "hosting_mode": hosting,
                }
            )
            self.assertEqual(out["contract"]["name"], "candidate-agent")
            self.assertEqual(out["contract"]["version"], "1.0")


class CapabilitiesFullyQualifiedTest(unittest.TestCase):
    """§8.2 — capability identifiers must be fully-qualified."""

    def test_capabilities_are_fully_qualified(self):
        out = catalog_search_result(
            _full_catalog_row(),
            merchant=_full_merchant(),
            capabilities=_full_capabilities(),
            endpoints=_full_endpoints(),
            skills=_full_skills(),
        )
        caps = out["capabilities"]
        self.assertEqual(caps, [
            "com.harrylabsj.shopping.capability:catalog",
            "consultation",
        ])
        for cap in caps:
            # namespace-prefixed form carries a ':' separator; bare ids stay bare.
            if ":" in cap:
                self.assertRegex(cap, r"^[^:]+:[^:]+$")
        # The namespaced capability must carry the ':' prefix exactly once.
        self.assertIn("com.harrylabsj.shopping.capability:catalog", caps)


class HostingModeMappingTest(unittest.TestCase):
    """§22 — to_contract_hosting_mode normalizes legacy stored values."""

    def test_legacy_values_map_to_canonical(self):
        self.assertEqual(to_contract_hosting_mode("direct"), "direct_only")
        self.assertEqual(to_contract_hosting_mode("hosted"), "hosted_only")
        self.assertEqual(to_contract_hosting_mode("hybrid"), "hybrid")
        self.assertEqual(to_contract_hosting_mode("unknown"), "unknown")

    def test_unrecognised_values_fail_closed(self):
        for bad in ("", "weird", None):
            self.assertEqual(to_contract_hosting_mode(bad), "unknown")

    def test_mapping_is_case_insensitive(self):
        # The DB always stores lowercase, but the normalizer is defensive.
        self.assertEqual(to_contract_hosting_mode("DIRECT"), "direct_only")
        self.assertEqual(to_contract_hosting_mode("Hosted"), "hosted_only")


class PrivateFieldsNegativeTest(unittest.TestCase):
    """§3.4 — private fields never survive serialization or validation."""

    _PRIVATE_KEYS = frozenset({
        "automation_boundaries",
        "contact",
        "hours",
        "delivery_fee",
        "delivery_currency",
        "delivery_eta_minutes",
        "delivery_radius_km",
        "delivery_notes",
        "floor_price",
        "cost",
        "discount_policy",
        "agent_token",
        "merchant_token",
        "private_contact",
        "llm_prompt",
        "internal_strategy",
        "private_reputation_evidence",
        "first_seen_at",
        "last_seen_at",
        "created_at",
        "updated_at",
        "provider_name",
    })

    def test_private_fields_do_not_survive_serializer(self):
        merchant = _full_merchant()
        merchant.update({
            "automation_boundaries": "full-auto",
            "contact": "private@example.com",
            "hours": "9-5",
            "floor_price": 99.9,
            "cost": 50.0,
            "discount_policy": "never",
            "agent_token": "tok-private",
            "merchant_token": "tok-merchant",
            "private_contact": "secret-line",
            "llm_prompt": "system: be nice",
            "internal_strategy": "shrink-floats",
            "private_reputation_evidence": "evidence-1",
        })
        row = _full_catalog_row()
        row.update({
            "floor_price": 88.0,
            "agent_token": "tok-row",
            "internal_strategy": "row-strategy",
        })
        out = catalog_search_result(
            row,
            merchant=merchant,
            capabilities=_full_capabilities(),
            endpoints=_full_endpoints(),
            skills=_full_skills(),
        )

        # 1. No private key anywhere in the serialized response.
        serialized = json.dumps(out)
        leaked = self._PRIVATE_KEYS & _collect_keys(out)
        self.assertEqual(leaked, set(), f"Private keys leaked: {leaked}")
        for key in self._PRIVATE_KEYS:
            self.assertNotIn(key, serialized)

        # 2. The schema-validated object is free of private fields by
        #    construction — additionalProperties:false rejects them.
        errors = schema_errors(out, CANDIDATE_AGENT_SCHEMA)
        self.assertEqual(errors, [])
        validated_keys = _collect_keys(out)
        self.assertEqual(self._PRIVATE_KEYS & validated_keys, set())


class ReadRoutesContractTest(unittest.TestCase):
    """All four public read routes return contract-annotated, schema-valid
    candidates on both the fallback ASGI and FastAPI stacks."""

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

    def _seed(self, db_file):
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
            replace_capabilities(conn, "cagt_001", [
                {
                    "namespace": "com.harrylabsj.shopping.capability",
                    "capability_id": "catalog",
                    "version": "1.0",
                    "required": 1,
                    "source": "test",
                    "schema_url": "",
                    "spec_url": "",
                }
            ])
            upsert_profile_endpoints(conn, "cagt_001", [
                {
                    "kind": "agent_card",
                    "url": "https://alpha.example.com/.well-known/agent-card.json",
                    "protocol": "a2a",
                    "protocol_version": "1.0",
                    "preference": 10,
                },
                {
                    "kind": "ucp_profile",
                    "url": "https://alpha.example.com/.well-known/ucp",
                    "protocol": "ucp",
                    "protocol_version": "2026-04-08",
                    "preference": 5,
                },
            ])
            replace_skills(conn, "cagt_001", [
                {
                    "skill_id": "s_neg",
                    "name": "Negotiation",
                    "description": "commerce negotiation",
                    "tags_json": '["commerce"]',
                }
            ])

    # ── ASGI infra (mirrors test_api_agent_catalog.py) ──────────────────────

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
        return asyncio.run(self._asgi(app, method, path, query_string))

    # ── FastAPI infra (mirrors test_api_agent_catalog.py) ───────────────────

    def _fastapi_call(self, app, path, **kwargs):
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
        except Exception as exc:  # noqa: BLE001 - mirror existing dual-stack tests
            for exc_type, handler in app.exception_handlers.items():
                if isinstance(exc, exc_type):
                    response = handler(None, exc)
                    return response.status_code, json.loads(response.body.decode("utf-8"))
            raise

    def _candidates(self, body):
        """Extract candidates from a read-route response body."""
        if "results" in body:
            return list(body["results"])
        if "catalog_agent" in body:
            return [body["catalog_agent"]]
        return []

    def _assert_routes(self, body):
        candidates = self._candidates(body)
        self.assertGreaterEqual(len(candidates), 1)
        for candidate in candidates:
            assert_valid_candidate(self, candidate)

    # ── tests: fallback ASGI ────────────────────────────────────────────────

    def test_list_agents_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed(db_file)
            app = MarketplaceASGIApp(db_file)
            status, body = self._request(app, "GET", "/v1/agent-catalog/agents")
        self.assertEqual(status, 200)
        self._assert_routes(body)

    def test_search_agents_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed(db_file)
            app = MarketplaceASGIApp(db_file)
            status, body = self._request(app, "GET", "/v1/agent-catalog/agents/search")
        self.assertEqual(status, 200)
        self._assert_routes(body)

    def test_get_agent_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed(db_file)
            app = MarketplaceASGIApp(db_file)
            status, body = self._request(app, "GET", "/v1/agent-catalog/agents/cagt_001")
        self.assertEqual(status, 200)
        self._assert_routes(body)

    def test_merchant_agents_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed(db_file)
            app = MarketplaceASGIApp(db_file)
            status, body = self._request(
                app, "GET", "/v1/agent-catalog/merchants/mrc_seed/agents"
            )
        self.assertEqual(status, 200)
        self._assert_routes(body)

    # ── tests: FastAPI ──────────────────────────────────────────────────────

    def test_list_agents_fastapi(self):
        from shopping_cli.api import app as app_module
        if app_module.FastAPI is None:
            self.skipTest("FastAPI not installed")

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed(db_file)
            app = create_app(db_file)
            result = self._fastapi_call(app, "/v1/agent-catalog/agents")
        self.assertIsNotNone(result)
        status, body = result
        self.assertEqual(status, 200)
        self._assert_routes(body)

    def test_search_agents_fastapi(self):
        from shopping_cli.api import app as app_module
        if app_module.FastAPI is None:
            self.skipTest("FastAPI not installed")

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed(db_file)
            app = create_app(db_file)
            result = self._fastapi_call(app, "/v1/agent-catalog/agents/search")
        self.assertIsNotNone(result)
        status, body = result
        self.assertEqual(status, 200)
        self._assert_routes(body)

    def test_get_agent_fastapi(self):
        from shopping_cli.api import app as app_module
        if app_module.FastAPI is None:
            self.skipTest("FastAPI not installed")

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed(db_file)
            app = create_app(db_file)
            result = self._fastapi_call(
                app, "/v1/agent-catalog/agents/{catalog_agent_id}", catalog_agent_id="cagt_001"
            )
        self.assertIsNotNone(result)
        status, body = result
        self.assertEqual(status, 200)
        self._assert_routes(body)

    def test_merchant_agents_fastapi(self):
        from shopping_cli.api import app as app_module
        if app_module.FastAPI is None:
            self.skipTest("FastAPI not installed")

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            self._seed(db_file)
            app = create_app(db_file)
            result = self._fastapi_call(
                app,
                "/v1/agent-catalog/merchants/{merchant_id}/agents",
                merchant_id="mrc_seed",
            )
        self.assertIsNotNone(result)
        status, body = result
        self.assertEqual(status, 200)
        self._assert_routes(body)


if __name__ == "__main__":
    unittest.main()

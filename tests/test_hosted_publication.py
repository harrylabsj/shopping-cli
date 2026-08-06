"""Dual-stack tests for hosted A2A publication (v2.4-W1).

Covers the read-only projection layer (`shopping_cli.a2a`) and the two
public GET routes:

    GET /v1/hosted/agents/{catalog_agent_id}/agent-card.json
    GET /v1/hosted/agents/{catalog_agent_id}/ucp

Invariants exercised:

* generated Agent Card / UCP Profile round-trip through the §17.2 parsers;
* only `source_type=hosted` + `lifecycle_status=active` agents are publishable
  (everything else is an indistinguishable 404);
* §3.4 private fields never appear in the published JSON;
* §18 server-side ETag with If-None-Match → 304;
* the capability projection is one-way and matches `_PUBLICATION_CAPABILITY_MAP`.

Design: docs/shopping-cli-a2a-upgrade-design-v1.2.1.md §14, §18, §3.4
Binding: docs/a2a/shopping-cli-a2a-binding-1.0-rc1.md §5–§6
"""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shopping_cli.agent_catalog.serializers import public_merchant_ref
from shopping_cli.agent_catalog.sqlite_repository import upsert_catalog_agent
from shopping_cli.agents.tools import record_heartbeat
from shopping_cli.api.app import create_app
from shopping_cli.api.fallback_asgi import MarketplaceASGIApp
from shopping_cli.api.route_registry import route_info, routes_for_group
from shopping_cli.db.session import db_session, now_iso
from shopping_cli.services.agent_catalog import (
    _PUBLICATION_CAPABILITY_MAP,
    ensure_hosted_catalog_agent,
)


# ── constants ─────────────────────────────────────────────────────────────────

CARD_PATH = "/v1/hosted/agents/{catalog_agent_id}/agent-card.json"
UCP_PATH = "/v1/hosted/agents/{catalog_agent_id}/ucp"

BASE_URL = "https://shopping.example"

MERCHANT_ID = "mrc-host"
AGENT_ID = f"shopping-cli-merchant-agent:{MERCHANT_ID}"
CAGT_ID = f"cagt_{AGENT_ID}"

# §3.4 private fields that MUST NOT appear in any published document.
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
})


# ── seeding helpers ───────────────────────────────────────────────────────────


def _seed_merchant(db_file: Path, merchant_id: str = MERCHANT_ID, name: str = "Hosted Tea Shop"):
    """Create a merchant with distinctive §3.4 private content (automation
    boundaries + private contact) so leak tests have something to look for."""
    with db_session(db_file):
        pass  # initialize schema
    with closing(sqlite3.connect(db_file)) as raw:
        raw.execute("pragma foreign_keys = on")
        ts = now_iso()
        raw.execute(
            """
            insert into merchants(
                id, name, city, service_area, contact, hours,
                automation_boundaries, tags_json, created_at, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)
            """,
            (
                merchant_id,
                name,
                "Hangzhou",
                "Xihu",
                "private@example.com",
                "9-5",
                "full-auto",
                ts,
                ts,
            ),
        )
        raw.commit()


def _seed_hosted_agent(
    db_file: Path,
    merchant_id: str = MERCHANT_ID,
    capabilities: list[str] | None = None,
) -> None:
    """Create the runtime agent + hosted catalog entry via the heartbeat path."""
    with db_session(db_file) as conn:
        record_heartbeat(conn, merchant_id, capabilities=capabilities or ["catalog"])


def _seed_non_publishable_agents(db_file: Path) -> None:
    """Add catalog agents that must NOT be publishable (404 / NotFoundError)."""
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


def _expected_capability_ids(capabilities: list[str]) -> list[str]:
    """Expected UCP capability ids for *capabilities* per the allowlist."""
    expected: list[str] = []
    seen: set[str] = set()
    for name in capabilities:
        mapping = _PUBLICATION_CAPABILITY_MAP.get(name)
        if mapping is None:
            continue
        fqid = f"{mapping[0]}:{mapping[1]}"
        if fqid not in seen:
            seen.add(fqid)
            expected.append(fqid)
    return sorted(expected)


def _collect_keys(obj: object) -> set[str]:
    """Recursively collect all dict keys in a JSON-serializable object."""
    seen: set[str] = set()

    def _walk(o: object) -> None:
        if isinstance(o, dict):
            for key, item in o.items():
                seen.add(str(key))
                _walk(item)
        elif isinstance(o, list):
            for item in o:
                _walk(item)

    _walk(obj)
    return seen


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


# ── base test class with shared harness ───────────────────────────────────────


class _HostedPublicationTestCase(unittest.TestCase):
    """Shared env patching + dual-stack request helpers."""

    def setUp(self):
        self._base_env = patch.dict(
            "os.environ",
            {
                "SHOPPING_HOSTED_A2A_BASE_URL": BASE_URL,
                "SHOPPING_ADMIN_TOKEN": "test-admin-token-hosted",
            },
            clear=False,
        )
        self._base_env.start()

    def tearDown(self):
        self._base_env.stop()

    # ── fallback ASGI ─────────────────────────────────────────────────────

    async def _asgi(self, app, method, path, headers=None):
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

        req_headers = [(b"content-type", b"application/json")]
        for key, value in (headers or {}).items():
            req_headers.append((str(key).lower().encode("latin1"), str(value).encode("latin1")))

        await app(
            {
                "type": "http",
                "method": method,
                "path": path,
                "query_string": b"",
                "headers": req_headers,
            },
            receive,
            send,
        )
        status = next(
            m["status"] for m in sent if m["type"] == "http.response.start"
        )
        resp_headers = {
            k.decode("latin1").lower(): v.decode("latin1")
            for m in sent
            if m["type"] == "http.response.start"
            for k, v in m.get("headers", [])
        }
        body = b"".join(
            m.get("body", b"") for m in sent if m["type"] == "http.response.body"
        )
        parsed = json.loads(body.decode("utf-8")) if body else {}
        return status, parsed, resp_headers

    def _fallback_get(self, app, path, headers=None):
        return asyncio.run(self._asgi(app, "GET", path, headers=headers))

    # ── FastAPI harness ───────────────────────────────────────────────────

    def _fastapi_app(self, db_file):
        with patch("shopping_cli.api.app.FastAPI", FakeFastAPI):
            return create_app(db_file)

    def _fastapi_get(self, app, path, catalog_agent_id, if_none_match=""):
        """Call a FastAPI GET route endpoint, handling Response returns."""
        endpoint = next(
            (
                route.endpoint
                for route in app.routes
                if route.path == path and "GET" in route.methods
            ),
            None,
        )
        if endpoint is None:
            raise AssertionError(f"No GET route for {path}")
        try:
            result = endpoint(catalog_agent_id, if_none_match)
        except Exception as exc:  # route raised → exception_handler mapping
            for exc_type, handler in app.exception_handlers.items():
                if isinstance(exc, exc_type):
                    response = handler(None, exc)
                    body_bytes = getattr(response, "body", b"")
                    if isinstance(body_bytes, str):
                        body_bytes = body_bytes.encode("utf-8")
                    return (
                        response.status_code,
                        json.loads(body_bytes.decode("utf-8") or "{}"),
                        {},
                    )
            raise

        status = result.status_code
        body_bytes = getattr(result, "body", b"")
        if isinstance(body_bytes, str):
            body_bytes = body_bytes.encode("utf-8")
        parsed = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        headers = {
            str(k).lower(): str(v)
            for k, v in getattr(result, "headers", {}).items()
        } if hasattr(result, "headers") else {}
        return status, parsed, headers


# ── builder-level tests ───────────────────────────────────────────────────────


class HostedPublicationBuilderTest(_HostedPublicationTestCase):
    """Agent Card / UCP Profile generation and structural self-checks."""

    def _card(self, db_file, catalog_agent_id=CAGT_ID):
        from shopping_cli.a2a.agent_card import build_hosted_agent_card

        with db_session(db_file) as conn:
            return build_hosted_agent_card(conn, catalog_agent_id, base_url=BASE_URL)

    def _ucp(self, db_file, catalog_agent_id=CAGT_ID):
        from shopping_cli.a2a.ucp_profile import build_hosted_ucp_profile

        with db_session(db_file) as conn:
            return build_hosted_ucp_profile(conn, catalog_agent_id, base_url=BASE_URL)

    # ── round-trips ──────────────────────────────────────────────────────

    def test_agent_card_round_trips_through_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant(db_file)
            _seed_hosted_agent(db_file, capabilities=["catalog", "inventory"])

            from shopping_cli.discovery.agent_card import parse_agent_card

            card = self._card(db_file)
            result = parse_agent_card(card, source_url=card["url"])

            self.assertEqual(result.name, "Hosted Tea Shop")
            self.assertEqual(result.version, "1.0.0")
            self.assertEqual(card["version"], "1.0.0")
            # The card is projected onto its own §14.1 shared-host URL.
            self.assertEqual(card["url"], f"{BASE_URL}/a2a/agents/{CAGT_ID}")

    def test_ucp_profile_round_trips_through_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant(db_file)
            _seed_hosted_agent(db_file, capabilities=["catalog"])

            from shopping_cli.discovery.ucp import parse_ucp_profile

            ucp = self._ucp(db_file)
            result = parse_ucp_profile(
                ucp,
                source_url=f"{BASE_URL}/v1/hosted/agents/{CAGT_ID}/ucp",
            )

            self.assertEqual(result.specification_version, "2026-04-08")
            self.assertEqual(ucp["specificationVersion"], "2026-04-08")
            self.assertEqual(result.service_identity_id, ucp["serviceIdentity"]["id"])

    # ── advertised shape ─────────────────────────────────────────────────

    def test_card_advertises_jsonrpc_interface_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant(db_file)
            _seed_hosted_agent(db_file)

            card = self._card(db_file)
            interfaces = card["supportedInterfaces"]
            self.assertEqual(interfaces, [{"name": "jsonrpc", "version": "1.0"}])

    def test_card_omits_capabilities_block_and_kiwi_extension(self):
        """No invented Kiwi negotiation URI, no A2A capability flags (rc1 §5).

        The production namespace for the Kiwi extension is not frozen, so the
        card MUST NOT emit a ``capabilities`` block with a made-up URI.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant(db_file)
            _seed_hosted_agent(db_file)

            card = self._card(db_file)
            self.assertNotIn("capabilities", card)
            self.assertNotIn("shopping.negotiation", json.dumps(card))
            self.assertNotIn("kiwi", json.dumps(card).lower())

    def test_card_projects_public_skills(self):
        from shopping_cli.a2a.agent_card import build_hosted_agent_card
        from shopping_cli.agent_catalog.sqlite_repository import replace_skills

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant(db_file)
            _seed_hosted_agent(db_file)
            with db_session(db_file) as conn:
                replace_skills(
                    conn,
                    CAGT_ID,
                    [
                        {
                            "skill_id": "skl_quote",
                            "name": "Quote",
                            "description": "Provide a quote",
                            "tags_json": '["pricing"]',
                            "input_modes_json": '["text"]',
                            "output_modes_json": '["text"]',
                        }
                    ],
                )
                card = build_hosted_agent_card(conn, CAGT_ID, base_url=BASE_URL)

            self.assertIn("skills", card)
            skill = card["skills"][0]
            self.assertEqual(skill["id"], "skl_quote")
            self.assertEqual(skill["name"], "Quote")
            self.assertEqual(skill["description"], "Provide a quote")
            self.assertEqual(skill["tags"], ["pricing"])
            self.assertEqual(skill["inputModes"], ["text"])
            self.assertEqual(skill["outputModes"], ["text"])

            # Skills projection is §3.4 public — a raw skill row's private
            # columns (none exist, but the projection must stay minimal).
            self.assertEqual(
                set(skill),
                {"id", "name", "description", "tags", "inputModes", "outputModes"},
            )

    def test_ucp_a2a_service_shape_matches_binding(self):
        """Binding rc1 §5: a2a transport with endpoint = Agent Card URL."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant(db_file)
            _seed_hosted_agent(db_file)

            ucp = self._ucp(db_file)
            service = ucp["services"][0]
            self.assertEqual(service["type"], "commerce")
            self.assertEqual(service["endpoints"], [
                {"uri": f"{BASE_URL}/a2a/agents/{CAGT_ID}", "protocol": "a2a"},
            ])
            # serviceIdentity.id is the Agent Card URL (rc1 §5).
            self.assertEqual(ucp["serviceIdentity"]["id"], f"{BASE_URL}/a2a/agents/{CAGT_ID}")

    # ── §3.4 private-field boundary ──────────────────────────────────────

    def test_merchant_public_ref_strips_private_fields(self):
        """The serializer boundary drops tokens / floor_price / boundaries."""
        merchant = {
            "id": "mrc-x",
            "name": "X",
            "city": "C",
            "service_area": "A",
            "tags_json": '["a"]',
            "floor_price": 99,
            "agent_token": "sk-agent-12345678901234567890123456789012",
            "merchant_token": "sk-merchant-12345678901234567890123456789012",
            "automation_boundaries": "full-auto",
            "contact": "private@example.com",
            "llm_prompt": "you are a seller",
            "delivery_fee": 5.0,
        }
        public = public_merchant_ref(merchant)
        self.assertNotIn("floor_price", public)
        self.assertNotIn("agent_token", public)
        self.assertNotIn("merchant_token", public)
        self.assertNotIn("automation_boundaries", public)
        self.assertNotIn("contact", public)
        self.assertNotIn("llm_prompt", public)
        self.assertNotIn("delivery_fee", public)

    def test_published_documents_contain_no_private_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant(db_file)  # seeds automation_boundaries + private contact
            _seed_hosted_agent(db_file)

            card = self._card(db_file)
            ucp = self._ucp(db_file)
            serialized = json.dumps(card) + "\n" + json.dumps(ucp)

            # Private content seeded into the merchant must not surface.
            for secret in ("automation_boundaries", "full-auto", "private@example.com", "9-5"):
                self.assertNotIn(secret, serialized, f"leaked {secret!r}")
            # Private field names must not appear at any nesting level.
            for field in _PRIVATE_FIELDS:
                self.assertNotIn(field, serialized, f"leaked field {field!r}")

    def test_card_does_not_carry_commerce_capabilities(self):
        """Commerce capabilities belong in the UCP, not the Agent Card (rc1 §5)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant(db_file)
            _seed_hosted_agent(db_file, capabilities=["catalog", "inventory"])

            card = self._card(db_file)
            self.assertNotIn("com.harrylabsj.shopping.capability", json.dumps(card))

    # ── publishability gating (§5.1) ─────────────────────────────────────

    def test_non_hosted_agent_is_not_publishable(self):
        from shopping_cli.core.errors import NotFoundError

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant(db_file)
            _seed_hosted_agent(db_file)
            _seed_non_publishable_agents(db_file)

            with db_session(db_file) as conn:
                from shopping_cli.a2a.agent_card import build_hosted_agent_card
                from shopping_cli.a2a.ucp_profile import build_hosted_ucp_profile

                with self.assertRaises(NotFoundError):
                    build_hosted_agent_card(conn, "cagt_direct", base_url=BASE_URL)
                with self.assertRaises(NotFoundError):
                    build_hosted_ucp_profile(conn, "cagt_direct", base_url=BASE_URL)

    def test_inactive_agent_is_not_publishable(self):
        from shopping_cli.core.errors import NotFoundError

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant(db_file)
            _seed_hosted_agent(db_file)
            _seed_non_publishable_agents(db_file)

            with db_session(db_file) as conn:
                from shopping_cli.a2a.agent_card import build_hosted_agent_card
                from shopping_cli.a2a.ucp_profile import build_hosted_ucp_profile

                with self.assertRaises(NotFoundError):
                    build_hosted_agent_card(conn, "cagt_inactive", base_url=BASE_URL)
                with self.assertRaises(NotFoundError):
                    build_hosted_ucp_profile(conn, "cagt_inactive", base_url=BASE_URL)

    def test_unknown_agent_is_not_publishable(self):
        from shopping_cli.core.errors import NotFoundError

        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant(db_file)

            with db_session(db_file) as conn:
                from shopping_cli.a2a.agent_card import build_hosted_agent_card
                from shopping_cli.a2a.ucp_profile import build_hosted_ucp_profile

                with self.assertRaises(NotFoundError):
                    build_hosted_agent_card(conn, "cagt_unknown", base_url=BASE_URL)
                with self.assertRaises(NotFoundError):
                    build_hosted_ucp_profile(conn, "cagt_unknown", base_url=BASE_URL)

    # ── base_url validation ──────────────────────────────────────────────

    def test_base_url_validation(self):
        from shopping_cli.a2a._common import validate_base_url
        from shopping_cli.core.errors import ValidationError

        # Invalid schemes / userinfo / empty are rejected.
        for bad in ("", "ftp://shopping.example", "https://user:pass@shopping.example",
                    "not-a-url"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    validate_base_url(bad)

        # http/https accepted; trailing slash stripped.
        self.assertEqual(validate_base_url("https://shopping.example/"), "https://shopping.example")
        self.assertEqual(validate_base_url("http://localhost"), "http://localhost")


# ── capability projection (one-way, allowlist) ────────────────────────────────


class HostedPublicationCapabilityProjectionTest(_HostedPublicationTestCase):
    """§25 Phase 1 — runtime capabilities → published UCP capabilities.

    The projection is an explicit allowlist (`_PUBLICATION_CAPABILITY_MAP`),
    strictly one-way: re-projecting never writes back to the runtime agents
    table.
    """

    def test_ucp_capabilities_match_publication_map(self):
        runtime = ["catalog", "inventory", "delivery", "consultation", "secret_feature"]
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant(db_file)
            _seed_hosted_agent(db_file, capabilities=runtime)

            from shopping_cli.a2a.ucp_profile import build_hosted_ucp_profile

            with db_session(db_file) as conn:
                ucp = build_hosted_ucp_profile(conn, CAGT_ID, base_url=BASE_URL)

            self.assertEqual(
                ucp["services"][0]["capabilities"],
                _expected_capability_ids(["catalog", "inventory", "delivery", "consultation"]),
            )
            # Unknown runtime capability is silently dropped (fail-closed).
            self.assertNotIn("secret_feature", json.dumps(ucp))

    def test_runtime_capabilities_reflected_after_reprojection(self):
        """Changing runtime capabilities_json and re-projecting is reflected.

        One-way: the catalog path never writes back to the agents table.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant(db_file)
            _seed_hosted_agent(db_file, capabilities=["catalog"])

            from shopping_cli.a2a.ucp_profile import build_hosted_ucp_profile

            with db_session(db_file) as conn:
                ucp1 = build_hosted_ucp_profile(conn, CAGT_ID, base_url=BASE_URL)
            self.assertEqual(
                ucp1["services"][0]["capabilities"],
                _expected_capability_ids(["catalog"]),
            )

            # Runtime capability set grows → re-run the one-way projection.
            new_runtime = ["catalog", "inventory", "delivery"]
            with db_session(db_file) as conn:
                ensure_hosted_catalog_agent(
                    conn,
                    agent_id=AGENT_ID,
                    merchant_id=MERCHANT_ID,
                    merchant_name="Hosted Tea Shop",
                    runtime_capabilities=new_runtime,
                )
                ucp2 = build_hosted_ucp_profile(conn, CAGT_ID, base_url=BASE_URL)

            self.assertEqual(
                ucp2["services"][0]["capabilities"],
                _expected_capability_ids(new_runtime),
            )
            # The change is a projection, not a write-back: the runtime agent's
            # own capabilities_json is left untouched by the catalog path.
            with db_session(db_file) as conn:
                row = conn.execute(
                    "select capabilities_json from agents where id = ?", (AGENT_ID,)
                ).fetchone()
            self.assertEqual(json.loads(row["capabilities_json"]), ["catalog"])

    def test_card_capabilities_never_advertise_runtime_short_names(self):
        """Short runtime names must not leak into the published card."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant(db_file)
            _seed_hosted_agent(db_file, capabilities=["catalog"])

            from shopping_cli.a2a.agent_card import build_hosted_agent_card

            with db_session(db_file) as conn:
                card = build_hosted_agent_card(conn, CAGT_ID, base_url=BASE_URL)
            self.assertNotIn("catalog", json.dumps(card))
            self.assertNotIn("com.harrylabsj.shopping.capability", json.dumps(card))


# ── route-level tests (dual stack) ────────────────────────────────────────────


class HostedPublicationApiTest(_HostedPublicationTestCase):
    """GET routes, 404 gating, §18 ETag/304, and registry consistency."""

    def _seed_publishable(self, db_file):
        _seed_merchant(db_file)
        _seed_hosted_agent(db_file, capabilities=["catalog", "inventory"])
        _seed_non_publishable_agents(db_file)
        return CAGT_ID

    # ── 2 routes × fallback stack ────────────────────────────────────────

    def test_agent_card_route_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            cagt_id = self._seed_publishable(db_file)
            app = MarketplaceASGIApp(db_file)

            status, body, headers = self._fallback_get(
                app, CARD_PATH.format(catalog_agent_id=cagt_id)
            )

        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "Hosted Tea Shop")
        self.assertEqual(body["version"], "1.0.0")
        self.assertEqual(body["url"], f"{BASE_URL}/a2a/agents/{cagt_id}")
        self.assertTrue(headers.get("etag"), "response must carry an ETag")

    def test_ucp_route_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            cagt_id = self._seed_publishable(db_file)
            app = MarketplaceASGIApp(db_file)

            status, body, headers = self._fallback_get(
                app, UCP_PATH.format(catalog_agent_id=cagt_id)
            )

        self.assertEqual(status, 200)
        self.assertEqual(body["specificationVersion"], "2026-04-08")
        self.assertEqual(body["services"][0]["endpoints"], [
            {"uri": f"{BASE_URL}/a2a/agents/{cagt_id}", "protocol": "a2a"},
        ])
        self.assertTrue(headers.get("etag"))

    # ── 2 routes × FastAPI stack ─────────────────────────────────────────

    def test_agent_card_route_fastapi(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            cagt_id = self._seed_publishable(db_file)
            app = self._fastapi_app(db_file)

            status, body, headers = self._fastapi_get(
                app, CARD_PATH, cagt_id
            )

        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "Hosted Tea Shop")
        self.assertEqual(body["url"], f"{BASE_URL}/a2a/agents/{cagt_id}")
        self.assertTrue(headers.get("etag"))

    def test_ucp_route_fastapi(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            cagt_id = self._seed_publishable(db_file)
            app = self._fastapi_app(db_file)

            status, body, headers = self._fastapi_get(app, UCP_PATH, cagt_id)

        self.assertEqual(status, 200)
        self.assertEqual(body["specificationVersion"], "2026-04-08")
        self.assertTrue(headers.get("etag"))

    # ── 404 gating (no existence oracle) ─────────────────────────────────

    def test_non_hosted_agent_404_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            self._seed_publishable(db_file)
            app = MarketplaceASGIApp(db_file)

            for path in (CARD_PATH.format(catalog_agent_id="cagt_direct"),
                         UCP_PATH.format(catalog_agent_id="cagt_direct")):
                with self.subTest(path=path):
                    status, body, _ = self._fallback_get(app, path)
                    self.assertEqual(status, 404)
                    self.assertFalse(body["ok"])

    def test_non_hosted_agent_404_fastapi(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            self._seed_publishable(db_file)
            app = self._fastapi_app(db_file)

            for path in (CARD_PATH, UCP_PATH):
                with self.subTest(path=path):
                    status, body, _ = self._fastapi_get(app, path, "cagt_direct")
                    self.assertEqual(status, 404)
                    self.assertFalse(body["ok"])

    def test_inactive_agent_404_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            self._seed_publishable(db_file)
            app = MarketplaceASGIApp(db_file)

            status, body, _ = self._fallback_get(
                app, CARD_PATH.format(catalog_agent_id="cagt_inactive")
            )
            self.assertEqual(status, 404)

    def test_unknown_agent_404_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            self._seed_publishable(db_file)
            app = MarketplaceASGIApp(db_file)

            status, body, _ = self._fallback_get(
                app, UCP_PATH.format(catalog_agent_id="cagt_unknown")
            )
            self.assertEqual(status, 404)

    def test_404_body_reveals_no_existence_details(self):
        """Non-hosted, inactive, and unknown ids are indistinguishable.

        Each 404 carries the same error template that only echoes the
        request-supplied id, so a caller cannot tell "exists but not
        publishable" apart from "does not exist".
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            self._seed_publishable(db_file)
            app = MarketplaceASGIApp(db_file)

            for cagt_id in ("cagt_direct", "cagt_inactive", "cagt_unknown"):
                status, body, _ = self._fallback_get(
                    app, CARD_PATH.format(catalog_agent_id=cagt_id)
                )
                self.assertEqual(status, 404)
                self.assertFalse(body["ok"])
                # Only echoes the requested id; never reveals the agent's state.
                self.assertEqual(body["error"], f"Unknown catalog agent: {cagt_id}")

    # ── §18 ETag / If-None-Match → 304 ───────────────────────────────────

    def test_etag_304_round_trip_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            cagt_id = self._seed_publishable(db_file)
            app = MarketplaceASGIApp(db_file)
            path = UCP_PATH.format(catalog_agent_id=cagt_id)

            status, body, headers = self._fallback_get(app, path)
            self.assertEqual(status, 200)
            etag = headers["etag"]
            self.assertTrue(etag.startswith('"') and etag.endswith('"'))

            # Matching If-None-Match → 304 with empty body.
            status, body, headers = self._fallback_get(app, path, headers={"If-None-Match": etag})
            self.assertEqual(status, 304)
            self.assertEqual(body, {})
            self.assertEqual(headers.get("etag"), etag)

            # Weak form also matches.
            status, _, _ = self._fallback_get(
                app, path, headers={"If-None-Match": f"W/{etag}"}
            )
            self.assertEqual(status, 304)

            # Wildcard matches.
            status, _, _ = self._fallback_get(app, path, headers={"If-None-Match": "*"})
            self.assertEqual(status, 304)

            # Mismatch → 200 with fresh body.
            status, body, _ = self._fallback_get(
                app, path, headers={"If-None-Match": '"deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"'}
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["specificationVersion"], "2026-04-08")

    def test_etag_304_round_trip_fastapi(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            cagt_id = self._seed_publishable(db_file)
            app = self._fastapi_app(db_file)

            status, _, headers = self._fastapi_get(app, CARD_PATH, cagt_id)
            self.assertEqual(status, 200)
            etag = headers["etag"]

            status, body, _ = self._fastapi_get(app, CARD_PATH, cagt_id, if_none_match=etag)
            self.assertEqual(status, 304)
            self.assertEqual(body, {})

    def test_wildcard_if_none_match_does_not_304_unknown_agent(self):
        """A 404 must never be revalidated as 304 (not even for `*`)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            self._seed_publishable(db_file)
            app = MarketplaceASGIApp(db_file)

            status, body, _ = self._fallback_get(
                app,
                CARD_PATH.format(catalog_agent_id="cagt_unknown"),
                headers={"If-None-Match": "*"},
            )
            self.assertEqual(status, 404)
            self.assertFalse(body["ok"])

    def test_etag_is_stable_and_stack_consistent(self):
        """Same document → same ETag on both stacks (same hash input)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            cagt_id = self._seed_publishable(db_file)

            fallback_app = MarketplaceASGIApp(db_file)
            fastapi_app = self._fastapi_app(db_file)

            _, _, fh = self._fallback_get(fallback_app, UCP_PATH.format(catalog_agent_id=cagt_id))
            _, _, xh = self._fastapi_get(fastapi_app, UCP_PATH, cagt_id)
            self.assertEqual(fh["etag"], xh["etag"])

    # ── registry consistency ─────────────────────────────────────────────

    def test_hosted_routes_in_registry(self):
        paths = {route.path: route.methods for route in route_info()}
        self.assertIn(CARD_PATH, paths)
        self.assertEqual(paths[CARD_PATH], {"GET"})
        self.assertIn(UCP_PATH, paths)
        self.assertEqual(paths[UCP_PATH], {"GET"})

    def test_hosted_routes_have_marketplace_group(self):
        marketplace_paths = {route.path for route in routes_for_group("marketplace")}
        self.assertIn(CARD_PATH, marketplace_paths)
        self.assertIn(UCP_PATH, marketplace_paths)

    def test_hosted_routes_have_agent_catalog_group(self):
        catalog_paths = {route.path for route in routes_for_group("agent_catalog")}
        self.assertIn(CARD_PATH, catalog_paths)
        self.assertIn(UCP_PATH, catalog_paths)

    def test_fastapi_app_registers_hosted_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            app = self._fastapi_app(db_file)
            route_paths = {
                route.path for route in getattr(app, "routes", []) if hasattr(route, "path")
            }
            self.assertIn(CARD_PATH, route_paths)
            self.assertIn(UCP_PATH, route_paths)

    # ── no private fields in route payloads ──────────────────────────────

    def test_no_private_fields_in_route_responses(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            cagt_id = self._seed_publishable(db_file)
            app = MarketplaceASGIApp(db_file)

            for path in (CARD_PATH.format(catalog_agent_id=cagt_id),
                         UCP_PATH.format(catalog_agent_id=cagt_id)):
                with self.subTest(path=path):
                    status, body, _ = self._fallback_get(app, path)
                    self.assertEqual(status, 200)
                    keys = _collect_keys(body)
                    leaked = _PRIVATE_FIELDS & keys
                    self.assertEqual(leaked, set(), f"Private fields leaked: {leaked}")
                    serialized = json.dumps(body)
                    for secret in ("automation_boundaries", "private@example.com"):
                        self.assertNotIn(secret, serialized)


if __name__ == "__main__":
    unittest.main()

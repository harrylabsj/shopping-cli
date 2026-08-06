"""Tests for the Agent Catalog service layer (T2)."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import sqlite3

from shopping_cli.agents.tools import record_heartbeat
from shopping_cli.core.catalog import create_merchant
from shopping_cli.core.errors import NotFoundError, ValidationError
from shopping_cli.db.session import db_session, decode_json, now_iso
from shopping_cli.services.agent_catalog import (
    ensure_hosted_catalog_agent,
    get_catalog_agent,
    search_catalog_agents,
)
from shopping_cli.services.agents import register_hosted_agent_in_catalog


# ── helpers ─────────────────────────────────────────────────────────────────


def _seed_merchant(conn, merchant_id="mrc-test", name="Test Merchant", **kwargs):
    """Create a merchant in a db_session-managed connection."""
    # create_merchant works with raw sqlite3.Connection and commits internally,
    # so we seed before entering db_session in tests that need a pre-seeded DB.
    try:
        return create_merchant(conn, merchant_id=merchant_id, name=name, **kwargs)
    except Exception:
        # may already exist in the same session
        return {"id": merchant_id, "name": name}


def _seed_merchant_in_db(db_file: Path, merchant_id="mrc-test", name="Test Merchant"):
    # Initialize the database schema first
    with db_session(db_file):
        pass
    with closing(sqlite3.connect(db_file)) as raw:
        raw.execute("pragma foreign_keys = on")
        ts = now_iso()
        try:
            raw.execute(
                """
                insert into merchants(id, name, city, service_area, contact, hours,
                    automation_boundaries, tags_json, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)
                """,
                (merchant_id, name, "Test City", "Test Area", "test@example.com",
                 "9-5", "none", ts, ts),
            )
            raw.commit()
        except sqlite3.IntegrityError:
            pass


def _heartbeat(conn, merchant_id="mrc-test", capabilities=None):
    """Simulate a merchant agent heartbeat."""
    return record_heartbeat(
        conn,
        merchant_id=merchant_id,
        status="online",
        capabilities=capabilities or ["catalog", "inventory"],
    )


# ── tests ───────────────────────────────────────────────────────────────────


class HostedAutoCreationTest(unittest.TestCase):
    """Heartbeat-triggered catalog entry creation (§25 Phase 1)."""

    def test_first_heartbeat_creates_catalog_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant_in_db(db_file, "mrc-auto")
            with db_session(db_file) as conn:
                result = _heartbeat(conn, "mrc-auto")
                self.assertEqual(result["owner_id"], "mrc-auto")

                # Catalog entry should exist
                cagt = get_catalog_agent(conn, "cagt_shopping-cli-merchant-agent:mrc-auto")
                self.assertEqual(cagt["catalog_agent_id"], "cagt_shopping-cli-merchant-agent:mrc-auto")
                self.assertEqual(cagt["hosting"]["mode"], "hosted")
                self.assertEqual(cagt["verification"]["status"], "commerce_verified")
                self.assertIn("merchant", cagt)

    def test_second_heartbeat_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant_in_db(db_file, "mrc-idem")
            with db_session(db_file) as conn:
                _heartbeat(conn, "mrc-idem")
                cagt1 = get_catalog_agent(conn, "cagt_shopping-cli-merchant-agent:mrc-idem")

                # Second heartbeat should not fail or duplicate
                _heartbeat(conn, "mrc-idem")
                cagt2 = get_catalog_agent(conn, "cagt_shopping-cli-merchant-agent:mrc-idem")

                self.assertEqual(cagt1["catalog_agent_id"], cagt2["catalog_agent_id"])

    def test_heartbeat_without_merchant_does_not_crash(self):
        """Heartbeat when merchant doesn't exist should raise from require_merchant,
        not from catalog projection."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            with db_session(db_file) as conn:
                with self.assertRaises(Exception):
                    # record_heartbeat calls require_merchant which raises NotFoundError
                    record_heartbeat(conn, merchant_id="nonexistent")


class OneWayProjectionTest(unittest.TestCase):
    """agents.capabilities_json → agent_capabilities is strictly one-way (§25)."""

    def test_runtime_capabilities_projected_to_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant_in_db(db_file, "mrc-proj")
            with db_session(db_file) as conn:
                _heartbeat(conn, "mrc-proj", capabilities=["catalog", "inventory", "delivery"])

                rows = conn.execute(
                    "select capability_id from agent_capabilities where catalog_agent_id = ? order by capability_id",
                    ("cagt_shopping-cli-merchant-agent:mrc-proj",),
                ).fetchall()
                cap_ids = [r["capability_id"] for r in rows]
                # Only capabilities in the publication allowlist are projected
                self.assertIn("catalog", cap_ids)
                self.assertIn("inventory", cap_ids)
                self.assertIn("delivery", cap_ids)

    def test_unknown_capability_not_projected(self):
        """Capabilities not in the publication allowlist are silently dropped."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant_in_db(db_file, "mrc-unknown")
            with db_session(db_file) as conn:
                _heartbeat(conn, "mrc-unknown", capabilities=["catalog", "secret_internal_feature"])

                rows = conn.execute(
                    "select capability_id from agent_capabilities where catalog_agent_id = ?",
                    ("cagt_shopping-cli-merchant-agent:mrc-unknown",),
                ).fetchall()
                cap_ids = [r["capability_id"] for r in rows]
                self.assertIn("catalog", cap_ids)
                self.assertNotIn("secret_internal_feature", cap_ids)

    def test_catalog_capabilities_never_write_back_to_agents(self):
        """The projection is one-way: catalog writes must not modify agents table."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant_in_db(db_file, "mrc-oneway")
            with db_session(db_file) as conn:
                _heartbeat(conn, "mrc-oneway", capabilities=["catalog"])

                # Capture original agents.capabilities_json
                agent_row = conn.execute(
                    "select capabilities_json from agents where id = ?",
                    ("shopping-cli-merchant-agent:mrc-oneway",),
                ).fetchone()
                original_caps = decode_json(agent_row["capabilities_json"], [])

                # Now call ensure_hosted_catalog_agent directly (simulating re-projection)
                ensure_hosted_catalog_agent(
                    conn,
                    agent_id="shopping-cli-merchant-agent:mrc-oneway",
                    merchant_id="mrc-oneway",
                    merchant_name="One Way",
                    runtime_capabilities=["catalog", "inventory"],
                )

                # agents.capabilities_json must still be original (not modified by catalog path)
                agent_row2 = conn.execute(
                    "select capabilities_json from agents where id = ?",
                    ("shopping-cli-merchant-agent:mrc-oneway",),
                ).fetchone()
                after_caps = decode_json(agent_row2["capabilities_json"], [])
                self.assertEqual(after_caps, original_caps)


class PublicSerializerLeakTest(unittest.TestCase):
    """Public serializer MUST NOT expose §3.4 private fields."""

    def test_private_merchant_fields_not_in_public_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant_in_db(db_file, "mrc-leak")
            with db_session(db_file) as conn:
                _heartbeat(conn, "mrc-leak")

                result = get_catalog_agent(conn, "cagt_shopping-cli-merchant-agent:mrc-leak")

                # Serialize to JSON and back to check all nested keys
                serialized = json.dumps(result)
                self.assertNotIn("automation_boundaries", serialized)
                self.assertNotIn("contact", serialized)
                self.assertNotIn("hours", serialized)
                self.assertNotIn("floor_price", serialized)
                self.assertNotIn("agent_token", serialized)
                self.assertNotIn("merchant_token", serialized)
                self.assertNotIn("llm_prompt", serialized)
                self.assertNotIn("private_contact", serialized)

    def test_internal_catalog_fields_not_in_public_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant_in_db(db_file, "mrc-internal")
            with db_session(db_file) as conn:
                _heartbeat(conn, "mrc-internal")

                result = get_catalog_agent(conn, "cagt_shopping-cli-merchant-agent:mrc-internal")
                serialized = json.dumps(result)

                # Internal-only catalog fields must not leak
                self.assertNotIn("first_seen_at", serialized)
                self.assertNotIn("last_seen_at", serialized)
                self.assertNotIn("created_at", serialized)
                self.assertNotIn("updated_at", serialized)
                self.assertNotIn("provider_name", serialized)

    def test_search_results_also_use_public_serializer(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant_in_db(db_file, "mrc-search-leak")
            with db_session(db_file) as conn:
                _heartbeat(conn, "mrc-search-leak")

                results = search_catalog_agents(conn)
                self.assertGreaterEqual(len(results["results"]), 1)

                serialized = json.dumps(results["results"])
                self.assertNotIn("automation_boundaries", serialized)
                self.assertNotIn("contact", serialized)
                self.assertNotIn("llm_prompt", serialized)
                self.assertNotIn("first_seen_at", serialized)


class SearchFiltersTest(unittest.TestCase):
    """Hard-filtered, deterministic search (§8.3, §10.1)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self._tmp.name) / "test.sqlite"
        # Seed two merchants with different characteristics
        _seed_merchant_in_db(self.db_file, "mrc-a", "Alpha Merchant")
        _seed_merchant_in_db(self.db_file, "mrc-b", "Beta Merchant")
        with db_session(self.db_file) as conn:
            _heartbeat(conn, "mrc-a", capabilities=["catalog", "inventory"])
            _heartbeat(conn, "mrc-b", capabilities=["catalog", "delivery"])

    def tearDown(self):
        self._tmp.cleanup()

    def test_search_no_filters_returns_all(self):
        with db_session(self.db_file) as conn:
            results = search_catalog_agents(conn)
            self.assertGreaterEqual(len(results["results"]), 2)

    def test_search_by_hosting_mode(self):
        with db_session(self.db_file) as conn:
            results = search_catalog_agents(conn, hosting_mode="hosted")
            self.assertGreaterEqual(len(results["results"]), 2)
            for r in results["results"]:
                self.assertEqual(r["hosting"]["mode"], "hosted")

            # Filter that matches nothing
            results2 = search_catalog_agents(conn, hosting_mode="direct")
            self.assertEqual(len(results2["results"]), 0)

    def test_search_by_verification_status(self):
        with db_session(self.db_file) as conn:
            results = search_catalog_agents(conn, verification_status="commerce_verified")
            self.assertGreaterEqual(len(results["results"]), 2)

            results2 = search_catalog_agents(conn, verification_status="discovered")
            self.assertEqual(len(results2["results"]), 0)

    def test_search_by_text_query(self):
        with db_session(self.db_file) as conn:
            results = search_catalog_agents(conn, q="Alpha")
            self.assertEqual(len(results["results"]), 1)
            self.assertIn("Alpha", results["results"][0]["merchant"]["name"])

    def test_search_by_capability(self):
        with db_session(self.db_file) as conn:
            # Full capability_id match
            results = search_catalog_agents(conn, capability="inventory")
            self.assertEqual(len(results["results"]), 1)

            results2 = search_catalog_agents(conn, capability="delivery")
            self.assertEqual(len(results2["results"]), 1)

    def test_search_by_protocol(self):
        with db_session(self.db_file) as conn:
            # No endpoints seeded yet, so protocol filter returns empty
            results = search_catalog_agents(conn, protocol="a2a")
            self.assertEqual(len(results["results"]), 0)

    def test_search_pagination(self):
        with db_session(self.db_file) as conn:
            # First page with limit=1
            page1 = search_catalog_agents(conn, limit=1)
            self.assertEqual(len(page1["results"]), 1)
            self.assertIsNotNone(page1["next_cursor"])

            # Second page with cursor
            page2 = search_catalog_agents(conn, limit=1, cursor=page1["next_cursor"])
            self.assertEqual(len(page2["results"]), 1)
            self.assertNotEqual(
                page1["results"][0]["catalog_agent_id"],
                page2["results"][0]["catalog_agent_id"],
            )

    def test_verified_after_filter(self):
        with db_session(self.db_file) as conn:
            # All agents are commerce_verified with last_verified_at set at creation
            # Filter for a future date → empty
            results = search_catalog_agents(conn, verified_after="2099-01-01T00:00:00")
            self.assertEqual(len(results["results"]), 0)

            # Filter for a past date → returns all
            results2 = search_catalog_agents(conn, verified_after="2020-01-01T00:00:00")
            self.assertGreaterEqual(len(results2["results"]), 2)


class GetCatalogAgentTest(unittest.TestCase):
    """get_catalog_agent returns §8.2 contract shape."""

    def test_get_existing_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant_in_db(db_file, "mrc-get")
            with db_session(db_file) as conn:
                _heartbeat(conn, "mrc-get")
                result = get_catalog_agent(conn, "cagt_shopping-cli-merchant-agent:mrc-get")

                # Verify §8.2 contract shape
                self.assertIn("catalog_agent_id", result)
                self.assertIn("merchant", result)
                self.assertIn("verification", result)
                self.assertIn("hosting", result)
                self.assertIn("capabilities", result)
                self.assertEqual(result["verification"]["status"], "commerce_verified")
                self.assertEqual(result["hosting"]["mode"], "hosted")

    def test_get_nonexistent_agent_raises_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            with db_session(db_file) as conn:
                with self.assertRaises(NotFoundError):
                    get_catalog_agent(conn, "cagt_nonexistent")


class InvariantEnforcementTest(unittest.TestCase):
    """§5.1 invariants are enforced at the service layer."""

    def test_hosted_commerce_verified_without_runtime_id_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant_in_db(db_file, "mrc-inv1")
            # Use db_session to init DB, but test validates invariant directly
            with db_session(db_file):
                pass

            # Directly try to create a hosted catalog agent that violates §5.1
            from shopping_cli.services.agent_catalog import _validate_hosting_invariant

            with self.assertRaises(ValidationError) as ctx:
                _validate_hosting_invariant("hosted", "commerce_verified", "")
            self.assertIn("hosted_runtime_agent_id", str(ctx.exception))

    def test_non_hosted_commerce_verified_with_runtime_id_raises(self):
        from shopping_cli.services.agent_catalog import _validate_hosting_invariant

        with self.assertRaises(ValidationError) as ctx:
            _validate_hosting_invariant("self_registered", "commerce_verified", "some-agent-id")
        self.assertIn("hosted_runtime_agent_id", str(ctx.exception))

    def test_invariant_not_enforced_for_non_commerce_verified(self):
        """Invariant only applies at COMMERCE_VERIFIED publish time."""
        from shopping_cli.services.agent_catalog import _validate_hosting_invariant

        # Should not raise — status is not commerce_verified
        _validate_hosting_invariant("hosted", "discovered", "")


class AuditEventTest(unittest.TestCase):
    """catalog_agent_registered audit event is written (§23)."""

    def test_heartbeat_writes_catalog_agent_registered_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant_in_db(db_file, "mrc-audit")
            with db_session(db_file) as conn:
                _heartbeat(conn, "mrc-audit")

                audit_rows = conn.execute(
                    "select * from audit_events where event = 'catalog_agent_registered' order by id"
                ).fetchall()
                self.assertGreaterEqual(len(audit_rows), 1)
                details = decode_json(audit_rows[0]["details_json"], {})
                self.assertEqual(details.get("event_type"), "catalog_agent_registered")
                self.assertEqual(details.get("source_type"), "hosted")
                self.assertEqual(details.get("hosting_mode"), "hosted")
                self.assertIn("catalog_agent_id", details)

    def test_ensure_hosted_catalog_agent_writes_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant_in_db(db_file, "mrc-audit2")
            with db_session(db_file) as conn:
                # Seed the runtime agent row to satisfy FK
                conn.execute(
                    """
                    insert into agents(id, type, owner_id, status, capabilities_json, last_seen_at)
                    values (?, 'merchant', ?, 'online', '[]', ?)
                    """,
                    ("test-agent-1", "mrc-audit2", now_iso()),
                )
                ensure_hosted_catalog_agent(
                    conn,
                    agent_id="test-agent-1",
                    merchant_id="mrc-audit2",
                    merchant_name="Audit Test",
                    runtime_capabilities=["catalog"],
                )

                audit_rows = conn.execute(
                    "select * from audit_events where event = 'catalog_agent_registered' order by id"
                ).fetchall()
                self.assertGreaterEqual(len(audit_rows), 1)


class ExplicitRegistrationTest(unittest.TestCase):
    """register_hosted_agent_in_catalog service hook point."""

    def test_explicit_registration_creates_catalog_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            _seed_merchant_in_db(db_file, "mrc-explicit")
            with db_session(db_file) as conn:
                # Seed the runtime agent row to satisfy FK
                agent_id = "shopping-cli-merchant-agent:mrc-explicit"
                conn.execute(
                    """
                    insert into agents(id, type, owner_id, status, capabilities_json, last_seen_at)
                    values (?, 'merchant', ?, 'online', '[]', ?)
                    """,
                    (agent_id, "mrc-explicit", now_iso()),
                )
                result = register_hosted_agent_in_catalog(
                    conn,
                    agent_id=agent_id,
                    merchant_id="mrc-explicit",
                    merchant_name="Explicit Merchant",
                    runtime_capabilities=["consultation"],
                )
                self.assertEqual(
                    result["catalog_agent_id"],
                    "cagt_shopping-cli-merchant-agent:mrc-explicit",
                )
                self.assertEqual(result["hosting"]["mode"], "hosted")

    def test_explicit_registration_without_merchant_id_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            with db_session(db_file) as conn:
                with self.assertRaises(ValidationError):
                    register_hosted_agent_in_catalog(
                        conn,
                        agent_id="some-agent",
                        merchant_id="",
                    )


class DeterministicOrderingTest(unittest.TestCase):
    """Search results follow deterministic ordering (§8.3)."""

    def test_commerce_verified_ranks_highest(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "test.sqlite"
            # Seed merchants
            _seed_merchant_in_db(db_file, "mrc-rank-a")
            _seed_merchant_in_db(db_file, "mrc-rank-b")
            with db_session(db_file) as conn:
                # Create two catalog agents: one commerce_verified, one discovered
                from shopping_cli.agent_catalog.sqlite_repository import upsert_catalog_agent

                upsert_catalog_agent(
                    conn,
                    catalog_agent_id="cagt_special_verified",
                    merchant_id="mrc-rank-a",
                    display_name="Verified First",
                    source_type="hosted",
                    hosting_mode="hosted",
                    verification_status="commerce_verified",
                )
                upsert_catalog_agent(
                    conn,
                    catalog_agent_id="cagt_special_discovered",
                    merchant_id="mrc-rank-b",
                    display_name="Discovered Later",
                    source_type="discovered",
                    hosting_mode="unknown",
                    verification_status="discovered",
                )

                results = search_catalog_agents(conn, limit=50)
                # commerce_verified should appear before discovered
                statuses = [r["verification"]["status"] for r in results["results"]
                           if r["catalog_agent_id"] in ("cagt_special_verified", "cagt_special_discovered")]
                self.assertEqual(statuses, ["commerce_verified", "discovered"])


if __name__ == "__main__":
    unittest.main()

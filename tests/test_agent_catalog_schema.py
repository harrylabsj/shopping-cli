"""Tests for the Agent Catalog schema (migration v10)."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from shopping_cli.db import migrations as migrations_module
from shopping_cli.db.migrations import CURRENT_SCHEMA_VERSION
from shopping_cli.db.session import db_session, now_iso

# ---------------------------------------------------------------------------
# Expected columns per table (name only — exact type/constraint tests below)
# ---------------------------------------------------------------------------

_CATALOG_AGENTS_COLUMNS = {
    "catalog_agent_id",
    "merchant_id",
    "hosted_runtime_agent_id",
    "display_name",
    "provider_name",
    "canonical_domain",
    "agent_type",
    "source_type",
    "lifecycle_status",
    "verification_status",
    "hosting_mode",
    "first_seen_at",
    "last_seen_at",
    "last_verified_at",
    "created_at",
    "updated_at",
}

_AGENT_ENDPOINTS_COLUMNS = {
    "endpoint_id",
    "catalog_agent_id",
    "kind",
    "url",
    "protocol",
    "protocol_version",
    "preference",
    "auth_summary_json",
    "status",
    "last_checked_at",
}

_AGENT_CAPABILITIES_COLUMNS = {
    "catalog_agent_id",
    "namespace",
    "capability_id",
    "version",
    "required",
    "source",
    "schema_url",
    "spec_url",
    "last_verified_at",
}

_AGENT_SKILLS_COLUMNS = {
    "catalog_agent_id",
    "skill_id",
    "name",
    "description",
    "tags_json",
    "input_modes_json",
    "output_modes_json",
}

_AGENT_PROFILE_SNAPSHOTS_COLUMNS = {
    "snapshot_id",
    "catalog_agent_id",
    "profile_type",
    "source_url",
    "etag",
    "last_modified",
    "content_hash",
    "raw_json",
    "fetched_at",
    "fresh_until",
    "validation_status",
}

_AGENT_VERIFICATIONS_COLUMNS = {
    "verification_id",
    "catalog_agent_id",
    "verification_type",
    "result",
    "evidence_json",
    "checked_at",
    "expires_at",
}

_AGENT_TRUST_OBSERVATIONS_COLUMNS = {
    "observation_id",
    "catalog_agent_id",
    "kind",
    "value",
    "source",
    "evidence_ref",
    "observed_at",
    "expires_at",
}

_ALL_TABLES = {
    "catalog_agents": _CATALOG_AGENTS_COLUMNS,
    "agent_endpoints": _AGENT_ENDPOINTS_COLUMNS,
    "agent_capabilities": _AGENT_CAPABILITIES_COLUMNS,
    "agent_skills": _AGENT_SKILLS_COLUMNS,
    "agent_profile_snapshots": _AGENT_PROFILE_SNAPSHOTS_COLUMNS,
    "agent_verifications": _AGENT_VERIFICATIONS_COLUMNS,
    "agent_trust_observations": _AGENT_TRUST_OBSERVATIONS_COLUMNS,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}


def _seed_catalog_agent(conn: sqlite3.Connection, catalog_agent_id: str = "cat-001") -> None:
    """Insert a minimal valid catalog_agents row so FK children can be inserted."""
    ts = now_iso()
    conn.execute(
        """
        insert into catalog_agents(
            catalog_agent_id, display_name, source_type, lifecycle_status,
            verification_status, hosting_mode, first_seen_at, last_seen_at,
            created_at, updated_at
        ) values (?, ?, 'hosted', 'active', 'discovered', 'hosted', ?, ?, ?, ?)
        """,
        (catalog_agent_id, "Test Agent", ts, ts, ts, ts),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class AgentCatalogSchemaTest(unittest.TestCase):
    """Verify that a fresh database initialises all 6 agent catalog tables."""

    def test_fresh_init_creates_all_agent_catalog_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                master = {
                    row["name"]
                    for row in conn.execute(
                        "select name from sqlite_master where type = 'table' order by name"
                    ).fetchall()
                }
            for table in _ALL_TABLES:
                with self.subTest(table=table):
                    self.assertIn(table, master)

    def test_fresh_init_catalog_agents_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                columns = _table_columns(conn, "catalog_agents")
            self.assertEqual(columns, _CATALOG_AGENTS_COLUMNS)

    def test_fresh_init_agent_endpoints_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                columns = _table_columns(conn, "agent_endpoints")
            self.assertEqual(columns, _AGENT_ENDPOINTS_COLUMNS)

    def test_fresh_init_agent_capabilities_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                columns = _table_columns(conn, "agent_capabilities")
            self.assertEqual(columns, _AGENT_CAPABILITIES_COLUMNS)

    def test_fresh_init_agent_skills_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                columns = _table_columns(conn, "agent_skills")
            self.assertEqual(columns, _AGENT_SKILLS_COLUMNS)

    def test_fresh_init_agent_profile_snapshots_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                columns = _table_columns(conn, "agent_profile_snapshots")
            self.assertEqual(columns, _AGENT_PROFILE_SNAPSHOTS_COLUMNS)

    def test_fresh_init_agent_verifications_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                columns = _table_columns(conn, "agent_verifications")
            self.assertEqual(columns, _AGENT_VERIFICATIONS_COLUMNS)

    def test_fresh_init_agent_trust_observations_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                columns = _table_columns(conn, "agent_trust_observations")
            self.assertEqual(columns, _AGENT_TRUST_OBSERVATIONS_COLUMNS)

    def test_fresh_init_sets_user_version_to_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                user_version = conn.execute("pragma user_version").fetchone()[0]
            self.assertEqual(user_version, CURRENT_SCHEMA_VERSION)
            self.assertEqual(CURRENT_SCHEMA_VERSION, 14)


class AgentCatalogCheckConstraintsTest(unittest.TestCase):
    """Verify that CHECK constraints reject invalid values."""

    def test_source_type_rejects_invalid_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass  # initialise
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma foreign_keys = on")
                ts = now_iso()
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into catalog_agents(
                            catalog_agent_id, display_name, source_type,
                            lifecycle_status, verification_status, hosting_mode,
                            first_seen_at, last_seen_at, created_at, updated_at
                        ) values ('cat-bad', 'Bad', 'invalid_source', 'active',
                                  'discovered', 'hosted', ?, ?, ?, ?)
                        """,
                        (ts, ts, ts, ts),
                    )

    def test_hosting_mode_rejects_invalid_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma foreign_keys = on")
                ts = now_iso()
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into catalog_agents(
                            catalog_agent_id, display_name, source_type,
                            lifecycle_status, verification_status, hosting_mode,
                            first_seen_at, last_seen_at, created_at, updated_at
                        ) values ('cat-bad2', 'Bad2', 'hosted', 'active',
                                  'discovered', 'invalid_mode', ?, ?, ?, ?)
                        """,
                        (ts, ts, ts, ts),
                    )

    def test_verification_status_rejects_invalid_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma foreign_keys = on")
                ts = now_iso()
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into catalog_agents(
                            catalog_agent_id, display_name, source_type,
                            lifecycle_status, verification_status, hosting_mode,
                            first_seen_at, last_seen_at, created_at, updated_at
                        ) values ('cat-bad3', 'Bad3', 'hosted', 'active',
                                  'invalid_status', 'hosted', ?, ?, ?, ?)
                        """,
                        (ts, ts, ts, ts),
                    )

    def test_lifecycle_status_rejects_invalid_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma foreign_keys = on")
                ts = now_iso()
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into catalog_agents(
                            catalog_agent_id, display_name, source_type,
                            lifecycle_status, verification_status, hosting_mode,
                            first_seen_at, last_seen_at, created_at, updated_at
                        ) values ('cat-bad4', 'Bad4', 'hosted', 'invalid_lifecycle',
                                  'discovered', 'hosted', ?, ?, ?, ?)
                        """,
                        (ts, ts, ts, ts),
                    )

    def test_endpoint_kind_rejects_invalid_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                _seed_catalog_agent(conn)
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma foreign_keys = on")
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into agent_endpoints(catalog_agent_id, kind)
                        values ('cat-001', 'invalid_kind')
                        """
                    )

    def test_profile_snapshot_type_rejects_invalid_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                _seed_catalog_agent(conn)
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma foreign_keys = on")
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into agent_profile_snapshots(catalog_agent_id, profile_type)
                        values ('cat-001', 'invalid_type')
                        """
                    )

    def test_trust_observation_kind_rejects_invalid_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                _seed_catalog_agent(conn)
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma foreign_keys = on")
                ts = now_iso()
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into agent_trust_observations(
                            catalog_agent_id, kind, value, observed_at
                        ) values ('cat-001', 'invalid_kind', 1.0, ?)
                        """,
                        (ts,),
                    )

    def test_trust_observation_all_valid_kinds_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                _seed_catalog_agent(conn)
            with closing(sqlite3.connect(db_file)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("pragma foreign_keys = on")
                ts = now_iso()
                for idx, kind in enumerate(
                    (
                        "protocol_compliance",
                        "timeout_rate",
                        "schema_error_rate",
                        "successful_exchange",
                        "local_asserted_dispute",
                    )
                ):
                    conn.execute(
                        """
                        insert into agent_trust_observations(
                            catalog_agent_id, kind, value, source, evidence_ref,
                            observed_at, expires_at
                        ) values ('cat-001', ?, ?, 'test-source', 'ev-ref', ?, '')
                        """,
                        (kind, float(idx + 1), ts),
                    )
                rows = conn.execute(
                    "select kind from agent_trust_observations order by observation_id"
                ).fetchall()
            self.assertEqual(
                [row["kind"] for row in rows],
                [
                    "protocol_compliance",
                    "timeout_rate",
                    "schema_error_rate",
                    "successful_exchange",
                    "local_asserted_dispute",
                ],
            )

    def test_all_valid_source_types_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            with closing(sqlite3.connect(db_file)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("pragma foreign_keys = on")
                ts = now_iso()
                for idx, source_type in enumerate(
                    ("hosted", "self_registered", "discovered", "imported", "admin_curated")
                ):
                    conn.execute(
                        """
                        insert into catalog_agents(
                            catalog_agent_id, display_name, source_type,
                            lifecycle_status, verification_status, hosting_mode,
                            first_seen_at, last_seen_at, created_at, updated_at
                        ) values (?, ?, ?, 'active', 'discovered', 'hosted', ?, ?, ?, ?)
                        """,
                        (f"cat-valid-{idx}", f"Valid {source_type}", source_type, ts, ts, ts, ts),
                    )
                rows = conn.execute("select source_type from catalog_agents order by catalog_agent_id").fetchall()
            self.assertEqual(
                [row["source_type"] for row in rows],
                ["hosted", "self_registered", "discovered", "imported", "admin_curated"],
            )

    def test_all_valid_hosting_modes_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            with closing(sqlite3.connect(db_file)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("pragma foreign_keys = on")
                ts = now_iso()
                for idx, mode in enumerate(("direct", "hosted", "hybrid", "unknown")):
                    conn.execute(
                        """
                        insert into catalog_agents(
                            catalog_agent_id, display_name, source_type,
                            lifecycle_status, verification_status, hosting_mode,
                            first_seen_at, last_seen_at, created_at, updated_at
                        ) values (?, ?, 'hosted', 'active', 'discovered', ?, ?, ?, ?, ?)
                        """,
                        (f"cat-mode-{idx}", f"Mode {mode}", mode, ts, ts, ts, ts),
                    )
                rows = conn.execute("select hosting_mode from catalog_agents order by catalog_agent_id").fetchall()
            self.assertEqual(
                [row["hosting_mode"] for row in rows],
                ["direct", "hosted", "hybrid", "unknown"],
            )


class AgentCatalogForeignKeyTest(unittest.TestCase):
    """Verify foreign key constraints are enforced."""

    def test_catalog_agents_merchant_id_fk_rejects_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma foreign_keys = on")
                ts = now_iso()
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into catalog_agents(
                            catalog_agent_id, merchant_id, display_name, source_type,
                            lifecycle_status, verification_status, hosting_mode,
                            first_seen_at, last_seen_at, created_at, updated_at
                        ) values ('cat-fk', 'nonexistent', 'FK Test', 'hosted',
                                  'active', 'discovered', 'hosted', ?, ?, ?, ?)
                        """,
                        (ts, ts, ts, ts),
                    )

    def test_catalog_agents_hosted_runtime_agent_id_fk_rejects_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma foreign_keys = on")
                ts = now_iso()
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into catalog_agents(
                            catalog_agent_id, hosted_runtime_agent_id, display_name,
                            source_type, lifecycle_status, verification_status,
                            hosting_mode, first_seen_at, last_seen_at, created_at, updated_at
                        ) values ('cat-fk2', 'nonexistent-agent', 'FK Test 2', 'hosted',
                                  'active', 'discovered', 'hosted', ?, ?, ?, ?)
                        """,
                        (ts, ts, ts, ts),
                    )

    def test_agent_endpoints_catalog_agent_id_fk_rejects_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma foreign_keys = on")
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        "insert into agent_endpoints(catalog_agent_id, kind) values ('nonexistent', 'a2a')"
                    )

    def test_agent_capabilities_catalog_agent_id_fk_rejects_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma foreign_keys = on")
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into agent_capabilities(catalog_agent_id, namespace, capability_id)
                        values ('nonexistent', 'test', 'test.cap')
                        """
                    )

    def test_agent_skills_catalog_agent_id_fk_rejects_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma foreign_keys = on")
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into agent_skills(catalog_agent_id, skill_id, name)
                        values ('nonexistent', 'skill-1', 'Test Skill')
                        """
                    )

    def test_agent_profile_snapshots_catalog_agent_id_fk_rejects_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma foreign_keys = on")
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into agent_profile_snapshots(catalog_agent_id, profile_type)
                        values ('nonexistent', 'agent_card')
                        """
                    )

    def test_agent_verifications_catalog_agent_id_fk_rejects_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma foreign_keys = on")
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into agent_verifications(catalog_agent_id, verification_type)
                        values ('nonexistent', 'domain_control')
                        """
                    )

    def test_agent_trust_observations_catalog_agent_id_fk_rejects_orphan(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma foreign_keys = on")
                ts = now_iso()
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        insert into agent_trust_observations(
                            catalog_agent_id, kind, value, observed_at
                        ) values ('nonexistent', 'timeout_rate', 0.5, ?)
                        """,
                        (ts,),
                    )


class AgentCatalogMigrationV9ToV10Test(unittest.TestCase):
    """Verify the migration path from schema v9 to v10."""

    def test_v9_to_v10_upgrade_creates_all_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "legacy-v9.sqlite"

            # Build a v9 database via the normal init path then rewind user_version.
            with db_session(db_file):
                pass

            with closing(sqlite3.connect(db_file)) as raw:
                raw.execute("pragma user_version = 9")
                # Drop the v10 tables so they don't exist yet (simulate pre-v10 state).
                for table in _ALL_TABLES:
                    raw.execute(f"drop table if exists {table}")
                raw.commit()

            # Open with db_session — should run only migration 10.
            with db_session(db_file) as conn:
                master = {
                    row["name"]
                    for row in conn.execute(
                        "select name from sqlite_master where type = 'table' order by name"
                    ).fetchall()
                }
                user_version = conn.execute("pragma user_version").fetchone()[0]

            for table in _ALL_TABLES:
                with self.subTest(table=table):
                    self.assertIn(table, master)
            self.assertEqual(user_version, CURRENT_SCHEMA_VERSION)

    def test_v9_to_v10_columns_match_fresh_init(self):
        """All 6 tables created via migration v9→v10 have the same columns as a fresh init."""
        with tempfile.TemporaryDirectory() as tmp:
            # Fresh init (v10 from scratch).
            fresh_file = Path(tmp) / "fresh.sqlite"
            with db_session(fresh_file):
                pass

            # v9 → v10 upgrade path.
            legacy_file = Path(tmp) / "legacy.sqlite"
            with db_session(legacy_file):
                pass
            with closing(sqlite3.connect(legacy_file)) as raw:
                raw.execute("pragma user_version = 9")
                for table in _ALL_TABLES:
                    raw.execute(f"drop table if exists {table}")
                raw.commit()
            with db_session(legacy_file):
                pass

            with db_session(fresh_file) as fresh_conn, db_session(legacy_file) as legacy_conn:
                for table in _ALL_TABLES:
                    with self.subTest(table=table):
                        fresh_cols = _table_columns(fresh_conn, table)
                        legacy_cols = _table_columns(legacy_conn, table)
                        self.assertEqual(fresh_cols, legacy_cols)

    def test_migration_is_idempotent(self):
        """Opening a v10 database runs no migration steps beyond v10."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass

            calls = []
            fake_migration = migrations_module.Migration(
                CURRENT_SCHEMA_VERSION,
                "already_applied",
                lambda _conn: calls.append("ran"),
            )
            with patch.object(migrations_module, "MIGRATIONS", (fake_migration,)):
                with db_session(db_file):
                    pass
            self.assertEqual(calls, [])

    def test_migration_v10_applies_when_user_version_is_9(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass

            with closing(sqlite3.connect(db_file)) as raw:
                raw.execute("pragma user_version = 9")
                for table in _ALL_TABLES:
                    raw.execute(f"drop table if exists {table}")
                raw.commit()

            calls = []
            real_migration = migrations_module.Migration(
                10, "agent_catalog", lambda _conn: calls.append("ran")
            )
            with patch.object(migrations_module, "MIGRATIONS", (real_migration,)):
                with db_session(db_file):
                    pass
            self.assertEqual(calls, ["ran"])


class AgentCatalogMigrationV12ToV13Test(unittest.TestCase):
    """Verify the v12 → v13 migration creates ``agent_trust_observations``."""

    def test_v12_to_v13_upgrade_creates_observation_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "legacy-v12.sqlite"

            # Build a v13 database then rewind to the v12 state: drop only the
            # v13 artifacts (table + index) and set user_version to 12.
            with db_session(db_file):
                pass
            with closing(sqlite3.connect(db_file)) as raw:
                raw.execute("drop index if exists idx_agent_trust_observations_catalog_agent")
                raw.execute("drop table if exists agent_trust_observations")
                raw.execute("pragma user_version = 12")
                raw.commit()

            # Reopen — only migration 13 should run.
            with db_session(db_file) as conn:
                columns = _table_columns(conn, "agent_trust_observations")
                user_version = conn.execute("pragma user_version").fetchone()[0]
                indexes = {
                    row["name"]
                    for row in conn.execute(
                        "select name from sqlite_master where type = 'index'"
                    ).fetchall()
                }

            self.assertEqual(user_version, 14)
            self.assertEqual(columns, _AGENT_TRUST_OBSERVATIONS_COLUMNS)
            self.assertIn("idx_agent_trust_observations_catalog_agent", indexes)

    def test_v12_to_v13_observation_table_matches_fresh_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            fresh_file = Path(tmp) / "fresh.sqlite"
            with db_session(fresh_file):
                pass

            legacy_file = Path(tmp) / "legacy.sqlite"
            with db_session(legacy_file):
                pass
            with closing(sqlite3.connect(legacy_file)) as raw:
                raw.execute("drop index if exists idx_agent_trust_observations_catalog_agent")
                raw.execute("drop table if exists agent_trust_observations")
                raw.execute("pragma user_version = 12")
                raw.commit()
            with db_session(legacy_file):
                pass

            with db_session(fresh_file) as fresh_conn, db_session(legacy_file) as legacy_conn:
                fresh_cols = _table_columns(fresh_conn, "agent_trust_observations")
                legacy_cols = _table_columns(legacy_conn, "agent_trust_observations")
                self.assertEqual(fresh_cols, legacy_cols)


if __name__ == "__main__":
    unittest.main()

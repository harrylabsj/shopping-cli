"""Tests for the private-only §5.7 ``agent_trust_observations`` service.

Covers write/read round-trips, §5.7 kind/value validation, the opaque local
aggregates, and the hard private-only boundary: observation content must never
appear in the public service layer (search / get) or any public API output.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from shopping_cli.agent_catalog.sqlite_repository import (
    TRUST_OBSERVATION_KINDS,
    upsert_catalog_agent,
)
from shopping_cli.core.errors import ValidationError
from shopping_cli.db.session import db_session
from shopping_cli.services.agent_catalog import get_catalog_agent, search_catalog_agents
from shopping_cli.services.agent_trust_observations import (
    list_observations,
    observation_stats,
    record_observation,
)

_MARKER_EVIDENCE = "EVID-LEAK-9f2cbb17"
_MARKER_SOURCE = "SRC-LEAK-9f2cbb17"


def _seed_agent(conn, catalog_agent_id: str = "cagt_obs_001") -> None:
    upsert_catalog_agent(
        conn,
        catalog_agent_id=catalog_agent_id,
        merchant_id="",
        display_name="Observed Agent",
        canonical_domain="observed.example",
        source_type="self_registered",
        verification_status="discovered",
        hosting_mode="direct",
    )


def _collect_strings(obj) -> list[str]:
    """Recursively collect every string key and string value in obj."""
    found: list[str] = []

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


class AgentTrustObservationServiceTest(unittest.TestCase):
    def test_record_and_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                _seed_agent(conn)
                stored = record_observation(
                    conn,
                    catalog_agent_id="cagt_obs_001",
                    kind="timeout_rate",
                    value=0.25,
                    evidence_ref=_MARKER_EVIDENCE,
                )
                observation_id = int(stored["observation_id"])

                rows = list_observations(conn, catalog_agent_id="cagt_obs_001")
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["observation_id"], observation_id)
                self.assertEqual(row["kind"], "timeout_rate")
                self.assertAlmostEqual(float(row["value"]), 0.25)
                self.assertEqual(row["evidence_ref"], _MARKER_EVIDENCE)

    def test_default_source_is_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                _seed_agent(conn)
                record_observation(
                    conn,
                    catalog_agent_id="cagt_obs_001",
                    kind="successful_exchange",
                    value=1.0,
                )
                row = list_observations(conn, catalog_agent_id="cagt_obs_001")[0]
                self.assertEqual(row["source"], "local")

    def test_rejects_unknown_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                _seed_agent(conn)
                with self.assertRaises(ValidationError):
                    record_observation(
                        conn,
                        catalog_agent_id="cagt_obs_001",
                        kind="reputation_score",
                        value=1.0,
                    )

    def test_rejects_non_finite_and_negative_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                _seed_agent(conn)
                for bad_value in (float("nan"), float("inf"), -1.0):
                    with self.subTest(value=bad_value):
                        with self.assertRaises(ValidationError):
                            record_observation(
                                conn,
                                catalog_agent_id="cagt_obs_001",
                                kind="timeout_rate",
                                value=bad_value,
                            )

    def test_list_filters_by_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                _seed_agent(conn)
                for kind in ("protocol_compliance", "timeout_rate"):
                    record_observation(
                        conn,
                        catalog_agent_id="cagt_obs_001",
                        kind=kind,
                        value=1.0,
                    )
                timeout_rows = list_observations(conn, kind="timeout_rate")
                self.assertEqual(len(timeout_rows), 1)
                self.assertEqual(timeout_rows[0]["kind"], "timeout_rate")

    def test_observation_stats_returns_opaque_counts_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                _seed_agent(conn)
                record_observation(
                    conn,
                    catalog_agent_id="cagt_obs_001",
                    kind="local_asserted_dispute",
                    value=1.0,
                    evidence_ref=_MARKER_EVIDENCE,
                    source=_MARKER_SOURCE,
                )
                record_observation(
                    conn,
                    catalog_agent_id="cagt_obs_001",
                    kind="timeout_rate",
                    value=0.1,
                    evidence_ref=_MARKER_EVIDENCE,
                    source=_MARKER_SOURCE,
                )

                stats = observation_stats(conn)
                self.assertEqual(stats["total"], 2)
                self.assertEqual(stats["by_kind"], {
                    "local_asserted_dispute": 1,
                    "timeout_rate": 1,
                })
                # The opaque aggregate must not leak observation content.
                serialized = str(stats)
                self.assertNotIn(_MARKER_EVIDENCE, serialized)
                self.assertNotIn(_MARKER_SOURCE, serialized)

    def test_kind_set_matches_design(self):
        self.assertEqual(
            TRUST_OBSERVATION_KINDS,
            {
                "protocol_compliance",
                "timeout_rate",
                "schema_error_rate",
                "successful_exchange",
                "local_asserted_dispute",
            },
        )


class AgentTrustObservationPrivacyBoundaryTest(unittest.TestCase):
    """Hard private-only boundary at the public service layer (§3.4, §5.7)."""

    def _seed_public_agents(self, conn) -> None:
        upsert_catalog_agent(
            conn,
            catalog_agent_id="cagt_obs_a",
            merchant_id="",
            display_name="Alpha",
            canonical_domain="alpha.example",
            source_type="self_registered",
            verification_status="commerce_verified",
            hosting_mode="direct",
        )
        upsert_catalog_agent(
            conn,
            catalog_agent_id="cagt_obs_b",
            merchant_id="",
            display_name="Beta",
            canonical_domain="beta.example",
            source_type="self_registered",
            verification_status="domain_verified",
            hosting_mode="hosted",
        )
        # Insert observations that MUST NOT leak into public output.
        record_observation(
            conn,
            catalog_agent_id="cagt_obs_a",
            kind="local_asserted_dispute",
            value=3.0,
            source=_MARKER_SOURCE,
            evidence_ref=_MARKER_EVIDENCE,
        )
        record_observation(
            conn,
            catalog_agent_id="cagt_obs_b",
            kind="schema_error_rate",
            value=0.5,
            source=_MARKER_SOURCE,
            evidence_ref=_MARKER_EVIDENCE,
        )

    def test_public_search_response_has_no_observation_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                self._seed_public_agents(conn)
                result = search_catalog_agents(conn, q="", limit=20)

            strings = _collect_strings(result)
            self.assertNotIn(_MARKER_EVIDENCE, strings)
            self.assertNotIn(_MARKER_SOURCE, strings)
            keys = {str(k) for s in strings for k in [s]}
            self.assertNotIn("evidence_ref", keys)

    def test_public_get_response_has_no_observation_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                self._seed_public_agents(conn)
                detail = get_catalog_agent(conn, "cagt_obs_a")

            strings = _collect_strings(detail)
            self.assertNotIn(_MARKER_EVIDENCE, strings)
            self.assertNotIn(_MARKER_SOURCE, strings)


if __name__ == "__main__":
    unittest.main()

"""Tests for the W3 verification pipeline — state machine, service, queue.

Design: docs/shopping-cli-a2a-upgrade-design-v1.2.1.md §6, §6.1, §6.2, §23, §25 Phase 2

All network I/O is mocked: the service's ``ProfileFetcher`` is replaced by a
``FakeFetcher`` and the bounded queue is exercised with injected fake
services, so no test ever touches the wire.  The DB is real SQLite (in-memory
for service tests, temp-file for queue tests because the queue opens a fresh
connection per task).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shopping_cli.agent_catalog.sqlite_repository import (
    latest_profile_snapshot,
    list_profile_snapshots,
    list_verifications,
    require_catalog_agent,
    set_verification_status,
)
from shopping_cli.db.session import init_db, now_iso
from shopping_cli.discovery.fetcher import FetchError, FetchResult, ProfileFetcher, SSRFBlockError
from shopping_cli.discovery.trust import TrustPolicy
from shopping_cli.discovery.verifier import (
    AGENT_VERIFIED,
    COMMERCE_VERIFIED,
    DISCOVERED,
    DOMAIN_VERIFIED,
    PROFILE_VALID,
    REJECTED,
    STALE,
    SUSPENDED,
    UNREACHABLE,
    InvalidStateTransitionError,
    VerificationStateMachine,
)
from shopping_cli.services.agent_verification import (
    VerificationQueue,
    VerificationQueueConfig,
    VerificationQueueFullError,
    VerificationQueueShutdownError,
    VerificationService,
    VerificationTaskResult,
    make_verification_worker,
)

DOMAIN = "merchant.example"
CARD_URL = f"https://{DOMAIN}/agent-card.json"
UCP_URL = f"https://{DOMAIN}/ucp"
WELl_KNOWN_CARD = f"https://{DOMAIN}/.well-known/agent-card.json"
WELL_KNOWN_UCP = f"https://{DOMAIN}/.well-known/ucp"


# ── Fixtures ───────────────────────────────────────────────────────────────


def _valid_card() -> dict:
    return {
        "name": "Acme Merchant Agent",
        "url": f"https://{DOMAIN}/agent",
        "version": "1.0.0",
        "documentationUrl": f"https://{DOMAIN}/docs",
        "provider": {"organization": "Acme", "url": f"https://{DOMAIN}"},
        "skills": [
            {
                "id": "industrial-displays",
                "name": "Industrial Displays",
                "description": "Data only, never an instruction.",
                "tags": ["displays"],
                "examples": ["quote a 21 inch panel"],
                "inputModes": ["text"],
                "outputModes": ["text"],
            }
        ],
        "capabilities": {"streaming": True, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
    }


def _valid_ucp() -> dict:
    return {
        "specificationVersion": "2026-04-08",
        "implementationVersion": "1.0.0",
        "serviceIdentity": {"id": "urn:acme:agent", "name": "Acme Agent"},
        "services": [
            {
                "id": "urn:acme:service:shopping",
                "type": "urn:acme:type:shopping",
                "capabilities": ["urn:acme:capability:shopping.negotiation", "shopping"],
                "endpoints": [
                    {"uri": f"https://{DOMAIN}/a2a", "protocol": "a2a", "version": "1.0.0"}
                ],
                "specifications": [{"id": "urn:acme:spec:knp", "label": "KNP", "version": "1.0"}],
            }
        ],
        "specifications": [
            {
                "id": "urn:acme:spec:knp",
                "label": "KNP",
                "version": "1.0",
                "openAPIDocument": f"https://{DOMAIN}/knp.yaml",
            }
        ],
    }


class FakeFetcher:
    """SSRF-free stand-in for ProfileFetcher (all responses are local dicts)."""

    def __init__(self, *, card: dict | None = None, ucp: dict | None = None) -> None:
        self.card = card if card is not None else _valid_card()
        self.ucp = ucp if ucp is not None else _valid_ucp()
        self.card_status = 200
        self.ucp_status = 200
        self.well_known_status = 200
        self.profile_errors: dict[str, Exception] = {}
        self.calls: list[tuple[str, str | None, str | None]] = []
        self._seq = 0

    def fetch(self, url: str, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        self.calls.append((url, etag, last_modified))
        self._seq += 1
        fetched_at = 1000.0 + self._seq
        if url in self.profile_errors:
            raise self.profile_errors[url]
        if url == CARD_URL:
            if self.card_status != 200:
                return FetchResult(url=url, status_code=self.card_status, fetched_at=fetched_at)
            return FetchResult(
                url=url, status_code=200, body=json.dumps(self.card), parsed=self.card,
                etag=f"card-etag-{self._seq}", last_modified="Mon, 01 Jan 2026 00:00:00 GMT",
                fetched_at=fetched_at,
            )
        if url == UCP_URL:
            if self.ucp_status != 200:
                return FetchResult(url=url, status_code=self.ucp_status, fetched_at=fetched_at)
            return FetchResult(
                url=url, status_code=200, body=json.dumps(self.ucp), parsed=self.ucp,
                etag=f"ucp-etag-{self._seq}", last_modified="Mon, 01 Jan 2026 00:00:00 GMT",
                fetched_at=fetched_at,
            )
        if url in (WELl_KNOWN_CARD, WELL_KNOWN_UCP):
            return FetchResult(url=url, status_code=self.well_known_status, parsed={}, fetched_at=fetched_at)
        raise AssertionError(f"unexpected fetch: {url}")


# ── DB helpers ─────────────────────────────────────────────────────────────


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    init_db(conn)
    return conn


def _seed_agent(
    conn: sqlite3.Connection,
    catalog_agent_id: str = "cagt-1",
    *,
    source_type: str = "self_registered",
    hosting_mode: str = "direct",
    hosted_runtime_agent_id: str = "",
    verification_status: str = DISCOVERED,
) -> None:
    ts = now_iso()
    runtime = hosted_runtime_agent_id or None
    conn.execute(
        """
        insert into catalog_agents(
            catalog_agent_id, display_name, provider_name, canonical_domain, agent_type,
            source_type, lifecycle_status, verification_status, hosting_mode,
            first_seen_at, last_seen_at, created_at, updated_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            catalog_agent_id,
            "Acme Agent",
            "Acme",
            DOMAIN,
            "merchant",
            source_type,
            "active",
            verification_status,
            hosting_mode,
            ts,
            ts,
            ts,
            ts,
        ),
    )
    if runtime:
        conn.execute(
            "insert or ignore into agents(id, type, owner_id, status, last_seen_at) values (?, ?, ?, ?, ?)",
            (runtime, "hosted", "merchant-acme", "online", ts),
        )
        conn.execute(
            "update catalog_agents set hosted_runtime_agent_id = ? where catalog_agent_id = ?",
            (runtime, catalog_agent_id),
        )
    conn.commit()


def _seed_endpoints(conn: sqlite3.Connection, catalog_agent_id: str = "cagt-1") -> None:
    ts = now_iso()
    for kind, url in (("agent_card", CARD_URL), ("ucp_profile", UCP_URL)):
        conn.execute(
            """
            insert into agent_endpoints(
                catalog_agent_id, kind, url, protocol, protocol_version, preference,
                auth_summary_json, status, last_checked_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (catalog_agent_id, kind, url, "a2a", "1.0.0", 1, "{}", "active", ts),
        )
    conn.commit()


def _make_service(conn: sqlite3.Connection, *, fetcher: FakeFetcher, policy: TrustPolicy | None = None, **kwargs) -> VerificationService:
    return VerificationService(
        conn,
        fetcher=fetcher,
        policy=policy or TrustPolicy.defaults(),
        now=lambda: 1000.0,
        **kwargs,
    )


def _audit_events(conn: sqlite3.Connection) -> list[tuple[str, dict]]:
    rows = conn.execute("select event, details_json from audit_events order by id").fetchall()
    return [(row["event"], json.loads(row["details_json"])) for row in rows]


def _verification_rows(conn: sqlite3.Connection, catalog_agent_id: str = "cagt-1") -> list[dict]:
    return list_verifications(conn, catalog_agent_id)


# ── State machine (pure, verifier.py) ──────────────────────────────────────


class StateMachineTest(unittest.TestCase):
    def test_full_promotion_is_one_rung_at_a_time(self) -> None:
        sm = VerificationStateMachine()
        rungs = [DISCOVERED, PROFILE_VALID, DOMAIN_VERIFIED, AGENT_VERIFIED, COMMERCE_VERIFIED]
        for i in range(len(rungs) - 1):
            with self.subTest(step=f"{rungs[i]} -> {rungs[i + 1]}"):
                self.assertTrue(sm.can_transition(rungs[i], rungs[i + 1]))
                self.assertEqual(sm.transition(rungs[i], rungs[i + 1]), rungs[i + 1])

    def test_illegal_jumps_are_rejected(self) -> None:
        sm = VerificationStateMachine()
        illegal = [
            (DISCOVERED, COMMERCE_VERIFIED),
            (DISCOVERED, AGENT_VERIFIED),
            (DISCOVERED, DOMAIN_VERIFIED),
            (PROFILE_VALID, AGENT_VERIFIED),
            (PROFILE_VALID, COMMERCE_VERIFIED),
            (DOMAIN_VERIFIED, COMMERCE_VERIFIED),
        ]
        for current, target in illegal:
            with self.subTest(step=f"{current} -> {target}"):
                self.assertFalse(sm.can_transition(current, target))
                with self.assertRaises(InvalidStateTransitionError):
                    sm.transition(current, target)

    def test_terminal_states_have_no_outgoing_transitions(self) -> None:
        # v3.0 P2 (moderation): SUSPENDED gained exactly one explicit exit —
        # operator reinstate resets to DISCOVERED.  Everything else is still
        # a closed terminal state.  REJECTED remains fully terminal.
        sm = VerificationStateMachine()
        for terminal in (REJECTED, SUSPENDED):
            for target in (
                PROFILE_VALID,
                DOMAIN_VERIFIED,
                AGENT_VERIFIED,
                COMMERCE_VERIFIED,
                STALE,
                UNREACHABLE,
            ):
                with self.subTest(step=f"{terminal} -> {target}"):
                    self.assertFalse(sm.can_transition(terminal, target))
                    with self.assertRaises(InvalidStateTransitionError):
                        sm.transition(terminal, target)
        self.assertTrue(sm.can_transition(SUSPENDED, DISCOVERED))
        with self.assertRaises(InvalidStateTransitionError):
            sm.transition(REJECTED, DISCOVERED)

    def test_stale_and_unreachable_can_reenter_the_ladder(self) -> None:
        sm = VerificationStateMachine()
        for entry in (STALE, UNREACHABLE):
            for rung in (PROFILE_VALID, DOMAIN_VERIFIED, AGENT_VERIFIED, COMMERCE_VERIFIED):
                with self.subTest(step=f"{entry} -> {rung}"):
                    self.assertTrue(sm.can_transition(entry, rung))


# ── Service: full promotion ────────────────────────────────────────────────


class FullPromotionTest(unittest.TestCase):
    def _setup(self) -> tuple[sqlite3.Connection, FakeFetcher, VerificationService]:
        conn = _make_conn()
        _seed_agent(conn)
        _seed_endpoints(conn)
        fetcher = FakeFetcher()
        service = _make_service(conn, fetcher=fetcher)
        return conn, fetcher, service

    def test_full_promotion_to_commerce_verified(self) -> None:
        conn, fetcher, service = self._setup()
        result = service.verify("cagt-1")

        self.assertEqual(result.previous_status, DISCOVERED)
        self.assertEqual(result.status, COMMERCE_VERIFIED)
        self.assertEqual(
            [stage.stage for stage in result.stages],
            ["profile", "domain_control", "agent_identity", "commerce_capability"],
        )
        # The catalog_agents row now reflects the final status.
        self.assertEqual(require_catalog_agent(conn, "cagt-1")["verification_status"], COMMERCE_VERIFIED)

    def test_snapshots_persisted_with_cache_metadata(self) -> None:
        conn, _, service = self._setup()
        service.verify("cagt-1")

        for kind in ("agent_card", "ucp"):
            with self.subTest(kind=kind):
                snapshot = latest_profile_snapshot(conn, "cagt-1", kind)
                self.assertIsNotNone(snapshot)
                assert snapshot is not None
                self.assertEqual(snapshot["validation_status"], "valid")
                self.assertEqual(snapshot["profile_type"], kind)
                self.assertTrue(snapshot["etag"])
                self.assertTrue(snapshot["content_hash"])
                self.assertTrue(snapshot["fetched_at"])
                self.assertTrue(snapshot["fresh_until"])
                self.assertTrue(snapshot["source_url"])
                # raw_json is the public projection, parseable and non-secret.
                raw = json.loads(snapshot["raw_json"])
                self.assertIsInstance(raw, dict)

    def test_verifications_pin_trust_policy_version(self) -> None:
        policy = TrustPolicy.from_config(policy_version=7)
        conn = _make_conn()
        _seed_agent(conn)
        _seed_endpoints(conn)
        fetcher = FakeFetcher()
        service = _make_service(conn, fetcher=fetcher, policy=policy)
        service.verify("cagt-1")

        rows = _verification_rows(conn)
        self.assertEqual(
            {row["verification_type"] for row in rows},
            {"domain_control", "agent_identity", "commerce_capability"},
        )
        for row in rows:
            evidence = json.loads(row["evidence_json"])
            with self.subTest(vtype=row["verification_type"]):
                self.assertEqual(evidence["trust_policy_version"], 7)
                self.assertEqual(row["result"], "passed")

    def test_audit_events_written_without_secrets(self) -> None:
        conn, _, service = self._setup()
        service.verify("cagt-1")
        events = _audit_events(conn)
        names = [event for event, _ in events]
        self.assertIn("catalog_agent_refreshed", names)
        self.assertIn("catalog_agent_verified", names)
        for event, details in events:
            with self.subTest(event=event):
                self.assertEqual(details["schema_version"], 1)
                self.assertEqual(details["event_type"], event)
                self.assertNotIn("raw_json", details)
                self.assertNotIn("token", json.dumps(details).lower())
                self.assertNotIn("secret", json.dumps(details).lower())

    def test_fresh_agent_returns_unchanged(self) -> None:
        conn, _, service = self._setup()
        service.verify("cagt-1")
        second = service.verify("cagt-1")
        self.assertEqual(second.status, COMMERCE_VERIFIED)
        self.assertEqual(second.previous_status, COMMERCE_VERIFIED)
        self.assertEqual(second.stages, ())

    def test_refresh_forces_full_reverification(self) -> None:
        conn, _, service = self._setup()
        service.verify("cagt-1")
        before = len(list_profile_snapshots(conn, "cagt-1"))
        result = service.refresh("cagt-1")
        after = len(list_profile_snapshots(conn, "cagt-1"))
        self.assertEqual(result.status, COMMERCE_VERIFIED)
        self.assertEqual(after, before + 2)  # both profiles re-fetched

    def test_unknown_agent_raises_not_found(self) -> None:
        conn = _make_conn()
        fetcher = FakeFetcher()
        service = _make_service(conn, fetcher=fetcher)
        with self.assertRaises(Exception):
            service.verify("does-not-exist")


# ── Service: failure states ────────────────────────────────────────────────


class FailureStatesTest(unittest.TestCase):
    def _service(self, *, card: dict | None = None, ucp: dict | None = None) -> tuple[sqlite3.Connection, FakeFetcher, VerificationService]:
        conn = _make_conn()
        _seed_agent(conn)
        _seed_endpoints(conn)
        fetcher = FakeFetcher(card=card, ucp=ucp)
        service = _make_service(conn, fetcher=fetcher)
        return conn, fetcher, service

    def test_invalid_profile_schema_is_rejected(self) -> None:
        card = _valid_card()
        card["version"] = "9.9.9"  # not in allowed_a2a_versions
        conn, _, service = self._service(card=card)
        result = service.verify("cagt-1")
        self.assertEqual(result.status, REJECTED)
        self.assertEqual(result.stages[0].stage, "profile")
        self.assertEqual(result.stages[0].outcome, "rejected")
        # No snapshots are persisted for an invalid profile.
        self.assertEqual(list_profile_snapshots(conn, "cagt-1"), [])
        names = [event for event, _ in _audit_events(conn)]
        self.assertIn("catalog_agent_verification_failed", names)

    def test_profile_with_secret_like_field_is_rejected_and_not_persisted(self) -> None:
        card = _valid_card()
        card["access_token"] = "sk-super-secret-value-1234567890"
        conn, _, service = self._service(card=card)
        result = service.verify("cagt-1")
        self.assertEqual(result.status, REJECTED)
        self.assertEqual(list_profile_snapshots(conn, "cagt-1"), [])
        # The audit trail never embeds the secret value.
        for event, details in _audit_events(conn):
            with self.subTest(event=event):
                self.assertNotIn("sk-super-secret-value", json.dumps(details))

    def test_unreachable_when_fetch_fails_without_snapshot(self) -> None:
        conn, fetcher, service = self._service()
        fetcher.profile_errors[CARD_URL] = FetchError("boom")
        result = service.verify("cagt-1")
        self.assertEqual(result.status, UNREACHABLE)
        self.assertEqual(require_catalog_agent(conn, "cagt-1")["verification_status"], UNREACHABLE)

    def test_unreachable_when_http_error(self) -> None:
        conn, fetcher, service = self._service()
        fetcher.card_status = 500
        result = service.verify("cagt-1")
        self.assertEqual(result.status, UNREACHABLE)

    def test_ssrf_block_is_rejected(self) -> None:
        conn, fetcher, service = self._service()
        fetcher.profile_errors[CARD_URL] = SSRFBlockError("blocked")
        result = service.verify("cagt-1")
        self.assertEqual(result.status, REJECTED)

    def test_stale_when_fetch_fails_with_existing_snapshot(self) -> None:
        conn, fetcher, service = self._service()
        service.verify("cagt-1")  # seed a snapshot
        fetcher.profile_errors[CARD_URL] = FetchError("origin down")
        result = service.verify("cagt-1", force=True)
        self.assertEqual(result.status, STALE)
        names = [event for event, _ in _audit_events(conn)]
        self.assertIn("catalog_agent_stale", names)

    def test_domain_control_failure_is_rejected_with_evidence(self) -> None:
        conn, fetcher, service = self._service()
        fetcher.well_known_status = 404
        result = service.verify("cagt-1")
        self.assertEqual(result.status, REJECTED)
        domain_rows = [r for r in _verification_rows(conn) if r["verification_type"] == "domain_control"]
        self.assertEqual(len(domain_rows), 1)
        evidence = json.loads(domain_rows[0]["evidence_json"])
        self.assertEqual(evidence["result"], "failed")
        self.assertIn("trust_policy_version", evidence)
        names = [event for event, _ in _audit_events(conn)]
        self.assertIn("catalog_agent_verification_failed", names)

    def test_identity_binding_failure_is_rejected(self) -> None:
        card = _valid_card()
        card["url"] = f"http://{DOMAIN}/agent"  # same domain but not HTTPS
        conn, _, service = self._service(card=card)
        result = service.verify("cagt-1")
        self.assertEqual(result.status, REJECTED)
        self.assertEqual(result.stages[2].stage, "agent_identity")
        self.assertEqual(result.stages[2].outcome, "rejected")

    def test_commerce_capability_failure_is_rejected(self) -> None:
        ucp = _valid_ucp()
        ucp["services"][0]["capabilities"] = []  # no commerce capabilities
        conn, _, service = self._service(ucp=ucp)
        result = service.verify("cagt-1")
        self.assertEqual(result.status, REJECTED)
        self.assertEqual(result.stages[3].stage, "commerce_capability")
        self.assertEqual(result.stages[3].outcome, "rejected")


# ── Service: §5.1 publish-state invariants ─────────────────────────────────


class PublishInvariantTest(unittest.TestCase):
    def test_hosted_without_runtime_id_cannot_be_commerce_verified(self) -> None:
        conn = _make_conn()
        _seed_agent(conn, source_type="hosted", hosting_mode="hosted", hosted_runtime_agent_id="")
        _seed_endpoints(conn)
        fetcher = FakeFetcher()
        service = _make_service(conn, fetcher=fetcher)
        result = service.verify("cagt-1")
        self.assertEqual(result.status, REJECTED)
        self.assertIn("5.1", result.stages[-1].reason)

    def test_non_hosted_with_runtime_id_cannot_be_commerce_verified(self) -> None:
        conn = _make_conn()
        _seed_agent(conn, source_type="self_registered", hosting_mode="direct", hosted_runtime_agent_id="agent-1")
        _seed_endpoints(conn)
        fetcher = FakeFetcher()
        service = _make_service(conn, fetcher=fetcher)
        result = service.verify("cagt-1")
        self.assertEqual(result.status, REJECTED)
        self.assertIn("5.1", result.stages[-1].reason)

    def test_hosted_with_runtime_id_publishes(self) -> None:
        conn = _make_conn()
        _seed_agent(conn, source_type="hosted", hosting_mode="hosted", hosted_runtime_agent_id="agent-1")
        _seed_endpoints(conn)
        fetcher = FakeFetcher()
        service = _make_service(conn, fetcher=fetcher)
        result = service.verify("cagt-1")
        self.assertEqual(result.status, COMMERCE_VERIFIED)


# ── Service: state transitions ─────────────────────────────────────────────


class StateTransitionsTest(unittest.TestCase):
    def test_illegal_jump_via_granular_method_raises(self) -> None:
        conn = _make_conn()
        _seed_agent(conn)  # still DISCOVERED
        _seed_endpoints(conn)
        fetcher = FakeFetcher()
        service = _make_service(conn, fetcher=fetcher)
        with self.assertRaises(InvalidStateTransitionError):
            service.verify_commerce("cagt-1")  # DISCOVERED -> COMMERCE_VERIFIED is illegal

    def test_granular_method_advances_one_rung(self) -> None:
        conn = _make_conn()
        _seed_agent(conn)
        _seed_endpoints(conn)
        fetcher = FakeFetcher()
        service = _make_service(conn, fetcher=fetcher)
        result = service.verify_profile("cagt-1")
        self.assertEqual(result.status, PROFILE_VALID)
        self.assertEqual(require_catalog_agent(conn, "cagt-1")["verification_status"], PROFILE_VALID)

    def test_terminal_agent_cannot_be_reverified(self) -> None:
        conn, _, service = FailureStatesTest()._service()
        # Build a rejected agent.
        card = _valid_card()
        card["version"] = "9.9.9"
        fetcher = FakeFetcher(card=card)
        service = _make_service(conn, fetcher=fetcher)
        service.verify("cagt-1")
        self.assertEqual(require_catalog_agent(conn, "cagt-1")["verification_status"], REJECTED)
        with self.assertRaises(InvalidStateTransitionError):
            service.verify("cagt-1")

    def test_mark_stale_demotes_and_audits(self) -> None:
        conn = _make_conn()
        _seed_agent(conn)
        _seed_endpoints(conn)
        fetcher = FakeFetcher()
        service = _make_service(conn, fetcher=fetcher)
        result = service.mark_stale("cagt-1")
        self.assertEqual(result.status, STALE)
        names = [event for event, _ in _audit_events(conn)]
        self.assertIn("catalog_agent_stale", names)

    def test_suspend_is_terminal(self) -> None:
        conn = _make_conn()
        _seed_agent(conn)
        _seed_endpoints(conn)
        fetcher = FakeFetcher()
        service = _make_service(conn, fetcher=fetcher)
        result = service.suspend("cagt-1")
        self.assertEqual(result.status, SUSPENDED)
        names = [event for event, _ in _audit_events(conn)]
        self.assertIn("catalog_agent_suspended", names)
        with self.assertRaises(InvalidStateTransitionError):
            service.verify("cagt-1")

    def test_suspend_is_idempotent_and_records_reason(self) -> None:
        conn = _make_conn()
        _seed_agent(conn)
        service = _make_service(conn, fetcher=FakeFetcher())
        first = service.suspend("cagt-1", actor="admin", reason="spam agent")
        second = service.suspend("cagt-1", actor="admin", reason="spam agent")
        self.assertEqual(first.status, SUSPENDED)
        self.assertEqual(second.status, SUSPENDED)
        events = [e for e, _ in _audit_events(conn) if e == "catalog_agent_suspended"]
        # Idempotent second call writes no extra audit event.
        self.assertEqual(len(events), 1)
        details = [d for _, d in _audit_events(conn) if d.get("reason")]
        self.assertIn("spam agent", [d["reason"] for d in details])


class ReinstateTest(unittest.TestCase):
    """v3.0 moderation / P2: the SUSPENDED → DISCOVERED operator path."""

    def _setup(self) -> tuple[sqlite3.Connection, VerificationService]:
        conn = _make_conn()
        _seed_agent(conn)
        service = _make_service(conn, fetcher=FakeFetcher())
        self.assertEqual(service.suspend("cagt-1").status, SUSPENDED)
        return conn, service

    def test_reinstate_resets_suspended_to_discovered(self) -> None:
        conn, service = self._setup()
        result = service.reinstate("cagt-1", actor="admin", reason="false positive")
        self.assertEqual(result.previous_status, SUSPENDED)
        self.assertEqual(result.status, DISCOVERED)
        self.assertEqual(require_catalog_agent(conn, "cagt-1")["verification_status"], DISCOVERED)

    def test_reinstate_clears_last_verified_at(self) -> None:
        conn = _make_conn()
        _seed_agent(conn)
        set_verification_status(conn, "cagt-1", COMMERCE_VERIFIED, last_verified_at="2026-08-01T00:00:00+00:00")
        service = _make_service(conn, fetcher=FakeFetcher())
        service.suspend("cagt-1")
        service.reinstate("cagt-1")
        self.assertEqual(require_catalog_agent(conn, "cagt-1")["last_verified_at"], "")

    def test_reinstate_requires_suspended_fail_closed(self) -> None:
        conn = _make_conn()
        _seed_agent(conn, verification_status=COMMERCE_VERIFIED)
        service = _make_service(conn, fetcher=FakeFetcher())
        with self.assertRaises(InvalidStateTransitionError):
            service.reinstate("cagt-1")
        # State untouched.
        self.assertEqual(require_catalog_agent(conn, "cagt-1")["verification_status"], COMMERCE_VERIFIED)

    def test_reinstate_rejects_non_suspended_even_at_discovered(self) -> None:
        # DISCOVERED → DISCOVERED is a legal state-machine self-transition
        # (re-registration entry), so reinstate must gate on status explicitly.
        conn = _make_conn()
        _seed_agent(conn, verification_status=DISCOVERED)
        service = _make_service(conn, fetcher=FakeFetcher())
        with self.assertRaises(InvalidStateTransitionError):
            service.reinstate("cagt-1")
        self.assertEqual(require_catalog_agent(conn, "cagt-1")["verification_status"], DISCOVERED)

    def test_reinstate_records_audit_with_previous_status(self) -> None:
        conn, service = self._setup()
        service.reinstate("cagt-1", actor="admin", reason="false positive")
        audit = [d for e, d in _audit_events(conn) if e == "catalog_agent_reinstated"]
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["previous_status"], SUSPENDED)
        self.assertEqual(audit[0]["reason"], "false positive")

    def test_suspended_agent_cannot_be_promoted_after_reinstate_without_verify(self) -> None:
        conn, service = self._setup()
        service.reinstate("cagt-1")
        # Reinstate only resets to DISCOVERED — the pre-suspension status is
        # not restored automatically (v3.0 P2 decision).
        self.assertEqual(require_catalog_agent(conn, "cagt-1")["verification_status"], DISCOVERED)


# ── Bounded in-process verification queue (§25 Phase 2) ────────────────────


class _BlockingService:
    """A fake VerificationService that blocks until released."""

    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self._started = started
        self._release = release
        self.close_called = False

    def verify(self, catalog_agent_id: str, *, actor: str = "verification_worker") -> object:
        self._started.set()
        self._release.wait(timeout=15)
        return SimpleNamespace(status=COMMERCE_VERIFIED)

    def close(self) -> None:
        self.close_called = True


class VerificationQueueTest(unittest.TestCase):
    def _queue(
        self,
        config: VerificationQueueConfig | None = None,
        factory=None,
    ) -> VerificationQueue:
        return VerificationQueue(service_factory=factory, config=config, now=lambda: 1000.0)

    def test_enqueue_wait_completes(self) -> None:
        calls: list[str] = []

        def factory():
            def verify(catalog_agent_id, *, actor="verification_worker"):
                calls.append(catalog_agent_id)
                return SimpleNamespace(status=COMMERCE_VERIFIED)

            return SimpleNamespace(verify=verify, close=lambda: None)

        queue = self._queue(factory=factory)
        try:
            result = queue.enqueue("cagt-1", wait=True, timeout=5)
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.verification_status, COMMERCE_VERIFIED)
            self.assertEqual(calls, ["cagt-1"])
        finally:
            queue.shutdown()

    def test_enqueue_without_wait_returns_enqueued(self) -> None:
        def factory():
            def verify(catalog_agent_id, *, actor="verification_worker"):
                return SimpleNamespace(status=COMMERCE_VERIFIED)

            return SimpleNamespace(verify=verify, close=lambda: None)

        queue = self._queue(factory=factory)
        try:
            result = queue.enqueue("cagt-1", wait=False)
            self.assertEqual(result.status, "enqueued")
            self.assertTrue(result.task_id)
        finally:
            queue.shutdown()

    def test_service_failure_is_reported_not_raised(self) -> None:
        def factory():
            def verify(catalog_agent_id, *, actor="verification_worker"):
                raise RuntimeError("internal boom")

            return SimpleNamespace(verify=verify, close=lambda: None)

        queue = self._queue(factory=factory)
        try:
            result = queue.enqueue("cagt-1", wait=True, timeout=5)
            self.assertEqual(result.status, "failed")
            self.assertIn("internal boom", result.error)
        finally:
            queue.shutdown()

    def test_worker_commits_after_success_before_close(self) -> None:
        calls: list[str] = []

        class RecordingService:
            def verify(self, catalog_agent_id, *, actor="verification_worker"):
                calls.append("verify")
                return SimpleNamespace(status=COMMERCE_VERIFIED)

            def commit(self):
                calls.append("commit")

            def close(self):
                calls.append("close")

        queue = self._queue(factory=lambda: RecordingService())
        try:
            result = queue.enqueue("cagt-1", wait=True, timeout=5)
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.verification_status, COMMERCE_VERIFIED)
            self.assertEqual(calls, ["verify", "commit", "close"])
        finally:
            queue.shutdown()

    def test_commit_failure_is_reported_not_raised(self) -> None:
        def factory():
            def verify(catalog_agent_id, *, actor="verification_worker"):
                return SimpleNamespace(status=COMMERCE_VERIFIED)

            def commit():
                raise RuntimeError("disk full")

            return SimpleNamespace(verify=verify, commit=commit, close=lambda: None)

        queue = self._queue(factory=factory)
        try:
            result = queue.enqueue("cagt-1", wait=True, timeout=5)
            self.assertEqual(result.status, "failed")
            self.assertIn("disk full", result.error)
            self.assertEqual(result.verification_status, "")
        finally:
            queue.shutdown()

    def test_queue_is_bounded(self) -> None:
        started = threading.Event()
        release = threading.Event()
        config = VerificationQueueConfig(max_pending=1, concurrency=1, task_timeout_seconds=10)
        queue = self._queue(config=config, factory=lambda: _BlockingService(started, release))
        try:
            queue.enqueue("cagt-a", wait=False)  # worker picks it up and blocks
            self.assertTrue(started.wait(3))
            queue.enqueue("cagt-b", wait=False)  # fills the single pending slot
            with self.assertRaises(VerificationQueueFullError):
                queue.enqueue("cagt-c", wait=False)  # queue is full → fail-closed
        finally:
            release.set()
            queue.shutdown()

    def test_concurrency_budget_is_respected(self) -> None:
        active: list[int] = [0]
        max_active: list[int] = [0]
        started_count: list[int] = [0]
        lock = threading.Lock()
        release = threading.Event()
        config = VerificationQueueConfig(max_pending=16, concurrency=2, task_timeout_seconds=10)

        def factory():
            def verify(catalog_agent_id, *, actor="verification_worker"):
                with lock:
                    active[0] += 1
                    max_active[0] = max(max_active[0], active[0])
                    started_count[0] += 1
                try:
                    release.wait(timeout=15)
                finally:
                    with lock:
                        active[0] -= 1
                return SimpleNamespace(status=COMMERCE_VERIFIED)

            return SimpleNamespace(verify=verify, close=lambda: None)

        queue = self._queue(config=config, factory=factory)
        try:
            for i in range(4):
                queue.enqueue(f"cagt-{i}", wait=False)
            deadline = time.monotonic() + 3
            while started_count[0] < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(started_count[0], 2, "only the concurrency budget may run")
            time.sleep(0.2)  # give the queue a chance to over-schedule
            self.assertLessEqual(max_active[0], 2)
            self.assertEqual(active[0], 2, "both slots busy, remaining tasks queued")
        finally:
            release.set()
            queue.shutdown()

    def test_per_task_timeout(self) -> None:
        never = threading.Event()
        config = VerificationQueueConfig(max_pending=4, concurrency=1, task_timeout_seconds=0.2)

        def factory():
            def verify(catalog_agent_id, *, actor="verification_worker"):
                never.wait(timeout=60)
                return SimpleNamespace(status=COMMERCE_VERIFIED)

            return SimpleNamespace(verify=verify, close=lambda: None)

        queue = self._queue(config=config, factory=factory)
        try:
            start = time.monotonic()
            result = queue.enqueue("cagt-slow", wait=True, timeout=5)
            elapsed = time.monotonic() - start
            self.assertEqual(result.status, "timeout")
            self.assertLess(elapsed, 3.0)
        finally:
            queue.shutdown()

    def test_enqueue_after_shutdown_raises(self) -> None:
        def factory():
            return SimpleNamespace(verify=lambda catalog_agent_id, actor="verification_worker": None, close=lambda: None)

        queue = self._queue(factory=factory)
        queue.shutdown()
        with self.assertRaises(VerificationQueueShutdownError):
            queue.enqueue("cagt-1", wait=False)

    def test_drain_returns_all_results(self) -> None:
        def factory():
            def verify(catalog_agent_id, *, actor="verification_worker"):
                return SimpleNamespace(status=COMMERCE_VERIFIED)

            return SimpleNamespace(verify=verify, close=lambda: None)

        queue = self._queue(factory=factory)
        try:
            queue.enqueue("cagt-1", wait=False)
            queue.enqueue("cagt-2", wait=False)
            results = queue.drain(timeout=5)
            self.assertEqual(len(results), 2)
            self.assertTrue(all(r.status == "completed" for r in results))
        finally:
            queue.shutdown()


# ── make_verification_worker integration ───────────────────────────────────


class MakeVerificationWorkerTest(unittest.TestCase):
    def test_queue_drives_the_real_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "catalog.sqlite"
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            conn.execute("pragma foreign_keys = on")
            init_db(conn)
            _seed_agent(conn)
            _seed_endpoints(conn)
            conn.close()

            fetcher = FakeFetcher()
            policy = TrustPolicy.defaults()

            def fake_fetch(self, url, etag=None, last_modified=None):
                return fetcher.fetch(url, etag=etag, last_modified=last_modified)

            with patch.object(ProfileFetcher, "fetch", autospec=True, side_effect=fake_fetch):
                queue = make_verification_worker(
                    db_file,
                    policy=policy,
                    config=VerificationQueueConfig(max_pending=8, concurrency=1, task_timeout_seconds=10),
                )
                try:
                    result = queue.enqueue("cagt-1", wait=True, timeout=10)
                    self.assertEqual(result.status, "completed")
                    self.assertEqual(result.verification_status, COMMERCE_VERIFIED)
                    self.assertIsInstance(result, VerificationTaskResult)
                finally:
                    queue.shutdown()

            # Re-open and confirm the work was persisted.
            conn = sqlite3.connect(db_file)
            conn.row_factory = sqlite3.Row
            self.assertEqual(
                conn.execute("select verification_status from catalog_agents where catalog_agent_id = ?", ("cagt-1",)).fetchone()["verification_status"],
                COMMERCE_VERIFIED,
            )
            self.assertEqual(
                conn.execute("select count(*) from agent_profile_snapshots where catalog_agent_id = ?", ("cagt-1",)).fetchone()[0],
                2,
            )
            conn.close()


if __name__ == "__main__":
    unittest.main()

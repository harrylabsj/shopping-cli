"""Tests for §24 runtime metrics registry + instrumentation.

Design: docs/shopping-cli-a2a-upgrade-design-v1.2.1.md §24

Covers the process-wide registry (counters / latency / gauges / funnel), the
instrumentation helpers, the integration points (search / verification queue /
profile fetch / conversation + registration funnel / hosted gateway), and the
``catalog_stats()`` ``runtime_metrics`` subtree.  Network I/O is mocked; the
DB is real SQLite in-memory.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from shopping_cli.agent_catalog.search import search_catalog_agents
from shopping_cli.db.session import init_db, now_iso
from shopping_cli.discovery.fetcher import FetchError, FetchResult, ProfileFetcher
from shopping_cli.discovery.trust import TrustPolicy
from shopping_cli.discovery.verifier import COMMERCE_VERIFIED, DISCOVERED
from shopping_cli.services import catalog_runtime_metrics as metrics
from shopping_cli.services.agent_catalog_metrics import catalog_stats
from shopping_cli.services.agent_catalog_writes import register_catalog_agent
from shopping_cli.services.agent_verification import (
    VerificationQueue,
    VerificationQueueConfig,
    VerificationService,
)
from shopping_cli.services.conversations import create_conversation

DOMAIN = "merchant.example"
CARD_URL = f"https://{DOMAIN}/agent-card.json"
UCP_URL = f"https://{DOMAIN}/ucp"
WELL_KNOWN_CARD = f"https://{DOMAIN}/.well-known/agent-card.json"
WELL_KNOWN_UCP = f"https://{DOMAIN}/.well-known/ucp"

# Keys present in catalog_stats() before this feature (unchanged contract).
_BASE_STATS_KEYS = frozenset(
    {
        "catalog_agent_count",
        "verified_agent_count",
        "unverified_agent_count",
        "stale_agent_count",
        "suspended_agent_count",
        "rejected_agent_count",
        "verification_status_distribution",
        "hosting_mode_distribution",
        "source_type_distribution",
        "lifecycle_status_distribution",
        "capability_count",
        "endpoint_count",
        "skill_count",
        "profile_snapshot_count",
    }
)


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
        self.well_known_status = 200
        self._seq = 0

    def fetch(self, url: str, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
        self._seq += 1
        fetched_at = 1000.0 + self._seq
        if url == CARD_URL:
            return FetchResult(
                url=url, status_code=200, body=json.dumps(self.card), parsed=self.card,
                etag=f"card-etag-{self._seq}", last_modified="Mon, 01 Jan 2026 00:00:00 GMT",
                fetched_at=fetched_at,
            )
        if url == UCP_URL:
            return FetchResult(
                url=url, status_code=200, body=json.dumps(self.ucp), parsed=self.ucp,
                etag=f"ucp-etag-{self._seq}", last_modified="Mon, 01 Jan 2026 00:00:00 GMT",
                fetched_at=fetched_at,
            )
        if url in (WELL_KNOWN_CARD, WELL_KNOWN_UCP):
            return FetchResult(url=url, status_code=self.well_known_status, parsed={}, fetched_at=fetched_at)
        raise AssertionError(f"unexpected fetch: {url}")


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    init_db(conn)
    return conn


def _seed_merchant(conn: sqlite3.Connection, merchant_id: str = "cagt-1") -> None:
    ts = now_iso()
    conn.execute(
        """
        insert into merchants(id, name, city, created_at, updated_at)
        values (?, ?, '', ?, ?)
        """,
        (merchant_id, "Acme Merchant", ts, ts),
    )
    conn.execute(
        """
        insert into products(sku, merchant_id, title, price, stock, created_at, updated_at)
        values (?, ?, ?, ?, ?, ?, ?)
        """,
        ("sku-1", merchant_id, "21 inch panel", 999.0, 5, ts, ts),
    )
    conn.commit()


def _seed_agent(
    conn: sqlite3.Connection,
    catalog_agent_id: str = "cagt-1",
    *,
    hosting_mode: str = "direct",
) -> None:
    ts = now_iso()
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
            "self_registered",
            "active",
            DISCOVERED,
            hosting_mode,
            ts,
            ts,
            ts,
            ts,
        ),
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


def _make_verification_service(conn: sqlite3.Connection) -> VerificationService:
    return VerificationService(
        conn,
        fetcher=FakeFetcher(),
        policy=TrustPolicy.defaults(),
        now=lambda: 1000.0,
    )


class _SlowFakeVerificationService:
    """Queue service stand-in whose verify() blocks so tasks stay pending."""

    def __init__(self, delay: float = 0.2) -> None:
        self.delay = delay
        self.verified: list[str] = []

    def verify(self, catalog_agent_id: str, *, actor: str = "verification_worker") -> object:
        time.sleep(self.delay)
        self.verified.append(catalog_agent_id)
        return type("R", (), {"status": "commerce_verified"})()

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


# ── Registry unit tests ─────────────────────────────────────────────────────


class RuntimeMetricsRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        metrics.reset_runtime_metrics()

    def test_counter_accumulates_with_delta(self) -> None:
        registry = metrics.get_runtime_metrics()
        registry.increment_counter("a")
        registry.increment_counter("a")
        registry.increment_counter("a", 5)
        self.assertEqual(metrics.snapshot_runtime_metrics()["counters"]["a"], 7)

    def test_latency_records_count_sum_max_avg(self) -> None:
        registry = metrics.get_runtime_metrics()
        registry.record_latency("lat", 0.1)
        registry.record_latency("lat", 0.3)
        registry.record_latency("lat", 0.2)
        rec = metrics.snapshot_runtime_metrics()["latency"]["lat"]
        self.assertEqual(rec["count"], 3)
        self.assertAlmostEqual(rec["sum"], 0.6)
        self.assertAlmostEqual(rec["max"], 0.3)
        self.assertAlmostEqual(rec["avg"], 0.2)

    def test_latency_rejects_negative(self) -> None:
        with self.assertRaises(ValueError):
            metrics.get_runtime_metrics().record_latency("lat", -1.0)

    def test_gauge_last_write_wins(self) -> None:
        registry = metrics.get_runtime_metrics()
        registry.set_gauge("g", 1.0)
        registry.set_gauge("g", 3.5)
        self.assertEqual(metrics.snapshot_runtime_metrics()["gauges"]["g"], 3.5)

    def test_funnel_counts_known_stages_and_ignores_unknown(self) -> None:
        registry = metrics.get_runtime_metrics()
        registry.increment_funnel("discovery")
        registry.increment_funnel("connected")
        registry.increment_funnel("no_such_stage")
        funnel = metrics.snapshot_runtime_metrics()["funnel"]
        self.assertEqual(funnel, {"discovery": 1, "connected": 1})

    def test_snapshot_structure(self) -> None:
        registry = metrics.get_runtime_metrics()
        registry.increment_counter("c")
        registry.record_latency("l", 0.1)
        registry.set_gauge("g", 2.0)
        registry.increment_funnel("verified")
        snap = metrics.snapshot_runtime_metrics()
        self.assertEqual(
            set(snap),
            {"counters", "latency", "gauges", "funnel"},
        )
        self.assertEqual(snap["counters"]["c"], 1)
        self.assertEqual(snap["latency"]["l"]["count"], 1)
        self.assertEqual(snap["gauges"]["g"], 2.0)
        self.assertEqual(snap["funnel"]["verified"], 1)

    def test_reset_clears_everything(self) -> None:
        registry = metrics.get_runtime_metrics()
        registry.increment_counter("c")
        registry.record_latency("l", 0.1)
        registry.set_gauge("g", 2.0)
        metrics.reset_runtime_metrics()
        snap = metrics.snapshot_runtime_metrics()
        self.assertEqual(snap["counters"], {})
        self.assertEqual(snap["latency"], {})
        self.assertEqual(snap["gauges"], {})
        self.assertEqual(snap["funnel"], {})

    def test_concurrent_counters_are_exact(self) -> None:
        registry = metrics.get_runtime_metrics()
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(1000):
                    registry.increment_counter("shared")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(metrics.snapshot_runtime_metrics()["counters"]["shared"], 8000)

    def test_concurrent_latency_is_exact(self) -> None:
        registry = metrics.get_runtime_metrics()
        errors: list[Exception] = []

        def worker() -> None:
            try:
                for _ in range(500):
                    registry.record_latency("lat", 0.001)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        rec = metrics.snapshot_runtime_metrics()["latency"]["lat"]
        self.assertEqual(rec["count"], 4000)
        self.assertAlmostEqual(rec["sum"], 4.0)


# ── Instrumentation helpers ──────────────────────────────────────────────────


class InstrumentationHelpersTest(unittest.TestCase):
    def setUp(self) -> None:
        metrics.reset_runtime_metrics()

    def test_profile_fetch_ok_error_and_error_rate(self) -> None:
        record_profile_fetch = metrics.record_profile_fetch
        record_profile_fetch(0.1, ok=True)
        record_profile_fetch(0.2, ok=True)
        record_profile_fetch(0.3, ok=False)
        snap = metrics.snapshot_runtime_metrics()
        self.assertEqual(snap["counters"]["profile_fetch_ok"], 2)
        self.assertEqual(snap["counters"]["profile_fetch_error"], 1)
        self.assertEqual(snap["latency"]["profile_fetch_latency"]["count"], 3)
        derived = metrics.derived_metrics(snap)
        self.assertAlmostEqual(derived["profile_fetch_error_rate"], round(1 / 3, 6))
        self.assertEqual(derived["catalog_to_connection_conversion"], 0.0)

    def test_search_records_latency_and_count(self) -> None:
        metrics.record_search(0.05, 3)
        metrics.record_search(0.07, 0)
        snap = metrics.snapshot_runtime_metrics()
        self.assertEqual(snap["latency"]["catalog_search_latency"]["count"], 2)
        self.assertAlmostEqual(snap["latency"]["catalog_search_latency"]["max"], 0.07)
        self.assertEqual(snap["counters"]["catalog_search_result_count"], 3)

    def test_queue_depth_and_hosted_gateway(self) -> None:
        metrics.set_queue_depth(4)
        metrics.record_hosted_gateway_request()
        metrics.record_hosted_gateway_request()
        snap = metrics.snapshot_runtime_metrics()
        self.assertEqual(snap["gauges"]["verification_queue_depth"], 4.0)
        self.assertEqual(snap["counters"]["hosted_gateway_requests"], 2)

    def test_derived_metrics_zero_when_no_samples(self) -> None:
        derived = metrics.derived_metrics()
        self.assertEqual(derived["profile_fetch_error_rate"], 0.0)
        self.assertEqual(derived["catalog_to_connection_conversion"], 0.0)

    def test_conversion_ratio_from_funnel(self) -> None:
        metrics.record_funnel("discovery")
        metrics.record_funnel("discovery")
        metrics.record_funnel("connected")
        derived = metrics.derived_metrics()
        self.assertAlmostEqual(derived["catalog_to_connection_conversion"], 0.5)


# ── Instrumentation integration ──────────────────────────────────────────────


class InstrumentationIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        metrics.reset_runtime_metrics()

    def test_search_instruments_real_path(self) -> None:
        conn = _make_conn()
        _seed_agent(conn, "cagt-1")
        _seed_agent(conn, "cagt-2")
        results, _ = search_catalog_agents(conn, q="Acme")
        self.assertEqual(len(results), 2)
        search_catalog_agents(conn, q="nothing-matches")
        snap = metrics.snapshot_runtime_metrics()
        self.assertEqual(snap["latency"]["catalog_search_latency"]["count"], 2)
        self.assertEqual(snap["counters"]["catalog_search_result_count"], 2)

    def test_queue_depth_tracks_lifecycle(self) -> None:
        config = VerificationQueueConfig(max_pending=10, concurrency=1)
        queue = VerificationQueue(service_factory=lambda: _SlowFakeVerificationService(delay=0.2), config=config)
        try:
            self.assertEqual(
                metrics.snapshot_runtime_metrics()["gauges"]["verification_queue_depth"], 0.0
            )
            queue.enqueue("cagt-1", wait=False)
            queue.enqueue("cagt-2", wait=False)
            # concurrency=1 → one in-flight, one pending → depth 2.
            depth = metrics.snapshot_runtime_metrics()["gauges"]["verification_queue_depth"]
            self.assertEqual(depth, 2.0)
            queue.drain(timeout=5.0)
            self.assertEqual(
                metrics.snapshot_runtime_metrics()["gauges"]["verification_queue_depth"], 0.0
            )
        finally:
            queue.shutdown()

    def test_fetch_records_success_and_304(self) -> None:
        fetcher = ProfileFetcher(TrustPolicy.defaults())
        ok_result = FetchResult(url=CARD_URL, status_code=200, fetched_at=1000.0)
        with patch.object(fetcher, "_fetch", return_value=ok_result):
            fetcher.fetch(CARD_URL)
        with patch.object(fetcher, "_fetch", return_value=FetchResult(url=CARD_URL, status_code=304, fetched_at=1001.0)):
            fetcher.fetch(CARD_URL)
        snap = metrics.snapshot_runtime_metrics()
        self.assertEqual(snap["counters"]["profile_fetch_ok"], 2)
        self.assertEqual(snap["counters"].get("profile_fetch_error", 0), 0)

    def test_fetch_records_error_and_rethrows(self) -> None:
        fetcher = ProfileFetcher(TrustPolicy.defaults())

        def boom(url: str, **kwargs: object) -> FetchResult:
            raise FetchError("boom")

        with patch.object(fetcher, "_fetch", side_effect=boom):
            with self.assertRaises(FetchError):
                fetcher.fetch(CARD_URL)
        snap = metrics.snapshot_runtime_metrics()
        self.assertEqual(snap["counters"]["profile_fetch_error"], 1)
        self.assertEqual(snap["counters"].get("profile_fetch_ok", 0), 0)

    def test_register_records_discovery_funnel(self) -> None:
        conn = _make_conn()
        register_catalog_agent(conn, domain="merchant.example", actor="test")
        self.assertEqual(metrics.snapshot_runtime_metrics()["funnel"]["discovery"], 1)

    def test_create_conversation_records_connected_and_negotiation_started(self) -> None:
        conn = _make_conn()
        _seed_merchant(conn, "cagt-1")
        _seed_agent(conn, "cagt-1")
        create_conversation(
            conn,
            buyer_id="buyer-1",
            merchant_id="cagt-1",
            sku="sku-1",
            reuse_open=False,
        )
        create_conversation(
            conn,
            buyer_id="buyer-1",
            merchant_id="cagt-1",
            sku="sku-1",
            text="how much for a 21 inch panel?",
            intent="ask_product",
            reuse_open=False,
        )
        funnel = metrics.snapshot_runtime_metrics()["funnel"]
        self.assertEqual(funnel["connected"], 2)
        self.assertEqual(funnel["negotiation_started"], 1)

    def test_verify_success_records_verified_funnel(self) -> None:
        conn = _make_conn()
        _seed_agent(conn, "cagt-1")
        _seed_endpoints(conn, "cagt-1")
        service = _make_verification_service(conn)
        result = service.verify("cagt-1")
        self.assertEqual(result.status, COMMERCE_VERIFIED)
        self.assertEqual(metrics.snapshot_runtime_metrics()["funnel"]["verified"], 1)

    def test_failed_verify_does_not_record_verified_funnel(self) -> None:
        conn = _make_conn()
        _seed_agent(conn, "cagt-1")
        # No endpoints → profile stage fails → no verified funnel event.
        service = _make_verification_service(conn)
        result = service.verify("cagt-1")
        self.assertNotEqual(result.status, COMMERCE_VERIFIED)
        self.assertEqual(metrics.snapshot_runtime_metrics()["funnel"].get("verified", 0), 0)


# ── catalog_stats() runtime_metrics subtree ──────────────────────────────────


class CatalogStatsRuntimeSubtreeTest(unittest.TestCase):
    def setUp(self) -> None:
        metrics.reset_runtime_metrics()

    def test_empty_db_keeps_existing_keys_and_adds_subtree(self) -> None:
        conn = _make_conn()
        stats = catalog_stats(conn)
        self.assertEqual(set(stats) - _BASE_STATS_KEYS, {"runtime_metrics"})
        subtree = stats["runtime_metrics"]
        self.assertEqual(
            set(subtree),
            {"counters", "latency", "gauges", "funnel", "derived"},
        )
        derived = subtree["derived"]
        self.assertEqual(derived["profile_fetch_error_rate"], 0.0)
        self.assertEqual(derived["catalog_to_connection_conversion"], 0.0)
        self.assertEqual(derived["direct_a2a_ratio"], 0.0)
        self.assertEqual(derived["hosted_gateway_ratio"], 0.0)

    def test_ratios_derived_from_hosting_mode_distribution(self) -> None:
        conn = _make_conn()
        _seed_agent(conn, "cagt-1", hosting_mode="direct")
        _seed_agent(conn, "cagt-2", hosting_mode="direct")
        _seed_agent(conn, "cagt-3", hosting_mode="hosted")
        _seed_agent(conn, "cagt-4", hosting_mode="hybrid")
        _seed_agent(conn, "cagt-5", hosting_mode="unknown")  # excluded from denominators
        stats = catalog_stats(conn)
        derived = stats["runtime_metrics"]["derived"]
        self.assertEqual(stats["hosting_mode_distribution"]["direct"], 2)
        self.assertAlmostEqual(derived["direct_a2a_ratio"], 2 / 4)
        self.assertAlmostEqual(derived["hosted_gateway_ratio"], 1 / 4)

    def test_subtree_reflects_runtime_activity(self) -> None:
        conn = _make_conn()
        _seed_agent(conn, "cagt-1")
        search_catalog_agents(conn, q="Acme")
        metrics.record_profile_fetch(0.1, ok=False)
        stats = catalog_stats(conn)
        subtree = stats["runtime_metrics"]
        self.assertEqual(subtree["latency"]["catalog_search_latency"]["count"], 1)
        self.assertEqual(subtree["counters"]["profile_fetch_error"], 1)
        self.assertAlmostEqual(subtree["derived"]["profile_fetch_error_rate"], 1.0)

    def test_stats_maps_to_cli_json_format(self) -> None:
        # The CLI --format json path emits catalog_stats as-is; runtime_metrics
        # must survive JSON serialization (no sets/bytes).
        conn = _make_conn()
        stats = catalog_stats(conn)
        json.dumps(stats)
        self.assertIn("runtime_metrics", stats)


if __name__ == "__main__":
    unittest.main()

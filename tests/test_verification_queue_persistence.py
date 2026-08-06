"""Tests for the persistent verification queue (v3.0-P4, schema v15).

Design: docs/shopping-cli-a2a-upgrade-design-v1.2.1.md §25 Phase 2 +
v3.0-P4 — the in-process bounded queue writes through to the
``verification_queue_tasks`` ledger so tasks survive a process restart.

Covers: enqueue persistence, terminal-row writes (completed/failed/timeout),
crash recovery (pending + running rows re-enqueued into a new queue),
wait() rebuilding outcomes from the ledger, and result JSON round-trips.
The DB is a real temp-file SQLite (migrations applied via init_db).
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from shopping_cli.db.session import init_db
from shopping_cli.discovery.verifier import COMMERCE_VERIFIED
from shopping_cli.services.agent_verification import (
    StageResult,
    VerificationQueue,
    VerificationQueueConfig,
    VerificationResult,
    _deserialize_verification_result,
    _serialize_verification_result,
)


def _make_db() -> Path:
    tmp = tempfile.mkdtemp()
    db_file = Path(tmp) / "queue.sqlite"
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row  # migrations' _table_columns requires it
    init_db(conn)
    conn.close()
    return db_file


def _ledger_rows(db_file: Path) -> list[dict]:
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "select * from verification_queue_tasks order by enqueued_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _fast_factory(calls: list[str] | None = None) -> object:
    def factory():
        def verify(catalog_agent_id, *, actor="verification_worker"):
            if calls is not None:
                calls.append(catalog_agent_id)
            return SimpleNamespace(
                status=COMMERCE_VERIFIED,
                catalog_agent_id=catalog_agent_id,
                previous_status="discovered",
                stages=(StageResult("profile", "passed", "commerce_verified"),),
            )

        return SimpleNamespace(verify=verify, close=lambda: None)

    return factory


class _SlowFactory:
    """Slow fake service so tasks stay pending/running long enough to observe."""

    def __init__(self, delay: float = 0.3) -> None:
        self.delay = delay
        self.executed: list[str] = []

    def __call__(self) -> object:
        def run(catalog_agent_id, *, actor="verification_worker"):
            time.sleep(self.delay)
            self.executed.append(catalog_agent_id)
            return SimpleNamespace(status=COMMERCE_VERIFIED)

        return SimpleNamespace(verify=run, refresh=run, mark_stale=run, suspend=run, close=lambda: None)


class PersistenceLedgerTest(unittest.TestCase):
    def test_enqueue_persists_pending_row(self) -> None:
        db_file = _make_db()
        # Slow service so the row is observable before a worker finishes it.
        queue = VerificationQueue(
            service_factory=_SlowFactory(delay=0.4),
            config=VerificationQueueConfig(concurrency=1),
            db_path=db_file,
            now=lambda: 1000.0,
        )
        try:
            queue.enqueue("cagt-1", kind="refresh", actor="admin", wait=False)
            time.sleep(0.05)
            rows = _ledger_rows(db_file)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["catalog_agent_id"], "cagt-1")
            self.assertEqual(rows[0]["kind"], "refresh")
            self.assertEqual(rows[0]["actor"], "admin")
            # The row was persisted as pending (a worker may have claimed it
            # already, in which case it is running — never dropped).
            self.assertIn(rows[0]["status"], ("pending", "running"))
        finally:
            queue.shutdown()

    def test_worker_writes_terminal_row_with_result(self) -> None:
        db_file = _make_db()
        queue = VerificationQueue(
            service_factory=_fast_factory(),
            config=VerificationQueueConfig(),
            db_path=db_file,
            now=lambda: 1000.0,
        )
        try:
            result = queue.enqueue("cagt-1", wait=True, timeout=10)
        finally:
            queue.shutdown()
        self.assertEqual(result.status, "completed")
        rows = _ledger_rows(db_file)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "completed")
        self.assertEqual(rows[0]["verification_status"], COMMERCE_VERIFIED)
        rebuilt = _deserialize_verification_result(rows[0]["result_json"])
        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt.status, COMMERCE_VERIFIED)  # type: ignore[union-attr]
        self.assertEqual(rebuilt.stages[0].stage, "profile")  # type: ignore[union-attr]

    def test_result_json_roundtrip_with_evidence(self) -> None:
        result = VerificationResult(
            catalog_agent_id="cagt-1",
            previous_status="discovered",
            status=COMMERCE_VERIFIED,
            stages=(
                StageResult(
                    stage="domain_control",
                    outcome="passed",
                    target_status="domain_verified",
                    reason="https verified",
                    verification_id=7,
                    snapshot_ids=(1, 2),
                    evidence={"trust_policy_version": "1.0"},
                ),
            ),
        )
        raw = _serialize_verification_result(result)
        rebuilt = _deserialize_verification_result(raw)
        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt.previous_status, "discovered")  # type: ignore[union-attr]
        stage = rebuilt.stages[0]  # type: ignore[union-attr]
        self.assertEqual(stage.verification_id, 7)
        self.assertEqual(stage.snapshot_ids, (1, 2))
        self.assertEqual(stage.evidence, {"trust_policy_version": "1.0"})

    def test_empty_serialization_is_none(self) -> None:
        self.assertEqual(_deserialize_verification_result("{}"), None)
        self.assertEqual(_deserialize_verification_result(""), None)
        self.assertEqual(_serialize_verification_result(None), "{}")


class CrashRecoveryTest(unittest.TestCase):
    def test_pending_tasks_survive_restart(self) -> None:
        db_file = _make_db()
        # Queue 1: slow service, enqueue two tasks, abandon without shutdown
        # (simulates a crash — workers are daemon threads, the ledger rows
        # are left pending/running).
        slow = _SlowFactory(delay=0.4)
        queue1 = VerificationQueue(
            service_factory=slow,
            config=VerificationQueueConfig(concurrency=1),
            db_path=db_file,
            now=lambda: 1000.0,
        )
        queue1.enqueue("cagt-1", wait=False)
        queue1.enqueue("cagt-2", wait=False)
        time.sleep(0.1)  # first task is running, second still pending
        queue1._db_conn.close()  # noqa: SLF001 — simulate the process going away
        del queue1

        # Queue 2: a new process instance recovers both rows and drains them.
        calls: list[str] = []
        queue2 = VerificationQueue(
            service_factory=_fast_factory(calls),
            config=VerificationQueueConfig(concurrency=2),
            db_path=db_file,
            now=lambda: 2000.0,
        )
        try:
            results = queue2.drain(timeout=15)
        finally:
            queue2.shutdown()
            time.sleep(0.5)  # let queue1's runaway daemon finish writing

        self.assertEqual(sorted(r.catalog_agent_id for r in results), ["cagt-1", "cagt-2"])
        self.assertTrue(all(r.status == "completed" for r in results))
        self.assertEqual(sorted(calls), ["cagt-1", "cagt-2"])
        rows = _ledger_rows(db_file)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["status"] == "completed" for r in rows))

    def test_running_row_is_recovered(self) -> None:
        db_file = _make_db()
        # Simulate a crash mid-task: a row stuck in running.
        conn = sqlite3.connect(db_file)
        conn.execute(
            "insert into verification_queue_tasks("
            " task_id, catalog_agent_id, kind, actor, status, enqueued_at,"
            " created_at, updated_at)"
            " values ('vt-000001-dead', 'cagt-1', 'verify', 'verification_worker',"
            " 'running', 1000.0, '2026-08-06T00:00:00+00:00', '2026-08-06T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        calls: list[str] = []
        queue = VerificationQueue(
            service_factory=_fast_factory(calls),
            config=VerificationQueueConfig(),
            db_path=db_file,
            now=lambda: 2000.0,
        )
        try:
            results = queue.drain(timeout=15)
        finally:
            queue.shutdown()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].task_id, "vt-000001-dead")
        self.assertEqual(results[0].status, "completed")
        self.assertEqual(calls, ["cagt-1"])

    def test_wait_rebuilds_result_from_ledger_after_restart(self) -> None:
        db_file = _make_db()
        queue1 = VerificationQueue(
            service_factory=_fast_factory(),
            config=VerificationQueueConfig(),
            db_path=db_file,
            now=lambda: 1000.0,
        )
        try:
            first = queue1.enqueue("cagt-1", wait=True, timeout=10)
        finally:
            queue1.shutdown()
        task_id = first.task_id
        self.assertEqual(first.status, "completed")

        # A fresh process has no in-memory result for this task — wait()
        # must rebuild it from the ledger.
        queue2 = VerificationQueue(
            service_factory=_fast_factory(),
            config=VerificationQueueConfig(),
            db_path=db_file,
            now=lambda: 2000.0,
        )
        try:
            rebuilt = queue2.wait(task_id, timeout=0.1)
        finally:
            queue2.shutdown()
        self.assertEqual(rebuilt.status, "completed")
        self.assertEqual(rebuilt.verification_status, COMMERCE_VERIFIED)
        self.assertIsNotNone(rebuilt.result)

    def test_concurrency_budget_holds_in_persistent_mode(self) -> None:
        db_file = _make_db()
        slow = _SlowFactory(delay=0.4)
        queue = VerificationQueue(
            service_factory=slow,
            config=VerificationQueueConfig(concurrency=1),
            db_path=db_file,
            now=lambda: 1000.0,
        )
        try:
            queue.enqueue("cagt-1", wait=False)
            queue.enqueue("cagt-2", wait=False)
            time.sleep(0.15)
            rows = _ledger_rows(db_file)
            running = [r for r in rows if r["status"] == "running"]
            # concurrency=1 → never more than one running row.
            self.assertLessEqual(len(running), 1)
            results = queue.drain(timeout=15)
        finally:
            queue.shutdown()
        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()

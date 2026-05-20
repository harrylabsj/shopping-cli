import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shopping_cli.agents import buyer_cli, merchant_agent
from shopping_cli.core import conversations, harness
from shopping_cli.core.catalog import create_merchant, create_product
from shopping_cli.core.conversations import conversation_summary, next_conversation_id
from shopping_cli.core.harness import abandon_agent_message, abandon_stale_agent_messages, claim_agent_message, complete_agent_message, fail_agent_message
from shopping_cli.core.tokens import token_digest
from shopping_cli.db.session import db_session, decode_json


class OrchestrationHarnessTest(unittest.TestCase):
    def test_next_conversation_id_reads_only_the_max_numeric_suffix(self):
        class Cursor:
            def fetchone(self):
                return {"max_id": 10000}

            def fetchall(self):
                raise AssertionError("next_conversation_id should not load every conversation id")

        class Connection:
            sql = ""

            def execute(self, sql):
                self.sql = sql
                return Cursor()

        conn = Connection()

        self.assertEqual(next_conversation_id(conn), "CONV-10001")
        self.assertIn("limit 1", conn.sql.lower())

    def seed_conversation(self, db_file: Path) -> None:
        with db_session(db_file) as conn:
            create_merchant(
                conn,
                merchant_id="seller-a",
                name="West Lake Tea",
                city="Hangzhou",
                service_area="West Lake",
                delivery_eta_minutes=45,
            )
            create_product(
                conn,
                merchant_id="seller-a",
                sku="tea-a",
                title="Longjing Gift Box",
                price=88,
                stock=5,
                tags=["longjing", "gift"],
            )
            buyer_cli.ask(conn, "alice", "longjing gift delivery today", city="Hangzhou")

    def test_harness_records_next_actor_idempotency_and_audit_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_conversation(db_file)

            with db_session(db_file) as conn:
                conversation = conversation_summary(conn, "CONV-0001")
                self.assertEqual(conversation["status"], "waiting_merchant")
                self.assertEqual(conversation["next_actor"], "merchant_agent")
                self.assertTrue(
                    any(event["event"] == "message_appended" and event["actor"] == "buyer" for event in conversation["audit_events"])
                )

                first = merchant_agent.process_once(conn, "seller-a")
                self.assertEqual(first["replied"][0]["conversation_id"], "CONV-0001")

                updated = conversation_summary(conn, "CONV-0001")
                self.assertEqual(updated["status"], "waiting_buyer")
                self.assertEqual(updated["next_actor"], "buyer")

                process = conn.execute(
                    "select * from agent_message_processes where agent_id = ? and message_id = ?",
                    ("shopping-cli-merchant-agent:seller-a", 1),
                ).fetchone()
                self.assertIsNotNone(process)
                self.assertEqual(process["status"], "processed")
                self.assertEqual(process["attempts"], 1)
                self.assertEqual(process["idempotency_key"], "shopping-cli-merchant-agent:seller-a:1")

                agent_reply = updated["messages"][-1]
                self.assertEqual(agent_reply["structured_payload"]["processed_message_id"], 1)
                self.assertEqual(agent_reply["structured_payload"]["idempotency_key"], "shopping-cli-merchant-agent:seller-a:1")
                self.assertTrue(
                    any(event["event"] == "agent_message_processed" for event in updated["audit_events"])
                )

    def test_conversation_audit_events_use_list_rows_without_per_event_hydration(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_conversation(db_file)

            with db_session(db_file) as conn:
                with patch("shopping_cli.core.harness.audit_event_summary", wraps=harness.audit_event_summary) as summary:
                    events = harness.conversation_audit_events(conn, "CONV-0001")

            self.assertTrue(events)
            self.assertEqual(summary.call_count, 0)

    def test_conversation_messages_and_flags_use_list_rows_without_per_item_hydration(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_conversation(db_file)

            with db_session(db_file) as conn:
                conversations.add_flag(conn, "CONV-0001", "low_confidence")
                with (
                    patch("shopping_cli.core.conversations.message_summary", wraps=conversations.message_summary) as messages,
                    patch("shopping_cli.core.conversations.flag_summary", wraps=conversations.flag_summary) as flags,
                ):
                    listed_messages = conversations.conversation_messages(conn, "CONV-0001")
                    listed_flags = conversations.conversation_flags(conn, "CONV-0001")

            self.assertTrue(listed_messages)
            self.assertTrue(listed_flags)
            self.assertEqual(messages.call_count, 0)
            self.assertEqual(flags.call_count, 0)

    def test_merchant_conversations_treats_negative_limit_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_conversation(db_file)

            with db_session(db_file) as conn:
                listed = conversations.merchant_conversations(conn, "seller-a", limit=-1, offset=-1)

            self.assertEqual(listed, [])

    def test_ensure_conversation_retries_when_generated_id_collides(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_conversation(db_file)

            with db_session(db_file) as conn:
                with patch("shopping_cli.core.conversations.next_conversation_id", side_effect=["CONV-0001", "CONV-0002"]):
                    conversation = conversations.ensure_conversation(
                        conn,
                        buyer_id="bob",
                        merchant_id="seller-a",
                        sku="tea-a",
                        reuse_open=False,
                    )

            self.assertEqual(conversation["id"], "CONV-0002")
            self.assertEqual(conversation["buyer_id"], "bob")

    def test_processing_claim_is_not_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_conversation(db_file)

            with db_session(db_file) as conn:
                first = claim_agent_message(conn, "merchant-agent", "CONV-0001", 1, "merchant-agent:1")
                second = claim_agent_message(conn, "merchant-agent", "CONV-0001", 1, "merchant-agent:1")

                self.assertTrue(first["claimed"])
                self.assertFalse(second["claimed"])
                self.assertEqual(second["status"], "processing")

                process = conn.execute(
                    """
                    select attempts, status from agent_message_processes
                    where agent_id = ? and message_id = ?
                    """,
                    ("merchant-agent", 1),
                ).fetchone()
                self.assertEqual(int(process["attempts"]), 1)
                self.assertEqual(process["status"], "processing")

    def test_processing_claim_tolerates_corrupt_attempts_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_conversation(db_file)

            with db_session(db_file) as conn:
                claim_agent_message(conn, "merchant-agent", "CONV-0001", 1, "merchant-agent:1")
                conn.execute(
                    """
                    update agent_message_processes
                    set attempts = 'bad'
                    where agent_id = ? and message_id = ?
                    """,
                    ("merchant-agent", 1),
                )

                second = claim_agent_message(conn, "merchant-agent", "CONV-0001", 1, "merchant-agent:1")

                self.assertFalse(second["claimed"])
                self.assertEqual(second["status"], "processing")
                self.assertEqual(second["attempts"], 0)

    def test_processing_claim_tolerates_non_finite_attempts_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_conversation(db_file)

            with db_session(db_file) as conn:
                claim_agent_message(conn, "merchant-agent", "CONV-0001", 1, "merchant-agent:1")
                conn.execute(
                    """
                    update agent_message_processes
                    set attempts = ?
                    where agent_id = ? and message_id = ?
                    """,
                    (float("inf"), "merchant-agent", 1),
                )

                second = claim_agent_message(conn, "merchant-agent", "CONV-0001", 1, "merchant-agent:1")

                self.assertFalse(second["claimed"])
                self.assertEqual(second["status"], "processing")
                self.assertEqual(second["attempts"], 0)

    def test_abandoned_processing_claim_can_be_retried_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_conversation(db_file)

            with db_session(db_file) as conn:
                first = claim_agent_message(conn, "merchant-agent", "CONV-0001", 1, "merchant-agent:1")
                abandoned = abandon_agent_message(conn, "merchant-agent", 1, "worker stopped before reply")
                second = claim_agent_message(conn, "merchant-agent", "CONV-0001", 1, "merchant-agent:1")

                self.assertTrue(first["claimed"])
                self.assertEqual(abandoned["status"], "abandoned")
                self.assertEqual(abandoned["last_error"], "worker stopped before reply")
                self.assertTrue(second["claimed"])
                self.assertEqual(second["attempts"], 2)

                process = conn.execute(
                    """
                    select attempts, status, last_error from agent_message_processes
                    where agent_id = ? and message_id = ?
                    """,
                    ("merchant-agent", 1),
                ).fetchone()
                self.assertEqual(int(process["attempts"]), 2)
                self.assertEqual(process["status"], "processing")
                self.assertEqual(process["last_error"], "")

                events = conversation_summary(conn, "CONV-0001")["audit_events"]
                self.assertTrue(any(event["event"] == "agent_message_abandoned" for event in events))

    def test_completed_or_failed_claims_are_not_rewritten_by_invalid_transitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_conversation(db_file)

            with db_session(db_file) as conn:
                claim_agent_message(conn, "merchant-agent", "CONV-0001", 1, "merchant-agent:1")
                fail_agent_message(conn, "merchant-agent", 1, "temporary failure")
                complete_after_failed = complete_agent_message(conn, "merchant-agent", 1)
                retry = claim_agent_message(conn, "merchant-agent", "CONV-0001", 1, "merchant-agent:1")
                complete_agent_message(conn, "merchant-agent", 1)
                failed_after_processed = fail_agent_message(conn, "merchant-agent", 1, "late failure")

                self.assertEqual(complete_after_failed["status"], "failed")
                self.assertTrue(retry["claimed"])
                self.assertEqual(failed_after_processed["status"], "processed")

                process = conn.execute(
                    """
                    select attempts, status, last_error from agent_message_processes
                    where agent_id = ? and message_id = ?
                    """,
                    ("merchant-agent", 1),
                ).fetchone()
                self.assertEqual(int(process["attempts"]), 2)
                self.assertEqual(process["status"], "processed")
                self.assertEqual(process["last_error"], "")

    def test_stale_processing_claims_are_abandoned_and_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_conversation(db_file)

            with db_session(db_file) as conn:
                claim_agent_message(conn, "merchant-agent", "CONV-0001", 1, "merchant-agent:1")
                conn.execute(
                    """
                    update agent_message_processes
                    set updated_at = '2026-05-11T00:00:00'
                    where agent_id = ? and message_id = ?
                    """,
                    ("merchant-agent", 1),
                )
                abandoned = abandon_stale_agent_messages(
                    conn,
                    "merchant-agent",
                    stale_after_seconds=60,
                    now="2026-05-11T00:02:01",
                )
                retry = claim_agent_message(conn, "merchant-agent", "CONV-0001", 1, "merchant-agent:1")

                self.assertEqual(len(abandoned), 1)
                self.assertEqual(abandoned[0]["status"], "abandoned")
                self.assertIn("stale processing claim", abandoned[0]["last_error"])
                self.assertTrue(retry["claimed"])
                self.assertEqual(retry["attempts"], 2)

                events = conversation_summary(conn, "CONV-0001")["audit_events"]
                self.assertTrue(
                    any(
                        event["event"] == "agent_message_abandoned"
                        and event["details"]["reason"] == "stale_processing_claim"
                        for event in events
                    )
                )

    def test_fresh_processing_claims_are_not_abandoned_by_ttl_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_conversation(db_file)

            with db_session(db_file) as conn:
                claim_agent_message(conn, "merchant-agent", "CONV-0001", 1, "merchant-agent:1")
                conn.execute(
                    """
                    update agent_message_processes
                    set updated_at = '2026-05-11T00:01:30'
                    where agent_id = ? and message_id = ?
                    """,
                    ("merchant-agent", 1),
                )
                abandoned = abandon_stale_agent_messages(
                    conn,
                    "merchant-agent",
                    stale_after_seconds=60,
                    now="2026-05-11T00:02:01",
                )

                self.assertEqual(abandoned, [])
                process = conn.execute(
                    """
                    select attempts, status from agent_message_processes
                    where agent_id = ? and message_id = ?
                    """,
                    ("merchant-agent", 1),
                ).fetchone()
                self.assertEqual(int(process["attempts"]), 1)
                self.assertEqual(process["status"], "processing")

    def test_stale_processing_claim_recovery_tolerates_invalid_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_conversation(db_file)

            with db_session(db_file) as conn:
                claim_agent_message(conn, "merchant-agent", "CONV-0001", 1, "merchant-agent:1")
                conn.execute(
                    """
                    update agent_message_processes
                    set updated_at = '2026-05-11T00:00:00'
                    where agent_id = ? and message_id = ?
                    """,
                    ("merchant-agent", 1),
                )

                try:
                    abandoned = abandon_stale_agent_messages(
                        conn,
                        "merchant-agent",
                        stale_after_seconds="bad",
                        now="2026-05-11T00:10:01",
                    )
                except ValueError as exc:
                    self.fail(f"stale claim recovery should tolerate invalid ttl values: {exc}")

                self.assertEqual(len(abandoned), 1)
                self.assertIn("300 seconds", abandoned[0]["last_error"])

    def test_stale_processing_claim_recovery_tolerates_oversized_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_conversation(db_file)

            with db_session(db_file) as conn:
                claim_agent_message(conn, "merchant-agent", "CONV-0001", 1, "merchant-agent:1")
                conn.execute(
                    """
                    update agent_message_processes
                    set updated_at = '2026-05-11T00:00:00'
                    where agent_id = ? and message_id = ?
                    """,
                    ("merchant-agent", 1),
                )

                abandoned = abandon_stale_agent_messages(
                    conn,
                    "merchant-agent",
                    stale_after_seconds=10**100,
                    now="2026-05-11T00:10:01",
                )

                self.assertEqual(len(abandoned), 1)
                self.assertIn("300 seconds", abandoned[0]["last_error"])

    def test_schema_migration_adds_harness_tables_to_existing_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "old.sqlite"
            conn = sqlite3.connect(db_file)
            try:
                conn.execute(
                    """
                    create table conversations (
                        id text primary key,
                        buyer_id text not null,
                        merchant_id text not null,
                        sku text not null default '',
                        status text not null,
                        created_at text not null,
                        updated_at text not null,
                        last_sender text not null default ''
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with db_session(db_file) as conn:
                conversation_columns = {row["name"] for row in conn.execute("pragma table_info(conversations)").fetchall()}
                tables = {row["name"] for row in conn.execute("select name from sqlite_master where type = 'table'")}

            self.assertIn("next_actor", conversation_columns)
            self.assertIn("audit_events", tables)
            self.assertIn("agent_message_processes", tables)

    def test_schema_migration_adds_columns_before_operational_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "legacy-flags.sqlite"
            conn = sqlite3.connect(db_file)
            try:
                conn.execute(
                    """
                    create table moderation_flags (
                        id integer primary key autoincrement,
                        conversation_id text not null default '',
                        sku text not null default '',
                        reason text not null,
                        severity text not null default 'review',
                        created_at text not null
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            with db_session(db_file) as conn:
                columns = {row["name"] for row in conn.execute("pragma table_info(moderation_flags)").fetchall()}
                indexes = {row["name"] for row in conn.execute("select name from sqlite_master where type = 'index'")}

            self.assertIn("resolved_at", columns)
            self.assertIn("idx_moderation_flags_conversation_resolved", indexes)

    def test_schema_migration_hashes_legacy_plaintext_api_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "legacy-tokens.sqlite"
            raw_token = "shopping_seller-a_legacy-secret"
            conn = sqlite3.connect(db_file)
            try:
                conn.execute(
                    """
                    create table api_tokens (
                        token text primary key,
                        role text not null,
                        merchant_id text not null default '',
                        buyer_id text not null default '',
                        created_at text not null
                    )
                    """
                )
                conn.execute(
                    """
                    insert into api_tokens(token, role, merchant_id, buyer_id, created_at)
                    values (?, 'merchant', 'seller-a', '', '2026-05-14T00:00:00')
                    """,
                    (raw_token,),
                )
                conn.commit()
            finally:
                conn.close()

            with db_session(db_file) as conn:
                row = conn.execute(
                    "select token, token_hash, token_prefix, token_suffix from api_tokens"
                ).fetchone()

            self.assertEqual(row["token"], token_digest(raw_token))
            self.assertEqual(row["token_hash"], token_digest(raw_token))
            self.assertEqual(row["token_prefix"], raw_token[:24])
            self.assertEqual(row["token_suffix"], raw_token[-6:])

    def test_schema_migration_preserves_existing_token_digest_without_rehashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "digest-token.sqlite"
            raw_token = "shopping_seller-a_already-hashed"
            digest = token_digest(raw_token)
            conn = sqlite3.connect(db_file)
            try:
                conn.execute(
                    """
                    create table api_tokens (
                        token text primary key,
                        token_hash text not null default '',
                        token_prefix text not null default '',
                        token_suffix text not null default '',
                        role text not null,
                        merchant_id text not null default '',
                        buyer_id text not null default '',
                        agent_id text not null default '',
                        conversation_id text not null default '',
                        revoked_at text not null default '',
                        expires_at text not null default '',
                        created_at text not null
                    )
                    """
                )
                conn.execute(
                    """
                    insert into api_tokens(token, role, merchant_id, created_at)
                    values (?, 'merchant', 'seller-a', '2026-05-14T00:00:00')
                    """,
                    (digest,),
                )
                conn.commit()
            finally:
                conn.close()

            with db_session(db_file) as conn:
                row = conn.execute(
                    "select token, token_hash, token_prefix, token_suffix from api_tokens"
                ).fetchone()

            self.assertEqual(row["token"], digest)
            self.assertEqual(row["token_hash"], digest)
            self.assertEqual(row["token_prefix"], digest[:24])
            self.assertEqual(row["token_suffix"], digest[-6:])

    def test_suspicious_conversation_routes_to_operator_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                create_merchant(conn, merchant_id="seller-a", name="West Lake Tea", city="Hangzhou", service_area="West Lake")
                create_product(
                    conn,
                    merchant_id="seller-a",
                    sku="tea-a",
                    title="Longjing",
                    price=88,
                    stock=5,
                    tags=["longjing"],
                )
                buyer_cli.ask(conn, "alice", "Can you help with fake id and longjing?", city="Hangzhou")

                result = merchant_agent.process_once(conn, "seller-a")
                self.assertEqual(result["replied"][0]["reason"], "suspicious_content")

                conversation = conversation_summary(conn, "CONV-0001")
                self.assertEqual(conversation["status"], "human_required")
                self.assertEqual(conversation["next_actor"], "operator")
                self.assertTrue(
                    any(
                        event["event"] == "human_review_flagged"
                        and event["details"]["reason"] == "suspicious_content"
                        and event["details"]["next_actor"] == "operator"
                        for event in conversation["audit_events"]
                    )
                )

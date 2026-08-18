import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from shopping_cli import VERSION
from shopping_cli.core.tokens import token_digest, token_prefix, token_suffix
from shopping_cli.db import migrations as migrations_module
from shopping_cli.db.migrations import CURRENT_SCHEMA_VERSION
from shopping_cli.db.session import SQLITE_BUSY_TIMEOUT_MS, db_session, open_connection


class DbSessionTest(unittest.TestCase):
    def test_open_connection_enables_wal_busy_timeout_and_waits_for_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass

            errors = []

            def write_from_second_connection():
                try:
                    with db_session(db_file) as conn:
                        conn.execute("insert or replace into meta(key, value) values('thread_writer', 'ok')")
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            first = open_connection(db_file)
            try:
                busy_timeout = first.execute("pragma busy_timeout").fetchone()[0]
                journal_mode = first.execute("pragma journal_mode").fetchone()[0]
                self.assertGreaterEqual(int(busy_timeout), SQLITE_BUSY_TIMEOUT_MS)
                self.assertEqual(str(journal_mode).lower(), "wal")

                first.execute("begin immediate")
                first.execute("insert or replace into meta(key, value) values('main_writer', 'holding')")
                thread = threading.Thread(target=write_from_second_connection)
                thread.start()
                time.sleep(0.2)
                first.commit()
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
            finally:
                first.close()

            self.assertEqual(errors, [])
            with db_session(db_file) as conn:
                row = conn.execute("select value from meta where key = 'thread_writer'").fetchone()
            self.assertEqual(row["value"], "ok")

    def test_explicit_migrations_use_user_version_not_package_version(self):
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

            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("update meta set value = ? where key = 'package_version'", ("old-version",))
                conn.commit()

            with patch.object(migrations_module, "MIGRATIONS", (fake_migration,)):
                with db_session(db_file):
                    pass

            self.assertEqual(calls, [])

            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("pragma user_version = 0")
                conn.commit()

            with patch.object(migrations_module, "MIGRATIONS", (fake_migration,)):
                with db_session(db_file):
                    pass

            self.assertEqual(calls, ["ran"])
            with db_session(db_file) as conn:
                row = conn.execute("select value from meta where key = 'schema_version'").fetchone()
                package = conn.execute("select value from meta where key = 'package_version'").fetchone()
                user_version = conn.execute("pragma user_version").fetchone()[0]
            self.assertEqual(row["value"], str(CURRENT_SCHEMA_VERSION))
            self.assertEqual(package["value"], VERSION)
            self.assertEqual(user_version, CURRENT_SCHEMA_VERSION)

    def test_explicit_migrations_upgrade_representative_legacy_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "legacy.sqlite"
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute("create table meta (key text primary key, value text not null)")
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
                conn.execute(
                    """
                    insert into conversations(id, buyer_id, merchant_id, sku, status, created_at, updated_at, last_sender)
                    values('CONV-0001', 'alice', 'seller-a', '', 'waiting_merchant', '2026-01-01T00:00:00', '2026-01-01T00:00:00', 'buyer')
                    """
                )
                conn.execute(
                    """
                    create table agents (
                        id text primary key,
                        type text not null,
                        owner_id text not null,
                        status text not null,
                        capabilities_json text not null default '[]',
                        last_seen_at text not null
                    )
                    """
                )
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
                    values('plain-token', 'merchant', 'seller-a', '', '2026-01-01T00:00:00')
                    """
                )
                conn.commit()

            with db_session(db_file) as conn:
                conversation_columns = {row["name"] for row in conn.execute("pragma table_info(conversations)").fetchall()}
                agent_columns = {row["name"] for row in conn.execute("pragma table_info(agents)").fetchall()}
                flag_columns = {row["name"] for row in conn.execute("pragma table_info(moderation_flags)").fetchall()}
                token_columns = {row["name"] for row in conn.execute("pragma table_info(api_tokens)").fetchall()}
                conversation = conn.execute("select next_actor from conversations where id = 'CONV-0001'").fetchone()
                token = conn.execute("select token, token_hash, token_prefix, token_suffix from api_tokens").fetchone()
                schema_version = conn.execute("select value from meta where key = 'schema_version'").fetchone()
                package_version = conn.execute("select value from meta where key = 'package_version'").fetchone()
                user_version = conn.execute("pragma user_version").fetchone()[0]

            self.assertIn("next_actor", conversation_columns)
            self.assertEqual(conversation["next_actor"], "merchant_agent")
            self.assertTrue({"pid", "version", "last_error", "checked_count", "replied_count"} <= agent_columns)
            self.assertTrue({"resolved_at", "resolution", "resolved_by"} <= flag_columns)
            self.assertTrue({"token_hash", "token_prefix", "token_suffix", "agent_id", "conversation_id", "revoked_at", "expires_at"} <= token_columns)
            self.assertEqual(token["token"], token_digest("plain-token"))
            self.assertEqual(token["token_hash"], token_digest("plain-token"))
            self.assertEqual(token["token_prefix"], token_prefix("plain-token"))
            self.assertEqual(token["token_suffix"], token_suffix("plain-token"))
            self.assertEqual(schema_version["value"], str(CURRENT_SCHEMA_VERSION))
            self.assertEqual(package_version["value"], VERSION)
            self.assertEqual(user_version, CURRENT_SCHEMA_VERSION)

    def test_migration_v9_dedupes_open_reuse_key_and_installs_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "legacy-v8.sqlite"
            with db_session(db_file):
                pass

            reuse_key = token_digest("alice\nseller-a\n")
            with closing(sqlite3.connect(db_file)) as raw:
                raw.execute("drop index idx_conversations_unique_open_key")
                raw.execute(
                    """
                    insert into merchants(id, name, created_at, updated_at)
                    values ('seller-a', 'Seller A', '2025-01-01T00:00:00', '2025-01-01T00:00:00')
                    """
                )
                raw.executemany(
                    """
                    insert into conversations(
                        id, buyer_id, merchant_id, sku, reuse_key, status, next_actor,
                        created_at, updated_at, last_sender
                    )
                    values (?, 'alice', 'seller-a', '', ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        # Loser: older duplicate open row sharing the reuse key.
                        ("CONV-1001", reuse_key, "waiting_merchant", "merchant_agent", "2026-01-01T00:00:00", "2026-01-01T00:00:00", "buyer"),
                        # Winner: newest non-closed row, matching ensure_conversation's reuse order.
                        ("CONV-1002", reuse_key, "open", "buyer", "2026-01-02T00:00:00", "2026-01-02T00:00:00", "buyer"),
                        # Already-closed duplicates are outside the index predicate and stay untouched.
                        ("CONV-1003", reuse_key, "closed", "", "2025-12-31T00:00:00", "2025-12-31T00:00:00", "merchant"),
                        # Empty reuse_key rows are explicit independent conversations and stay open.
                        ("CONV-1004", "", "open", "buyer", "2026-01-03T00:00:00", "2026-01-03T00:00:00", "buyer"),
                    ],
                )
                raw.execute(
                    """
                    insert into messages(conversation_id, sender, intent, text, structured_payload_json, created_at)
                    values ('CONV-1001', 'buyer', 'ask_product', 'legacy message', '{}', '2026-01-01T00:00:00')
                    """
                )
                raw.execute(
                    """
                    insert into moderation_flags(conversation_id, sku, reason, severity, created_at)
                    values ('CONV-1001', '', 'manual_confirmation', 'review', '2026-01-01T00:00:00')
                    """
                )
                raw.execute(
                    """
                    insert into audit_events(conversation_id, actor, event, details_json, created_at)
                    values ('CONV-1001', 'system', 'conversation_created', '{}', '2026-01-01T00:00:00')
                    """
                )
                raw.execute("pragma user_version = 8")
                raw.commit()

            with db_session(db_file) as conn:
                rows = {
                    row["id"]: row
                    for row in conn.execute(
                        "select id, status, next_actor, last_sender from conversations"
                    ).fetchall()
                }
                self.assertEqual(rows["CONV-1002"]["status"], "open")
                self.assertEqual(rows["CONV-1002"]["last_sender"], "buyer")
                self.assertEqual(rows["CONV-1001"]["status"], "closed")
                self.assertEqual(rows["CONV-1001"]["next_actor"], "")
                self.assertEqual(rows["CONV-1001"]["last_sender"], "system")
                self.assertEqual(rows["CONV-1003"]["status"], "closed")
                self.assertEqual(rows["CONV-1003"]["last_sender"], "merchant")
                self.assertEqual(rows["CONV-1004"]["status"], "open")

                messages = conn.execute(
                    "select text from messages where conversation_id = 'CONV-1001'"
                ).fetchall()
                self.assertEqual([row["text"] for row in messages], ["legacy message"])
                flags = conn.execute(
                    "select resolved_at from moderation_flags where conversation_id = 'CONV-1001'"
                ).fetchall()
                self.assertEqual(len(flags), 1)
                audits = conn.execute(
                    "select event, details_json from audit_events where conversation_id = 'CONV-1001' order by id"
                ).fetchall()
                self.assertEqual([row["event"] for row in audits], ["conversation_created", "conversation_closed"])
                details = json.loads(audits[1]["details_json"])
                self.assertEqual(details["reason"], "duplicate_open_reuse_key")
                self.assertEqual(details["winner_conversation_id"], "CONV-1002")

                index = conn.execute(
                    """
                    select name from sqlite_master
                    where type = 'index' and name = 'idx_conversations_unique_open_key'
                    """
                ).fetchone()
                self.assertIsNotNone(index)
                schema_version = conn.execute("select value from meta where key = 'schema_version'").fetchone()
                user_version = conn.execute("pragma user_version").fetchone()[0]

            self.assertEqual(schema_version["value"], str(CURRENT_SCHEMA_VERSION))
            self.assertEqual(user_version, CURRENT_SCHEMA_VERSION)
            self.assertEqual(CURRENT_SCHEMA_VERSION, 27)

            # The installed index enforces uniqueness for new open reuse rows.
            with self.assertRaises(sqlite3.IntegrityError) as raised:
                with db_session(db_file) as conn:
                    conn.execute(
                        """
                        insert into conversations(
                            id, buyer_id, merchant_id, sku, reuse_key, status, next_actor,
                            created_at, updated_at, last_sender
                        )
                        values ('CONV-1005', 'alice', 'seller-a', '', ?, 'open', 'buyer',
                                '2026-01-04T00:00:00', '2026-01-04T00:00:00', 'buyer')
                        """,
                        (reuse_key,),
                    )
            self.assertIn("unique", str(raised.exception).lower())

            # Reopening the migrated database is a no-op fast path.
            with db_session(db_file) as conn:
                again = conn.execute("pragma user_version").fetchone()[0]
            self.assertEqual(again, CURRENT_SCHEMA_VERSION)

    def test_v18_legacy_database_drops_catalog_tables_on_upgrade(self):
        """A v3.0 upgrade of a pre-existing v18 database removes the Agent
        Catalog / A2A / kiwi-catalog era tables while keeping core tables."""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "legacy-v18.sqlite"
            conn = sqlite3.connect(db_file)
            try:
                # 只预置 catalog-era 残留表（含用户数据）；核心表由 init_db
                # 的 SCHEMA 全量重建（IF NOT EXISTS）。
                conn.execute("create table catalog_agents (catalog_agent_id text primary key)")
                conn.execute("create table listing_publications (listing_id text primary key)")
                conn.execute("pragma user_version = 18")
                conn.commit()
            finally:
                conn.close()

            with db_session(db_file) as conn:
                tables = {
                    row[0]
                    for row in conn.execute("select name from sqlite_master where type = 'table'")
                }
                user_version = conn.execute("pragma user_version").fetchone()[0]

            self.assertNotIn("catalog_agents", tables)
            self.assertNotIn("listing_publications", tables)
            self.assertIn("moderation_flags", tables)
            self.assertIn("conversations", tables)
            self.assertEqual(user_version, CURRENT_SCHEMA_VERSION)

    def test_failed_migration_rolls_back_without_advancing_user_version(self):
        """迁移在 SAVEPOINT 内执行——中途失败回滚，user_version 不推进。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "rollback.sqlite"
            with db_session(db_file):
                pass
            # 模拟"还有待应用迁移"的库：user_version 降到 CURRENT-1
            with closing(sqlite3.connect(db_file)) as conn:
                conn.execute(f"pragma user_version = {CURRENT_SCHEMA_VERSION - 1}")
                conn.commit()

            def boom(_conn):
                raise RuntimeError("migration failed")

            with patch.object(
                migrations_module,
                "MIGRATIONS",
                (migrations_module.Migration(CURRENT_SCHEMA_VERSION, "boom", boom),),
            ):
                with self.assertRaises(RuntimeError):
                    with db_session(db_file):
                        pass

            with closing(sqlite3.connect(db_file)) as conn:
                user_version = conn.execute("pragma user_version").fetchone()[0]
            # 失败迁移未推进版本——重跑仍会尝试（可修复后重试）
            self.assertEqual(user_version, CURRENT_SCHEMA_VERSION - 1)


if __name__ == "__main__":
    unittest.main()

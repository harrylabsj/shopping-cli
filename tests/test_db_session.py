import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from shopping_cli import VERSION
from shopping_cli.db import session as session_module
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

    def test_versioned_migrations_do_not_scan_on_every_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file):
                pass

            with (
                patch.object(
                    session_module,
                    "migrate_api_tokens_to_hashes",
                    wraps=session_module.migrate_api_tokens_to_hashes,
                ) as migrate,
                patch.object(
                    session_module,
                    "backfill_conversation_next_actor",
                    wraps=session_module.backfill_conversation_next_actor,
                ) as backfill,
            ):
                with db_session(db_file):
                    pass

            self.assertEqual(migrate.call_count, 0)
            self.assertEqual(backfill.call_count, 0)

            with sqlite3.connect(db_file) as conn:
                conn.execute("update meta set value = ? where key = 'schema_version'", ("old-version",))

            with (
                patch.object(
                    session_module,
                    "migrate_api_tokens_to_hashes",
                    wraps=session_module.migrate_api_tokens_to_hashes,
                ) as migrate,
                patch.object(
                    session_module,
                    "backfill_conversation_next_actor",
                    wraps=session_module.backfill_conversation_next_actor,
                ) as backfill,
            ):
                with db_session(db_file):
                    pass

            self.assertEqual(migrate.call_count, 1)
            self.assertEqual(backfill.call_count, 1)
            with db_session(db_file) as conn:
                row = conn.execute("select value from meta where key = 'schema_version'").fetchone()
            self.assertEqual(row["value"], VERSION)


if __name__ == "__main__":
    unittest.main()

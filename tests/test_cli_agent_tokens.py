import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from helpers import run_cli as run_cli_helper
from shopping_cli.core.tokens import token_digest


class CliAgentTokenLifecycleTest(unittest.TestCase):
    def run_cli(self, db_file, *args):
        return run_cli_helper(db_file, *args, db_flag="--data")

    def test_agent_rotate_token_command_revokes_old_and_issues_new_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.run_cli(db_file, "merchant", "create", "--id", "seller-a", "--name", "West Lake Tea")
            issued = json.loads(
                self.run_cli(db_file, "agent", "token", "--merchant", "seller-a", "--format", "json")
            )
            old_token = issued["agent_token"]

            output = self.run_cli(
                db_file,
                "agent",
                "rotate-token",
                "--merchant",
                "seller-a",
                "--token",
                old_token,
                "--ttl-seconds",
                "3600",
                "--format",
                "json",
            )
            rotated = json.loads(output)

            self.assertNotIn(old_token, output)
            self.assertNotEqual(rotated["agent_token"], old_token)
            self.assertTrue(rotated["expires_at"])
            self.assertEqual(rotated["previous_token"]["token_prefix"], old_token[:24])
            conn = sqlite3.connect(db_file)
            try:
                old_row = conn.execute(
                    "select revoked_at from api_tokens where token_hash = ?",
                    (token_digest(old_token),),
                ).fetchone()
                new_row = conn.execute(
                    "select expires_at, revoked_at from api_tokens where token_hash = ?",
                    (token_digest(rotated["agent_token"]),),
                ).fetchone()
            finally:
                conn.close()
            self.assertTrue(old_row[0])
            self.assertEqual(new_row[0], rotated["expires_at"])
            self.assertEqual(new_row[1], "")

    def test_agent_rotate_token_command_rejects_oversized_ttl_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.run_cli(db_file, "merchant", "create", "--id", "seller-a", "--name", "West Lake Tea")
            issued = json.loads(
                self.run_cli(db_file, "agent", "token", "--merchant", "seller-a", "--format", "json")
            )

            with self.assertRaises(SystemExit) as raised:
                self.run_cli(
                    db_file,
                    "agent",
                    "rotate-token",
                    "--merchant",
                    "seller-a",
                    "--token",
                    issued["agent_token"],
                    "--ttl-seconds",
                    str(10**100),
                    "--format",
                    "json",
                )
            self.assertIn("ttl_seconds is too large", str(raised.exception))

    def test_agent_token_cli_lifecycle_records_audit_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.run_cli(db_file, "merchant", "create", "--id", "seller-a", "--name", "West Lake Tea")
            issued = json.loads(
                self.run_cli(db_file, "agent", "token", "--merchant", "seller-a", "--ttl-seconds", "3600", "--format", "json")
            )
            old_token = issued["agent_token"]
            rotated = json.loads(
                self.run_cli(
                    db_file,
                    "agent",
                    "rotate-token",
                    "--merchant",
                    "seller-a",
                    "--token",
                    old_token,
                    "--ttl-seconds",
                    "7200",
                    "--format",
                    "json",
                )
            )
            new_token = rotated["agent_token"]
            revoked = json.loads(
                self.run_cli(
                    db_file,
                    "agent",
                    "revoke-token",
                    "--merchant",
                    "seller-a",
                    "--token",
                    new_token,
                    "--format",
                    "json",
                )
            )

            conn = sqlite3.connect(db_file)
            try:
                rows = conn.execute(
                    "select actor, event, details_json from audit_events where conversation_id = '' order by id"
                ).fetchall()
            finally:
                conn.close()
            # catalog 写操作审计（v3.0）也落在 conversation_id='' 域——这里只
            # 断言 agent token 生命周期事件按序记录且无 secrets。
            token_events = [row[1] for row in rows if row[1].startswith("agent_token_")]
            self.assertEqual(token_events, ["agent_token_issued", "agent_token_rotated", "agent_token_revoked"])
            self.assertTrue(all(row[0] == "seller-a" for row in rows))
            serialized = json.dumps([json.loads(row[2]) for row in rows], sort_keys=True)
            self.assertNotIn(old_token, serialized)
            self.assertNotIn(new_token, serialized)
            self.assertIn(issued["agent_id"], serialized)
            self.assertIn(revoked["revoked_at"], serialized)


if __name__ == "__main__":
    unittest.main()

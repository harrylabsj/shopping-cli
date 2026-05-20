import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MAI = ROOT / "scripts" / "shopping.py"
sys.path.insert(0, str(ROOT / "scripts"))

import shopping  # noqa: E402


PUBLIC_SUBCOMMANDS = [
    ("merchant", "create"),
    ("merchant", "list"),
    ("merchant", "update"),
    ("merchant", "human-review"),
    ("delivery", "set"),
    ("product", "add"),
    ("product", "stock"),
    ("product", "update"),
    ("search", "products"),
    ("search", "merchants"),
    ("channel", "ingest"),
    ("buyer", "ask"),
    ("buyer", "summarize"),
    ("buyer", "intent"),
    ("buyer", "chat"),
    ("conversation", "create"),
    ("conversation", "show"),
    ("conversation", "list"),
    ("conversation", "message"),
    ("conversation", "close"),
    ("conversation", "human-review"),
    ("conversation", "resolve-review"),
    ("agent", "start"),
    ("agent", "stop"),
    ("agent", "status"),
    ("agent", "logs"),
    ("agent", "list"),
    ("agent", "show"),
    ("agent", "run"),
    ("agent", "heartbeat"),
    ("agent", "token"),
    ("agent", "tokens"),
    ("agent", "rotate-token"),
    ("agent", "revoke-token"),
    ("human-review", "queue"),
    ("human-review", "show"),
    ("human-review", "resolve"),
    ("audit", "events"),
    ("llm", "run"),
    ("adapter", "inspect"),
    ("adapter", "doctor"),
    ("adapter", "install-command"),
    ("legacy", "import"),
    ("api", "routes"),
    ("api", "serve"),
]

TOP_LEVEL_CHOICES = "{merchant,delivery,product,search,channel,buyer,conversation,agent,human-review,llm,adapter,legacy,api}"


class CliContractTest(unittest.TestCase):
    def run_cli(self, *args):
        output = StringIO()
        with redirect_stdout(output):
            shopping.main(list(args))
        return output.getvalue()

    def parse_single_json_value(self, output):
        decoder = json.JSONDecoder()
        value, end = decoder.raw_decode(output)
        self.assertEqual(output[end:].strip(), "")
        return value

    def test_nested_help_for_every_public_subcommand_shows_subcommand_options(self):
        for command in PUBLIC_SUBCOMMANDS:
            with self.subTest(command=" ".join(command)):
                result = subprocess.run(
                    [sys.executable, str(MAI), *command, "--help"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)
                self.assertIn("--help", result.stdout)
                self.assertNotIn(TOP_LEVEL_CHOICES, result.stdout)

    def test_one_shot_json_output_is_a_single_json_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            created = self.parse_single_json_value(
                self.run_cli(
                    "--db",
                    str(db_file),
                    "merchant",
                    "create",
                    "--id",
                    "seller-a",
                    "--name",
                    "West Lake Tea",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(created["merchant"]["id"], "seller-a")

            listed = self.parse_single_json_value(
                self.run_cli("--db", str(db_file), "merchant", "list", "--format", "json")
            )
            self.assertEqual(listed["results"][0]["id"], "seller-a")

    def test_chat_json_output_is_one_json_object_per_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.run_cli("--db", str(db_file), "merchant", "create", "--id", "seller-a", "--name", "West Lake Tea")
            self.run_cli(
                "--db",
                str(db_file),
                "product",
                "add",
                "--merchant",
                "seller-a",
                "--sku",
                "tea-a",
                "--title",
                "Longjing Gift Box",
                "--price",
                "88",
                "--stock",
                "5",
                "--tags",
                "longjing",
            )

            output = StringIO()
            with patch("sys.stdin", StringIO("longjing delivery today\n/quit\n")), redirect_stdout(output):
                shopping.main(
                    [
                        "--db",
                        str(db_file),
                        "buyer",
                        "chat",
                        "--buyer",
                        "alice",
                        "--format",
                        "json",
                    ]
                )

            events = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([event["event"] for event in events], ["ask", "quit"])

    def test_db_and_legacy_data_flags_resolve_to_database_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "data-flag.sqlite"
            self.run_cli("--data", str(db_file), "merchant", "create", "--id", "seller-a", "--name", "West Lake Tea")
            listed = self.parse_single_json_value(
                self.run_cli("--db", str(db_file), "merchant", "list", "--format", "json")
            )
            self.assertEqual(listed["results"][0]["id"], "seller-a")

    def test_agent_command_db_alias_takes_precedence_over_global_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            global_db = Path(tmp) / "global.sqlite"
            agent_db = Path(tmp) / "agent.sqlite"
            self.run_cli("--db", str(agent_db), "merchant", "create", "--id", "seller-a", "--name", "West Lake Tea")

            heartbeat = self.parse_single_json_value(
                self.run_cli(
                    "--db",
                    str(global_db),
                    "agent",
                    "heartbeat",
                    "--merchant",
                    "seller-a",
                    "--db",
                    str(agent_db),
                    "--format",
                    "json",
                )
            )

            self.assertEqual(heartbeat["agent"]["owner_id"], "seller-a")
            self.assertFalse(global_db.exists())
            conn = sqlite3.connect(agent_db)
            try:
                count = conn.execute("select count(*) from agents").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()

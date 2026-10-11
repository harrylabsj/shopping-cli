from __future__ import annotations
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from shopping_cli import cli
from shopping_cli.core import catalog
from shopping_cli.db.session import db_session

TOKEN = "shopping_merchant_dummy_a423_canary"
AUTH = "DUMMY-AUTH-CANARY-A423"


class RuntimeOutputTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "db.sqlite"

    def actual_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--db", str(self.path), *args])
        return out.getvalue()

    def test_actual_run_json_and_text_redact_error_token_without_changing_counts(self):
        result = {
            "merchant_id": "m",
            "checked": 1,
            "replied": [],
            "abandoned": [],
            "failed": [
                {"conversation_id": "c", "message_id": 1, "error": f"backend rejected Bearer {AUTH} and {TOKEN}"}
            ],
        }
        for format in ("json", "text"):
            with (
                self.subTest(format=format),
                mock.patch("shopping_cli.agents.merchant_agent.process_once", return_value=result),
            ):
                output = self.actual_cli("agent", "run", "--merchant", "m", "--once", "--format", format)
                self.assertNotIn(TOKEN, output)
                self.assertNotIn(AUTH, output)
                if format == "json":
                    parsed = json.loads(output)
                    self.assertEqual(parsed["checked"], 1)
                    self.assertEqual(parsed["failed"][0]["message_id"], 1)

    def test_actual_status_and_logs_json_redact_nested_credential_fields_and_plain_errors(self):
        fixtures = [
            (
                "status",
                "status_agent",
                {
                    "merchant_id": "m",
                    "running": False,
                    "last_error": f"invalid {TOKEN}",
                    "nested": {"authorization": AUTH},
                },
            ),
            (
                "logs",
                "logs_agent",
                {
                    "merchant_id": "m",
                    "entries": [{"text": f"Bearer {AUTH}", "error": TOKEN}],
                    "nested": {"auth_token": AUTH},
                },
            ),
        ]
        for action, method, result in fixtures:
            with (
                self.subTest(action=action),
                mock.patch("shopping_cli.agents.merchant_daemon." + method, return_value=result),
            ):
                output = self.actual_cli("agent", action, "--merchant", "m", "--format", "json")
                self.assertNotIn(TOKEN, output)
                self.assertNotIn(AUTH, output)
                self.assertEqual(json.loads(output)["merchant_id"], "m")

    def test_explicit_first_agent_token_issue_is_not_redacted_by_runtime_output_guard(self):
        with db_session(self.path) as c:
            catalog.create_merchant(c, merchant_id="m", name="Synthetic")
        output = self.actual_cli("agent", "token", "--merchant", "m", "--format", "json")
        parsed = json.loads(output)
        self.assertTrue(parsed["agent_token"].startswith("shopping_agent_"))
        self.assertIn(parsed["agent_token"], parsed["message"])

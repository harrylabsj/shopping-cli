"""CLI tests for `shopping-cli agent catalog {search,get}`."""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from helpers import run_cli as run_cli_helper

from shopping_cli.agent_catalog.sqlite_repository import upsert_catalog_agent
from shopping_cli.db.session import now_iso


def _seed_merchant(raw: sqlite3.Connection, merchant_id: str, name: str) -> None:
    raw.row_factory = sqlite3.Row
    raw.execute("pragma foreign_keys = on")
    ts = now_iso()
    try:
        raw.execute(
            """
            insert into merchants(id, name, city, service_area, contact, hours,
                automation_boundaries, tags_json, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)
            """,
            (merchant_id, name, "Hangzhou", "West Lake", "test@example.com",
             "9-5", "none", ts, ts),
        )
        raw.commit()
    except sqlite3.IntegrityError:
        pass


def _seed_catalog_agent(
    raw: sqlite3.Connection,
    catalog_agent_id: str,
    merchant_id: str = "",
    display_name: str = "",
    verification_status: str = "discovered",
    hosting_mode: str = "hosted",
    last_verified_at: str = "",
) -> None:
    raw.row_factory = sqlite3.Row
    # source_type must match CHECK constraint: hosted, self_registered, discovered, imported, admin_curated
    source_type = "hosted" if hosting_mode == "hosted" else "discovered"
    upsert_catalog_agent(
        raw,
        catalog_agent_id=catalog_agent_id,
        merchant_id=merchant_id,
        display_name=display_name,
        source_type=source_type,
        verification_status=verification_status,
        hosting_mode=hosting_mode,
    )
    if last_verified_at:
        raw.execute(
            "update catalog_agents set last_verified_at = ? where catalog_agent_id = ?",
            (last_verified_at, catalog_agent_id),
        )
    raw.commit()


class CliAgentCatalogSearchTest(unittest.TestCase):
    def run_cli(self, db_file, *args):
        return run_cli_helper(db_file, *args, db_flag="--data")

    def _init_db(self, db_file: Path) -> None:
        from shopping_cli.db.session import db_session
        with db_session(db_file):
            pass

    def test_search_no_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)

            output = self.run_cli(db_file, "agent", "catalog", "search")
            self.assertIn("No catalog agents found.", output)

    def test_search_returns_agents_in_text_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_merchant(raw, "mrc-alpha", "Alpha Electronics")
                _seed_catalog_agent(
                    raw,
                    "cagt_test-alpha",
                    merchant_id="mrc-alpha",
                    display_name="Alpha Electronics",
                    verification_status="commerce_verified",
                    hosting_mode="hosted",
                    last_verified_at="2026-08-01T12:00:00",
                )

            output = self.run_cli(db_file, "agent", "catalog", "search")

            self.assertIn("CATALOG_AGENT_ID", output)
            self.assertIn("cagt_test-alpha", output)
            self.assertIn("Alpha Electronics", output)
            self.assertIn("commerce_verified", output)
            self.assertIn("2026-08-01T12:00:00", output)
            self.assertIn("hosted", output)
            self.assertNotIn('"results"', output)

    def test_search_json_output_is_parseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_merchant(raw, "mrc-alpha", "Alpha Electronics")
                _seed_catalog_agent(
                    raw,
                    "cagt_test-alpha",
                    merchant_id="mrc-alpha",
                    display_name="Alpha Electronics",
                    verification_status="commerce_verified",
                    hosting_mode="hosted",
                )

            output = self.run_cli(
                db_file, "agent", "catalog", "search", "--format", "json",
            )
            data = json.loads(output)

            self.assertTrue(data["ok"])
            self.assertIsInstance(data["results"], list)
            self.assertGreaterEqual(len(data["results"]), 1)
            agent = data["results"][0]
            self.assertEqual(agent["catalog_agent_id"], "cagt_test-alpha")
            self.assertIn("verification", agent)
            self.assertIn("hosting", agent)
            self.assertIn("merchant", agent)

    def test_search_supports_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_merchant(raw, "mrc-a", "Alpha")
                _seed_merchant(raw, "mrc-b", "Beta")
                _seed_catalog_agent(
                    raw, "cagt_a", merchant_id="mrc-a", display_name="Alpha",
                    verification_status="commerce_verified", hosting_mode="hosted",
                )
                _seed_catalog_agent(
                    raw, "cagt_b", merchant_id="mrc-b", display_name="Beta",
                    verification_status="discovered", hosting_mode="direct",
                )

            # Filter by hosting_mode
            out1 = self.run_cli(
                db_file, "agent", "catalog", "search",
                "--hosting-mode", "hosted", "--format", "json",
            )
            data1 = json.loads(out1)
            ids1 = [r["catalog_agent_id"] for r in data1["results"]]
            self.assertIn("cagt_a", ids1)
            self.assertNotIn("cagt_b", ids1)

            # Filter by verification_status
            out2 = self.run_cli(
                db_file, "agent", "catalog", "search",
                "--verification-status", "discovered", "--format", "json",
            )
            data2 = json.loads(out2)
            ids2 = [r["catalog_agent_id"] for r in data2["results"]]
            self.assertIn("cagt_b", ids2)
            self.assertNotIn("cagt_a", ids2)

    def test_search_respects_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_merchant(raw, "mrc-x", "Test Merchant")
                for i in range(5):
                    _seed_catalog_agent(
                        raw, f"cagt_{i}", merchant_id="mrc-x",
                        display_name=f"Agent {i}",
                    )

            output = self.run_cli(
                db_file, "agent", "catalog", "search",
                "--limit", "2", "--format", "json",
            )
            data = json.loads(output)
            self.assertEqual(len(data["results"]), 2)


class CliAgentCatalogGetTest(unittest.TestCase):
    def run_cli(self, db_file, *args):
        return run_cli_helper(db_file, *args, db_flag="--data")

    def _init_db(self, db_file: Path) -> None:
        from shopping_cli.db.session import db_session
        with db_session(db_file):
            pass

    def test_get_known_agent_text_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_merchant(raw, "mrc-alpha", "Alpha Electronics")
                _seed_catalog_agent(
                    raw,
                    "cagt_test-alpha",
                    merchant_id="mrc-alpha",
                    display_name="Alpha Electronics",
                    verification_status="commerce_verified",
                    hosting_mode="hosted",
                    last_verified_at="2026-08-05T09:00:00",
                )

            output = self.run_cli(
                db_file, "agent", "catalog", "get", "cagt_test-alpha",
            )

            self.assertIn("Catalog Agent:", output)
            self.assertIn("cagt_test-alpha", output)
            self.assertIn("Alpha Electronics", output)
            self.assertIn("mrc-alpha", output)
            self.assertIn("commerce_verified", output)
            self.assertIn("2026-08-05T09:00:00", output)
            self.assertIn("Hosting Mode:", output)
            self.assertIn("hosted", output)
            self.assertNotIn('"agent"', output)

    def test_get_known_agent_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_merchant(raw, "mrc-alpha", "Alpha Electronics")
                _seed_catalog_agent(
                    raw,
                    "cagt_test-alpha",
                    merchant_id="mrc-alpha",
                    display_name="Alpha Electronics",
                    verification_status="commerce_verified",
                    hosting_mode="hosted",
                )

            output = self.run_cli(
                db_file, "agent", "catalog", "get", "cagt_test-alpha",
                "--format", "json",
            )
            data = json.loads(output)

            self.assertTrue(data["ok"])
            agent = data["agent"]
            self.assertEqual(agent["catalog_agent_id"], "cagt_test-alpha")
            self.assertEqual(agent["verification"]["status"], "commerce_verified")
            self.assertEqual(agent["hosting"]["mode"], "hosted")

    def test_get_unknown_agent_exits_with_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)

            with self.assertRaises(SystemExit) as raised:
                self.run_cli(db_file, "agent", "catalog", "get", "cagt_nonexistent")
            self.assertIn("cagt_nonexistent", str(raised.exception))


class CliAgentCatalogHelpTest(unittest.TestCase):
    """Verify help text surfaces the new agent catalog commands — use
    ArgumentParser.format_help() to avoid argparse's --help exit(0)."""

    def test_agent_help_includes_catalog(self):
        from shopping_cli.cli import build_parser

        parser = build_parser()
        # Navigate to the 'agent' subparser
        agent_parser = None
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                agent_parser = action.choices.get("agent")
                break

        self.assertIsNotNone(agent_parser, "agent subparser not found")
        help_text = agent_parser.format_help()
        self.assertIn("catalog", help_text)

    def test_agent_catalog_help_lists_search_and_get(self):
        from shopping_cli.cli import build_parser

        parser = build_parser()
        agent_parser = None
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                agent_parser = action.choices.get("agent")
                break

        catalog_parser = None
        for action in agent_parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                catalog_parser = action.choices.get("catalog")
                break

        self.assertIsNotNone(catalog_parser, "agent catalog subparser not found")
        help_text = catalog_parser.format_help()
        self.assertIn("search", help_text)
        self.assertIn("get", help_text)

    def test_agent_catalog_search_help_includes_filter_options(self):
        from shopping_cli.cli import build_parser

        parser = build_parser()
        agent_parser = None
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                agent_parser = action.choices.get("agent")
                break

        catalog_parser = None
        for action in agent_parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                catalog_parser = action.choices.get("catalog")
                break

        search_parser = None
        for action in catalog_parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                search_parser = action.choices.get("search")
                break

        self.assertIsNotNone(search_parser, "agent catalog search subparser not found")
        help_text = search_parser.format_help()
        self.assertIn("--q", help_text)
        self.assertIn("--capability", help_text)
        self.assertIn("--protocol", help_text)
        self.assertIn("--hosting-mode", help_text)
        self.assertIn("--verification-status", help_text)
        self.assertIn("--format", help_text)
        self.assertIn("--limit", help_text)

"""CLI tests for `shopping-cli agent catalog {search,get,register,verify,refresh,claim}`.

Search/get are read-only (§10.1); register (§10.2), verify/refresh (§10.3) and
claim (§10.4) are the v2.2 write commands.  All network I/O in the write tests
is mocked — the verification service and the HTTPS domain-control identity
verifier are replaced by fakes, so no test ever touches the wire.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    canonical_domain: str = "",
    last_verified_at: str = "",
    source_type: str | None = None,
) -> None:
    raw.row_factory = sqlite3.Row
    # source_type must match CHECK constraint: hosted, self_registered, discovered, imported, admin_curated
    if source_type is None:
        source_type = "hosted" if hosting_mode == "hosted" else "discovered"
    upsert_catalog_agent(
        raw,
        catalog_agent_id=catalog_agent_id,
        merchant_id=merchant_id,
        display_name=display_name,
        canonical_domain=canonical_domain,
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


# ── v2.2 write commands (§10.2–§10.4) ───────────────────────────────────────
# All network I/O is mocked: the verification service and the HTTPS
# domain-control identity verifier are replaced by fakes, so no CLI write test
# ever touches the wire.

def _fake_stage(
    stage: str = "profile",
    outcome: str = "passed",
    target: str = "profile_valid",
    reason: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        stage=stage,
        outcome=outcome,
        target_status=target,
        reason=reason,
        verification_id=1,
        snapshot_ids=(1,),
    )


def _fake_verify_result(
    catalog_agent_id: str,
    status: str = "domain_verified",
    previous: str = "discovered",
) -> SimpleNamespace:
    return SimpleNamespace(
        catalog_agent_id=catalog_agent_id,
        previous_status=previous,
        status=status,
        stages=[_fake_stage()],
    )


class FakeVerificationService:
    """Records verify/refresh calls and returns a canned §6 ladder result."""

    def __init__(self, result: SimpleNamespace) -> None:
        self.result = result
        self.verify_calls: list[tuple[str, str, bool]] = []
        self.refresh_calls: list[tuple[str, str]] = []

    def verify(self, catalog_agent_id: str, *, actor: str = "verification_worker", force: bool = False) -> SimpleNamespace:
        self.verify_calls.append((catalog_agent_id, actor, force))
        return self.result

    def refresh(self, catalog_agent_id: str, *, actor: str = "verification_worker") -> SimpleNamespace:
        self.refresh_calls.append((catalog_agent_id, actor))
        return self.result


class FakeIdentityVerifier:
    """Domain-control stand-in; ``passed=False`` models a failed challenge."""

    def __init__(self, passed: bool = True) -> None:
        self.passed = passed
        self.domains: list[str] = []

    def verify_domain_control(self, canonical_domain: str, declared: dict | None = None) -> SimpleNamespace:
        self.domains.append(canonical_domain)
        if self.passed:
            return SimpleNamespace(passed=True, reason="https domain control verified")
        return SimpleNamespace(passed=False, reason="domain control challenge failed")


class CliAgentCatalogWriteTest(unittest.TestCase):
    def run_cli(self, db_file, *args):
        return run_cli_helper(db_file, *args, db_flag="--data")

    def _init_db(self, db_file: Path) -> None:
        from shopping_cli.db.session import db_session
        with db_session(db_file):
            pass

    def _audit_events(self, db_file: Path) -> list[tuple[str, str, dict]]:
        with closing(sqlite3.connect(db_file)) as raw:
            raw.row_factory = sqlite3.Row
            rows = raw.execute(
                "select event, actor, details_json from audit_events order by id"
            ).fetchall()
        return [(r["event"], r["actor"], json.loads(r["details_json"] or "{}")) for r in rows]

    def _catalog_agent(self, db_file: Path, catalog_agent_id: str) -> dict:
        from shopping_cli.agent_catalog.sqlite_repository import require_catalog_agent
        from shopping_cli.db.session import db_session
        with db_session(db_file) as conn:
            return dict(require_catalog_agent(conn, catalog_agent_id))

    # ── register (§10.2) ────────────────────────────────────────────────────

    def test_register_creates_discovered_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)

            output = self.run_cli(
                db_file, "agent", "catalog", "register", "--domain", "merchant.example",
            )

            self.assertIn("Registered catalog agent: cagt_", output)
            self.assertIn("Domain: merchant.example", output)
            self.assertIn("Verification Status: discovered", output)

            events = [e for e in self._audit_events(db_file) if e[0] == "catalog_agent_registered"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][1], "cli")
            self.assertEqual(events[0][2]["canonical_domain"], "merchant.example")

    def test_register_json_output_is_parseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)

            output = self.run_cli(
                db_file, "agent", "catalog", "register",
                "--domain", "merchant.example",
                "--agent-card-url", "https://merchant.example/agent-card.json",
                "--format", "json",
            )
            data = json.loads(output)

            self.assertTrue(data["ok"])
            agent = data["catalog_agent"]
            self.assertEqual(agent["canonical_domain"], "merchant.example")
            self.assertEqual(agent["source_type"], "self_registered")
            self.assertEqual(agent["verification"]["status"], "discovered")

    def test_register_rejects_url_domain(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)

            with self.assertRaises(SystemExit) as raised:
                self.run_cli(
                    db_file, "agent", "catalog", "register",
                    "--domain", "https://merchant.example",
                )
            self.assertIn("invalid canonical domain", str(raised.exception))

    def test_register_with_merchant_binding_uses_admin_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_merchant(raw, "mrc-cli-reg", "CLI Register Merchant")

            output = self.run_cli(
                db_file, "agent", "catalog", "register",
                "--domain", "merchant.example",
                "--merchant-id", "mrc-cli-reg",
                "--admin-token", "some-admin-token",
            )
            data_agent_id = output.split("Catalog Agent: ")[1].split()[0]
            agent = self._catalog_agent(db_file, data_agent_id)
            self.assertEqual(agent["merchant_id"], "mrc-cli-reg")

            events = [e for e in self._audit_events(db_file) if e[0] == "catalog_agent_registered"]
            self.assertEqual(events[-1][1], "admin")

    # ── verify (§10.3) ──────────────────────────────────────────────────────

    def test_verify_reports_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_catalog_agent(raw, "cagt_cli_verify", hosting_mode="direct")

            fake = FakeVerificationService(
                _fake_verify_result("cagt_cli_verify", status="domain_verified")
            )
            with patch("shopping_cli.cli_agent_catalog_commands.VerificationService", return_value=fake):
                output = self.run_cli(db_file, "agent", "catalog", "verify", "cagt_cli_verify")

            self.assertIn("Verification Status: domain_verified (was discovered)", output)
            self.assertEqual(fake.verify_calls[0][0], "cagt_cli_verify")

    def test_verify_passes_force_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_catalog_agent(raw, "cagt_cli_verify", hosting_mode="direct")

            fake = FakeVerificationService(_fake_verify_result("cagt_cli_verify"))
            with patch("shopping_cli.cli_agent_catalog_commands.VerificationService", return_value=fake):
                self.run_cli(db_file, "agent", "catalog", "verify", "cagt_cli_verify", "--force")

            self.assertTrue(fake.verify_calls[0][2])

    def test_verify_json_output_is_parseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_catalog_agent(raw, "cagt_cli_verify", hosting_mode="direct")

            fake = FakeVerificationService(
                _fake_verify_result("cagt_cli_verify", status="domain_verified")
            )
            with patch("shopping_cli.cli_agent_catalog_commands.VerificationService", return_value=fake):
                output = self.run_cli(
                    db_file, "agent", "catalog", "verify", "cagt_cli_verify", "--format", "json",
                )
            data = json.loads(output)

            self.assertTrue(data["ok"])
            self.assertEqual(data["catalog_agent_id"], "cagt_cli_verify")
            self.assertEqual(data["verification_status"], "domain_verified")
            self.assertEqual(data["previous_status"], "discovered")
            self.assertEqual(data["stages"][0]["stage"], "profile")

    # ── refresh (§10.3) ─────────────────────────────────────────────────────

    def test_refresh_reports_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_catalog_agent(raw, "cagt_cli_refresh", hosting_mode="direct")

            fake = FakeVerificationService(
                _fake_verify_result("cagt_cli_refresh", status="commerce_verified", previous="domain_verified")
            )
            with patch("shopping_cli.cli_agent_catalog_commands.VerificationService", return_value=fake):
                output = self.run_cli(db_file, "agent", "catalog", "refresh", "cagt_cli_refresh")

            self.assertIn("Verification Status: commerce_verified (was domain_verified)", output)
            self.assertEqual(fake.refresh_calls[0][0], "cagt_cli_refresh")

    def test_refresh_json_output_is_parseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_catalog_agent(raw, "cagt_cli_refresh", hosting_mode="direct")

            fake = FakeVerificationService(
                _fake_verify_result("cagt_cli_refresh", status="domain_verified")
            )
            with patch("shopping_cli.cli_agent_catalog_commands.VerificationService", return_value=fake):
                output = self.run_cli(
                    db_file, "agent", "catalog", "refresh", "cagt_cli_refresh", "--format", "json",
                )
            data = json.loads(output)

            self.assertTrue(data["ok"])
            self.assertEqual(data["verification_status"], "domain_verified")

    # ── claim (§10.4, §6.2) ─────────────────────────────────────────────────

    def test_claim_self_registered_challenge_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_merchant(raw, "mrc-cli-claim", "CLI Claim Merchant")
                _seed_catalog_agent(
                    raw, "cagt_cli_claim", hosting_mode="direct", canonical_domain="merchant.example",
                )

            fake = FakeIdentityVerifier(passed=True)
            with patch(
                "shopping_cli.services.agent_catalog_writes._default_identity_verifier",
                return_value=fake,
            ):
                output = self.run_cli(
                    db_file, "agent", "catalog", "claim", "cagt_cli_claim",
                    "--merchant-id", "mrc-cli-claim",
                )

            self.assertIn("Claimed catalog agent: cagt_cli_claim", output)
            self.assertIn("mrc-cli-claim", output)
            self.assertEqual(fake.domains, ["merchant.example"])
            agent = self._catalog_agent(db_file, "cagt_cli_claim")
            self.assertEqual(agent["merchant_id"], "mrc-cli-claim")

            events = [e for e in self._audit_events(db_file) if e[0] == "catalog_agent_claimed"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0][2]["claim_method"], "https_domain_control")
            self.assertEqual(events[0][2]["merchant_id"], "mrc-cli-claim")

    def test_claim_challenge_failure_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_merchant(raw, "mrc-cli-claim", "CLI Claim Merchant")
                _seed_catalog_agent(
                    raw, "cagt_cli_claim", hosting_mode="direct", canonical_domain="merchant.example",
                )

            fake = FakeIdentityVerifier(passed=False)
            with patch(
                "shopping_cli.services.agent_catalog_writes._default_identity_verifier",
                return_value=fake,
            ):
                with self.assertRaises(SystemExit) as raised:
                    self.run_cli(
                        db_file, "agent", "catalog", "claim", "cagt_cli_claim",
                        "--merchant-id", "mrc-cli-claim",
                    )
            self.assertIn("claim denied", str(raised.exception))

            # The merchant binding must not have changed.
            agent = self._catalog_agent(db_file, "cagt_cli_claim")
            self.assertEqual(agent["merchant_id"] or "", "")

    def test_claim_hosted_agent_uses_identity_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_merchant(raw, "mrc-cli-claim", "CLI Claim Merchant")
                _seed_catalog_agent(
                    raw, "cagt_hosted_cli", merchant_id="mrc-cli-claim",
                    hosting_mode="hosted", canonical_domain="merchant.example",
                )

            fake = FakeIdentityVerifier(passed=True)
            with patch(
                "shopping_cli.services.agent_catalog_writes._default_identity_verifier",
                return_value=fake,
            ):
                self.run_cli(
                    db_file, "agent", "catalog", "claim", "cagt_hosted_cli",
                    "--merchant-id", "mrc-cli-claim",
                )

            # Hosted identity is proof — no HTTPS domain-control challenge.
            self.assertEqual(fake.domains, [])
            events = [e for e in self._audit_events(db_file) if e[0] == "catalog_agent_claimed"]
            self.assertEqual(events[-1][2]["claim_method"], "hosted_identity")

    def test_claim_unknown_agent_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)

            with self.assertRaises(SystemExit) as raised:
                self.run_cli(
                    db_file, "agent", "catalog", "claim", "cagt_missing",
                    "--merchant-id", "mrc-cli-claim",
                )
            self.assertIn("Unknown catalog agent", str(raised.exception))

    def test_write_commands_help_lists_register_refresh_verify_claim(self):
        from shopping_cli.cli import build_parser

        parser = build_parser()
        agent_parser = None
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                agent_parser = action.choices.get("agent")
                break
        self.assertIsNotNone(agent_parser, "agent subparser not found")

        catalog_parser = None
        for action in agent_parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                catalog_parser = action.choices.get("catalog")
                break
        self.assertIsNotNone(catalog_parser, "agent catalog subparser not found")
        help_text = catalog_parser.format_help()
        for cmd in ("register", "refresh", "verify", "claim"):
            self.assertIn(cmd, help_text)


def _run_captured(db_file: Path, *args: str) -> tuple[str, int]:
    """Run the CLI, capturing stdout and the exit code (SystemExit is caught)."""
    from contextlib import redirect_stdout
    from io import StringIO

    import shopping

    output = StringIO()
    try:
        with redirect_stdout(output):
            shopping.main(["--data", str(db_file), *args])
    except SystemExit as exc:
        code = exc.code
        return output.getvalue(), (0 if code is None else int(code))
    return output.getvalue(), 0


def _seed_expired_snapshot(raw: sqlite3.Connection, catalog_agent_id: str) -> None:
    raw.row_factory = sqlite3.Row
    raw.execute("pragma foreign_keys = on")
    raw.execute(
        """
        insert into agent_profile_snapshots(
            catalog_agent_id, profile_type, fetched_at, fresh_until, validation_status
        ) values (?, 'agent_card', ?, '2020-01-01T00:00:00', 'valid')
        """,
        (catalog_agent_id, now_iso()),
    )
    raw.commit()


class CliAgentCatalogStatsDoctorTest(unittest.TestCase):
    """CLI tests for `shopping-cli agent catalog {stats,doctor}` (§24)."""

    def _init_db(self, db_file: Path) -> None:
        from shopping_cli.db.session import db_session
        with db_session(db_file):
            pass

    def _seed(self, db_file: Path) -> None:
        with closing(sqlite3.connect(db_file)) as raw:
            _seed_merchant(raw, "mrc-stats", "Stats Merchant")
            _seed_catalog_agent(
                raw,
                "cagt_stats_verified",
                merchant_id="mrc-stats",
                display_name="Verified",
                verification_status="commerce_verified",
                hosting_mode="hosted",
                canonical_domain="verified.example",
            )
            _seed_catalog_agent(
                raw,
                "cagt_stats_stale",
                merchant_id="mrc-stats",
                display_name="Stale",
                verification_status="stale",
                hosting_mode="direct",
                canonical_domain="stale.example",
            )
            _seed_catalog_agent(
                raw,
                "cagt_stats_unverified",
                merchant_id="",
                display_name="Unverified",
                verification_status="discovered",
                hosting_mode="direct",
                canonical_domain="unverified.example",
                source_type="self_registered",
            )

    # ── stats ────────────────────────────────────────────────────────────────

    def test_stats_text_output_reports_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            self._seed(db_file)

            output, exit_code = _run_captured(db_file, "agent", "catalog", "stats")

        self.assertEqual(exit_code, 0)
        self.assertIn("Catalog agents:        3", output)
        self.assertIn("Verified agents:       1", output)
        self.assertIn("Stale agents:          1", output)
        self.assertIn("commerce_verified", output)
        self.assertIn("self_registered", output)

    def test_stats_json_output_is_parseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            self._seed(db_file)

            output, exit_code = _run_captured(
                db_file, "agent", "catalog", "stats", "--format", "json",
            )

        self.assertEqual(exit_code, 0)
        data = json.loads(output)
        self.assertTrue(data["ok"])
        self.assertEqual(data["catalog_agent_count"], 3)
        self.assertEqual(data["verified_agent_count"], 1)
        self.assertEqual(data["verification_status_distribution"], {
            "commerce_verified": 1,
            "discovered": 1,
            "stale": 1,
        })

    def test_stats_empty_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)

            output, exit_code = _run_captured(db_file, "agent", "catalog", "stats")

        self.assertEqual(exit_code, 0)
        self.assertIn("Catalog agents:        0", output)

    # ── doctor ───────────────────────────────────────────────────────────────

    def test_doctor_empty_catalog_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)

            output, exit_code = _run_captured(db_file, "agent", "catalog", "doctor")

        self.assertEqual(exit_code, 0)
        self.assertIn("Health: OK", output)

    def test_doctor_reports_issues_and_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            self._seed(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_expired_snapshot(raw, "cagt_stats_verified")

            output, exit_code = _run_captured(db_file, "agent", "catalog", "doctor")

        self.assertEqual(exit_code, 1)
        self.assertIn("Stale agents", output)
        self.assertIn("[ISSUE] 1 stale agent(s)", output)
        self.assertIn("[ISSUE] 1 unverified registration(s)", output)
        self.assertIn("[ISSUE] 1 expired profile snapshot(s)", output)
        self.assertIn("Health: 3 issue(s) found", output)

    def test_doctor_json_output_is_parseable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self._init_db(db_file)
            self._seed(db_file)
            with closing(sqlite3.connect(db_file)) as raw:
                _seed_expired_snapshot(raw, "cagt_stats_verified")

            output, exit_code = _run_captured(
                db_file, "agent", "catalog", "doctor", "--format", "json",
            )

        self.assertEqual(exit_code, 1)
        data = json.loads(output)
        self.assertFalse(data["healthy"])
        self.assertEqual(data["stale_agents"], 1)
        self.assertEqual(data["unverified_registrations"], 1)
        self.assertEqual(data["expired_profile_snapshots"], 1)
        self.assertIn("1 stale agent(s)", data["issues"])

    def test_doctor_help_lists_stats_and_doctor(self):
        from shopping_cli.cli import build_parser

        parser = build_parser()
        agent_parser = None
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                agent_parser = action.choices.get("agent")
                break
        self.assertIsNotNone(agent_parser, "agent subparser not found")

        catalog_parser = None
        for action in agent_parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                catalog_parser = action.choices.get("catalog")
                break
        self.assertIsNotNone(catalog_parser, "agent catalog subparser not found")
        help_text = catalog_parser.format_help()
        self.assertIn("stats", help_text)
        self.assertIn("doctor", help_text)

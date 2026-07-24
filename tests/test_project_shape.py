import importlib
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from shopping_cli.db.session import db_session


class ProjectShapeTest(unittest.TestCase):
    def test_api_dependencies_are_optional_extras(self):
        pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        required = set(pyproject["project"].get("dependencies") or [])
        api_extra = set(pyproject["project"]["optional-dependencies"]["api"])

        self.assertFalse(any(dep.startswith(("fastapi", "pydantic", "uvicorn")) for dep in required))
        self.assertTrue({"fastapi>=0.110", "pydantic>=2", "uvicorn>=0.27"}.issubset(api_extra))

    def test_documented_modules_are_importable(self):
        module_names = [
            "shopping_cli.config",
            "shopping_cli.core.delivery",
            "shopping_cli.api.routes_merchants",
            "shopping_cli.api.routes_marketplace",
            "shopping_cli.api.routes_conversations",
            "shopping_cli.api.routes_agents",
            "shopping_cli.adapters.openclaw",
            "shopping_cli.adapters.hermes",
        ]

        for module_name in module_names:
            with self.subTest(module=module_name):
                self.assertIsNotNone(importlib.import_module(module_name))

    def test_route_modules_expose_documented_route_groups(self):
        from shopping_cli.api import routes_agents, routes_conversations, routes_marketplace, routes_merchants

        self.assertIn("/merchants", routes_merchants.route_paths())
        self.assertIn("/products/{sku}", routes_merchants.route_paths())
        self.assertIn("/buyer/ask", routes_marketplace.route_paths())
        self.assertIn("/search/products", routes_marketplace.route_paths())
        self.assertIn("/conversations/{conversation_id}/messages", routes_conversations.route_paths())
        self.assertIn("/human-review/queue", routes_conversations.route_paths())
        self.assertIn("/agents/heartbeat", routes_agents.route_paths())
        self.assertIn("/merchants/{merchant_id}/agents", routes_agents.route_paths())

    def test_sqlite_schema_creates_operational_indexes(self):
        expected = {
            "idx_conversations_merchant_status_updated",
            "idx_conversations_merchant_updated",
            "idx_conversations_buyer_updated",
            "idx_conversations_buyer_merchant_sku_created",
            "idx_messages_conversation_id",
            "idx_moderation_flags_conversation_resolved",
            "idx_moderation_flags_conversation_id",
            "idx_moderation_flags_queue",
            "idx_api_tokens_merchant_role_created",
            "idx_api_tokens_token_hash",
            "idx_api_tokens_merchant_role_prefix",
            "idx_agents_owner_id",
            "idx_agent_message_processes_agent_status_updated",
            "idx_audit_events_actor_event_id",
            "idx_audit_events_conversation_id",
            "idx_products_active_merchant",
            "idx_products_active_stock_price",
            "idx_merchants_city_lower",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with db_session(Path(tmp) / "shopping.sqlite") as conn:
                indexes = {row["name"] for row in conn.execute("select name from sqlite_master where type = 'index'")}

        self.assertTrue(expected.issubset(indexes), sorted(expected - indexes))

    def test_runtime_config_reads_api_environment(self):
        from shopping_cli import config

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "shopping.sqlite"
            with patch.dict(
                "os.environ",
                {
                    "SHOPPING_DB": str(db_path),
                    "SHOPPING_API_HOST": "0.0.0.0",
                    "SHOPPING_API_PORT": "9876",
                    "SHOPPING_DEPLOYMENT_PROFILE": "production",
                    "SHOPPING_PUBLIC_BASE_URL": "https://marketplace.example.test",
                },
                clear=False,
            ):
                runtime = config.RuntimeConfig.from_env()

        self.assertEqual(runtime.db_path, db_path)
        self.assertEqual(runtime.api_host, "0.0.0.0")
        self.assertEqual(runtime.api_port, 9876)
        self.assertEqual(runtime.deployment_profile, "production")
        self.assertEqual(runtime.public_base_url, "https://marketplace.example.test")

    def test_runtime_config_rejects_invalid_api_port(self):
        from shopping_cli import config

        for value in ("not-a-port", "0", "70000"):
            with self.subTest(value=value):
                with patch.dict("os.environ", {"SHOPPING_API_PORT": value}, clear=False):
                    with self.assertRaises(config.ConfigError):
                        config.RuntimeConfig.from_env()

    def test_runtime_config_rejects_unsupported_database_url(self):
        from shopping_cli import config

        with patch.dict("os.environ", {"SHOPPING_DATABASE_URL": "postgresql://user:pass@db/shopping"}, clear=False):
            with self.assertRaises(config.ConfigError) as raised:
                config.RuntimeConfig.from_env()

        self.assertIn("Postgres/RDS is not supported", str(raised.exception))

    def test_production_preflight_rejects_placeholder_tokens(self):
        from shopping_cli import config

        with patch.dict(
            "os.environ",
            {
                "SHOPPING_DEPLOYMENT_PROFILE": "production",
                "SHOPPING_ADMIN_TOKEN": "replace-with-a-long-random-secret",
                "SHOPPING_BUYER_BOOTSTRAP_TOKEN": "b" * 40,
            },
            clear=False,
        ):
            with self.assertRaises(config.ConfigError) as raised:
                config.validate_production_config()

        self.assertIn("SHOPPING_ADMIN_TOKEN", str(raised.exception))

    def test_production_preflight_allows_disabled_channel_ingress(self):
        from shopping_cli import config

        with patch.dict(
            "os.environ",
            {
                "SHOPPING_DEPLOYMENT_PROFILE": "production",
                "SHOPPING_ADMIN_TOKEN": "a" * 40,
                "SHOPPING_BUYER_BOOTSTRAP_TOKEN": "b" * 40,
                "SHOPPING_CHANNEL_TOKEN": "",
                "SHOPPING_CHANNEL_TOKENS": "telegram:replace-with-channel-token",
            },
            clear=False,
        ):
            config.validate_production_config()

    def test_production_preflight_rejects_short_shared_secrets(self):
        from shopping_cli import config

        with patch.dict(
            "os.environ",
            {
                "SHOPPING_DEPLOYMENT_PROFILE": "production",
                "SHOPPING_ADMIN_TOKEN": "a",
                "SHOPPING_BUYER_BOOTSTRAP_TOKEN": "b",
            },
            clear=False,
        ):
            with self.assertRaises(config.ConfigError) as raised:
                config.validate_production_config()

        self.assertIn("at least 32", str(raised.exception))

    def test_release_metadata_versions_stay_aligned(self):
        import json
        import re

        root = Path(".")
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        clawhub = json.loads((root / "clawhub.json").read_text(encoding="utf-8"))
        plugin_package = json.loads((root / "plugins" / "shopping-plugin" / "package.json").read_text(encoding="utf-8"))
        plugin = json.loads((root / "plugins" / "shopping-plugin" / "openclaw.plugin.json").read_text(encoding="utf-8"))
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        skill_version = re.search(r"^version:\s*([^\n]+)$", skill, flags=re.MULTILINE)

        version = package["version"]
        self.assertEqual(pyproject["project"]["version"], version)
        self.assertEqual(clawhub["version"], version)
        self.assertEqual(plugin_package["version"], version)
        self.assertEqual(plugin["version"], version)
        self.assertIsNotNone(skill_version)
        self.assertEqual(skill_version.group(1).strip(), version)
        self.assertEqual(set(package["bin"]), set(pyproject["project"]["scripts"]))
        for script_path in package["bin"].values():
            self.assertTrue((root / script_path).exists(), script_path)

    def test_config_and_host_adapters_expose_stable_entrypoints(self):
        from shopping_cli import config
        from shopping_cli.adapters import hermes, openclaw

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "shopping.sqlite"
            state_dir = Path(tmp) / "state"
            with patch.dict(
                "os.environ",
                {
                    "SHOPPING_DB": str(db_path),
                    "SHOPPING_CLI_STATE_DIR": str(state_dir),
                    "SHOPPING_BUYER_BOOTSTRAP_TOKEN": "",
                },
            ):
                runtime = config.RuntimeConfig.from_env()

            self.assertEqual(runtime.db_path, db_path)
            self.assertEqual(runtime.state_dir, state_dir)

            merchant_command = openclaw.merchant_agent_command("seller-a", db_path=db_path, once=True)
            self.assertIn("agent", merchant_command)
            self.assertIn("run", merchant_command)
            self.assertIn("--once", merchant_command)

            api_agent_command = openclaw.merchant_agent_command(
                "seller-a",
                api_url="http://shopping.test",
                agent_token="agent-token",
                session_id="openclaw-session-1",
                once=True,
            )
            self.assertIn("--api-url", api_agent_command)
            self.assertIn("http://shopping.test", api_agent_command)
            self.assertIn("--agent-token", api_agent_command)
            self.assertIn("agent-token", api_agent_command)
            self.assertIn("--host", api_agent_command)
            self.assertIn("openclaw", api_agent_command)
            self.assertIn("--session-id", api_agent_command)
            self.assertIn("openclaw-session-1", api_agent_command)
            self.assertNotIn("--db", api_agent_command)

            agent_context = openclaw.merchant_agent_context("seller-a", session_id="openclaw-session-1")
            self.assertEqual(
                agent_context,
                {
                    "host": "openclaw",
                    "session_id": "openclaw-session-1",
                    "actor": "shopping-cli-merchant-agent:seller-a",
                    "source_id": "openclaw-merchant:seller-a:openclaw-session-1",
                    "token_scope": "merchant_agent",
                },
            )

            buyer_command = hermes.buyer_ask_command("alice", "longjing gift", db_path=db_path, city="Hangzhou")
            self.assertIn("buyer", buyer_command)
            self.assertIn("ask", buyer_command)
            self.assertIn("--city", buyer_command)

            buyer_request = hermes.buyer_ask_request(
                "alice",
                "longjing gift",
                city="Hangzhou",
                area="West Lake",
                session_id="hermes-session-1",
            )
            self.assertEqual(buyer_request["method"], "POST")
            self.assertEqual(buyer_request["path"], "/buyer/ask")
            self.assertEqual(
                buyer_request["payload"],
                {
                    "buyer_id": "alice",
                    "text": "longjing gift",
                    "city": "Hangzhou",
                    "area": "West Lake",
                    "source_id": "hermes-buyer:alice",
                    "host": "hermes",
                    "session_id": "hermes-session-1",
                },
            )

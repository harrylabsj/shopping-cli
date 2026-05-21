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

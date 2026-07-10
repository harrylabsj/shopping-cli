import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shopping_cli.agents import merchant_agent
from shopping_cli.api.handlers import agents as agent_handlers
from shopping_cli.core.catalog import create_merchant
from shopping_cli.db.session import db_session
from shopping_cli.services import tokens as token_service


class ApiAgentsTest(unittest.TestCase):
    def seed_agent(self, db_file: Path) -> str:
        with db_session(db_file) as conn:
            create_merchant(conn, merchant_id="seller-a", name="West Lake Tea")
            merchant_token = token_service.issue_merchant_token(conn, "seller-a")
            merchant_agent.heartbeat(conn, merchant_id="seller-a")
            conn.execute(
                "update agents set last_seen_at = '2000-01-01T00:00:00' where id = 'shopping-cli-merchant-agent:seller-a'"
            )
            return merchant_token

    def test_agent_token_prefix_resolution_reads_at_most_two_matches(self):
        class Cursor:
            def __init__(self, sql):
                self.sql = sql

            def fetchall(self):
                if "limit 2" not in self.sql.lower():
                    raise AssertionError("prefix resolution should cap ambiguity checks at two rows")
                return [{"token": "first"}, {"token": "second"}]

        class Connection:
            def execute(self, sql, _params):
                return Cursor(sql)

        with self.assertRaises(ValueError) as raised:
            token_service.resolve_agent_token(Connection(), "seller-a", token_prefix_value="shopping_agent_")

        self.assertIn("ambiguous", str(raised.exception))

    def test_agent_stale_ttl_is_configurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            merchant_token = self.seed_agent(db_file)

            with patch.dict("os.environ", {"SHOPPING_AGENT_STALE_TTL_SECONDS": "9999999999"}):
                agents = agent_handlers.list_agents(db_file, {"merchant_token": merchant_token})

            self.assertFalse(agents["agents"][0]["stale"])
            self.assertEqual(agents["agents"][0]["stale_ttl_seconds"], 9999999999)

    def test_agent_stale_ttl_falls_back_when_env_is_too_large(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "marketplace.sqlite"
            merchant_token = self.seed_agent(db_file)

            with patch.dict("os.environ", {"SHOPPING_AGENT_STALE_TTL_SECONDS": str(10**100)}):
                agents = agent_handlers.list_agents(db_file, {"merchant_token": merchant_token})

            self.assertTrue(agents["agents"][0]["stale"])
            self.assertEqual(agents["agents"][0]["stale_ttl_seconds"], 60)


if __name__ == "__main__":
    unittest.main()

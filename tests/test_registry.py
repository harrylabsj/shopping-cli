import subprocess
import sys
import unittest
from pathlib import Path

from shopping_cli.api import routes_agents, routes_conversations, routes_marketplace, routes_merchants
from shopping_cli.api.route_registry import route_info, routes_for_group

ROOT = Path(__file__).resolve().parents[1]


class ShoppingRegistryCompatibilityTest(unittest.TestCase):
    def test_legacy_registry_script_points_to_new_api_without_payment_surface(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "shopping_registry.py"), "--help"],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("shopping-cli api serve", result.stdout)
        self.assertNotIn("payment", result.stdout.lower())
        self.assertNotIn("order", result.stdout.lower())

    def test_api_route_groups_are_derived_from_single_registry(self):
        for route in route_info():
            self.assertTrue(route.groups, route.path)

        self.assertEqual(routes_agents.routes(), routes_for_group("agents"))
        self.assertEqual(routes_conversations.routes(), routes_for_group("conversations"))
        self.assertEqual(routes_marketplace.routes(), routes_for_group("marketplace"))
        self.assertEqual(routes_merchants.routes(), routes_for_group("merchants"))

        route_keys = [(route.path, tuple(sorted(route.methods))) for route in route_info()]
        self.assertEqual(len(route_keys), len(set(route_keys)))


if __name__ == "__main__":
    unittest.main()

import subprocess
import sys
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()

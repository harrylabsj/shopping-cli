import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from shopping_cli.adapters import hermes, openclaw


class HostAdapterE2ETest(unittest.TestCase):
    def run_command(self, command):
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=10)
        if result.returncode != 0:
            self.fail(f"{' '.join(command)} failed\nstdout={result.stdout}\nstderr={result.stderr}")
        return json.loads(result.stdout)

    def test_openclaw_merchant_and_hermes_buyer_share_marketplace_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"

            merchant = self.run_command(
                openclaw.merchant_create_command(
                    "seller-a",
                    "West Lake Tea",
                    db_path=db_file,
                    city="Hangzhou",
                    service_area="West Lake",
                    delivery_eta_minutes=45,
                )
            )
            self.assertEqual(merchant["merchant"]["id"], "seller-a")

            product = self.run_command(
                openclaw.product_add_command(
                    "seller-a",
                    "tea-a",
                    "Longjing Gift Box",
                    88,
                    5,
                    db_path=db_file,
                    tags=["longjing", "gift"],
                )
            )
            self.assertEqual(product["product"]["sku"], "tea-a")

            ask = self.run_command(
                hermes.buyer_ask_command(
                    "alice",
                    "longjing gift delivery today",
                    db_path=db_file,
                    city="Hangzhou",
                )
            )
            self.assertEqual(ask["conversation"]["id"], "CONV-0001")
            self.assertEqual(ask["conversation"]["status"], "waiting_merchant")

            agent = self.run_command(openclaw.merchant_agent_command("seller-a", db_path=db_file, once=True))
            self.assertEqual(agent["replied"][0]["conversation_id"], "CONV-0001")

            summary = self.run_command(hermes.buyer_summarize_command("CONV-0001", db_path=db_file))
            self.assertEqual(summary["conversation"]["status"], "waiting_buyer")
            self.assertEqual(summary["option"]["sku"], "tea-a")
            self.assertTrue(summary["no_order_created"])
            self.assertTrue(summary["no_stock_reserved"])


if __name__ == "__main__":
    unittest.main()

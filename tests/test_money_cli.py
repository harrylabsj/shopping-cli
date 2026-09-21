from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from helpers import run_cli as run_cli_helper  # noqa: E402


class ExactMoneyCliTest(unittest.TestCase):
    def test_zero_dual_authority_operator_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "shopping.sqlite"

            def run(*args: str) -> str:
                return run_cli_helper(db, *args, db_flag="--data")

            run(
                "merchant",
                "create",
                "--id",
                "m1",
                "--name",
                "Merchant",
                "--delivery-fee",
                "12.34",
                "--format",
                "json",
            )
            run(
                "product",
                "add",
                "--merchant",
                "m1",
                "--sku",
                "sku-a",
                "--title",
                "A",
                "--price",
                "99.99",
                "--stock",
                "5",
                "--format",
                "json",
            )
            begun = json.loads(run("money", "begin", "--merchant", "m1", "--format", "json"))
            self.assertEqual(begun["authority_version"], 1)
            run(
                "money",
                "stage-product",
                "--merchant",
                "m1",
                "--sku",
                "sku-a",
                "--price-minor",
                "9999",
                "--floor-price-minor",
                "0",
            )
            run(
                "money",
                "stage-delivery",
                "--merchant",
                "m1",
                "--fee-minor",
                "1234",
            )
            activated = json.loads(run("money", "activate", "--merchant", "m1"))
            self.assertEqual(activated["authority_version"], 1)
            updated = json.loads(
                run(
                    "money",
                    "update-product",
                    "--merchant",
                    "m1",
                    "--sku",
                    "sku-a",
                    "--price-minor",
                    "9250",
                    "--floor-price-minor",
                    "8800",
                    "--authority-version",
                    "1",
                )
            )
            self.assertEqual(updated["money"]["price_minor"], "9250")
            created = json.loads(
                run(
                    "money",
                    "create-product",
                    "--merchant",
                    "m1",
                    "--sku",
                    "sku-b",
                    "--title",
                    "B",
                    "--price-minor",
                    "10000",
                    "--stock",
                    "3",
                    "--authority-version",
                    "1",
                )
            )
            self.assertEqual(created["product"]["price"], 100.0)


if __name__ == "__main__":
    unittest.main()

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import shopping  # noqa: E402
import shopping_agent  # noqa: E402


class ConversationAgentTest(unittest.TestCase):
    def run_cli(self, db_file, *args):
        output = StringIO()
        with redirect_stdout(output):
            shopping.main(["--db", str(db_file), *args])
        return output.getvalue()

    def test_agent_entrypoint_processes_one_pending_consultation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.run_cli(
                db_file,
                "merchant",
                "create",
                "--id",
                "seller-a",
                "--name",
                "West Lake Tea",
                "--city",
                "Hangzhou",
                "--service-area",
                "West Lake",
                "--delivery-eta-minutes",
                "45",
            )
            self.run_cli(
                db_file,
                "product",
                "add",
                "--merchant",
                "seller-a",
                "--sku",
                "tea-a",
                "--title",
                "Longjing Gift Box",
                "--price",
                "88",
                "--stock",
                "5",
                "--tags",
                "longjing,gift",
            )
            self.run_cli(
                db_file,
                "buyer",
                "ask",
                "--buyer",
                "alice",
                "--text",
                "longjing gift delivery today",
                "--city",
                "Hangzhou",
            )

            output = StringIO()
            with redirect_stdout(output):
                shopping_agent.main(["--db", str(db_file), "--merchant", "seller-a", "--once", "--format", "json"])
            result = json.loads(output.getvalue())
            self.assertEqual(result["replied"][0]["conversation_id"], "CONV-0001")
            self.assertFalse(result["replied"][0]["human_required"])


if __name__ == "__main__":
    unittest.main()

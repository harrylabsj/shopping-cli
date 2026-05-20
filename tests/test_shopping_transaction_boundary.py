import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from shopping_cli.api.app import route_info  # noqa: E402
from shopping_cli import cli  # noqa: E402
from shopping_cli.db.session import open_connection  # noqa: E402


class ShoppingTransactionBoundaryTest(unittest.TestCase):
    def test_cli_does_not_expose_quote_or_order_commands(self):
        parser = cli.build_parser()
        for command in ("quote", "order"):
            with self.subTest(command=command):
                stderr = StringIO()
                with redirect_stderr(stderr), self.assertRaises(SystemExit) as caught:
                    parser.parse_args([command, "show"])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("invalid choice", stderr.getvalue())

    def test_sqlite_schema_has_no_transaction_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "shopping-cli.sqlite"
            conn = open_connection(db_path)
            try:
                tables = {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}
            finally:
                conn.close()
        self.assertNotIn("quotes", tables)
        self.assertNotIn("orders", tables)
        self.assertNotIn("inventory_reservations", tables)
        self.assertNotIn("payments", tables)

    def test_api_route_catalog_has_no_transaction_routes(self):
        paths = {route.path for route in route_info()}
        self.assertFalse(any(path.startswith("/quotes") for path in paths))
        self.assertFalse(any(path.startswith("/orders") for path in paths))
        self.assertNotIn("/events", paths)

    def test_transaction_runtime_module_is_absent(self):
        self.assertFalse((ROOT / "shopping_cli" / "core" / "commerce.py").exists())


if __name__ == "__main__":
    unittest.main()

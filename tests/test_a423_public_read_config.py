import tempfile
from pathlib import Path
from unittest import TestCase, mock
from shopping_cli.api import auth
from shopping_cli.api.handlers.listings_projection import get_listing_projection
from shopping_cli.core.errors import AuthError, NotFoundError
from shopping_cli.db.session import open_connection
from shopping_cli.core.catalog import create_product
from shopping_cli.services.tokens import issue_merchant_token
from shopping_cli.listings.projection import list_publishable_listings, project_product_listing


class ReadConfig(TestCase):
    def test_public_inactive_and_paused_rejected_owner_diagnostic_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db.sqlite"
            conn = open_connection(path)
            conn.execute("insert into merchants(id,name,created_at,updated_at) values ('m','Synthetic','t','t')")
            token = issue_merchant_token(conn, "m")
            create_product(conn, "m", "sku", "Product", 99.0, 1)
            conn.commit()
            self.assertTrue(get_listing_projection(path, "sku")["ok"])
            for field in ["active", "listing_paused"]:
                with self.subTest(field=field):
                    conn.execute("update products set active=1,listing_paused=0")
                    conn.execute(f"update products set {field}=?", (0 if field == "active" else 1,))
                    conn.commit()
                    with self.assertRaises(NotFoundError):
                        get_listing_projection(path, "sku")
                    self.assertTrue(get_listing_projection(path, "sku", {"_auth_token": token})["ok"])
            conn.close()

    def test_public_list_remains_complete_sorted_and_same_projection_without_n_plus_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = open_connection(Path(tmp) / "db.sqlite")
            conn.execute("insert into merchants(id,name,created_at,updated_at) values ('m','Synthetic','t','t')")
            for n in range(25):
                create_product(conn, "m", f"sku{n:02d}", "Product", 99.0, 1)
            conn.commit()
            expected = [project_product_listing(conn, f"sku{n:02d}", merchant_id="m") for n in range(25)]
            queries = []
            conn.set_trace_callback(queries.append)
            actual = list_publishable_listings(conn, merchant_id="m")
            conn.set_trace_callback(None)
            for result in expected:
                result["_provenance"].pop("observed_at")
            for result in actual:
                result["_provenance"].pop("observed_at")
            self.assertEqual(actual, expected)
            self.assertEqual(len(actual), 25)
            self.assertLessEqual(len([q for q in queries if q.lstrip().lower().startswith("select")]), 2)
            conn.close()

    def test_runtime_prod_short_secrets_rejected_but_local_and_strong_accepted(self):
        for profile in ["local", "production"]:
            with (
                self.subTest(profile=profile),
                mock.patch.dict(
                    "os.environ",
                    {
                        "SHOPPING_DEPLOYMENT_PROFILE": profile,
                        "SHOPPING_ADMIN_TOKEN": "x",
                        "SHOPPING_BUYER_BOOTSTRAP_TOKEN": "x",
                        "SHOPPING_CHANNEL_TOKEN": "x",
                        "SHOPPING_CHANNEL_TOKENS": "",
                    },
                ),
            ):
                if profile == "production":
                    with self.assertRaises(AuthError):
                        auth.require_admin_token({"admin_token": "x"})
                    with self.assertRaises(AuthError):
                        auth.require_buyer_bootstrap_token({"buyer_bootstrap_token": "x"}, lambda t: t)
                    self.assertEqual(auth.channel_token_map(), {})
                else:
                    auth.require_admin_token({"admin_token": "x"})
                    self.assertEqual(
                        auth.require_buyer_bootstrap_token({"buyer_bootstrap_token": "x"}, lambda t: t), "x"
                    )
                    self.assertEqual(auth.channel_token_map(), {"*": "x"})
        value = "synthetic-auth-" + ("z" * 32)
        with mock.patch.dict(
            "os.environ", {"SHOPPING_DEPLOYMENT_PROFILE": "production", "SHOPPING_ADMIN_TOKEN": value}
        ):
            auth.require_admin_token({"admin_token": value})

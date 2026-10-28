from __future__ import annotations

import json
import secrets
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from shopping_cli.api.app import handle_request
from shopping_cli.db.session import db_session


class MerchantTokenRotationTest(unittest.TestCase):
    def test_token_status_requires_the_current_merchant_token_and_returns_only_expiry_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"SHOPPING_ADMIN_TOKEN": "admin-fixture"}):
            db_file = Path(tmp) / "shopping.sqlite"
            _, created = handle_request(db_file, "POST", "/merchants", {"id": "seller-a", "name": "Tea", "admin_token": "admin-fixture"})
            token = created["merchant_token"]
            status, result = handle_request(db_file, "GET", "/merchants/seller-a/token/status", {"merchant_token": token})
            self.assertEqual(status, 200)
            self.assertEqual(result["merchant_id"], "seller-a")
            self.assertTrue(result["active"])
            self.assertTrue(result["expires_at"])
            self.assertNotIn("merchant_token", result)
            self.assertNotIn(token, json.dumps(result))
            self.assertEqual(handle_request(db_file, "GET", "/merchants/seller-a/token/status", {})[0], 403)

    def test_prepare_is_recoverable_and_confirm_requires_replacement_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"SHOPPING_ADMIN_TOKEN": "admin-fixture"}):
            db_file = Path(tmp) / "shopping.sqlite"
            _, created = handle_request(db_file, "POST", "/merchants", {"id": "seller-a", "name": "Tea", "admin_token": "admin-fixture"})
            old = created["merchant_token"]
            replacement = "shopping_merchant_" + secrets.token_urlsafe(32)
            rotation_id = secrets.token_urlsafe(24)
            prepare_path = "/merchants/seller-a/token/rotate/prepare"
            body = {"merchant_token": old, "replacement_token": replacement, "rotation_id": rotation_id}
            status, first = handle_request(db_file, "POST", prepare_path, body)
            self.assertEqual(status, 200)
            self.assertEqual(first["status"], "pending")
            # Lost response: same request is idempotent and does not mint another token.
            status, retry = handle_request(db_file, "POST", prepare_path, body)
            self.assertEqual(status, 200)
            self.assertEqual(retry["rotation_id"], rotation_id)
            with db_session(db_file) as conn:
                self.assertEqual(conn.execute("select count(*) from api_tokens where merchant_id='seller-a' and role='merchant' and revoked_at='' ").fetchone()[0], 2)
            status, _ = handle_request(db_file, "POST", "/merchants/seller-a/token/rotate/confirm", {"merchant_token": old, "rotation_id": rotation_id})
            self.assertEqual(status, 403)
            status, confirmed = handle_request(db_file, "POST", "/merchants/seller-a/token/rotate/confirm", {"merchant_token": replacement, "rotation_id": rotation_id})
            self.assertEqual(status, 200)
            self.assertEqual(confirmed["status"], "confirmed")
            status, again = handle_request(db_file, "POST", "/merchants/seller-a/token/rotate/confirm", {"merchant_token": replacement, "rotation_id": rotation_id})
            self.assertEqual(status, 200)
            self.assertEqual(again["status"], "confirmed")
            with db_session(db_file) as conn:
                rows = conn.execute("select token_hash, revoked_at from api_tokens where merchant_id='seller-a' and role='merchant'").fetchall()
                self.assertEqual(sum(not row["revoked_at"] for row in rows), 1)
                self.assertNotIn(old, json.dumps([dict(row) for row in rows]))

    def test_rotation_id_cannot_be_reused_with_another_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"SHOPPING_ADMIN_TOKEN": "admin-fixture"}):
            db_file = Path(tmp) / "shopping.sqlite"
            _, created = handle_request(db_file, "POST", "/merchants", {"id": "seller-a", "name": "Tea", "admin_token": "admin-fixture"})
            old = created["merchant_token"]
            rotation_id = secrets.token_urlsafe(24)
            body = {"merchant_token": old, "replacement_token": "shopping_merchant_" + secrets.token_urlsafe(32), "rotation_id": rotation_id}
            self.assertEqual(handle_request(db_file, "POST", "/merchants/seller-a/token/rotate/prepare", body)[0], 200)
            body["replacement_token"] = "shopping_merchant_" + secrets.token_urlsafe(32)
            status, result = handle_request(db_file, "POST", "/merchants/seller-a/token/rotate/prepare", body)
            self.assertEqual(status, 409)
            self.assertNotIn("replacement_token", result)

    def test_concurrent_rotations_of_the_same_old_token_allow_only_one_pending_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"SHOPPING_ADMIN_TOKEN": "admin-fixture"}, clear=False):
            db_file = Path(tmp) / "shopping.sqlite"
            _, created = handle_request(db_file, "POST", "/merchants", {"id": "seller-a", "name": "Tea", "admin_token": "admin-fixture"})
            old = created["merchant_token"]
            requests = [
                {"merchant_token": old, "replacement_token": "shopping_merchant_" + secrets.token_urlsafe(32), "rotation_id": secrets.token_urlsafe(24)}
                for _ in range(2)
            ]

            def prepare(body: dict[str, str]) -> int:
                return handle_request(db_file, "POST", "/merchants/seller-a/token/rotate/prepare", body)[0]

            with ThreadPoolExecutor(max_workers=2) as pool:
                statuses = list(pool.map(prepare, requests))
            self.assertCountEqual(statuses, [200, 409])
            with db_session(db_file) as conn:
                pending = conn.execute("select count(*) from merchant_token_rotations where status='pending'").fetchone()[0]
                active = conn.execute("select count(*) from api_tokens where merchant_id='seller-a' and role='merchant' and revoked_at='' ").fetchone()[0]
            self.assertEqual(pending, 1)
            self.assertEqual(active, 2)

    def test_unconfirmed_replacement_expires_without_revoking_previous_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"SHOPPING_ADMIN_TOKEN": "admin-fixture"}):
            db_file = Path(tmp) / "shopping.sqlite"
            _, created = handle_request(db_file, "POST", "/merchants", {"id": "seller-a", "name": "Tea", "admin_token": "admin-fixture"})
            old = created["merchant_token"]
            replacement = "shopping_merchant_" + secrets.token_urlsafe(32)
            rotation_id = secrets.token_urlsafe(24)
            status, _ = handle_request(db_file, "POST", "/merchants/seller-a/token/rotate/prepare", {"merchant_token": old, "replacement_token": replacement, "rotation_id": rotation_id})
            self.assertEqual(status, 200)
            with db_session(db_file) as conn:
                conn.execute("update merchant_token_rotations set expires_at='2000-01-01T00:00:00' where rotation_id=?", (rotation_id,))
            # Expiry is enforced at auth even before a later prepare request records cleanup.
            status, _ = handle_request(db_file, "PATCH", "/merchants/seller-a", {"merchant_token": replacement, "name": "Tea updated"})
            self.assertEqual(status, 403)
            status, result = handle_request(db_file, "PATCH", "/merchants/seller-a", {"merchant_token": old, "name": "Tea updated"})
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()

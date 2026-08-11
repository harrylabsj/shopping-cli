"""免费档商品额度 + kiwi-catalog 门户代理凭据（KIWI_CATALOG_PROXY_TOKEN）测试。"""

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from shopping_cli.api.app import handle_request
from shopping_cli.api.handlers.catalog import free_product_quota
from shopping_cli.core.errors import AuthError
from shopping_cli.db.session import db_session
from shopping_cli.services import tokens as token_service

PROXY_SECRET = "test-catalog-proxy-secret"
FREE_MERCHANT = "mkt_smoke1"
GUIDANCE = "免费额度（10 件商品）已用完——请到 Kiwi Catalog 门户「我的账户」申请商家令牌"


def _proxy_env(**overrides: str) -> dict[str, str]:
    env = {
        "KIWI_CATALOG_PROXY_TOKEN": PROXY_SECRET,
        # 关闭跨服务 owner token 校验，避免测试误入网络分支
        "KIWI_CATALOG_AUTH_URL": "",
    }
    env.update(overrides)
    return env


class FreeProductQuotaTest(unittest.TestCase):
    def _create_product(
        self,
        db_file: Path,
        sku: str,
        merchant_id: str = FREE_MERCHANT,
        token: str = PROXY_SECRET,
    ) -> tuple[int, dict]:
        return handle_request(
            db_file,
            "POST",
            "/products",
            {
                "merchant_id": merchant_id,
                "sku": sku,
                "title": f"商品 {sku}",
                "price": 10,
                "stock": 1,
                "merchant_token": token,
            },
        )

    def test_proxy_credential_allows_ten_products_and_rejects_eleventh(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with patch.dict(os.environ, _proxy_env(), clear=False):
                for index in range(10):
                    status, body = self._create_product(db_file, f"sku-{index}")
                    self.assertEqual(status, 200, body)
                status, body = self._create_product(db_file, "sku-10")
            self.assertEqual(status, 403)
            self.assertFalse(body["ok"])
            self.assertEqual(body["error"], GUIDANCE)

    def test_proxy_auth_returns_catalog_proxy_sentinel(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with patch.dict(os.environ, _proxy_env(), clear=False):
                with db_session(db_file) as conn:
                    row = token_service.require_merchant_token(conn, FREE_MERCHANT, PROXY_SECRET)
            self.assertEqual(row, {"role": "catalog_proxy", "merchant_id": FREE_MERCHANT})

    def test_local_merchant_token_is_unlimited(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with patch.dict(
                os.environ,
                _proxy_env(SHOPPING_ADMIN_TOKEN="test-admin-bootstrap-token"),
                clear=False,
            ):
                _status, merchant = handle_request(
                    db_file,
                    "POST",
                    "/merchants",
                    {
                        "id": "seller-a",
                        "name": "West Lake Tea",
                        "admin_token": "test-admin-bootstrap-token",
                    },
                )
                for index in range(11):
                    status, body = self._create_product(
                        db_file,
                        f"sku-{index}",
                        merchant_id="seller-a",
                        token=merchant["merchant_token"],
                    )
                    self.assertEqual(status, 200, body)

    def test_deactivated_products_do_not_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with patch.dict(os.environ, _proxy_env(), clear=False):
                for index in range(10):
                    status, body = self._create_product(db_file, f"sku-{index}")
                    self.assertEqual(status, 200, body)
                with db_session(db_file) as conn:
                    conn.execute("update products set active = 0 where sku = 'sku-0'")
                status, body = self._create_product(db_file, "sku-10")
                self.assertEqual(status, 200, body)

    def test_proxy_secret_allows_regular_merchant_id(self):
        # catalog 是身份权威：代理凭据可对任意格式的 merchant_id 生效，
        # 且新注册商家尚无本地行时自动补建。
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with patch.dict(os.environ, _proxy_env(), clear=False):
                status, body = self._create_product(db_file, "sku-0", merchant_id="seller-b")
                self.assertEqual(status, 200, body)
                with db_session(db_file) as conn:
                    row = conn.execute("select id from merchants where id = 'seller-b'").fetchone()
            self.assertIsNotNone(row)

    def test_proxy_token_rejected_when_env_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with patch.dict(os.environ, {"KIWI_CATALOG_AUTH_URL": ""}, clear=False) as env:
                env.pop("KIWI_CATALOG_PROXY_TOKEN", None)
                status, body = self._create_product(db_file, "sku-0")
                self.assertEqual(status, 403)
                with self.assertRaises(AuthError):
                    with db_session(db_file) as conn:
                        token_service.require_merchant_token(conn, FREE_MERCHANT, PROXY_SECRET)

    def test_concurrent_creates_at_cap_allow_exactly_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with patch.dict(os.environ, _proxy_env(), clear=False):
                for index in range(9):
                    status, body = self._create_product(db_file, f"sku-{index}")
                    self.assertEqual(status, 200, body)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(
                        pool.map(
                            lambda sku: self._create_product(db_file, sku),
                            ["sku-race-a", "sku-race-b"],
                        )
                    )
                with db_session(db_file) as conn:
                    count = conn.execute(
                        "select count(*) from products where merchant_id = ? and active = 1",
                        (FREE_MERCHANT,),
                    ).fetchone()[0]
            statuses = sorted(status for status, _body in results)
            self.assertEqual(statuses, [200, 403], results)
            self.assertEqual(count, 10)

    def test_quota_env_override_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with patch.dict(os.environ, _proxy_env(SHOPPING_FREE_PRODUCT_QUOTA="3"), clear=False):
                for index in range(3):
                    status, body = self._create_product(db_file, f"sku-{index}")
                    self.assertEqual(status, 200, body)
                status, body = self._create_product(db_file, "sku-3")
            self.assertEqual(status, 403)
            self.assertIn("免费额度（3 件商品）已用完", body["error"])

    def test_quota_env_invalid_falls_back_to_default(self):
        with patch.dict(os.environ, {"SHOPPING_FREE_PRODUCT_QUOTA": "abc"}, clear=False):
            self.assertEqual(free_product_quota(), 10)
        with patch.dict(os.environ, {"SHOPPING_FREE_PRODUCT_QUOTA": "0"}, clear=False):
            self.assertEqual(free_product_quota(), 10)
        with patch.dict(os.environ, {"SHOPPING_FREE_PRODUCT_QUOTA": "-2"}, clear=False):
            self.assertEqual(free_product_quota(), 10)

    def test_update_with_proxy_credential_succeeds_even_at_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with patch.dict(os.environ, _proxy_env(), clear=False):
                for index in range(10):
                    status, body = self._create_product(db_file, f"sku-{index}")
                    self.assertEqual(status, 200, body)
                status, body = handle_request(
                    db_file,
                    "PATCH",
                    "/products/sku-0",
                    {
                        "merchant_id": FREE_MERCHANT,
                        "merchant_token": PROXY_SECRET,
                        "title": "改名后的商品",
                        "stock": 7,
                    },
                )
            self.assertEqual(status, 200, body)
            self.assertEqual(body["product"]["title"], "改名后的商品")


if __name__ == "__main__":
    unittest.main()

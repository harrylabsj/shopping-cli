"""Characterization tests for the extracted FastAPI route installation.

``shopping_cli/api/fastapi_routes.py`` owns the FastAPI branch of the dual
stack: the module-level availability guard, the Authorization /
Idempotency-Key header defaults, and ``register_fastapi_routes``.  These tests
pin that the module imports cleanly with or without fastapi, that its route
registration covers every fallback route-table path, and that the facade app
builds the same route set through it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shopping_cli.api import app as app_module
from shopping_cli.api.fastapi_routes import (
    AUTHORIZATION_HEADER,
    IDEMPOTENCY_KEY_HEADER,
    FastAPI,
    register_fastapi_routes,
)
from shopping_cli.api.route_table import _ROUTE_TABLE

_HAS_FASTAPI = FastAPI is not None


class FastAPIRoutesCharacterizationTest(unittest.TestCase):
    def test_module_imports_and_exposes_registration(self):
        """模块在无 fastapi 环境下也必须可导入（try/except guard → FastAPI=None）。"""
        self.assertTrue(callable(register_fastapi_routes))
        self.assertTrue(FastAPI is None or callable(FastAPI))

    def test_no_fastapi_import_guard(self):
        """子进程屏蔽 fastapi/starlette 导入，验证模块降级为 FastAPI=None 且 header 默认值可用。"""
        repo_root = Path(__file__).resolve().parent.parent
        code = (
            "import builtins\n"
            "_real_import = builtins.__import__\n"
            "def _blocked(name, *args, **kwargs):\n"
            "    if name == 'fastapi' or name.startswith('fastapi.') or name == 'starlette' "
            "or name.startswith('starlette.'):\n"
            "        raise ModuleNotFoundError('No module named %r' % name)\n"
            "    return _real_import(name, *args, **kwargs)\n"
            "builtins.__import__ = _blocked\n"
            "from shopping_cli.api.fastapi_routes import (\n"
            "    AUTHORIZATION_HEADER, IDEMPOTENCY_KEY_HEADER, FastAPI, register_fastapi_routes,\n"
            ")\n"
            "assert FastAPI is None, repr(FastAPI)\n"
            "assert AUTHORIZATION_HEADER == '', repr(AUTHORIZATION_HEADER)\n"
            "assert IDEMPOTENCY_KEY_HEADER == '', repr(IDEMPOTENCY_KEY_HEADER)\n"
            "assert callable(register_fastapi_routes)\n"
            "print('no-fastapi import ok')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, f"stderr={result.stderr}\nstdout={result.stdout}")
        self.assertIn("no-fastapi import ok", result.stdout)

    @unittest.skipUnless(_HAS_FASTAPI, "fastapi not installed")
    def test_register_fastapi_routes_covers_all_fallback_paths(self):
        """FastAPI 栈必须覆盖 fallback 路由表的每一条路径（双栈 route parity）。"""
        from fastapi import FastAPI as _FA

        # 与 create_app 相同的 docs/redoc/openapi 关闭项，保证路由集精确可比
        app = _FA(docs_url=None, redoc_url=None, openapi_url=None)
        register_fastapi_routes(app, ":db:")
        fastapi_paths = {route.path for route in app.routes if hasattr(route, "path")}
        fallback_paths = {entry.path_template for entry in _ROUTE_TABLE}
        self.assertLessEqual(fallback_paths, fastapi_paths)
        self.assertEqual(fallback_paths, fastapi_paths)

    @unittest.skipUnless(_HAS_FASTAPI, "fastapi not installed")
    def test_header_defaults_are_fastapi_header(self):
        from fastapi.params import Header

        self.assertIsInstance(AUTHORIZATION_HEADER, Header)
        self.assertIsInstance(IDEMPOTENCY_KEY_HEADER, Header)
        # 幂等键走 Idempotency-Key 别名（与 fallback header 名一致）
        self.assertEqual(IDEMPOTENCY_KEY_HEADER.alias, "Idempotency-Key")

    @unittest.skipUnless(_HAS_FASTAPI, "fastapi not installed")
    def test_facade_app_uses_extracted_registration(self):
        """create_app 与直接 register_fastapi_routes 产出同一路由集。"""
        from fastapi import FastAPI as _FA

        # 与 create_app 相同的 docs/redoc/openapi 关闭项，只比 marketplace 路由
        direct = _FA(docs_url=None, redoc_url=None, openapi_url=None)
        register_fastapi_routes(direct, ":db:")
        direct_paths = {route.path for route in direct.routes if hasattr(route, "path")}

        with tempfile.TemporaryDirectory() as tmp:
            facade = app_module.create_app(Path(tmp) / "shopping.sqlite")
        facade_paths = {route.path for route in facade.routes if hasattr(route, "path")}

        self.assertEqual(direct_paths, facade_paths)
        self.assertIn("/products", facade_paths)
        self.assertIn("/negotiation/decisions", facade_paths)


if __name__ == "__main__":
    unittest.main()

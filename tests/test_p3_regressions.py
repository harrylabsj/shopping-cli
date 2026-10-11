import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shopping_cli.api.app import _ROUTE_TABLE, handle_request
from shopping_cli.api.route_registry import route_info
from shopping_cli.db.migrations import migration_023_remove_scheme_a_stub_merchants
from shopping_cli.db.session import db_session


ROOT = Path(__file__).resolve().parents[1]


class P3RegressionTest(unittest.TestCase):
    def test_route_registry_derives_methods_from_executable_router(self):
        executable: dict[str, set[str]] = {}
        for entry in _ROUTE_TABLE:
            executable.setdefault(entry.path_template, set()).update(entry.methods)

        self.assertEqual({route.path: route.methods for route in route_info()}, executable)

    def test_container_build_uses_wheel_and_non_root_runtime(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("AS builder", dockerfile)
        self.assertIn("python -m build --wheel", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertNotIn("pip install --no-cache-dir -e", dockerfile)
        self.assertNotIn("COPY . ", dockerfile)

    def test_compose_uses_installed_console_entrypoints(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("scripts/shopping_api.py", compose)
        self.assertNotIn("scripts/shopping.py", compose)
        self.assertIn("shopping-cli-api", compose)
        self.assertIn("- shopping-cli\n", compose)

    def test_deployment_guide_does_not_reuse_example_env_as_private_env_file(self):
        guide = (ROOT / "references" / "public-deployment.md").read_text(encoding="utf-8")
        self.assertNotIn("--env-file marketplace.example.env", guide)

    def test_env_files_and_secrets_are_git_ignored_but_examples_are_allowed(self):
        patterns = set((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
        self.assertTrue({".env", ".env.*", "*.pem", "*.key", "*secret*", "*credentials*"} <= patterns)
        self.assertIn("!marketplace.example.env", patterns)
        self.assertIn("!.env.example", patterns)
        dockerignore = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
        self.assertIn("!marketplace.example.env", dockerignore)
        self.assertTrue((ROOT / "marketplace.example.env").exists())

    def test_docker_context_excludes_common_secrets(self):
        patterns = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
        self.assertTrue({".env", ".env.*", "*.pem", "*.key", "*secret*", "*credentials*"} <= patterns)

    def test_openclaw_skill_path_is_consistent(self):
        canonical = ".openclaw/skills/shopping-cli"
        install = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        plugin_readme = (ROOT / "plugins" / "shopping-plugin" / "README.md").read_text(encoding="utf-8")
        compat = (ROOT / "plugins" / "shopping-plugin" / "openclaw_compat.js").read_text(encoding="utf-8")
        self.assertIn(canonical, install)
        self.assertIn(canonical, plugin_readme)
        self.assertIn("'.openclaw', 'skills', 'shopping-cli'", compat)

    def test_plugin_manifest_and_package_versions_match(self):
        package = json.loads((ROOT / "plugins" / "shopping-plugin" / "package.json").read_text(encoding="utf-8"))
        manifest = json.loads(
            (ROOT / "plugins" / "shopping-plugin" / "openclaw.plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(package["name"], manifest["id"])
        self.assertEqual(package["version"], manifest["version"])

    def _assert_release_install_contract(self, script):
        self.assertIn('TMP_DIR="$(mktemp -d)"', script)
        self.assertIn("trap 'rm -rf \"$TMP_DIR\"' EXIT", script)
        self.assertIn('python3 -m venv "$TMP_DIR/venv"', script)
        self.assertNotIn("--system-site-packages", script)
        self.assertNotIn("--no-deps", script)
        self.assertIn('set -- "$TMP_DIR"/dist/*.whl', script)
        self.assertIn('"$#" -ne 1 || ! -f "$1" || -L "$1"', script)
        self.assertIn('"$TMP_DIR/venv/bin/python" -m pip install --force-reinstall "$1[api]"', script)
        self.assertIn('(\n  cd "$TMP_DIR"\n  "$TMP_DIR/venv/bin/python" -m pip check', script)
        self.assertIn('"$TMP_DIR/venv/bin/python" - <<\'PYTHON\'', script)
        self.assertIn('import psutil, fastapi, uvicorn', script)
        self.assertIn('package_file = Path(shopping_cli.__file__).resolve()', script)
        self.assertIn('venv_prefix = Path(sys.prefix).resolve()', script)
        self.assertIn('if not package_file.is_relative_to(venv_prefix):', script)
        self.assertIn('raise SystemExit("shopping_cli import escaped the isolated venv")', script)
        self.assertIn("('decision', 'snapshot', 'policy-result', 'capabilities')", script)
        for command in ("shopping-cli", "shopping-cli-api", "shopping-cli-agent"):
            self.assertIn(f'"$TMP_DIR/venv/bin/{command}" --help', script)
        self.assertIn(')\nnpm pack --dry-run', script)  # Packing remains in the checkout.

    def test_release_verifier_uses_an_isolated_install(self):
        script = (ROOT / "scripts" / "verify_release.sh").read_text(encoding="utf-8")
        self._assert_release_install_contract(script)

    def test_release_install_contract_rejects_missing_or_host_dependent_gates(self):
        script = (ROOT / "scripts" / "verify_release.sh").read_text(encoding="utf-8")
        mutations = {
            "cleanup": ("trap 'rm -rf \"$TMP_DIR\"' EXIT", ""),
            "wheel-count": ('"$#" -ne 1', '"$#" -ne 2'),
            "wheel-regular": ('! -f "$1"', '! -d "$1"'),
            "wheel-symlink": (' || -L "$1"', ''),
            "host-python": ('"$TMP_DIR/venv/bin/python" -m pip install', 'python3 -m pip install'),
            "no-deps": ('pip install --force-reinstall', 'pip install --no-deps --force-reinstall'),
            "host-site": ('python3 -m venv', 'python3 -m venv --system-site-packages'),
            "api-extra": ('$1[api]', '$1'),
            "check": ('"$TMP_DIR/venv/bin/python" -m pip check', ''),
            "cwd": ('  cd "$TMP_DIR"', '  cd "$ROOT_DIR"'),
            "imports": ('import psutil, fastapi, uvicorn', 'import psutil'),
            "origin": ('if not package_file.is_relative_to(venv_prefix):', 'if False:'),
            "schemas": ("'capabilities'", "'missing'"),
            "api-cli": ('"$TMP_DIR/venv/bin/shopping-cli-api" --help', ''),
        }
        for name, (before, after) in mutations.items():
            with self.subTest(gate=name):
                self.assertIn(before, script)
                with self.assertRaises(AssertionError):
                    self._assert_release_install_contract(script.replace(before, after))

    def test_listing_projection_list_requires_merchant_id(self):
        """审查 P3-01：list 出口必须带 merchant_id 过滤——无过滤的匿名枚举
        返回 400，跨商家枚举不再可能；带 merchant_id 的匿名读保持公开且不含
        handoff_destination，owner token 读保留完整字段（P2-B 逻辑不动）。"""
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with patch.dict(os.environ, {"SHOPPING_ADMIN_TOKEN": "admin-secret"}, clear=False):
                _status, merchant = handle_request(
                    db_file, "POST", "/merchants", {"id": "seller-a", "name": "Tea", "admin_token": "admin-secret"}
                )
                token = merchant["merchant_token"]
                status, _created = handle_request(
                    db_file,
                    "POST",
                    "/products",
                    {
                        "merchant_id": "seller-a",
                        "sku": "tea-a",
                        "title": "Longjing",
                        "price": 88,
                        "stock": 5,
                        "merchant_token": token,
                        "handoff_destination": "https://shop.example/checkout/tea-a",
                    },
                )
                self.assertEqual(status, 200)

                status, denied = handle_request(db_file, "GET", "/v1/merchant/listings/projections")
                self.assertEqual(status, 400)
                self.assertFalse(denied["ok"])

                status, listed = handle_request(
                    db_file, "GET", "/v1/merchant/listings/projections", query={"merchant_id": "seller-a"}
                )
                self.assertEqual(status, 200)
                self.assertEqual(listed["count"], 1)
                self.assertNotIn("handoff_destination", listed["results"][0])

                auth = {"_auth_token": token}
                status, listed = handle_request(
                    db_file, "GET", "/v1/merchant/listings/projections", auth, {"merchant_id": "seller-a"}
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    listed["results"][0]["handoff_destination"], "https://shop.example/checkout/tea-a"
                )

    def test_migration_023_removes_scheme_a_stub_merchants(self):
        """审查 P3-02：方案A stub 商家行（name == id、无 api_tokens）被迁移
        删除；正常商家、有 token 行的商家、名下有业务行的 stub 均保留
        （后者记 audit_events，不静默丢业务数据）。"""
        now = "2026-08-11T00:00:00"
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                conn.execute(
                    "insert into merchants(id, name, created_at, updated_at)"
                    " values ('stub-1', 'stub-1', ?, ?)",
                    (now, now),
                )
                conn.execute(
                    "insert into merchants(id, name, created_at, updated_at)"
                    " values ('seller-a', 'West Lake Tea', ?, ?)",
                    (now, now),
                )
                # name == id 但持 token 行：正常商家，不删
                conn.execute(
                    "insert into merchants(id, name, created_at, updated_at)"
                    " values ('legacy', 'legacy', ?, ?)",
                    (now, now),
                )
                conn.execute(
                    "insert into api_tokens(token, role, merchant_id, created_at)"
                    " values ('digest-x', 'merchant', 'legacy', ?)",
                    (now,),
                )
                # stub 但名下有 products：跳过并记录
                conn.execute(
                    "insert into merchants(id, name, created_at, updated_at)"
                    " values ('stub-busy', 'stub-busy', ?, ?)",
                    (now, now),
                )
                conn.execute(
                    "insert into products(sku, merchant_id, title, price, stock, created_at, updated_at)"
                    " values ('tea-1', 'stub-busy', 'Tea', 88, 5, ?, ?)",
                    (now, now),
                )

                migration_023_remove_scheme_a_stub_merchants(conn)

                remaining = {row["id"] for row in conn.execute("select id from merchants")}
                self.assertEqual(remaining, {"seller-a", "legacy", "stub-busy"})
                audits = conn.execute(
                    "select details_json from audit_events where event = 'stub_merchant_retained'"
                ).fetchall()
                self.assertEqual(len(audits), 1)
                details = json.loads(audits[0]["details_json"])
                self.assertEqual(details["merchant_id"], "stub-busy")
                self.assertEqual(details["dependents"], {"products": 1})
                # 重跑不误删保留行（迁移本身按 user_version 只执行一次）
                migration_023_remove_scheme_a_stub_merchants(conn)
                remaining = {row["id"] for row in conn.execute("select id from merchants")}
                self.assertEqual(remaining, {"seller-a", "legacy", "stub-busy"})


if __name__ == "__main__":
    unittest.main()

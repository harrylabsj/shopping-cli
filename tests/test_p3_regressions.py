import json
import unittest
from pathlib import Path

from shopping_cli.agents.tools import HTTPMerchantAgentTools
from shopping_cli.api.app import _ROUTE_TABLE
from shopping_cli.api.route_registry import route_info


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

    def test_release_verifier_uses_an_isolated_install(self):
        script = (ROOT / "scripts" / "verify_release.sh").read_text(encoding="utf-8")
        self.assertIn("python3 -m venv", script)
        self.assertIn("pip install --no-deps", script)
        self.assertIn("shopping-cli-api", script)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ContractGuardTest(unittest.TestCase):
    def fixture(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/verify_contract_lock.py", root / "scripts/verify_contract_lock.py")
        shutil.copytree(ROOT / "shopping_cli/contracts", root / "shopping_cli/contracts")
        return root

    def run_guard(self, root, extra=()):
        return subprocess.run(
            [sys.executable, str(root / "scripts/verify_contract_lock.py"), *extra],
            cwd=root,
            capture_output=True,
            text=True,
        )

    def test_default_guard_checks_each_vendored_schema_byte_without_sibling_checkout(self):
        root = self.fixture()
        schema = root / "shopping_cli/contracts/shopping.negotiation/0.1/decision.schema.json"
        schema.write_bytes(schema.read_bytes() + b"\n")
        self.assertNotEqual(self.run_guard(root).returncode, 0)

    def test_wrong_but_well_shaped_pin_or_unknown_lock_field_is_not_self_certification(self):
        for mutation in ("pin", "unknown"):
            with self.subTest(mutation=mutation):
                root = self.fixture()
                p = root / "shopping_cli/contracts/kiwi-contracts.lock.json"
                lock = json.loads(p.read_text())
                if mutation == "pin":
                    lock["source_commit"] = "0" * 40
                else:
                    lock["allow_unverified_vendor"] = True
                p.write_text(json.dumps(lock))
                self.assertNotEqual(self.run_guard(root).returncode, 0)

    def test_absent_or_changed_trusted_manifest_cannot_pass_default_guard(self):
        for mutation in ("absent", "changed"):
            with self.subTest(mutation=mutation):
                root = self.fixture()
                p = root / "shopping_cli/contracts/kiwi-contracts.manifest.json"
                if mutation == "absent":
                    p.unlink()
                else:
                    p.write_bytes(p.read_bytes() + b"\n")
                self.assertNotEqual(self.run_guard(root).returncode, 0)

    def test_clean_offline_pin_and_same_explicit_manifest_are_accepted(self):
        root = self.fixture()
        self.assertEqual(self.run_guard(root).returncode, 0)
        self.assertEqual(
            self.run_guard(
                root, ("--manifest", str(root / "shopping_cli/contracts/kiwi-contracts.manifest.json"))
            ).returncode,
            0,
        )


class PackageSourceGuardTest(unittest.TestCase):
    def test_docker_uses_unique_actual_project_wheel_not_fixed_version(self):
        docker = (ROOT / "Dockerfile").read_text()
        self.assertNotIn("shopping-cli[api]==3.0.1", docker)
        self.assertIn("set -- /wheels/shopping_cli-*.whl", docker)
        self.assertIn('test "$#" -eq 1 && test -f "$1"', docker)
        self.assertIn('"$1[api]"', docker)
        # Selection prefix only: do not run Docker or pip on fake wheels.
        command = 'root="$1"; set -- "$root"/shopping_cli-*.whl; test "$#" -eq 1 && test -f "$1"'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertNotEqual(subprocess.run(["bash", "-c", command, "selector", str(root)]).returncode, 0)
            (root / "shopping_cli-3.2.6-py3-none-any.whl").write_bytes(b"selection fixture only")
            self.assertEqual(subprocess.run(["bash", "-c", command, "selector", str(root)]).returncode, 0)
            (root / "shopping_cli-4.0.0-py3-none-any.whl").write_bytes(b"selection fixture only")
            self.assertNotEqual(subprocess.run(["bash", "-c", command, "selector", str(root)]).returncode, 0)

    def test_ci_and_release_sources_keep_existing_gates_and_require_normal_dependency_check(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn("uv run --locked bash scripts/quality.sh", workflow)
        self.assertIn("uv run --locked bash scripts/verify.sh", workflow)
        release = (ROOT / "scripts/verify_release.sh").read_text()
        self.assertNotIn("--no-deps", release)
        self.assertIn('pip install --force-reinstall "$1[api]"', release)
        self.assertIn("-m pip check", release)
        self.assertIn("import psutil, fastapi, uvicorn", release)

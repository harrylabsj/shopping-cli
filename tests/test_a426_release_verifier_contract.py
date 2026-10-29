"""Finite execution checks; no pip installation or release/package invocation."""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ReleaseVerifierExecutionTest(unittest.TestCase):
    def script(self):
        return (ROOT / "scripts/verify_release.sh").read_text(encoding="utf-8")

    def test_actual_shell_wheel_guard_accepts_only_one_regular_nonlink(self):
        script = self.script()
        start = script.index('set -- "$TMP_DIR"/dist/*.whl')
        end = script.index('"$TMP_DIR/venv/bin/python" -m pip install', start)
        guard = script[start:end]
        for mode in ("regular", "absent", "multiple", "directory", "symlink"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                dist = Path(root) / "dist"
                dist.mkdir()
                if mode == "regular":
                    (dist / "a.whl").write_bytes(b"synthetic guard fixture")
                elif mode == "multiple":
                    (dist / "a.whl").write_bytes(b"one")
                    (dist / "b.whl").write_bytes(b"two")
                elif mode == "directory":
                    (dist / "a.whl").mkdir()
                elif mode == "symlink":
                    target = Path(root) / "outside"
                    target.write_bytes(b"not an admitted wheel")
                    (dist / "a.whl").symlink_to(target)
                result = subprocess.run(
                    ["bash", "-c", 'set -euo pipefail\nTMP_DIR="$1"\n' + guard, "guard", root],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode == 0, mode == "regular", result.stderr)

    def run_import_gate(self, *, shadow=False, body_override=None):
        try:
            import psutil  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("psutil is not installed; run with the project venv (pip install -e .)")
        body = re.search(r"<<'PYTHON'\n(.*?)\nPYTHON", self.script(), re.DOTALL)
        self.assertIsNotNone(body)
        with tempfile.TemporaryDirectory() as root:
            prefix = Path(root) / "venv"
            site = prefix / "lib" / "site-packages"
            site.mkdir(parents=True)
            # Exact ordinary source as a package-location fixture, not an installed wheel claim.
            files = sorted((ROOT / "shopping_cli").rglob("*"))
            for src in files:
                if not src.is_file() or src.suffix not in {".py", ".json"}:
                    continue
                dst = site / src.relative_to(ROOT)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            working = Path(root) / "empty-working-directory"
            working.mkdir()
            setup = "import os, sys\n"
            setup += f"sys.prefix = {str(prefix)!r}\n"
            setup += f"sys.path.insert(0, {str(ROOT if shadow else site)!r})\n"
            setup += f"os.chdir({str(working)!r})\n"
            setup += "exec(" + repr(body_override if body_override is not None else body.group(1)) + ")\n"
            runner = Path(root) / "run_gate.py"
            runner.write_text(setup, encoding="utf-8")
            return subprocess.run([sys.executable, str(runner)], capture_output=True, text=True, check=False)

    def test_import_gate_executes_real_dependency_schema_imports_away_from_repository(self):
        result = self.run_import_gate()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_import_gate_rejects_real_source_checkout_shadow_even_if_import_succeeds(self):
        result = self.run_import_gate(shadow=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("shopping_cli import escaped the isolated venv", result.stderr)

    def test_legacy_imports_accept_checkout_shadow_but_new_origin_gate_refuses(self):
        # Exact historical Git blob, retained so archive/shallow CI need no history.
        legacy_bytes = (ROOT / "tests/fixtures/verify_release_7ec88.sh").read_bytes()
        self.assertEqual(
            hashlib.sha256(legacy_bytes).hexdigest(),
            "7d30b9deea321a86a9d6d17b397e29aea562072f067bb0da98993fc98b31f3c3",
        )
        legacy = legacy_bytes.decode("utf-8")
        snippet = re.search(r'-c "(import psutil[^\n]+)"', legacy)
        self.assertIsNotNone(snippet)
        old = self.run_import_gate(shadow=True, body_override=snippet.group(1))
        self.assertEqual(old.returncode, 0, old.stderr)
        fixed = self.run_import_gate(shadow=True)
        self.assertNotEqual(fixed.returncode, 0)
        self.assertIn("shopping_cli import escaped the isolated venv", fixed.stderr)

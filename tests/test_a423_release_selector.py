from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class ReleaseSelectorTest(unittest.TestCase):
    def test_guard_rejects_before_install_sentinel_and_accepts_single_regular_wheel(self):
        source = (ROOT / "scripts/verify_release.sh").read_text()
        start = source.index('set -- "$TMP_DIR"/dist/*.whl')
        end = source.index('"$TMP_DIR/venv/bin/python" -m pip install', start)
        fragment = source[start:end]
        for kind in ("zero", "multiple", "missing", "symlink", "single"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                dist = Path(directory) / "dist"
                dist.mkdir()
                if kind == "multiple":
                    (dist / "one.whl").write_bytes(b"selection fixture")
                    (dist / "two.whl").write_bytes(b"selection fixture")
                elif kind == "missing":
                    (dist / "missing.whl").symlink_to(dist / "absent.whl")
                elif kind == "symlink":
                    target = Path(directory) / "actual"
                    target.write_bytes(b"selection fixture")
                    (dist / "link.whl").symlink_to(target)
                elif kind == "single":
                    (dist / "one.whl").write_bytes(b"selection fixture")
                command = 'set -e\nTMP_DIR="$1"\n' + fragment + '\nprintf "WOULD_REACH_INSTALL\\n"\n'
                result = subprocess.run(["bash", "-c", command, "owned-selector", directory], capture_output=True, text=True)
                if kind == "single":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("WOULD_REACH_INSTALL", result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("WOULD_REACH_INSTALL", result.stdout)

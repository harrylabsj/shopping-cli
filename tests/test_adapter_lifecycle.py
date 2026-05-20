import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shopping_cli.adapters import hermes, openclaw


class AdapterLifecycleTest(unittest.TestCase):
    def make_fake_command(self, bin_dir: Path, name: str) -> None:
        path = bin_dir / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)

    def test_openclaw_inspect_doctor_and_install_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            self.make_fake_command(bin_dir, "openclaw")
            skill_root = tmp_path / ".openclaw" / "workspace" / "skills" / "shopping"
            skill_root.parent.mkdir(parents=True)
            skill_root.symlink_to(Path.cwd(), target_is_directory=True)
            db_file = tmp_path / "shopping.sqlite"

            with patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}):
                info = openclaw.inspect_host(db_path=db_file, project_root=Path.cwd(), skill_root=skill_root)
                doctor = openclaw.doctor(db_path=db_file, project_root=Path.cwd(), skill_root=skill_root)

            self.assertTrue(info["command_available"])
            self.assertTrue(info["project_root_valid"])
            self.assertTrue(info["skill_installed"])
            self.assertEqual(info["skill_target"], str(Path.cwd().resolve()))
            self.assertTrue(info["skill_points_to_project"])
            self.assertEqual(info["db_path"], str(db_file))
            self.assertTrue(doctor["ok"])
            install = openclaw.install_command(project_root=Path.cwd(), dry_run=True, force=True)
            self.assertEqual(install[-3:], ["--openclaw", "--dry-run", "--force"])

    def test_doctor_reports_stale_skill_symlink_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            self.make_fake_command(bin_dir, "openclaw")
            stale_root = tmp_path / "old-shopping"
            stale_root.mkdir()
            skill_root = tmp_path / ".openclaw" / "workspace" / "skills" / "shopping"
            skill_root.parent.mkdir(parents=True)
            skill_root.symlink_to(stale_root, target_is_directory=True)

            with patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}):
                info = openclaw.inspect_host(project_root=Path.cwd(), skill_root=skill_root)
                doctor = openclaw.doctor(project_root=Path.cwd(), skill_root=skill_root)

            self.assertEqual(info["skill_target"], str(stale_root.resolve()))
            self.assertFalse(info["skill_points_to_project"])
            self.assertFalse(doctor["ok"])
            self.assertIn("OpenClaw skill points to a different project root", doctor["issues"])

    def test_inspect_tolerates_skill_symlink_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            self.make_fake_command(bin_dir, "openclaw")
            skill_root = tmp_path / ".openclaw" / "workspace" / "skills" / "shopping"
            skill_root.parent.mkdir(parents=True)
            skill_root.symlink_to(skill_root, target_is_directory=True)

            with patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}):
                info = openclaw.inspect_host(project_root=Path.cwd(), skill_root=skill_root)
                doctor = openclaw.doctor(project_root=Path.cwd(), skill_root=skill_root)

            self.assertTrue(info["skill_is_symlink"])
            self.assertEqual(info["skill_target"], "")
            self.assertFalse(info["skill_points_to_project"])
            self.assertFalse(doctor["ok"])

    def test_hermes_inspect_reports_missing_host_or_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            missing_skill = tmp_path / ".hermes" / "skills" / "commerce" / "shopping"
            with patch.dict(os.environ, {"PATH": str(tmp_path)}):
                info = hermes.inspect_host(project_root=Path.cwd(), skill_root=missing_skill)
                doctor = hermes.doctor(project_root=Path.cwd(), skill_root=missing_skill)

            self.assertFalse(info["command_available"])
            self.assertFalse(info["skill_installed"])
            self.assertFalse(doctor["ok"])
            self.assertIn("hermes command not found", doctor["issues"])
            self.assertIn("Hermes skill is not installed", doctor["issues"])
            install = hermes.install_command(project_root=Path.cwd())
            self.assertEqual(install[-1], "--hermes")


if __name__ == "__main__":
    unittest.main()

import subprocess
import sys
import tempfile
from unittest import TestCase, mock
from shopping_cli.agents import merchant_daemon as daemon


class DaemonIdentity(TestCase):
    def setUp(self):
        # 进程身份核验依赖 psutil（项目的必需依赖）；缺依赖时这些用例无法
        # 判断身份，应跳过而不是把 fallback 行为当成产品缺陷。
        if daemon.psutil is None:
            self.skipTest("psutil is not installed; run with the project venv (pip install -e .)")

    def seed(self, tmp, pid, identity):
        paths = daemon.agent_paths("m", tmp)
        daemon.ensure_agent_dirs(paths)
        record = {
            "pid": pid,
            "merchant_id": "m",
            "launch_token": "synthetic-launch",
            "mode": "api",
            "interval": 3,
            "command": [sys.executable, "shopping_cli.cli", "agent", "run", "--merchant", "m"],
            **identity,
        }
        daemon.write_json_atomic(paths["pid_file"], record)
        daemon.write_json_atomic(
            paths["state_file"],
            {
                "pid": pid,
                "merchant_id": "m",
                "launch_token": "synthetic-launch",
                "running": True,
                "updated_at": "2000-01-01T00:00:00",
            },
        )
        return paths, record

    def test_real_owned_live_child_stale_state_blocks_second_spawn(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time;time.sleep(30)", "shopping_cli.cli", "agent", "run", "--merchant", "m"]
        )
        try:
            identity = daemon.process_identity(child.pid)
            self.assertIsNotNone(identity)
            with tempfile.TemporaryDirectory() as tmp:
                paths, record = self.seed(tmp, child.pid, identity)
                self.assertTrue(daemon.pid_record_matches_process(record, "m"))
                with mock.patch.object(daemon.subprocess, "Popen") as spawn:
                    with self.assertRaisesRegex(SystemExit, "already running"):
                        daemon.start_agent(
                            "unused", "m", state_dir=tmp, api_url="https://synthetic.invalid", agent_token="dummy"
                        )
                    spawn.assert_not_called()
                self.assertEqual(daemon.read_json(paths["pid_file"], {}), record)
        finally:
            child.terminate()
            child.wait(timeout=5)

    def test_unknown_eperm_blocks_start_and_stop_retains_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, record = self.seed(tmp, 123456, {"create_time": 1.0, "executable": sys.executable})
            with (
                mock.patch.object(daemon, "process_identity", return_value=None),
                mock.patch.object(daemon.os, "kill", side_effect=PermissionError("synthetic EPERM")) as kill,
                mock.patch.object(daemon.subprocess, "Popen") as spawn,
            ):
                with self.assertRaisesRegex(SystemExit, "unverified"):
                    daemon.start_agent(
                        "unused", "m", state_dir=tmp, api_url="https://synthetic.invalid", agent_token="dummy"
                    )
                result = daemon.stop_agent("unused", "m", state_dir=tmp, timeout=0)
                self.assertFalse(result["ok"])
                self.assertTrue(result["identity_unverified"])
                self.assertEqual(daemon.read_json(paths["pid_file"], {}), record)
                spawn.assert_not_called()
                self.assertTrue(all(call.args[1] == 0 for call in kill.call_args_list))

    def test_force_attempt_is_not_success_and_reports_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths, record = self.seed(tmp, 123456, {"create_time": 1.0, "executable": sys.executable})
            with (
                mock.patch.object(daemon, "pid_record_matches_process", return_value=True),
                mock.patch.object(daemon.os, "kill") as kill,
                mock.patch.object(daemon.time, "sleep"),
            ):
                result = daemon.stop_agent("unused", "m", state_dir=tmp, timeout=0)
            self.assertFalse(result["ok"])
            self.assertTrue(result["running"])
            self.assertTrue(result["stop_timed_out"])
            self.assertTrue(result["force_kill_attempted"])
            self.assertEqual(daemon.read_json(paths["pid_file"], {}), record)
            self.assertEqual(
                [call.args[1] for call in kill.call_args_list], [daemon.signal.SIGTERM, daemon.signal.SIGKILL]
            )

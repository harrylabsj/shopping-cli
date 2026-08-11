import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

try:
    import psutil
except ModuleNotFoundError:  # pragma: no cover - optional runtime dep
    psutil = None

ROOT = Path(__file__).resolve().parents[1]
MAI = ROOT / "scripts" / "shopping.py"
from shopping_cli.agents import merchant_daemon  # noqa: E402


class AgentDaemonLifecycleTest(unittest.TestCase):
    def run_shopping(self, *args, state_dir, check=True):
        env = os.environ.copy()
        env["SHOPPING_CLI_STATE_DIR"] = str(state_dir)
        proc = subprocess.run(
            [sys.executable, str(MAI), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        if check and proc.returncode != 0:
            self.fail(f"shopping.py {' '.join(args)} failed\nstdout={proc.stdout}\nstderr={proc.stderr}")
        return proc

    def seed_longjing_conversation(self, db_file, state_dir):
        self.run_shopping(
            "--db",
            str(db_file),
            "merchant",
            "create",
            "--id",
            "seller-a",
            "--name",
            "West Lake Tea",
            "--city",
            "Hangzhou",
            "--service-area",
            "West Lake",
            "--delivery-eta-minutes",
            "45",
            state_dir=state_dir,
        )
        self.run_shopping(
            "--db",
            str(db_file),
            "product",
            "add",
            "--merchant",
            "seller-a",
            "--sku",
            "tea-a",
            "--title",
            "Longjing Gift Box",
            "--price",
            "88",
            "--stock",
            "5",
            "--tags",
            "longjing,gift",
            state_dir=state_dir,
        )
        self.run_shopping(
            "--db",
            str(db_file),
            "buyer",
            "ask",
            "--buyer",
            "alice",
            "--text",
            "longjing gift delivery today",
            "--city",
            "Hangzhou",
            state_dir=state_dir,
        )

    def test_logs_agent_rejects_non_positive_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"

            for tail in (0, -1):
                with self.assertRaises(ValueError) as raised:
                    merchant_daemon.logs_agent("seller-a", tail=tail, state_dir=state_dir)
                self.assertIn("tail must be greater than 0", str(raised.exception))

    def test_logs_agent_tolerates_invalid_utf8_log_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            paths = merchant_daemon.agent_paths("seller-a", state_dir=state_dir)
            merchant_daemon.ensure_agent_dirs(paths)
            paths["log_file"].write_bytes(b"\xff")

            try:
                logs = merchant_daemon.logs_agent("seller-a", state_dir=state_dir)
            except UnicodeDecodeError as exc:
                self.fail(f"logs_agent should tolerate invalid UTF-8 log files: {exc}")

            self.assertEqual(logs["entries"], [])

    def test_logs_agent_treats_non_object_json_lines_as_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            paths = merchant_daemon.agent_paths("seller-a", state_dir=state_dir)
            merchant_daemon.ensure_agent_dirs(paths)
            paths["log_file"].write_text('"json string"\n[1]\n{"event": "ok"}\n', encoding="utf-8")

            logs = merchant_daemon.logs_agent("seller-a", tail=3, state_dir=state_dir)

            self.assertEqual(logs["entries"][0], {"event": "raw", "text": '"json string"'})
            self.assertEqual(logs["entries"][1], {"event": "raw", "text": "[1]"})
            self.assertEqual(logs["entries"][2], {"event": "ok"})

    def test_logs_agent_caps_oversized_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            paths = merchant_daemon.agent_paths("seller-a", state_dir=state_dir)
            merchant_daemon.ensure_agent_dirs(paths)
            lines = [f'{{"event": "line", "index": {index}}}' for index in range(1105)]
            paths["log_file"].write_text("\n".join(lines), encoding="utf-8")

            logs = merchant_daemon.logs_agent("seller-a", tail=10**100, state_dir=state_dir)

            self.assertEqual(len(logs["entries"]), 1000)
            self.assertEqual(logs["entries"][0]["index"], 105)
            self.assertEqual(logs["entries"][-1]["index"], 1104)

    def test_status_agent_tolerates_corrupt_pid_and_counters(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping.sqlite"
            state_dir = tmp_path / "state"
            paths = merchant_daemon.agent_paths("seller-a", state_dir=state_dir)
            merchant_daemon.ensure_agent_dirs(paths)
            paths["pid_file"].write_text(json.dumps({"pid": "bad"}), encoding="utf-8")
            paths["state_file"].write_text(
                json.dumps({"running": True, "counters": {"checked": "bad", "replied": "bad"}}),
                encoding="utf-8",
            )

            status = merchant_daemon.status_agent(db_file, "seller-a", state_dir=state_dir)

            self.assertIsNone(status["pid"])
            self.assertFalse(status["running"])
            self.assertEqual(status["counters"], {"checked": 0, "replied": 0})

    def test_status_agent_tolerates_non_finite_pid_and_counters(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping.sqlite"
            state_dir = tmp_path / "state"
            paths = merchant_daemon.agent_paths("seller-a", state_dir=state_dir)
            merchant_daemon.ensure_agent_dirs(paths)
            paths["pid_file"].write_text('{"pid": Infinity}', encoding="utf-8")
            paths["state_file"].write_text(
                '{"running": true, "counters": {"checked": Infinity, "replied": NaN}}',
                encoding="utf-8",
            )

            try:
                status = merchant_daemon.status_agent(db_file, "seller-a", state_dir=state_dir)
            except OverflowError as exc:
                self.fail(f"status_agent should tolerate non-finite state counters: {exc}")

            self.assertIsNone(status["pid"])
            self.assertFalse(status["running"])
            self.assertEqual(status["counters"], {"checked": 0, "replied": 0})

    def test_status_agent_tolerates_invalid_utf8_state_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping.sqlite"
            state_dir = tmp_path / "state"
            paths = merchant_daemon.agent_paths("seller-a", state_dir=state_dir)
            merchant_daemon.ensure_agent_dirs(paths)
            paths["pid_file"].write_bytes(b"\xff")
            paths["state_file"].write_bytes(b"\xff")

            try:
                status = merchant_daemon.status_agent(db_file, "seller-a", state_dir=state_dir)
            except UnicodeDecodeError as exc:
                self.fail(f"status_agent should tolerate invalid UTF-8 state files: {exc}")

            self.assertIsNone(status["pid"])
            self.assertFalse(status["running"])
            self.assertEqual(status["counters"], {"checked": 0, "replied": 0})

    def test_status_agent_tolerates_non_object_state_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping.sqlite"
            state_dir = tmp_path / "state"
            paths = merchant_daemon.agent_paths("seller-a", state_dir=state_dir)
            merchant_daemon.ensure_agent_dirs(paths)
            paths["pid_file"].write_text("[]", encoding="utf-8")
            paths["state_file"].write_text('"not an object"', encoding="utf-8")

            status = merchant_daemon.status_agent(db_file, "seller-a", state_dir=state_dir)

            self.assertIsNone(status["pid"])
            self.assertFalse(status["running"])
            self.assertEqual(status["counters"], {"checked": 0, "replied": 0})

    def test_process_loop_tolerates_non_json_result_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_file = tmp_path / "agent.state.json"
            stop_file = tmp_path / "agent.stop"
            marked_away = []

            def process_once():
                stop_file.write_text("stop", encoding="utf-8")
                return {"checked": 1, "replied": [{"raw": b"\xff"}]}

            def mark_away():
                marked_away.append(True)

            output = StringIO()
            with redirect_stdout(output):
                merchant_daemon._run_process_loop(
                    "seller-a",
                    process_once,
                    mark_away,
                    interval=0.01,
                    state_file=state_file,
                    stop_file=stop_file,
                )

            entries = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(entries[0]["event"], "process_once")
            self.assertEqual(entries[0]["checked"], 1)
            self.assertEqual(entries[0]["replied_count"], 1)
            self.assertIn("raw", entries[0]["result"]["replied"][0])
            self.assertEqual(marked_away, [True])
            self.assertFalse(json.loads(state_file.read_text(encoding="utf-8"))["running"])

    def test_process_loop_tolerates_corrupt_checked_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_file = tmp_path / "agent.state.json"
            stop_file = tmp_path / "agent.stop"

            def process_once():
                stop_file.write_text("stop", encoding="utf-8")
                return {"checked": "bad", "replied": [{"conversation_id": "CONV-0001"}]}

            output = StringIO()
            with redirect_stdout(output):
                merchant_daemon._run_process_loop(
                    "seller-a",
                    process_once,
                    lambda: None,
                    interval=0.01,
                    state_file=state_file,
                    stop_file=stop_file,
                )

            entries = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(entries[0]["event"], "process_once")
            self.assertEqual(entries[0]["checked"], 0)
            self.assertEqual(entries[0]["replied_count"], 1)
            final_state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertIsNone(final_state["last_error"])
            self.assertEqual(final_state["counters"], {"checked": 0, "replied": 1})

    def test_process_loop_tolerates_corrupt_replied_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_file = tmp_path / "agent.state.json"
            stop_file = tmp_path / "agent.stop"

            def process_once():
                stop_file.write_text("stop", encoding="utf-8")
                return {"checked": 1, "replied": "bad"}

            output = StringIO()
            with redirect_stdout(output):
                merchant_daemon._run_process_loop(
                    "seller-a",
                    process_once,
                    lambda: None,
                    interval=0.01,
                    state_file=state_file,
                    stop_file=stop_file,
                )

            entries = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(entries[0]["event"], "process_once")
            self.assertEqual(entries[0]["checked"], 1)
            self.assertEqual(entries[0]["replied_count"], 0)
            final_state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertIsNone(final_state["last_error"])
            self.assertEqual(final_state["counters"], {"checked": 1, "replied": 0})

    def test_process_loop_tolerates_non_finite_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            state_file = tmp_path / "agent.state.json"
            stop_file = tmp_path / "agent.stop"
            sleep_durations = []
            calls = []

            def process_once():
                calls.append(True)
                if len(calls) > 1:
                    stop_file.write_text("stop", encoding="utf-8")
                return {"checked": 1, "replied": []}

            def fake_sleep(duration):
                sleep_durations.append(duration)
                stop_file.write_text("stop", encoding="utf-8")

            with patch("shopping_cli.agents.merchant_daemon.time.sleep", side_effect=fake_sleep):
                output = StringIO()
                with redirect_stdout(output):
                    merchant_daemon._run_process_loop(
                        "seller-a",
                        process_once,
                        lambda: None,
                        interval=float("nan"),
                        state_file=state_file,
                        stop_file=stop_file,
                    )

            self.assertTrue(sleep_durations)
            self.assertGreater(sleep_durations[0], 0)
            self.assertEqual(len(calls), 1)

    def wait_for_status(self, db_file, state_dir, predicate, timeout=5):
        deadline = time.time() + timeout
        last_status = None
        while time.time() < deadline:
            proc = self.run_shopping(
                "agent",
                "status",
                "--merchant",
                "seller-a",
                "--db",
                str(db_file),
                "--format",
                "json",
                state_dir=state_dir,
            )
            last_status = json.loads(proc.stdout)
            if predicate(last_status):
                return last_status
            time.sleep(0.1)
        self.fail(f"status did not satisfy predicate; last={last_status}")

    def test_agent_daemon_start_status_logs_stop_and_duplicate_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping-cli.sqlite"
            state_dir = tmp_path / "state"
            self.seed_longjing_conversation(db_file, state_dir)

            start = json.loads(
                self.run_shopping(
                    "agent",
                    "start",
                    "--merchant",
                    "seller-a",
                    "--db",
                    str(db_file),
                    "--interval",
                    "0.1",
                    "--format",
                    "json",
                    state_dir=state_dir,
                ).stdout
            )
            self.assertTrue(start["running"])
            self.assertTrue(Path(start["pid_file"]).exists())
            self.assertTrue(Path(start["log_file"]).exists())

            try:
                status = self.wait_for_status(
                    db_file,
                    state_dir,
                    lambda value: value["running"] and value["counters"]["replied"] >= 1,
                )
                self.assertEqual(status["merchant_id"], "seller-a")
                self.assertEqual(status["heartbeat"]["status"], "online")
                self.assertGreaterEqual(status["counters"]["checked"], 1)

                summary = json.loads(
                    self.run_shopping(
                        "--db",
                        str(db_file),
                        "buyer",
                        "summarize",
                        "--conversation",
                        "CONV-0001",
                        "--format",
                        "json",
                        state_dir=state_dir,
                    ).stdout
                )
                self.assertEqual(summary["conversation"]["status"], "waiting_buyer")

                logs = json.loads(
                    self.run_shopping(
                        "agent",
                        "logs",
                        "--merchant",
                        "seller-a",
                        "--tail",
                        "20",
                        "--format",
                        "json",
                        state_dir=state_dir,
                    ).stdout
                )
                self.assertTrue(
                    any(entry.get("event") == "process_once" and entry.get("replied_count", 0) >= 1 for entry in logs["entries"])
                )

                duplicate = self.run_shopping(
                    "agent",
                    "start",
                    "--merchant",
                    "seller-a",
                    "--db",
                    str(db_file),
                    "--interval",
                    "0.1",
                    state_dir=state_dir,
                    check=False,
                )
                self.assertNotEqual(duplicate.returncode, 0)
                self.assertIn("already running", duplicate.stderr.lower())
            finally:
                self.run_shopping(
                    "agent",
                    "stop",
                    "--merchant",
                    "seller-a",
                    "--db",
                    str(db_file),
                    "--format",
                    "json",
                    state_dir=state_dir,
                    check=False,
                )

            stopped = self.wait_for_status(db_file, state_dir, lambda value: not value["running"])
            self.assertEqual(stopped["heartbeat"]["status"], "away")

    def test_api_backed_agent_start_does_not_require_local_merchant_or_leak_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping-cli.sqlite"
            state_dir = tmp_path / "state"

            class FakeProcess:
                pid = 987654321

            with (
                patch.dict(os.environ, {"SHOPPING_MERCHANT_TOKEN": "stale_merchant_secret"}, clear=False),
                patch("shopping_cli.agents.merchant_daemon.subprocess.Popen", return_value=FakeProcess()) as popen,
            ):
                started = merchant_daemon.start_agent(
                    db_file,
                    "seller-a",
                    interval=0.1,
                    state_dir=state_dir,
                    api_url="http://127.0.0.1:8765",
                    agent_token="agent_secret",
                    host="openclaw",
                    session_id="openclaw-session-1",
                )

            self.assertEqual(started["mode"], "api")
            self.assertEqual(started["host"], "openclaw")
            self.assertEqual(started["session_id"], "openclaw-session-1")
            pid_record = json.loads(Path(started["pid_file"]).read_text(encoding="utf-8"))
            command_text = " ".join(pid_record["command"])
            self.assertIn("--state-file", command_text)
            self.assertIn("--host openclaw", command_text)
            self.assertIn("--session-id openclaw-session-1", command_text)
            self.assertNotIn("agent_secret", command_text)
            self.assertEqual(pid_record["api_url"], "http://127.0.0.1:8765")
            self.assertEqual(pid_record["host"], "openclaw")
            self.assertEqual(pid_record["session_id"], "openclaw-session-1")

            child_env = popen.call_args.kwargs["env"]
            self.assertEqual(child_env["SHOPPING_MARKETPLACE_API_URL"], "http://127.0.0.1:8765")
            self.assertEqual(child_env["SHOPPING_AGENT_TOKEN"], "agent_secret")
            self.assertEqual(child_env["SHOPPING_AGENT_HOST"], "openclaw")
            self.assertEqual(child_env["SHOPPING_AGENT_SESSION_ID"], "openclaw-session-1")
            self.assertNotIn("SHOPPING_MERCHANT_TOKEN", child_env)

            stopped = merchant_daemon.stop_agent(db_file, "seller-a", state_dir=state_dir, timeout=0)
            self.assertTrue(stopped["ok"])
            self.assertEqual(stopped["mode"], "api")
            self.assertEqual(stopped["host"], "openclaw")
            self.assertEqual(stopped["session_id"], "openclaw-session-1")

            status = merchant_daemon.status_agent(db_file, "seller-a", state_dir=state_dir)
            self.assertFalse(status["running"])
            self.assertEqual(status["mode"], "api")
            self.assertEqual(status["host"], "openclaw")
            self.assertEqual(status["session_id"], "openclaw-session-1")

    def test_agent_start_tolerates_non_finite_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping-cli.sqlite"
            state_dir = tmp_path / "state"

            class FakeProcess:
                pid = 987654321

            with patch("shopping_cli.agents.merchant_daemon.subprocess.Popen", return_value=FakeProcess()):
                started = merchant_daemon.start_agent(
                    db_file,
                    "seller-a",
                    interval=float("nan"),
                    state_dir=state_dir,
                    api_url="http://127.0.0.1:8765",
                    agent_token="agent_secret",
                )

            pid_record = json.loads(Path(started["pid_file"]).read_text(encoding="utf-8"))
            command_text = " ".join(pid_record["command"])
            self.assertEqual(pid_record["interval"], 3.0)
            self.assertIn("--interval 3.0", command_text)
            self.assertNotIn("nan", command_text.lower())

    def test_agent_start_caps_oversized_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping-cli.sqlite"
            state_dir = tmp_path / "state"

            class FakeProcess:
                pid = 987654321

            with patch("shopping_cli.agents.merchant_daemon.subprocess.Popen", return_value=FakeProcess()):
                started = merchant_daemon.start_agent(
                    db_file,
                    "seller-a",
                    interval=10**100,
                    state_dir=state_dir,
                    api_url="http://127.0.0.1:8765",
                    agent_token="agent_secret",
                )

            pid_record = json.loads(Path(started["pid_file"]).read_text(encoding="utf-8"))
            command_text = " ".join(pid_record["command"])
            self.assertEqual(pid_record["interval"], 3600.0)
            self.assertIn("--interval 3600.0", command_text)

    def test_agent_start_tolerates_overflowing_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping-cli.sqlite"
            state_dir = tmp_path / "state"

            class FakeProcess:
                pid = 987654321

            with patch("shopping_cli.agents.merchant_daemon.subprocess.Popen", return_value=FakeProcess()):
                started = merchant_daemon.start_agent(
                    db_file,
                    "seller-a",
                    interval=10**4000,
                    state_dir=state_dir,
                    api_url="http://127.0.0.1:8765",
                    agent_token="agent_secret",
                )

            pid_record = json.loads(Path(started["pid_file"]).read_text(encoding="utf-8"))
            command_text = " ".join(pid_record["command"])
            self.assertEqual(pid_record["interval"], 3.0)
            self.assertIn("--interval 3.0", command_text)

    def test_agent_stop_tolerates_non_finite_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping-cli.sqlite"
            state_dir = tmp_path / "state"
            paths = merchant_daemon.agent_paths("seller-a", state_dir=state_dir)
            merchant_daemon.ensure_agent_dirs(paths)
            paths["pid_file"].write_text(json.dumps({"pid": 12345}), encoding="utf-8")
            paths["state_file"].write_text(
                json.dumps({"running": True, "counters": {"checked": 1, "replied": 0}}),
                encoding="utf-8",
            )
            sleep_durations = []

            def fake_sleep(duration):
                sleep_durations.append(duration)
                paths["state_file"].write_text(
                    json.dumps({"running": False, "counters": {"checked": 1, "replied": 0}}),
                    encoding="utf-8",
                )

            with (
                patch("shopping_cli.agents.merchant_daemon.is_process_running", return_value=True),
                patch("shopping_cli.agents.merchant_daemon.os.kill") as kill,
                patch("shopping_cli.agents.merchant_daemon.time.sleep", side_effect=fake_sleep),
                patch("shopping_cli.agents.merchant_agent.heartbeat"),
            ):
                stopped = merchant_daemon.stop_agent(
                    db_file,
                    "seller-a",
                    state_dir=state_dir,
                    timeout=float("nan"),
                )

            self.assertEqual(sleep_durations, [])
            kill.assert_not_called()
            self.assertTrue(stopped["ok"])

    def test_agent_stop_caps_oversized_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping-cli.sqlite"
            state_dir = tmp_path / "state"
            paths = merchant_daemon.agent_paths("seller-a", state_dir=state_dir)
            merchant_daemon.ensure_agent_dirs(paths)
            paths["pid_file"].write_text(json.dumps({"pid": 12345}), encoding="utf-8")
            paths["state_file"].write_text(
                json.dumps({"running": True, "counters": {"checked": 1, "replied": 0}}),
                encoding="utf-8",
            )
            sleep_durations = []

            def fake_sleep(duration):
                sleep_durations.append(duration)
                paths["state_file"].write_text(
                    json.dumps({"running": False, "counters": {"checked": 1, "replied": 0}}),
                    encoding="utf-8",
                )

            time_calls = 0

            def fake_time():
                nonlocal time_calls
                time_calls += 1
                return 0 if time_calls == 1 else 1000

            with (
                patch("shopping_cli.agents.merchant_daemon.is_process_running", return_value=True),
                patch("shopping_cli.agents.merchant_daemon.os.kill") as kill,
                patch("shopping_cli.agents.merchant_daemon.time.time", side_effect=fake_time),
                patch("shopping_cli.agents.merchant_daemon.time.sleep", side_effect=fake_sleep),
                patch("shopping_cli.agents.merchant_agent.heartbeat"),
            ):
                stopped = merchant_daemon.stop_agent(
                    db_file,
                    "seller-a",
                    state_dir=state_dir,
                    timeout=10**100,
                )

            self.assertEqual(sleep_durations, [])
            kill.assert_not_called()
            self.assertTrue(stopped["ok"])
            self.assertFalse(stopped["running"])

    def test_agent_stop_tolerates_overflowing_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping-cli.sqlite"
            state_dir = tmp_path / "state"
            paths = merchant_daemon.agent_paths("seller-a", state_dir=state_dir)
            merchant_daemon.ensure_agent_dirs(paths)
            paths["pid_file"].write_text(json.dumps({"pid": 12345}), encoding="utf-8")
            paths["state_file"].write_text(
                json.dumps({"running": True, "counters": {"checked": 1, "replied": 0}}),
                encoding="utf-8",
            )
            sleep_durations = []

            def fake_sleep(duration):
                sleep_durations.append(duration)
                paths["state_file"].write_text(
                    json.dumps({"running": False, "counters": {"checked": 1, "replied": 0}}),
                    encoding="utf-8",
                )

            with (
                patch("shopping_cli.agents.merchant_daemon.is_process_running", return_value=True),
                patch("shopping_cli.agents.merchant_daemon.os.kill") as kill,
                patch("shopping_cli.agents.merchant_daemon.time.sleep", side_effect=fake_sleep),
                patch("shopping_cli.agents.merchant_agent.heartbeat"),
            ):
                stopped = merchant_daemon.stop_agent(
                    db_file,
                    "seller-a",
                    state_dir=state_dir,
                    timeout=10**4000,
                )

            self.assertEqual(sleep_durations, [])
            kill.assert_not_called()
            self.assertTrue(stopped["ok"])


@unittest.skipIf(psutil is None, "psutil is required for process identity tests")
class AgentDaemonIdentityTest(unittest.TestCase):
    """P1-05 gates: identity mismatch is stale and is never signaled."""

    def spawn_child(self, *markers):
        return subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", *markers],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop_child(self, child):
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    def write_pid_record(self, paths, record):
        merchant_daemon.write_json_atomic(paths["pid_file"], record)

    def assert_no_signal_sent(self, kill_mock):
        # Signal-0 calls are liveness probes (e.g. from psutil), not signals.
        signaled = [call for call in kill_mock.call_args_list if call.args[1] != 0]
        self.assertEqual(signaled, [])

    def write_running_state(self, paths, merchant_id, pid, launch_token):
        merchant_daemon.write_state(
            paths["state_file"],
            merchant_id,
            running=True,
            counters={"checked": 0, "replied": 0},
            pid=pid,
            extra={"launch_token": launch_token},
        )

    def test_status_and_stop_treat_identity_mismatch_as_stale_and_never_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping-cli.sqlite"
            state_dir = tmp_path / "state"
            paths = merchant_daemon.agent_paths("seller-a", state_dir=state_dir)
            merchant_daemon.ensure_agent_dirs(paths)

            # Unrelated controlled child: no daemon markers in its cmdline.
            unrelated = self.spawn_child()
            # Controlled child that carries the daemon cmdline markers.
            marked = self.spawn_child("shopping_cli.cli", "agent", "run", "--merchant", "seller-a")
            children = [unrelated, marked]
            try:
                marked_identity = merchant_daemon.process_identity(marked.pid)
                self.assertIsNotNone(marked_identity)
                token = "launch-token-stale"
                base_record = {
                    "create_time": marked_identity["create_time"],
                    "executable": marked_identity["executable"],
                    "cmdline": marked_identity["cmdline"],
                    "launch_token": token,
                    "merchant_id": "seller-a",
                    "mode": "api",
                    "api_url": "http://127.0.0.1:9",
                    "command": [
                        sys.executable,
                        "-m",
                        "shopping_cli.cli",
                        "agent",
                        "run",
                        "--merchant",
                        "seller-a",
                    ],
                }
                cases = {
                    # PID reuse: a new process owns the PID with a different create_time.
                    "pid_reuse": {**base_record, "pid": marked.pid, "create_time": marked_identity["create_time"] + 100.0},
                    # Unrelated process: PID alive but cmdline is not the daemon.
                    "unrelated_process": {**base_record, "pid": unrelated.pid},
                    # Legacy pidfile: bare PID without any identity binding.
                    "legacy_pidfile": {"pid": marked.pid},
                    # Record written for a different merchant.
                    "merchant_mismatch": {**base_record, "pid": marked.pid, "merchant_id": "seller-b"},
                }
                for name, record in cases.items():
                    with self.subTest(case=name):
                        launch_token = str(record.get("launch_token") or "")
                        self.write_pid_record(paths, record)
                        self.write_running_state(paths, "seller-a", record["pid"], launch_token)

                        status = merchant_daemon.status_agent(db_file, "seller-a", state_dir=state_dir)
                        self.assertFalse(status["running"])
                        self.assertTrue(status["stale_pid"])

                        with (
                            patch("shopping_cli.agents.merchant_daemon.os.kill") as kill,
                            patch("shopping_cli.agents.merchant_agent.heartbeat"),
                        ):
                            stopped = merchant_daemon.stop_agent(
                                db_file,
                                "seller-a",
                                state_dir=state_dir,
                                timeout=0.1,
                            )
                        self.assertTrue(stopped["ok"])
                        self.assertFalse(stopped["was_running"])
                        self.assert_no_signal_sent(kill)
                        for child in children:
                            self.assertIsNone(child.poll())
            finally:
                for child in children:
                    self.stop_child(child)

    def test_start_replaces_legacy_pidfile_without_signaling(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping-cli.sqlite"
            state_dir = tmp_path / "state"
            paths = merchant_daemon.agent_paths("seller-a", state_dir=state_dir)
            merchant_daemon.ensure_agent_dirs(paths)

            child = self.spawn_child()
            try:
                # Legacy pidfile binds only a bare PID that is now owned by an
                # unrelated process; start must treat it as stale, never signal.
                paths["pid_file"].write_text(json.dumps({"pid": child.pid}), encoding="utf-8")

                class FakeProcess:
                    pid = 987654321

                with (
                    patch("shopping_cli.agents.merchant_daemon.subprocess.Popen", return_value=FakeProcess()),
                    patch("shopping_cli.agents.merchant_daemon.wait_for_process_identity", return_value={
                        "create_time": 1000.0, "executable": sys.executable,
                        "cmdline": ["shopping_cli.cli", "agent", "run", "--merchant", "seller-a"],
                    }),
                    patch("shopping_cli.agents.merchant_daemon.os.kill") as kill,
                ):
                    started = merchant_daemon.start_agent(
                        db_file,
                        "seller-a",
                        interval=0.1,
                        state_dir=state_dir,
                        api_url="http://127.0.0.1:9",
                        agent_token="agent_secret",
                    )

                self.assertTrue(started["ok"])
                self.assertTrue(started["stale_replaced"])
                self.assert_no_signal_sent(kill)
                self.assertIsNone(child.poll())
                record = json.loads(paths["pid_file"].read_text(encoding="utf-8"))
                self.assertEqual(record["pid"], 987654321)
                self.assertTrue(record["launch_token"])
                self.assertEqual(record["merchant_id"], "seller-a")
            finally:
                self.stop_child(child)

    def test_stop_racing_new_start_keeps_new_pidfile_state_and_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping-cli.sqlite"
            state_dir = tmp_path / "state"
            paths = merchant_daemon.agent_paths("seller-a", state_dir=state_dir)
            markers = ("shopping_cli.cli", "agent", "run", "--merchant", "seller-a")
            child_a = self.spawn_child(*markers)
            child_b = self.spawn_child(*markers)
            real_kill = os.kill
            try:
                with patch("shopping_cli.agents.merchant_daemon.subprocess.Popen", return_value=child_a):
                    started_a = merchant_daemon.start_agent(
                        db_file,
                        "seller-a",
                        interval=0.1,
                        state_dir=state_dir,
                        api_url="http://127.0.0.1:9",
                        agent_token="token-a",
                    )
                self.assertEqual(started_a["pid"], child_a.pid)
                token_a = json.loads(paths["pid_file"].read_text(encoding="utf-8"))["launch_token"]

                def kill_then_relaunch(pid, sig):
                    if sig == 0:
                        # Liveness probe (e.g. from psutil): pass through.
                        return real_kill(pid, sig)
                    # The old daemon exits on SIGTERM and a new start takes over
                    # before the in-flight stop re-acquires the lock.
                    real_kill(pid, sig)
                    child_a.wait(timeout=5)
                    with patch("shopping_cli.agents.merchant_daemon.subprocess.Popen", return_value=child_b):
                        merchant_daemon.start_agent(
                            db_file,
                            "seller-a",
                            interval=0.1,
                            state_dir=state_dir,
                            api_url="http://127.0.0.1:9",
                            agent_token="token-b",
                        )

                with patch("shopping_cli.agents.merchant_daemon.os.kill", side_effect=kill_then_relaunch) as kill:
                    stopped = merchant_daemon.stop_agent(
                        db_file,
                        "seller-a",
                        state_dir=state_dir,
                        timeout=0.5,
                    )

                # SIGTERM went only to the identity-verified old daemon.
                sigterm_calls = [
                    call for call in kill.call_args_list if call.args[1] == merchant_daemon.signal.SIGTERM
                ]
                self.assertEqual([call.args[0] for call in sigterm_calls], [child_a.pid])

                record_b = json.loads(paths["pid_file"].read_text(encoding="utf-8"))
                self.assertEqual(record_b["pid"], child_b.pid)
                self.assertTrue(record_b["launch_token"])
                self.assertNotEqual(record_b["launch_token"], token_a)
                self.assertTrue(stopped["was_running"])
                state_b = json.loads(paths["state_file"].read_text(encoding="utf-8"))
                self.assertTrue(state_b["running"])
                self.assertEqual(state_b["launch_token"], record_b["launch_token"])
                self.assertEqual(state_b["pid"], child_b.pid)
                self.assertIsNone(child_b.poll())
            finally:
                self.stop_child(child_a)
                self.stop_child(child_b)


class AgentDaemonFilePermissionsTest(unittest.TestCase):
    """P2-07：daemon 日志/stop 文件权限必须 0600——fresh/rotated/existing/stop 全覆盖。"""

    @staticmethod
    def file_mode(path) -> int:
        return Path(path).stat().st_mode & 0o777

    def test_fresh_log_file_created_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "logs" / "agent.log"
            log_file.parent.mkdir(parents=True)
            with merchant_daemon._open_log_append(log_file):
                log_file.write_text("fresh log line\n", encoding="utf-8")
            self.assertEqual(self.file_mode(log_file), 0o600)

    def test_existing_log_file_rechmodded_0600(self):
        # 修复前遗留的世界可读日志：_open_log_append 必须把已存在文件也 chmod 0600
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "logs" / "agent.log"
            log_file.parent.mkdir(parents=True)
            log_file.write_text("legacy world-readable log\n", encoding="utf-8")
            os.chmod(log_file, 0o644)
            self.assertEqual(self.file_mode(log_file), 0o644)
            with merchant_daemon._open_log_append(log_file):
                log_file.write_text("appended\n", encoding="utf-8")
            self.assertEqual(self.file_mode(log_file), 0o600)

    def test_rotated_log_backups_are_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = merchant_daemon.agent_paths("seller-a", tmp)
            merchant_daemon.ensure_agent_dirs(paths)
            log_file = paths["log_file"]
            log_file.write_bytes(b"x" * (merchant_daemon.MAX_AGENT_LOG_BYTES + 1))
            os.chmod(log_file, 0o644)
            self.assertTrue(merchant_daemon.rotate_agent_log(log_file))
            backup = Path(str(log_file) + ".1")
            self.assertTrue(backup.exists())
            self.assertEqual(self.file_mode(backup), 0o600)

    def test_stop_file_written_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = merchant_daemon.agent_paths("seller-a", tmp)
            merchant_daemon.ensure_agent_dirs(paths)
            merchant_daemon._write_private_text(paths["stop_file"], "launch-token")
            self.assertEqual(self.file_mode(paths["stop_file"]), 0o600)
            self.assertEqual(paths["stop_file"].read_text(encoding="utf-8"), "launch-token")
            # 覆盖已存在的世界可读 stop 文件也强制 0600
            os.chmod(paths["stop_file"], 0o644)
            merchant_daemon._write_private_text(paths["stop_file"], "rotated-token")
            self.assertEqual(self.file_mode(paths["stop_file"]), 0o600)
            self.assertEqual(paths["stop_file"].read_text(encoding="utf-8"), "rotated-token")

    def test_pid_lock_file_0600(self):
        # P3-03：_PidFileLock 锁文件与日志/stop 文件对齐 0600（此前 0644），
        # 已存在的遗留世界可读锁文件也显式 chmod
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "agent.pid"
            with merchant_daemon._PidFileLock(pid_file) as lock:
                self.assertEqual(self.file_mode(lock.lock_path), 0o600)
            os.chmod(pid_file.with_suffix(".pid.lock"), 0o644)
            with merchant_daemon._PidFileLock(pid_file) as lock:
                self.assertEqual(self.file_mode(lock.lock_path), 0o600)

    def test_start_agent_creates_0600_log_and_credential_files(self):
        from shopping_cli.core.catalog import create_merchant
        from shopping_cli.db.session import db_session

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_file = tmp_path / "shopping-cli.sqlite"
            state_dir = tmp_path / "state"
            with db_session(db_file) as conn:
                create_merchant(conn, "seller-a", "West Lake Tea", city="Hangzhou")
            started = None
            try:
                started = merchant_daemon.start_agent(
                    db_file, "seller-a", interval=1.0, state_dir=state_dir
                )
                self.assertTrue(started["running"])
                self.assertEqual(self.file_mode(started["log_file"]), 0o600)
                self.assertEqual(self.file_mode(started["pid_file"]), 0o600)
                self.assertEqual(self.file_mode(started["state_file"]), 0o600)
            finally:
                if started and started.get("running"):
                    merchant_daemon.stop_agent(db_file, "seller-a", state_dir=state_dir, timeout=2.0)


if __name__ == "__main__":
    unittest.main()

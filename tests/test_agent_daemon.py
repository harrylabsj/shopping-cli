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
                pid = 12345

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
                pid = 12345

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
                pid = 12345

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
                pid = 12345

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
                patch("shopping_cli.agents.merchant_daemon.os.kill"),
                patch("shopping_cli.agents.merchant_daemon.time.sleep", side_effect=fake_sleep),
                patch("shopping_cli.agents.merchant_agent.heartbeat"),
            ):
                stopped = merchant_daemon.stop_agent(
                    db_file,
                    "seller-a",
                    state_dir=state_dir,
                    timeout=float("nan"),
                )

            self.assertTrue(sleep_durations)
            self.assertGreater(sleep_durations[0], 0)
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
                patch("shopping_cli.agents.merchant_daemon.os.kill"),
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
            self.assertFalse(stopped["ok"])
            self.assertTrue(stopped["running"])

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
                patch("shopping_cli.agents.merchant_daemon.os.kill"),
                patch("shopping_cli.agents.merchant_daemon.time.sleep", side_effect=fake_sleep),
                patch("shopping_cli.agents.merchant_agent.heartbeat"),
            ):
                stopped = merchant_daemon.stop_agent(
                    db_file,
                    "seller-a",
                    state_dir=state_dir,
                    timeout=10**4000,
                )

            self.assertTrue(sleep_durations)
            self.assertTrue(stopped["ok"])


if __name__ == "__main__":
    unittest.main()

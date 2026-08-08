"""Local lifecycle management for resident merchant-agent processes."""

from __future__ import annotations

import json
import hashlib
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable
from datetime import datetime

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

try:
    import psutil
except ImportError:  # pragma: no cover - optional runtime dependency
    psutil = None

from shopping_cli.agents import merchant_agent
from shopping_cli.db.session import db_session, decode_json, now_iso
from shopping_cli.core.limits import safe_non_negative_int, safe_positive_float, safe_non_negative_float_with_max as safe_non_negative_float

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "shopping-cli"
MAX_AGENT_INTERVAL_SECONDS = 3600.0
MAX_AGENT_STOP_TIMEOUT_SECONDS = 300.0
MAX_AGENT_LOG_TAIL = 1000
MAX_AGENT_LOG_BYTES = 5 * 1024 * 1024
MAX_AGENT_LOG_BACKUPS = 3
MAX_AGENT_ERROR_BACKOFF_SECONDS = 60.0


def state_dir_from(value: str | Path | None = None) -> Path:
    return Path(value or os.environ.get("SHOPPING_CLI_STATE_DIR") or DEFAULT_STATE_DIR).expanduser()


def safe_merchant_id(merchant_id: str) -> str:
    raw = str(merchant_id or "")
    slug = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in raw).strip("._-")
    slug = (slug or "merchant")[:64]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{slug}-{digest}"


def agent_paths(merchant_id: str, state_dir: str | Path | None = None) -> dict[str, Path]:
    root = state_dir_from(state_dir)
    safe_id = safe_merchant_id(merchant_id)
    return {
        "state_dir": root,
        "pid_file": root / "agents" / f"{safe_id}.pid",
        "state_file": root / "agents" / f"{safe_id}.state.json",
        "stop_file": root / "agents" / f"{safe_id}.stop",
        "log_file": root / "logs" / f"{safe_id}.log",
    }


def ensure_agent_dirs(paths: dict[str, Path]) -> None:
    paths["pid_file"].parent.mkdir(parents=True, exist_ok=True)
    paths["state_file"].parent.mkdir(parents=True, exist_ok=True)
    paths["log_file"].parent.mkdir(parents=True, exist_ok=True)


def rotate_agent_log(log_file: Path) -> bool:
    try:
        if log_file.stat().st_size <= MAX_AGENT_LOG_BYTES:
            return False
    except OSError:
        return False
    oldest = log_file.with_suffix(log_file.suffix + f".{MAX_AGENT_LOG_BACKUPS}")
    if oldest.exists():
        oldest.unlink()
    for index in range(MAX_AGENT_LOG_BACKUPS - 1, 0, -1):
        source = log_file.with_suffix(log_file.suffix + f".{index}")
        if source.exists():
            source.replace(log_file.with_suffix(log_file.suffix + f".{index + 1}"))
    log_file.replace(log_file.with_suffix(log_file.suffix + ".1"))
    return True


def _redirect_stdout_to(log_path: Path) -> None:
    """Repoint stdout at a freshly rotated log file.

    Renaming the live log leaves the daemon writing into the renamed backup,
    so after an in-process rotation stdout must be attached to the new file.
    """
    try:
        target_fd = sys.stdout.fileno()
    except (OSError, ValueError):
        return
    fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.dup2(fd, target_fd)
    finally:
        os.close(fd)


def tail_log_lines(path: Path, count: int, max_bytes: int = 2 * 1024 * 1024) -> list[str]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            remaining = handle.tell()
            chunks: list[bytes] = []
            newlines = 0
            read_bytes = 0
            while remaining > 0 and newlines <= count and read_bytes < max_bytes:
                size = min(65536, remaining, max_bytes - read_bytes)
                remaining -= size
                handle.seek(remaining)
                chunk = handle.read(size)
                chunks.append(chunk)
                newlines += chunk.count(b"\n")
                read_bytes += len(chunk)
        return b"".join(reversed(chunks)).decode("utf-8").splitlines()[-count:]
    except (OSError, UnicodeDecodeError):
        return []


def read_json(path: Path, default: Any) -> Any:
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default
    if isinstance(default, dict) and not isinstance(decoded, dict):
        return default
    return decoded





def safe_replied_count(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    return len(value)


def permanent_agent_error(error: str) -> bool:
    lowered = str(error or "").lower()
    return any(marker in lowered for marker in ("invalid authorization", "revoked authorization", "expired authorization", "token required", "unknown merchant"))


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


class _PidFileLock:
    """Advisory exclusive lock on a companion lock file for the PID file.

    Uses ``fcntl`` on POSIX and ``msvcrt.locking`` on Windows.
    """

    def __init__(self, pid_file: Path) -> None:
        self.lock_path = Path(pid_file).with_suffix(pid_file.suffix + ".lock")
        self._fd: int | None = None

    def __enter__(self) -> "_PidFileLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(
            str(self.lock_path),
            os.O_RDWR | os.O_CREAT,
            0o644,
        )
        if fcntl is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise RuntimeError(f"Failed to acquire PID file lock ({self.lock_path}): {exc}") from exc
        elif msvcrt is not None:  # pragma: no cover - Windows
            try:
                if os.fstat(self._fd).st_size == 0:
                    os.write(self._fd, b"0")
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)
            except OSError as exc:
                raise RuntimeError(f"Failed to acquire PID file lock ({self.lock_path}): {exc}") from exc
        else:  # pragma: no cover - unsupported platform
            raise RuntimeError("No supported PID file locking implementation is available")
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._fd is None:
            return
        if fcntl is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            except OSError:
                pass
        elif msvcrt is not None:  # pragma: no cover - Windows
            try:
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None


def is_process_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if psutil is None:
        return _is_process_running_legacy(pid)
    try:
        if not psutil.pid_exists(pid):
            return False
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _is_process_running_legacy(pid: int) -> bool:
    """Unix-only fallback when psutil is unavailable."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    state = status.stdout.strip().upper()
    if status.returncode != 0 or not state:
        # os.kill(pid, 0) above proved that a process currently owns the PID.
        # Some sandboxes deny `ps`; cooperative stop remains safe because the
        # fallback never sends a signal without psutil identity verification.
        return True
    return "Z" not in state


def process_identity(pid: int) -> dict[str, Any] | None:
    if psutil is None or not is_process_running(pid):
        return None
    try:
        process = psutil.Process(pid)
        return {
            "create_time": float(process.create_time()),
            "executable": str(process.exe() or ""),
            "cmdline": [str(item) for item in process.cmdline()],
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
        return None


def wait_for_process_identity(pid: int, command: list[str], timeout: float = 0.5) -> dict[str, Any] | None:
    """Wait briefly for a newly spawned Python process to finish ``exec``."""
    deadline = time.monotonic() + timeout
    last_identity: dict[str, Any] | None = None
    ready_since: float | None = None
    required = ["shopping_cli.cli", "agent", "run", "--merchant"]
    while time.monotonic() < deadline:
        identity = process_identity(pid)
        if identity is not None:
            last_identity = identity
            cmdline = [str(item) for item in identity.get("cmdline") or []]
            if all(item in cmdline for item in required):
                now = time.monotonic()
                if ready_since is None:
                    ready_since = now
                elif now - ready_since >= 0.1:
                    return identity
            else:
                ready_since = None
        elif not is_process_running(pid):
            break
        time.sleep(0.01)
    return last_identity


def pid_record_matches_process(pid_record: dict[str, Any], merchant_id: str) -> bool:
    """Verify that a PID record still identifies this exact daemon process."""
    pid = safe_non_negative_int(pid_record.get("pid"))
    launch_token = str(pid_record.get("launch_token") or "")
    recorded_create_time = pid_record.get("create_time", 0.0)
    if not pid or not launch_token or str(pid_record.get("merchant_id") or "") != str(merchant_id):
        return False
    recorded_command = [str(item) for item in (pid_record.get("command") or [])]
    required = ["shopping_cli.cli", "agent", "run", "--merchant", str(merchant_id)]
    if not all(item in recorded_command for item in required):
        return False
    if psutil is None:
        return is_process_running(pid)
    identity = process_identity(pid)
    if identity is None:
        return False
    try:
        if abs(float(recorded_create_time) - float(identity["create_time"])) > 0.01:
            return False
    except (TypeError, ValueError):
        return False
    recorded_executable = str(pid_record.get("executable") or "")
    if recorded_executable and os.path.realpath(recorded_executable) != os.path.realpath(identity["executable"]):
        return False
    cmdline = list(identity.get("cmdline") or [])
    if not all(item in cmdline for item in required):
        return False
    return True


def state_matches_pid_record(state: dict[str, Any], pid_record: dict[str, Any], merchant_id: str) -> bool:
    launch_token = str(pid_record.get("launch_token") or "")
    if not launch_token or str(state.get("launch_token") or "") != launch_token:
        return False
    if str(state.get("merchant_id") or "") != str(merchant_id):
        return False
    state_pid = safe_non_negative_int(state.get("pid"))
    record_pid = safe_non_negative_int(pid_record.get("pid"))
    if state_pid and state_pid != record_pid:
        return False
    try:
        updated_at = datetime.fromisoformat(str(state.get("updated_at") or ""))
        interval = safe_positive_float(pid_record.get("interval"), 3.0, maximum=MAX_AGENT_INTERVAL_SECONDS)
        max_age = max(interval * 3, 60.0)
        if (datetime.now() - updated_at).total_seconds() > max_age:
            return False
    except (TypeError, ValueError):
        return False
    return True


def read_agent_heartbeat(db_path: str | Path, merchant_id: str) -> dict[str, Any]:
    agent_id = f"shopping-cli-merchant-agent:{merchant_id}"
    with db_session(db_path) as conn:
        row = conn.execute("select * from agents where id = ?", (agent_id,)).fetchone()
    if row is None:
        return {
            "id": agent_id,
            "type": "merchant",
            "owner_id": merchant_id,
            "status": "away",
            "capabilities": [],
            "last_seen_at": None,
        }
    return {
        "id": row["id"],
        "type": row["type"],
        "owner_id": row["owner_id"],
        "status": row["status"],
        "capabilities": decode_json(row["capabilities_json"], []),
        "last_seen_at": row["last_seen_at"],
    }


def write_state(
    state_file: Path,
    merchant_id: str,
    running: bool,
    counters: dict[str, int],
    last_error: str | None = None,
    pid: int | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = {
        "merchant_id": merchant_id,
        "running": running,
        "pid": pid,
        "updated_at": now_iso(),
        "counters": counters,
        "last_error": last_error,
    }
    if extra:
        state.update(extra)
    write_json_atomic(state_file, state)
    return state


def start_agent(
    db_path: str | Path,
    merchant_id: str,
    interval: float = 3.0,
    state_dir: str | Path | None = None,
    api_url: str = "",
    agent_token: str = "",
    merchant_token: str = "",
    host: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    api_url = str(api_url or "").strip()
    agent_token = str(agent_token or "").strip()
    merchant_token = str(merchant_token or "").strip()
    host = str(host or "").strip()
    session_id = str(session_id or "").strip()
    mode = "api" if api_url else "sqlite"
    if api_url and not (agent_token or merchant_token):
        raise SystemExit("--merchant-token or --agent-token is required with --api-url")
    interval = safe_positive_float(interval, 3.0, maximum=MAX_AGENT_INTERVAL_SECONDS)

    paths = agent_paths(merchant_id, state_dir)
    ensure_agent_dirs(paths)

    with _PidFileLock(paths["pid_file"]):
        pid_record = read_json(paths["pid_file"], {})
        existing_pid = safe_non_negative_int(pid_record.get("pid"))
        existing_state = read_json(paths["state_file"], {})
        existing_matches = (
            state_matches_pid_record(existing_state, pid_record, merchant_id)
            and pid_record_matches_process(pid_record, merchant_id)
        )
        stale_replaced = bool(existing_pid and not existing_matches)
        if existing_pid and existing_matches:
            raise SystemExit(f"Agent already running for merchant {merchant_id}: pid {existing_pid}")
        if paths["stop_file"].exists():
            paths["stop_file"].unlink()

        if not api_url:
            with db_session(db_path) as conn:
                merchant_agent.heartbeat(conn, merchant_id, status="online")

        rotate_agent_log(paths["log_file"])

        repo_root = Path(__file__).resolve().parents[2]
        command = [
            sys.executable,
            "-m",
            "shopping_cli.cli",
            "--db",
            str(Path(db_path).expanduser()),
            "agent",
            "run",
            "--merchant",
            merchant_id,
            "--interval",
            str(interval),
            "--format",
            "json",
            "--state-file",
            str(paths["state_file"]),
            "--stop-file",
            str(paths["stop_file"]),
        ]
        if host:
            command.extend(["--host", host])
        if session_id:
            command.extend(["--session-id", session_id])
        env = os.environ.copy()
        launch_token = secrets.token_urlsafe(24)
        env["SHOPPING_CLI_STATE_DIR"] = str(paths["state_dir"])
        env["SHOPPING_AGENT_LAUNCH_TOKEN"] = launch_token
        if api_url:
            env["SHOPPING_MARKETPLACE_API_URL"] = api_url
            if host:
                env["SHOPPING_AGENT_HOST"] = host
            if session_id:
                env["SHOPPING_AGENT_SESSION_ID"] = session_id
            if agent_token:
                env["SHOPPING_AGENT_TOKEN"] = agent_token
                env.pop("SHOPPING_MERCHANT_TOKEN", None)
            elif merchant_token:
                env["SHOPPING_MERCHANT_TOKEN"] = merchant_token
                env.pop("SHOPPING_AGENT_TOKEN", None)
        with paths["log_file"].open("ab", buffering=0) as log:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=str(repo_root),
                env=env,
                start_new_session=True,
            )

        started_at = now_iso()
        identity = wait_for_process_identity(process.pid, command) or {
            "create_time": 0.0,
            "executable": "",
            "cmdline": command,
        }
        pid_payload = {
            "pid": process.pid,
            "create_time": identity["create_time"],
            "executable": identity["executable"],
            "cmdline": identity["cmdline"],
            "launch_token": launch_token,
            "merchant_id": merchant_id,
            "db_path": str(Path(db_path).expanduser()),
            "interval": interval,
            "mode": mode,
            "api_url": api_url,
            "host": host,
            "session_id": session_id,
            "started_at": started_at,
            "command": command,
            "log_file": str(paths["log_file"]),
            "state_file": str(paths["state_file"]),
            "stop_file": str(paths["stop_file"]),
        }
        write_json_atomic(paths["pid_file"], pid_payload)

    write_state(
        paths["state_file"],
        merchant_id,
        running=True,
        counters={"checked": 0, "replied": 0},
        pid=process.pid,
        extra={
            "started_at": started_at,
            "mode": mode,
            "api_url": api_url,
            "host": host,
            "session_id": session_id,
            "launch_token": launch_token,
        },
    )
    return {
        "ok": True,
        "merchant_id": merchant_id,
        "pid": process.pid,
        "running": True,
        "mode": mode,
        "api_url": api_url,
        "host": host,
        "session_id": session_id,
        "stale_replaced": stale_replaced,
        "pid_file": str(paths["pid_file"]),
        "state_file": str(paths["state_file"]),
        "stop_file": str(paths["stop_file"]),
        "log_file": str(paths["log_file"]),
    }


def stop_agent(
    db_path: str | Path,
    merchant_id: str,
    state_dir: str | Path | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    timeout = safe_non_negative_float(timeout, 5.0, maximum=MAX_AGENT_STOP_TIMEOUT_SECONDS)
    paths = agent_paths(merchant_id, state_dir)
    with _PidFileLock(paths["pid_file"]):
        pid_record = read_json(paths["pid_file"], {})
        pid = safe_non_negative_int(pid_record.get("pid"))
        mode = str(pid_record.get("mode") or "sqlite")
        host = str(pid_record.get("host") or "")
        session_id = str(pid_record.get("session_id") or "")
        api_url = str(pid_record.get("api_url") or "")
        launch_token = str(pid_record.get("launch_token") or "")
        state_before_stop = read_json(paths["state_file"], {})
        state_launch_token = str(state_before_stop.get("launch_token") or "")
        was_running = bool(
            launch_token
            and state_launch_token == launch_token
            and state_matches_pid_record(state_before_stop, pid_record, merchant_id)
            and pid_record_matches_process(pid_record, merchant_id)
        )

    if was_running:
        paths["stop_file"].parent.mkdir(parents=True, exist_ok=True)
        paths["stop_file"].write_text(launch_token, encoding="utf-8")
        if psutil is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except PermissionError:
                pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = read_json(paths["state_file"], {})
            if state.get("running") is False or not pid_record_matches_process(pid_record, merchant_id):
                break
            time.sleep(0.1)
        # SIGKILL 兜底：SIGTERM 被阻塞的处理中进程（如卡在 HTTP 调用，
        # 默认 5s 内无法响应）停不掉——强杀并短等待。
        if pid_record_matches_process(pid_record, merchant_id):
            try:
                os.kill(pid, signal.SIGKILL)
            except (PermissionError, ProcessLookupError):
                pass
            time.sleep(0.2)

    with _PidFileLock(paths["pid_file"]):
        state_after_stop = read_json(paths["state_file"], {})
        current_record = read_json(paths["pid_file"], {})
        same_launch = bool(
            launch_token
            and str(current_record.get("launch_token") or "") == launch_token
            and safe_non_negative_int(current_record.get("pid")) == pid
        )
        # A different launch token means a new daemon took over the pid/state
        # files while this stop was in flight; it must remain untouched.
        replaced_by_new_launch = bool(str(current_record.get("launch_token") or "")) and not same_launch
        running = same_launch and pid_record_matches_process(current_record, merchant_id) and state_after_stop.get("running") is not False
        if not running and same_launch and paths["pid_file"].exists():
            paths["pid_file"].unlink()

    if mode != "api":
        with db_session(db_path) as conn:
            merchant_agent.heartbeat(conn, merchant_id, status="away")

    previous = read_json(paths["state_file"], {})
    counters = previous.get("counters") or {"checked": 0, "replied": 0}
    if not running and not replaced_by_new_launch:
        write_state(
            paths["state_file"],
            merchant_id,
            running=False,
            counters=counters,
            last_error=previous.get("last_error"),
            pid=pid or None,
            extra={
                "stopped_at": now_iso(),
                "stop_timed_out": running,
                "mode": mode,
                "api_url": api_url,
                "host": host,
                "session_id": session_id,
                "launch_token": launch_token,
            },
        )
    return {
        "ok": not running,
        "merchant_id": merchant_id,
        "pid": pid or None,
        "mode": mode,
        "api_url": api_url,
        "host": host,
        "session_id": session_id,
        "was_running": was_running,
        "running": running,
        "pid_file": str(paths["pid_file"]),
        "state_file": str(paths["state_file"]),
        "stop_file": str(paths["stop_file"]),
        "log_file": str(paths["log_file"]),
    }


def status_agent(db_path: str | Path, merchant_id: str, state_dir: str | Path | None = None) -> dict[str, Any]:
    paths = agent_paths(merchant_id, state_dir)
    with _PidFileLock(paths["pid_file"]):
        pid_record = read_json(paths["pid_file"], {})
        pid = safe_non_negative_int(pid_record.get("pid"))
    state = read_json(paths["state_file"], {})
    mode = str(pid_record.get("mode") or state.get("mode") or "sqlite")
    host = str(pid_record.get("host") or state.get("host") or "")
    session_id = str(pid_record.get("session_id") or state.get("session_id") or "")
    launch_token = str(pid_record.get("launch_token") or "")
    running = bool(
        launch_token
        and state_matches_pid_record(state, pid_record, merchant_id)
        and pid_record_matches_process(pid_record, merchant_id)
        and state.get("running") is not False
    )
    counters = state.get("counters") or {"checked": 0, "replied": 0}
    return {
        "ok": True,
        "merchant_id": merchant_id,
        "pid": pid or None,
        "mode": mode,
        "api_url": str(pid_record.get("api_url") or state.get("api_url") or ""),
        "host": host,
        "session_id": session_id,
        "running": running,
        "stale_pid": bool(pid and not running),
        "pid_file": str(paths["pid_file"]),
        "state_file": str(paths["state_file"]),
        "stop_file": str(paths["stop_file"]),
        "log_file": str(paths["log_file"]),
        "heartbeat": read_agent_heartbeat(db_path, merchant_id),
        "counters": {
            "checked": safe_non_negative_int(counters.get("checked")),
            "replied": safe_non_negative_int(counters.get("replied")),
        },
        "last_error": state.get("last_error"),
        "started_at": pid_record.get("started_at") or state.get("started_at"),
        "updated_at": state.get("updated_at"),
    }


def logs_agent(merchant_id: str, tail: int = 20, state_dir: str | Path | None = None) -> dict[str, Any]:
    if tail <= 0:
        raise ValueError("tail must be greater than 0")
    tail = min(tail, MAX_AGENT_LOG_TAIL)
    paths = agent_paths(merchant_id, state_dir)
    entries: list[dict[str, Any]] = []
    raw_lines: list[str] = []
    raw_lines = tail_log_lines(paths["log_file"], tail)
    for line in raw_lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            parsed = {"event": "raw", "text": line}
        if not isinstance(parsed, dict):
            parsed = {"event": "raw", "text": line}
        entries.append(parsed)
    return {"ok": True, "merchant_id": merchant_id, "log_file": str(paths["log_file"]), "entries": entries}


def _run_process_loop(
    merchant_id: str,
    process_once: Callable[[], dict[str, Any]],
    mark_away: Callable[[], Any],
    interval: float = 3.0,
    state_file: str | Path | None = None,
    stop_file: str | Path | None = None,
    state_extra: dict[str, Any] | None = None,
    log_file: str | Path | None = None,
) -> None:
    stop_requested = False
    counters = {"checked": 0, "replied": 0}
    last_error: str | None = None
    state_path = Path(state_file).expanduser() if state_file else None
    stop_path = Path(stop_file).expanduser() if stop_file else None
    log_path = Path(log_file).expanduser() if log_file else None
    interval = safe_positive_float(interval, 3.0, maximum=MAX_AGENT_INTERVAL_SECONDS)
    error_streak = 0

    def request_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        while not stop_requested and not (stop_path and stop_path.exists()):
            # Rotate while running, not only before start; otherwise a
            # long-lived daemon grows its log file without bound.
            if log_path is not None and rotate_agent_log(log_path):
                _redirect_stdout_to(log_path)
            try:
                result = process_once()
                checked = safe_non_negative_int(result.get("checked"))
                replied_count = safe_replied_count(result.get("replied"))
                counters["checked"] += checked
                counters["replied"] += replied_count
                last_error = None
                error_streak = 0
                event = {
                    "event": "process_once",
                    "at": now_iso(),
                    "merchant_id": merchant_id,
                    "checked": checked,
                    "replied_count": replied_count,
                    "counters": counters,
                    "result": result,
                }
            except Exception as exc:  # pragma: no cover - defensive runtime path
                error_streak += 1
                last_error = f"{type(exc).__name__}: {exc}"
                fatal = error_streak >= 3 and permanent_agent_error(last_error)
                if fatal:
                    stop_requested = True
                event = {
                    "event": "error",
                    "at": now_iso(),
                    "merchant_id": merchant_id,
                    "counters": counters,
                    "error": last_error,
                    "error_streak": error_streak,
                    "fatal": fatal,
                }
            print(json.dumps(event, ensure_ascii=False, sort_keys=True, default=str), flush=True)
            if state_path:
                write_state(
                    state_path,
                    merchant_id,
                    running=True,
                    counters=counters,
                    last_error=last_error,
                    pid=os.getpid(),
                    extra=state_extra,
                )

            retry_interval = min(interval * (2 ** min(error_streak, 6)), MAX_AGENT_ERROR_BACKOFF_SECONDS)
            deadline = time.time() + max(retry_interval, 0.05)
            while not stop_requested and not (stop_path and stop_path.exists()) and time.time() < deadline:
                time.sleep(min(0.1, max(deadline - time.time(), 0.01)))
    finally:
        try:
            mark_away()
        finally:
            if state_path:
                stopped_extra = dict(state_extra or {})
                stopped_extra["stopped_at"] = now_iso()
                write_state(
                    state_path,
                    merchant_id,
                    running=False,
                    counters=counters,
                    last_error=last_error,
                    pid=os.getpid(),
                    extra=stopped_extra,
                )
            if stop_path and stop_path.exists():
                stop_path.unlink()
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)


def run_forever(
    db_path: str | Path,
    merchant_id: str,
    interval: float = 3.0,
    state_file: str | Path | None = None,
    stop_file: str | Path | None = None,
) -> None:
    def process_once() -> dict[str, Any]:
        with db_session(db_path) as conn:
            return merchant_agent.process_once(conn, merchant_id)

    def mark_away() -> None:
        with db_session(db_path) as conn:
            merchant_agent.heartbeat(conn, merchant_id, status="away")

    _run_process_loop(
        merchant_id,
        process_once,
        mark_away,
        interval=interval,
        state_file=state_file,
        stop_file=stop_file,
        state_extra={
            "mode": "sqlite",
            "api_url": "",
            "launch_token": str(os.environ.get("SHOPPING_AGENT_LAUNCH_TOKEN") or ""),
        },
        log_file=agent_paths(merchant_id)["log_file"],
    )


def run_tools_forever(
    tools: Any,
    merchant_id: str,
    interval: float = 3.0,
    state_file: str | Path | None = None,
    stop_file: str | Path | None = None,
) -> None:
    def process_once() -> dict[str, Any]:
        return merchant_agent.process_once_with_tools(tools, merchant_id)

    def mark_away() -> None:
        tools.heartbeat(merchant_id, status="away")

    _run_process_loop(
        merchant_id,
        process_once,
        mark_away,
        interval=interval,
        state_file=state_file,
        stop_file=stop_file,
        state_extra={
            "mode": "api",
            "api_url": str(getattr(tools, "base_url", "") or ""),
            "host": str(getattr(tools, "host", "") or ""),
            "session_id": str(getattr(tools, "session_id", "") or ""),
            "launch_token": str(os.environ.get("SHOPPING_AGENT_LAUNCH_TOKEN") or ""),
        },
        log_file=agent_paths(merchant_id)["log_file"],
    )

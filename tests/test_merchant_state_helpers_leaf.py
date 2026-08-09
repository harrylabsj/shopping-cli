"""Characterization tests for the merchant_state_helpers leaf-module extraction.

The pure path-naming / id-sanitization / counter-coercion / error-classification
helpers moved move-only from ``shopping_cli.agents.merchant_daemon`` into the
leaf module ``shopping_cli.agents.merchant_state_helpers``. ``merchant_daemon``
re-exports the same objects so every existing ``merchant_daemon.<helper>``
access keeps its exact surface, path naming, return types and call order. These
tests pin that contract of the split.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from shopping_cli.agents import merchant_daemon
from shopping_cli.agents import merchant_state_helpers as helpers

# Names that physically moved into the leaf module and must be re-exported
# by ``merchant_daemon`` as the *identical* objects.
MOVED_NAMES = (
    "DEFAULT_STATE_DIR",
    "state_dir_from",
    "safe_merchant_id",
    "agent_paths",
    "safe_replied_count",
    "permanent_agent_error",
)

# Lifecycle/orchestration surface that must stay defined in ``merchant_daemon``
# (and must not leak into the leaf module).
KEPT_NAMES = (
    "MAX_AGENT_INTERVAL_SECONDS",
    "MAX_AGENT_STOP_TIMEOUT_SECONDS",
    "MAX_AGENT_LOG_TAIL",
    "MAX_AGENT_LOG_BYTES",
    "MAX_AGENT_LOG_BACKUPS",
    "MAX_AGENT_ERROR_BACKOFF_SECONDS",
    "ensure_agent_dirs",
    "rotate_agent_log",
    "_redirect_stdout_to",
    "tail_log_lines",
    "read_json",
    "write_json_atomic",
    "_PidFileLock",
    "is_process_running",
    "_is_process_running_legacy",
    "process_identity",
    "wait_for_process_identity",
    "pid_record_matches_process",
    "state_matches_pid_record",
    "read_agent_heartbeat",
    "write_state",
    "start_agent",
    "stop_agent",
    "status_agent",
    "logs_agent",
    "_run_process_loop",
    "run_forever",
    "run_tools_forever",
)


@pytest.mark.parametrize("name", MOVED_NAMES)
def test_moved_helpers_live_in_leaf_module(name: str) -> None:
    assert hasattr(helpers, name), f"leaf module missing {name}"


@pytest.mark.parametrize("name", MOVED_NAMES)
def test_merchant_daemon_re_exports_identical_objects(name: str) -> None:
    assert hasattr(merchant_daemon, name), f"merchant_daemon no longer exposes {name}"
    assert getattr(merchant_daemon, name) is getattr(helpers, name), f"{name} is not re-exported by identity"


@pytest.mark.parametrize("name", KEPT_NAMES)
def test_orchestration_surface_stays_in_merchant_daemon(name: str) -> None:
    assert hasattr(merchant_daemon, name), f"merchant_daemon lost orchestration name {name}"
    assert not hasattr(helpers, name), f"leaf module unexpectedly owns {name}"


def test_leaf_imports_no_runtime_deps() -> None:
    """Importing the leaf must not drag in psutil or the DB layer."""
    code = (
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "import shopping_cli.agents.merchant_state_helpers\n"
        "deps = sorted(m for m in sys.modules if m == 'psutil' or m.startswith('shopping_cli.db'))\n"
        "print(repr(deps))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]"


def test_state_dir_from_expands_user() -> None:
    expected = Path("~/x").expanduser()
    assert merchant_daemon.state_dir_from("~/x") == expected
    assert helpers.state_dir_from("~/x") == expected


class TestSafeMerchantId:
    def test_empty_and_none_fall_back_to_merchant_slug(self) -> None:
        for value in ("", None):
            safe = merchant_daemon.safe_merchant_id(value)
            assert safe.startswith("merchant-")
            slug, digest = safe.rsplit("-", 1)
            assert slug == "merchant"
            assert len(digest) == 16
            assert all(ch in "0123456789abcdef" for ch in digest)

    def test_deterministic(self) -> None:
        assert merchant_daemon.safe_merchant_id("seller-a") == merchant_daemon.safe_merchant_id("seller-a")

    def test_invalid_chars_replaced_with_underscore(self) -> None:
        assert merchant_daemon.safe_merchant_id("a/b c:d").startswith("a_b_c_d-")

    def test_slug_length_capped_at_64(self) -> None:
        safe = merchant_daemon.safe_merchant_id("x" * 200)
        slug, digest = safe.rsplit("-", 1)
        assert len(slug) == 64
        assert len(digest) == 16

    def test_path_traversal_sanitized(self) -> None:
        safe = merchant_daemon.safe_merchant_id("../etc/passwd")
        assert safe.startswith("etc_passwd-")
        assert "/" not in safe
        assert ".." not in safe

    def test_no_path_separators_ever(self) -> None:
        for raw in ("../x", "a/b", "a\\b", "a b", "a:b"):
            safe = merchant_daemon.safe_merchant_id(raw)
            assert "/" not in safe
            assert "\\" not in safe

    def test_digest_disambiguates_colliding_slugs(self) -> None:
        first = merchant_daemon.safe_merchant_id("a/b")
        second = merchant_daemon.safe_merchant_id("a b")
        assert first.split("-", 1)[0] == second.split("-", 1)[0]  # same slug
        assert first != second  # distinct digest keeps paths isolated


class TestAgentPaths:
    def test_state_dir_override_is_root(self, tmp_path: Path) -> None:
        paths = merchant_daemon.agent_paths("seller-a", state_dir=tmp_path)
        assert paths["state_dir"] == tmp_path
        assert paths["pid_file"].parent == tmp_path / "agents"
        assert paths["state_file"].parent == tmp_path / "agents"
        assert paths["stop_file"].parent == tmp_path / "agents"
        assert paths["log_file"].parent == tmp_path / "logs"

    def test_env_state_dir_used_when_no_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SHOPPING_CLI_STATE_DIR", str(tmp_path))
        assert merchant_daemon.agent_paths("seller-a")["state_dir"] == tmp_path

    def test_default_state_dir_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SHOPPING_CLI_STATE_DIR", raising=False)
        assert merchant_daemon.agent_paths("seller-a")["state_dir"] == helpers.DEFAULT_STATE_DIR

    def test_path_naming(self, tmp_path: Path) -> None:
        paths = merchant_daemon.agent_paths("seller-a", state_dir=tmp_path)
        safe_id = merchant_daemon.safe_merchant_id("seller-a")
        assert paths["pid_file"].name == f"{safe_id}.pid"
        assert paths["state_file"].name == f"{safe_id}.state.json"
        assert paths["stop_file"].name == f"{safe_id}.stop"
        assert paths["log_file"].name == f"{safe_id}.log"

    def test_merchant_isolation(self, tmp_path: Path) -> None:
        seller_a = merchant_daemon.agent_paths("seller-a", state_dir=tmp_path)
        seller_b = merchant_daemon.agent_paths("seller-b", state_dir=tmp_path)
        for key in ("pid_file", "state_file", "stop_file", "log_file"):
            assert seller_a[key] != seller_b[key]

    def test_slug_collision_isolation(self, tmp_path: Path) -> None:
        slash = merchant_daemon.agent_paths("a/b", state_dir=tmp_path)
        space = merchant_daemon.agent_paths("a b", state_dir=tmp_path)
        for key in ("pid_file", "state_file", "stop_file", "log_file"):
            assert slash[key] != space[key]


class TestSafeRepliedCount:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ([], 0),
            ([{"conversation_id": "CONV-0001"}], 1),
            ([1, 2, 3], 3),
            (None, 0),
            ("bad", 0),
            ({"conversation_id": "CONV-0001"}, 0),
            (42, 0),
            ((1, 2), 0),
        ],
    )
    def test_boundaries(self, value: Any, expected: int) -> None:
        assert merchant_daemon.safe_replied_count(value) == expected


class TestPermanentAgentError:
    @pytest.mark.parametrize(
        "message",
        [
            "Invalid Authorization",
            "invalid authorization header",
            "revoked authorization",
            "expired authorization token",
            "token required for this endpoint",
            "Unknown Merchant: seller-xyz",
            "INVALID AUTHORIZATION",
            "UNKNOWN MERCHANT",
        ],
    )
    def test_permanent_markers_classified(self, message: str) -> None:
        assert merchant_daemon.permanent_agent_error(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "",
            None,
            "connection refused",
            "HTTP 500 Internal Server Error",
            "timeout after 30s",
            "rate limited",
        ],
    )
    def test_non_permanent_messages_not_classified(self, message: str) -> None:
        assert merchant_daemon.permanent_agent_error(message) is False


class TestFacadeIdentity:
    def test_logs_agent_uses_re_exported_agent_paths(self, tmp_path: Path) -> None:
        """Patching the facade name must affect the internal call site."""
        fake = {"log_file": tmp_path / "agent.log"}
        with (
            patch("shopping_cli.agents.merchant_daemon.agent_paths", return_value=fake) as paths_mock,
            patch("shopping_cli.agents.merchant_daemon.tail_log_lines", return_value=[]),
        ):
            result = merchant_daemon.logs_agent("seller-a", tail=5, state_dir=tmp_path)
        paths_mock.assert_called_once_with("seller-a", tmp_path)
        assert result["ok"] is True
        assert result["merchant_id"] == "seller-a"
        assert result["log_file"] == str(tmp_path / "agent.log")
        assert result["entries"] == []

    def test_start_agent_call_order_preserved(self, tmp_path: Path) -> None:
        """agent_paths is still resolved before ensure_agent_dirs in the facade."""
        order: list[str] = []
        real_paths = merchant_daemon.agent_paths
        real_ensure_dirs = merchant_daemon.ensure_agent_dirs

        def tracked_paths(merchant_id: str, state_dir: str | Path | None = None) -> dict[str, Path]:
            order.append("agent_paths")
            return real_paths(merchant_id, state_dir)

        def tracked_ensure_dirs(paths: dict[str, Path]) -> None:
            order.append("ensure_agent_dirs")
            real_ensure_dirs(paths)

        class FakeProcess:
            pid = 987654321

        with (
            patch("shopping_cli.agents.merchant_daemon.agent_paths", side_effect=tracked_paths),
            patch(
                "shopping_cli.agents.merchant_daemon.ensure_agent_dirs",
                side_effect=tracked_ensure_dirs,
            ),
            patch("shopping_cli.agents.merchant_daemon.subprocess.Popen", return_value=FakeProcess()),
            patch(
                "shopping_cli.agents.merchant_daemon.wait_for_process_identity",
                return_value={"create_time": 0.0, "executable": "", "cmdline": []},
            ),
        ):
            started = merchant_daemon.start_agent(
                tmp_path / "shopping.sqlite",
                "seller-a",
                interval=0.1,
                state_dir=tmp_path,
                api_url="http://127.0.0.1:8765",
                agent_token="agent_secret",
            )

        assert order[:2] == ["agent_paths", "ensure_agent_dirs"]
        assert started["ok"] is True
        assert started["merchant_id"] == "seller-a"
        assert started["mode"] == "api"

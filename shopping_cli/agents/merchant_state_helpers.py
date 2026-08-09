"""Pure merchant-daemon state helpers: path naming, id sanitization, counters.

Move-only extraction from :mod:`shopping_cli.agents.merchant_daemon`: this leaf
module owns the merchant-id sanitization (path isolation via slug + digest),
the state/log/pid/stop file path naming, the replied-count coercion and the
permanent-error classification. It never starts or stops processes, takes
locks, installs signal handlers, touches the database or network, writes files,
or drives state transitions — everything here is a pure function of its
inputs plus the ``SHOPPING_CLI_STATE_DIR`` environment override.

The parent module re-exports these names unchanged (``as`` aliases) so the
public ``merchant_daemon`` surface, path naming, return types and call order
are preserved.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "shopping-cli"


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


def safe_replied_count(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    return len(value)


def permanent_agent_error(error: str) -> bool:
    lowered = str(error or "").lower()
    return any(marker in lowered for marker in ("invalid authorization", "revoked authorization", "expired authorization", "token required", "unknown merchant"))

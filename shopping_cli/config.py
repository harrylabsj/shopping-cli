"""Runtime configuration helpers for local shopping-cli hosts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "shopping-cli"
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "shopping-cli.sqlite"
DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "shopping-cli"
DEFAULT_AGENT_STALE_TTL_SECONDS = 60
MAX_AGENT_STALE_TTL_SECONDS = timedelta.max.days * 24 * 60 * 60 + timedelta.max.seconds


def db_path_from(value: str | Path | None = None) -> Path:
    return Path(value or os.environ.get("SHOPPING_DB") or os.environ.get("SHOPPING_DATA") or DEFAULT_DB_PATH).expanduser()


def state_dir_from(value: str | Path | None = None) -> Path:
    return Path(value or os.environ.get("SHOPPING_CLI_STATE_DIR") or DEFAULT_STATE_DIR).expanduser()


def agent_stale_ttl_seconds_from(value: str | int | None = None) -> int:
    raw = value if value is not None else os.environ.get("SHOPPING_AGENT_STALE_TTL_SECONDS")
    if raw in (None, ""):
        return DEFAULT_AGENT_STALE_TTL_SECONDS
    if isinstance(raw, bool):
        return DEFAULT_AGENT_STALE_TTL_SECONDS
    try:
        seconds = int(raw)
    except (OverflowError, TypeError, ValueError):
        return DEFAULT_AGENT_STALE_TTL_SECONDS
    if seconds <= 0 or seconds > MAX_AGENT_STALE_TTL_SECONDS:
        return DEFAULT_AGENT_STALE_TTL_SECONDS
    return seconds


@dataclass(frozen=True)
class RuntimeConfig:
    db_path: Path = DEFAULT_DB_PATH
    state_dir: Path = DEFAULT_STATE_DIR
    agent_stale_ttl_seconds: int = DEFAULT_AGENT_STALE_TTL_SECONDS

    @classmethod
    def from_env(cls, db_path: str | Path | None = None, state_dir: str | Path | None = None) -> "RuntimeConfig":
        return cls(
            db_path=db_path_from(db_path),
            state_dir=state_dir_from(state_dir),
            agent_stale_ttl_seconds=agent_stale_ttl_seconds_from(),
        )

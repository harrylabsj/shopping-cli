"""SQLite connection and serialization helpers."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from shopping_cli import VERSION
from shopping_cli.db.migrations import (
    CURRENT_SCHEMA_VERSION,
    backfill_conversation_next_actor,
    migrate_api_tokens_to_hashes,
    run_migrations,
)
from shopping_cli.db.models import INDEXES, SCHEMA

SQLITE_BUSY_TIMEOUT_MS = 5000


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def decode_json(value: str | None, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        decoded = json.loads(value)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return default
    if isinstance(default, list):
        if not isinstance(decoded, list):
            return default
        normalized: list[str] = []
        for item in decoded:
            if item is None or isinstance(item, (dict, list)):
                continue
            text = str(item).strip()
            if text:
                normalized.append(text)
        return normalized
    if isinstance(default, dict) and not isinstance(decoded, dict):
        return default
    return decoded


def open_connection(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"pragma busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("pragma journal_mode = wal")
    conn.execute("pragma foreign_keys = on")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        conn.execute(statement)
    run_migrations(conn)
    for statement in INDEXES:
        conn.execute(statement)
    conn.execute(
        "insert or replace into meta(key, value) values('schema_version', ?)",
        (str(CURRENT_SCHEMA_VERSION),),
    )
    conn.execute(
        "insert or replace into meta(key, value) values('package_version', ?)",
        (VERSION,),
    )
    conn.commit()


@contextmanager
def db_session(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = open_connection(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)

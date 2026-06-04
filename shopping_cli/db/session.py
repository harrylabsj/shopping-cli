"""SQLite connection and serialization helpers."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from shopping_cli import VERSION
from shopping_cli.core.tokens import is_sha256_digest, token_digest, token_prefix, token_suffix
from shopping_cli.db.models import EXTRA_COLUMNS, INDEXES, SCHEMA

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
    previous_schema_version = _schema_version(conn)
    for statement in SCHEMA:
        conn.execute(statement)
    added_columns: set[tuple[str, str]] = set()
    for table, columns in EXTRA_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
        for name, definition in columns:
            if name not in existing:
                conn.execute(f"alter table {table} add column {name} {definition}")
                added_columns.add((table, name))
    should_run_versioned_migrations = previous_schema_version != VERSION
    if should_run_versioned_migrations or ("conversations", "next_actor") in added_columns:
        backfill_conversation_next_actor(conn)
    if should_run_versioned_migrations or any(table == "api_tokens" for table, _name in added_columns):
        migrate_api_tokens_to_hashes(conn)
    for statement in INDEXES:
        conn.execute(statement)
    if previous_schema_version != VERSION:
        conn.execute(
            "insert or replace into meta(key, value) values('schema_version', ?)",
            (VERSION,),
        )
    conn.commit()


def _schema_version(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("select value from meta where key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return ""
    return str(row["value"] or "") if row is not None else ""


def backfill_conversation_next_actor(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        update conversations
        set next_actor = case status
            when 'waiting_merchant' then 'merchant_agent'
            when 'waiting_buyer' then 'buyer'
            when 'human_required' then 'merchant_human'
            when 'open' then 'buyer'
            else ''
        end
        where next_actor = ''
        """
    )


def migrate_api_tokens_to_hashes(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("pragma table_info(api_tokens)").fetchall()}
    if not {"token", "token_hash", "token_prefix", "token_suffix"}.issubset(columns):
        return
    rows = conn.execute("select token, token_hash, token_prefix, token_suffix from api_tokens").fetchall()
    for row in rows:
        stored = str(row["token"] or "")
        stored_hash = str(row["token_hash"] or "")
        if is_sha256_digest(stored) and stored_hash == stored:
            continue
        if is_sha256_digest(stored):
            conn.execute(
                """
                update api_tokens
                set token_hash = ?, token_prefix = coalesce(nullif(token_prefix, ''), ?),
                    token_suffix = coalesce(nullif(token_suffix, ''), ?)
                where token = ?
                """,
                (stored, row["token_prefix"] or stored[:24], row["token_suffix"] or stored[-6:], stored),
            )
            continue
        digest = token_digest(stored)
        conn.execute(
            """
            update api_tokens
            set token = ?, token_hash = ?, token_prefix = ?, token_suffix = ?
            where token = ?
            """,
            (digest, digest, token_prefix(stored), token_suffix(stored), stored),
        )


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

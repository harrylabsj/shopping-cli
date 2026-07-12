"""Explicit SQLite schema migrations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from shopping_cli.core.tokens import is_sha256_digest, token_digest, token_prefix, token_suffix

CURRENT_SCHEMA_VERSION = 6


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def schema_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("pragma user_version").fetchone()
    return int(row[0] or 0) if row is not None else 0


def _set_schema_user_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"pragma user_version = {int(version)}")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}


def ensure_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> bool:
    if name in _table_columns(conn, table):
        return False
    conn.execute(f"alter table {table} add column {name} {definition}")
    return True


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
    columns = _table_columns(conn, "api_tokens")
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


def migration_001_conversation_next_actor(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "conversations", "next_actor", "text not null default ''")
    backfill_conversation_next_actor(conn)


def migration_002_agent_runtime_columns(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "agents", "pid", "integer not null default 0")
    ensure_column(conn, "agents", "version", "text not null default ''")
    ensure_column(conn, "agents", "last_error", "text not null default ''")
    ensure_column(conn, "agents", "checked_count", "integer not null default 0")
    ensure_column(conn, "agents", "replied_count", "integer not null default 0")


def migration_003_human_review_resolution_columns(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "moderation_flags", "resolved_at", "text not null default ''")
    ensure_column(conn, "moderation_flags", "resolution", "text not null default ''")
    ensure_column(conn, "moderation_flags", "resolved_by", "text not null default ''")


def migration_004_api_token_hash_columns(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "api_tokens", "token_hash", "text not null default ''")
    ensure_column(conn, "api_tokens", "token_prefix", "text not null default ''")
    ensure_column(conn, "api_tokens", "token_suffix", "text not null default ''")
    migrate_api_tokens_to_hashes(conn)


def migration_005_api_token_scope_columns(conn: sqlite3.Connection) -> None:
    ensure_column(conn, "api_tokens", "agent_id", "text not null default ''")
    ensure_column(conn, "api_tokens", "conversation_id", "text not null default ''")
    ensure_column(conn, "api_tokens", "revoked_at", "text not null default ''")
    ensure_column(conn, "api_tokens", "expires_at", "text not null default ''")


def migration_006_search_indexes(conn: sqlite3.Connection) -> None:
    """Create FTS5 search index tables.

    SQLite builds without the FTS5 extension skip index creation; the catalog
    layer degrades to Python-side filtering via the
    ``*_search_index_available`` helpers. Other OperationalError cases (for
    example read-only media) are re-raised so they are not masked.
    """
    fts5_statements = [
        """
        create virtual table if not exists product_search_index
        using fts5(sku unindexed, merchant_id unindexed, text, tokenize='unicode61')
        """,
        """
        create virtual table if not exists merchant_search_index
        using fts5(id unindexed, text, tokenize='unicode61')
        """,
        """
        create virtual table if not exists policy_search_index
        using fts5(merchant_id unindexed, text, tokenize='unicode61')
        """,
    ]
    for statement in fts5_statements:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if "fts5" not in str(exc).lower():
                raise


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "conversation_next_actor", migration_001_conversation_next_actor),
    Migration(2, "agent_runtime_columns", migration_002_agent_runtime_columns),
    Migration(3, "human_review_resolution_columns", migration_003_human_review_resolution_columns),
    Migration(4, "api_token_hash_columns", migration_004_api_token_hash_columns),
    Migration(5, "api_token_scope_columns", migration_005_api_token_scope_columns),
    Migration(6, "search_indexes", migration_006_search_indexes),
)


def run_migrations(conn: sqlite3.Connection) -> None:
    current_version = schema_user_version(conn)
    for migration in MIGRATIONS:
        if migration.version <= current_version:
            continue
        migration.apply(conn)
        _set_schema_user_version(conn, migration.version)

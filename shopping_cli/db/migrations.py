"""Explicit SQLite schema migrations."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from shopping_cli.core.tokens import is_sha256_digest, token_digest, token_prefix, token_suffix

CURRENT_SCHEMA_VERSION = 9


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


def migration_007_atomic_lifecycle_constraints(conn: sqlite3.Connection) -> None:
    """Add the key used only by callers requesting open-conversation reuse."""
    ensure_column(conn, "conversations", "reuse_key", "text not null default ''")


def migration_008_rebuild_cjk_search_documents(conn: sqlite3.Connection) -> None:
    """Force lazy rebuild using the CJK bigram document format."""
    for table in ("product_search_index", "merchant_search_index", "policy_search_index"):
        try:
            conn.execute(f"delete from {table}")
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise


def migration_009_unique_open_reuse_key(conn: sqlite3.Connection) -> None:
    """Deduplicate open reuse-key conversations and add the guarding index.

    Databases already at schema v8 never re-run ``init_db`` (the
    ``open_connection`` fast path), so they never receive the partial unique
    index ``idx_conversations_unique_open_key``. Before creating it, collapse
    duplicate non-closed rows that share a non-empty ``reuse_key``: the most
    recently created row (matching the ``ensure_conversation`` reuse order)
    stays the winner, and losers are closed with their messages, flags, and
    audit history left intact for traceability. Rows with an empty
    ``reuse_key`` are explicit independent conversations and are never
    touched.
    """
    ensure_column(conn, "conversations", "reuse_key", "text not null default ''")
    # Legacy tables may predate the NOT NULL defaults; normalize so the
    # partial index predicate and the dedup comparisons behave. These are
    # no-ops when the columns are already NOT NULL.
    conn.execute("update conversations set reuse_key = '' where reuse_key is null")
    if "sku" in _table_columns(conn, "conversations"):
        conn.execute("update conversations set sku = '' where sku is null")
    rows = conn.execute(
        """
        select id, reuse_key from conversations
        where reuse_key != '' and status != 'closed'
        order by reuse_key, created_at desc, id desc
        """
    ).fetchall()
    winners: dict[str, str] = {}
    losers: list[tuple[str, str]] = []
    for row in rows:
        reuse_key = str(row["reuse_key"])
        if reuse_key in winners:
            losers.append((str(row["id"]), reuse_key))
        else:
            winners[reuse_key] = str(row["id"])
    now = datetime.now().replace(microsecond=0).isoformat()
    for conversation_id, reuse_key in losers:
        conn.execute(
            """
            update conversations
            set status = 'closed', next_actor = '', updated_at = ?, last_sender = 'system'
            where id = ? and status != 'closed'
            """,
            (now, conversation_id),
        )
        conn.execute(
            """
            insert into audit_events(conversation_id, actor, event, details_json, created_at)
            values (?, 'system', 'conversation_closed', ?, ?)
            """,
            (
                conversation_id,
                json.dumps(
                    {
                        "event_type": "conversation_closed",
                        "next_actor": "",
                        "reason": "duplicate_open_reuse_key",
                        "schema_version": 1,
                        "source": "migration_009_unique_open_reuse_key",
                        "winner_conversation_id": winners[reuse_key],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                now,
            ),
        )
    conn.execute(
        """
        create unique index if not exists idx_conversations_unique_open_key
        on conversations(reuse_key)
        where reuse_key != '' and status != 'closed'
        """
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "conversation_next_actor", migration_001_conversation_next_actor),
    Migration(2, "agent_runtime_columns", migration_002_agent_runtime_columns),
    Migration(3, "human_review_resolution_columns", migration_003_human_review_resolution_columns),
    Migration(4, "api_token_hash_columns", migration_004_api_token_hash_columns),
    Migration(5, "api_token_scope_columns", migration_005_api_token_scope_columns),
    Migration(6, "search_indexes", migration_006_search_indexes),
    Migration(7, "atomic_lifecycle_constraints", migration_007_atomic_lifecycle_constraints),
    Migration(8, "rebuild_cjk_search_documents", migration_008_rebuild_cjk_search_documents),
    Migration(9, "unique_open_reuse_key", migration_009_unique_open_reuse_key),
)


def run_migrations(conn: sqlite3.Connection) -> None:
    current_version = schema_user_version(conn)
    for migration in MIGRATIONS:
        if migration.version <= current_version:
            continue
        migration.apply(conn)
        _set_schema_user_version(conn, migration.version)

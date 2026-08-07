"""Explicit SQLite schema migrations."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from shopping_cli.core.tokens import is_sha256_digest, token_digest, token_prefix, token_suffix

CURRENT_SCHEMA_VERSION = 16


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


_AGENT_CATALOG_DDL = [
    """
    create table if not exists catalog_agents (
        catalog_agent_id text primary key,
        merchant_id text,
        hosted_runtime_agent_id text,
        display_name text not null,
        provider_name text not null default '',
        canonical_domain text not null default '',
        agent_type text not null default '',
        source_type text not null
            check(source_type in ('hosted','self_registered','discovered','imported','admin_curated')),
        lifecycle_status text not null default 'active'
            check(lifecycle_status in ('active','inactive','deprecated')),
        verification_status text not null default 'discovered'
            check(verification_status in (
                'discovered','profile_valid','domain_verified','agent_verified',
                'commerce_verified','stale','rejected','suspended','unreachable'
            )),
        hosting_mode text not null default 'unknown'
            check(hosting_mode in ('direct','hosted','hybrid','unknown')),
        first_seen_at text not null,
        last_seen_at text not null,
        last_verified_at text not null default '',
        created_at text not null,
        updated_at text not null,
        foreign key (merchant_id) references merchants(id),
        foreign key (hosted_runtime_agent_id) references agents(id)
    )
    """,
    """
    create table if not exists agent_endpoints (
        endpoint_id integer primary key autoincrement,
        catalog_agent_id text not null,
        kind text not null check(kind in ('a2a','agent_card','ucp_profile','hosted_gateway')),
        url text not null default '',
        protocol text not null default '',
        protocol_version text not null default '',
        preference integer not null default 0,
        auth_summary_json text not null default '{}',
        status text not null default 'active',
        last_checked_at text not null default '',
        foreign key (catalog_agent_id) references catalog_agents(catalog_agent_id)
    )
    """,
    """
    create table if not exists agent_capabilities (
        catalog_agent_id text not null,
        namespace text not null,
        capability_id text not null,
        version text not null default '',
        required integer not null default 0,
        source text not null default '',
        schema_url text not null default '',
        spec_url text not null default '',
        last_verified_at text not null default '',
        primary key (catalog_agent_id, namespace, capability_id),
        foreign key (catalog_agent_id) references catalog_agents(catalog_agent_id)
    )
    """,
    """
    create table if not exists agent_skills (
        catalog_agent_id text not null,
        skill_id text not null,
        name text not null,
        description text not null default '',
        tags_json text not null default '[]',
        input_modes_json text not null default '[]',
        output_modes_json text not null default '[]',
        primary key (catalog_agent_id, skill_id),
        foreign key (catalog_agent_id) references catalog_agents(catalog_agent_id)
    )
    """,
    """
    create table if not exists agent_profile_snapshots (
        snapshot_id integer primary key autoincrement,
        catalog_agent_id text not null,
        profile_type text not null check(profile_type in ('agent_card','ucp')),
        source_url text not null default '',
        etag text not null default '',
        last_modified text not null default '',
        content_hash text not null default '',
        raw_json text not null default '{}',
        fetched_at text not null default '',
        fresh_until text not null default '',
        validation_status text not null default 'pending',
        foreign key (catalog_agent_id) references catalog_agents(catalog_agent_id)
    )
    """,
    """
    create table if not exists agent_verifications (
        verification_id integer primary key autoincrement,
        catalog_agent_id text not null,
        verification_type text not null,
        result text not null default '',
        evidence_json text not null default '{}',
        checked_at text not null default '',
        expires_at text not null default '',
        foreign key (catalog_agent_id) references catalog_agents(catalog_agent_id)
    )
    """,
]


def migration_010_agent_catalog(conn: sqlite3.Connection) -> None:
    for statement in _AGENT_CATALOG_DDL:
        conn.execute(statement)


def migration_011_agent_catalog_register_limits(conn: sqlite3.Connection) -> None:
    """Per-domain registration budget (§17.4) for the public register route."""
    conn.execute(
        """
        create table if not exists agent_catalog_register_limits (
            canonical_domain text not null,
            window_start text not null,
            request_count integer not null default 0,
            updated_at text not null,
            primary key (canonical_domain, window_start)
        )
        """
    )


def migration_012_agent_catalog_write_idempotency(conn: sqlite3.Connection) -> None:
    """Generic idempotency + rate-limit tables for Agent Catalog writes (§10.4)."""
    conn.execute(
        """
        create table if not exists agent_catalog_write_idempotency (
            endpoint text not null,
            actor_key text not null,
            idempotency_key text not null,
            request_hash text not null,
            status text not null,
            response_json text not null default '{}',
            created_at text not null,
            updated_at text not null,
            primary key (endpoint, actor_key, idempotency_key)
        )
        """
    )
    conn.execute(
        """
        create table if not exists agent_catalog_write_rate_limits (
            actor_key text not null,
            window_start text not null,
            request_count integer not null default 0,
            updated_at text not null,
            primary key (actor_key, window_start)
        )
        """
    )


_AGENT_TRUST_OBSERVATIONS_DDL = """
    create table if not exists agent_trust_observations (
        observation_id integer primary key autoincrement,
        catalog_agent_id text not null,
        kind text not null
            check(kind in (
                'protocol_compliance','timeout_rate','schema_error_rate',
                'successful_exchange','local_asserted_dispute'
            )),
        value real not null,
        source text not null default '',
        evidence_ref text not null default '',
        observed_at text not null,
        expires_at text not null default '',
        foreign key (catalog_agent_id) references catalog_agents(catalog_agent_id)
    )
"""


def migration_013_agent_trust_observations(conn: sqlite3.Connection) -> None:
    """Add the §5.7 private-only ``agent_trust_observations`` table (v2.2 / Phase 2).

    Commercial reputation and protocol trust live in this table and are
    deliberately kept separate from public verification metadata.  Observations
    are private-only by default: no public serializer, search response, or any
    public API output may expose them (§5.7, §3.4).
    """
    conn.execute(_AGENT_TRUST_OBSERVATIONS_DDL)


_A2A_INBOUND_IDEMPOTENCY_DDL = """
    create table if not exists a2a_inbound_idempotency (
        sender_identity text not null,
        message_id text not null,
        digest text not null,
        status text not null default 'processing',
        response_json text not null default '{}',
        created_at text not null,
        updated_at text not null,
        primary key (sender_identity, message_id)
    )
"""


def migration_014_a2a_inbound_idempotency(conn: sqlite3.Connection) -> None:
    """Add the Hosted A2A inbound idempotency ledger (v2.4-W3, binding rc1 §3.6).

    ``(sender_identity, message_id)`` is the authoritative KNP idempotency key
    (D8): same id + same digest replays the stored response, same id +
    different digest fails closed as ``idempotency_conflict``.  The response
    snapshot holds the JSON-RPC ``{"result": ...}`` / ``{"error": ...}`` part
    so a replay can rebuild the identical response for the current request id.
    """
    conn.execute(_A2A_INBOUND_IDEMPOTENCY_DDL)


_VERIFICATION_QUEUE_TASKS_DDL = """
    create table if not exists verification_queue_tasks (
        task_id text primary key,
        catalog_agent_id text not null,
        kind text not null,
        actor text not null default 'verification_worker',
        status text not null default 'pending'
            check (status in ('pending','running','completed','failed','timeout')),
        enqueued_at real not null,
        started_at real not null default 0,
        finished_at real not null default 0,
        verification_status text not null default '',
        error text not null default '',
        result_json text not null default '{}',
        created_at text not null,
        updated_at text not null
    );
"""

_VERIFICATION_QUEUE_RECOVERY_INDEX_DDL = """
    create index if not exists idx_verification_queue_recovery
        on verification_queue_tasks(status)
"""


def migration_016_product_source_column(conn: sqlite3.Connection) -> None:
    """products.source —— 数据来源标注（shopping-cli data hub v0.2.1 §5）。

    local = 本地录入（LOCAL_AUTHORITATIVE）；erp = ERP 同步缓存
    （UPSTREAM_PROXY）。ERP 同步只覆盖 source='erp' 的行；本地手改行
    （source='local'）与 ERP 冲突时跳过并报错（绝不静默合并冲突权威源）。
    """
    cols = [row[1] for row in conn.execute("pragma table_info(products)").fetchall()]
    if "source" not in cols:
        conn.execute(
            "alter table products add column source text not null default 'local'"
            " check(source in ('local','erp'))"
        )


def migration_015_verification_queue_tasks(conn: sqlite3.Connection) -> None:
    """Add the persistent verification queue ledger (v3.0-P4, §25 Phase 2).

    The in-process queue writes through to this table so tasks survive a
    process restart: ``pending`` / ``running`` rows are recovered into a new
    queue instance on startup (verification tasks are idempotent — refresh /
    verify / mark_stale / suspend are safe to re-run).  ``result_json`` holds
    a serialized :class:`VerificationResult` so a restarted queue can rebuild
    the outcome for ``wait()``.  Terminal rows (completed / failed / timeout)
    are kept as an audit trail; only pending / running are recovered.
    """
    conn.execute(_VERIFICATION_QUEUE_TASKS_DDL)
    conn.execute(_VERIFICATION_QUEUE_RECOVERY_INDEX_DDL)


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
    Migration(10, "agent_catalog", migration_010_agent_catalog),
    Migration(11, "agent_catalog_register_limits", migration_011_agent_catalog_register_limits),
    Migration(12, "agent_catalog_write_idempotency", migration_012_agent_catalog_write_idempotency),
    Migration(13, "agent_trust_observations", migration_013_agent_trust_observations),
    Migration(14, "a2a_inbound_idempotency", migration_014_a2a_inbound_idempotency),
    Migration(15, "verification_queue_tasks", migration_015_verification_queue_tasks),
    Migration(16, "product_source_column", migration_016_product_source_column),
)


def run_migrations(conn: sqlite3.Connection) -> None:
    current_version = schema_user_version(conn)
    for migration in MIGRATIONS:
        if migration.version <= current_version:
            continue
        migration.apply(conn)
        _set_schema_user_version(conn, migration.version)

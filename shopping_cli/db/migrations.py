"""Explicit SQLite schema migrations."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from shopping_cli.core.tokens import is_sha256_digest, token_digest, token_prefix, token_suffix

CURRENT_SCHEMA_VERSION = 31


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


def migration_017_product_provenance(conn: sqlite3.Connection) -> None:
    """products per-field provenance 列（shopping-cli v0.3 §5；评审 P2）。

    source_revision / observed_at / fresh_until：权威源版本、观察时间、事实
    TTL。幂等 ALTER（pragma table_info 检查，参照 v16 模式）；fresh 路径由
    models.py SCHEMA 创建，旧库在此补列。
    """
    cols = [row[1] for row in conn.execute("pragma table_info(products)").fetchall()]
    for name, ddl in (
        ("source_revision", "text not null default ''"),
        ("observed_at", "text not null default ''"),
        ("fresh_until", "text not null default ''"),
    ):
        if name not in cols:
            conn.execute(f"alter table products add column {name} {ddl}")


_CATALOG_ERA_TABLES = (
    "catalog_agents",
    "agent_endpoints",
    "agent_capabilities",
    "agent_skills",
    "agent_profile_snapshots",
    "agent_verifications",
    "agent_catalog_register_limits",
    "agent_catalog_write_idempotency",
    "agent_catalog_write_rate_limits",
    "agent_trust_observations",
    "a2a_inbound_idempotency",
    "verification_queue_tasks",
    "listing_publications",
)


def migration_019_remove_catalog_subsystem_tables(conn: sqlite3.Connection) -> None:
    """v3.0: drop the Agent Catalog / A2A / kiwi-catalog era tables.

    The Discovery/A2A/Agent-Catalog/kiwi-catalog subsystems moved to the
    standalone kiwi-catalog service.  Fresh databases never create these
    tables (the SCHEMA list dropped them), so this migration is a no-op
    there; legacy v18 databases get the orphaned tables removed.
    """
    for table in _CATALOG_ERA_TABLES:
        conn.execute(f"drop table if exists {table}")


def migration_020_buyer_ledger_buyer_dimension(conn: sqlite3.Connection) -> None:
    """v3.0 安全加固：buyer 幂等/限流账本加 buyer_id 维度。

    共享 bootstrap token 是全站唯一的——此前 (token_hash, idempotency_key)
    的键空间让一个客户端可以耗尽所有人的限流预算、抢占他人的幂等键。
    旧行 buyer_id 回填 ''（历史数据，不影响新写入）。
    """
    conn.execute(
        """
        create table buyer_request_idempotency_v20 (
            endpoint text not null,
            token_hash text not null,
            buyer_id text not null,
            idempotency_key text not null,
            request_hash text not null,
            status text not null,
            response_json text not null default '{}',
            conversation_id text not null default '',
            message_id integer not null default 0,
            created_at text not null,
            updated_at text not null,
            primary key (endpoint, token_hash, buyer_id, idempotency_key)
        )
        """
    )
    conn.execute(
        """
        insert into buyer_request_idempotency_v20(
            endpoint, token_hash, buyer_id, idempotency_key, request_hash,
            status, response_json, conversation_id, message_id, created_at, updated_at
        )
        select endpoint, token_hash, '', idempotency_key, request_hash,
               status, response_json, conversation_id, message_id, created_at, updated_at
        from buyer_request_idempotency
        """
    )
    conn.execute("drop table buyer_request_idempotency")
    conn.execute("alter table buyer_request_idempotency_v20 rename to buyer_request_idempotency")

    conn.execute(
        """
        create table buyer_bootstrap_rate_limits_v20 (
            token_hash text not null,
            buyer_id text not null,
            window_start text not null,
            request_count integer not null default 0,
            updated_at text not null,
            primary key (token_hash, buyer_id, window_start)
        )
        """
    )
    conn.execute(
        """
        insert into buyer_bootstrap_rate_limits_v20(
            token_hash, buyer_id, window_start, request_count, updated_at
        )
        select token_hash, '', window_start, request_count, updated_at
        from buyer_bootstrap_rate_limits
        """
    )
    conn.execute("drop table buyer_bootstrap_rate_limits")
    conn.execute("alter table buyer_bootstrap_rate_limits_v20 rename to buyer_bootstrap_rate_limits")


def migration_021_channel_ingress_stale_marker(conn: sqlite3.Connection) -> None:
    """v3.0: channel ingress 的 stale 处理从"删除重处理"改为"标记 stale_at"。

    删除会让合法但缓慢的处理（>300s）被重复执行（重复买家消息）；标记后
    保留审计痕迹，且 stale_at 非空的行允许被后续重处理覆盖（幂等）。
    """
    ensure_column(conn, "channel_message_ingresses", "stale_at", "text not null default ''")


def migration_022_product_handoff_destination(conn: sqlite3.Connection) -> None:
    """products.handoff_destination 列（每商品成交入口，KTH destination_ref）。

    商家自行维护每商品的交易入口（URL 类为 https URL；联系/会话类为 opaque
    ref），`kiwi merchant publish` 同步进 catalog listing 的
    handoff_destination_ref。幂等 ALTER（ensure_column，参照 v21 模式）。
    """
    ensure_column(conn, "products", "handoff_destination", "text not null default ''")


def migration_024_product_source_csv_excel(conn: sqlite3.Connection) -> None:
    """products.source CHECK 扩展：允许 'csv_excel'（Issue 14 / Adapter SDK 首条路径）。

    v16 的 CHECK 只允许 ('local','erp')；CSV/Excel 适配器新增 source='csv_excel'
    （UPSTREAM_PROXY，同 ERP 权威语义）。SQLite 不能 ALTER CHECK，故重建表
    （复制全列 + 数据 + 索引），幂等：schema 已含 csv_excel 时跳过。
    """
    row = conn.execute(
        "select sql from sqlite_master where type='table' and name='products'"
    ).fetchone()
    if row is None or "csv_excel" in (row[0] or ""):
        return
    conn.execute(
        """
        create table products_v24 (
            sku text primary key,
            merchant_id text not null,
            title text not null,
            description text not null default '',
            category text not null default '',
            tags_json text not null default '[]',
            price real not null,
            currency text not null default 'CNY',
            stock integer not null,
            delivery_attributes_json text not null default '[]',
            handoff_destination text not null default '',
            active integer not null default 1,
            source text not null default 'local'
                check(source in ('local','erp','csv_excel')),
            source_revision text not null default '',
            observed_at text not null default '',
            fresh_until text not null default '',
            created_at text not null,
            updated_at text not null,
            foreign key (merchant_id) references merchants(id)
        )
        """
    )
    conn.execute(
        """
        insert into products_v24(
            sku, merchant_id, title, description, category, tags_json,
            price, currency, stock, delivery_attributes_json, handoff_destination,
            active, source, source_revision, observed_at, fresh_until,
            created_at, updated_at
        )
        select
            sku, merchant_id, title, description, category, tags_json,
            price, currency, stock, delivery_attributes_json, handoff_destination,
            active, source, source_revision, observed_at, fresh_until,
            created_at, updated_at
        from products
        """
    )
    conn.execute("drop table products")
    conn.execute("alter table products_v24 rename to products")
    conn.execute(
        "create index if not exists idx_products_active_merchant on products(active, merchant_id)"
    )
    conn.execute(
        "create index if not exists idx_products_active_stock_price on products(active, stock, price, sku)"
    )


def migration_023_remove_scheme_a_stub_merchants(conn: sqlite3.Connection) -> None:
    """方案A 窗口期 stub 商家行清理（审查 P3-02）。

    已拆除的方案A ``_ensure_merchant_exists``（5ad9ec8 移除）曾插入
    ``name == id``、无任何 api_tokens 行的 stub 商家：这些行无法认证，却挡
    ``POST /merchants`` 同 id 重建（ConflictError）。删除无 token 且名下无
    业务行的 stub；仍有 products/policies/delivery_rules/conversations 行的
    跳过并记 audit_events——外键无 ON DELETE CASCADE，不静默丢业务数据。
    """
    child_tables = ("products", "policies", "delivery_rules", "conversations")
    rows = conn.execute(
        """
        select id from merchants
        where name = id
          and not exists (
              select 1 from api_tokens where api_tokens.merchant_id = merchants.id
          )
        """
    ).fetchall()
    now = datetime.now().replace(microsecond=0).isoformat()
    for row in rows:
        merchant_id = str(row["id"])
        dependents = {
            table: int(
                conn.execute(
                    f"select count(*) from {table} where merchant_id = ?", (merchant_id,)
                ).fetchone()[0]
            )
            for table in child_tables
        }
        dependents = {table: count for table, count in dependents.items() if count}
        if dependents:
            conn.execute(
                """
                insert into audit_events(conversation_id, actor, event, details_json, created_at)
                values ('', 'system', 'stub_merchant_retained', ?, ?)
                """,
                (
                    json.dumps(
                        {
                            "event_type": "stub_merchant_retained",
                            "merchant_id": merchant_id,
                            "dependents": dependents,
                            "reason": "scheme_a_stub_with_business_rows",
                            "source": "migration_023_remove_scheme_a_stub_merchants",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            continue
        conn.execute("delete from merchants where id = ?", (merchant_id,))


def migration_025_negotiation_decision_idempotency(conn: sqlite3.Connection) -> None:
    """协商决策幂等账本（审查 H2 2026-08-16）。

    ``submit_decision`` 此前靠"先扫描 messages 再 append"判断幂等，并发相同
    idempotency_key 的两个请求都能通过预读并各自写入决策消息（违反冻结协议
    "同 key 重放绝不重复"）。新增 (conversation_id, agent_id, idempotency_key)
    唯一表，claim 时由唯一约束兜底并发——与 buyer_request_idempotency 同模式。
    """
    conn.execute(
        """
        create table if not exists negotiation_decision_idempotency (
            conversation_id text not null,
            agent_id text not null,
            idempotency_key text not null,
            message_id integer not null default 0,
            decision_json text not null default '{}',
            created_at text not null,
            primary key (conversation_id, agent_id, idempotency_key)
        )
        """
    )


def migration_026_product_pricing_boundaries(conn: sqlite3.Connection) -> None:
    """products 商家私有价格边界（结构化字段，替代 automation_boundaries 自由文本）。

    底价/折扣/促销从「商家级自由文本 + 正则解析」迁移为「每商品结构化列」：
    floor_price（底价，0=未设）、max_discount_percent（最大折扣率 0-100，0=无
    折扣授权）、promotions_json（结构化促销数组）。三者均私有，买家投影剥离。
    幂等加列（ensure_column 已存在列跳过）；存量行默认 0/[]，行为向后兼容。
    """
    ensure_column(conn, "products", "floor_price", "real not null default 0")
    ensure_column(conn, "products", "max_discount_percent", "real not null default 0")
    ensure_column(conn, "products", "promotions_json", "text not null default '[]'")


def migration_027_delivery_times(conn: sqlite3.Connection) -> None:
    """delivery_rules.delivery_times_json 列（商家按区域配送时效，发现指标）。

    商家级一个字段：区域 → {min_days, max_days}（如 东北 3-4 天、华北 1-2 天）。
    买家在发现/搜索阶段看到摘要（listings 投影 → commercial_hints.lead_time_hint）。
    幂等加列（ensure_column 已存在列跳过）；存量行默认 '{}'，行为向后兼容。
    """
    ensure_column(conn, "delivery_rules", "delivery_times_json", "text not null default '{}'")


def migration_028_exact_money_authority(conn: sqlite3.Connection) -> None:
    """Workbench 精确金额迁移骨架；不从历史 REAL 猜回 minor units。

    新列保持 NULL，直到商家进入 FROZEN 并从权威源逐 SKU 重新读取十进制文本。
    只有全量 staging/对账完成后才切到 EXACT_MINOR；REAL 此后只是派生兼容投影。
    """
    ensure_column(conn, "products", "price_minor_text", "text")
    ensure_column(conn, "products", "floor_price_minor_text", "text")
    ensure_column(conn, "products", "money_currency_table_version", "text not null default ''")
    ensure_column(conn, "delivery_rules", "fee_minor_text", "text")
    ensure_column(conn, "delivery_rules", "money_currency_table_version", "text not null default ''")
    conn.execute(
        """
        create table if not exists merchant_money_authority (
            merchant_id text primary key,
            mode text not null default 'LEGACY_REAL'
                check(mode in ('LEGACY_REAL','FROZEN','EXACT_MINOR')),
            authority_version integer not null default 0,
            started_at text,
            activated_at text,
            updated_at text not null,
            foreign key (merchant_id) references merchants(id)
        )
        """
    )


def migration_029_merchant_product_operation_receipts(conn: sqlite3.Connection) -> None:
    """Operation-scoped receipts for exact Workbench product writes."""
    conn.execute(
        """
        create table if not exists merchant_product_operations (
            operation_id text primary key,
            merchant_id text not null,
            operation_kind text not null
                check(operation_kind in ('exact_product_create','exact_product_money_update')),
            sku text not null,
            request_hash text not null,
            status text not null check(status in ('succeeded')),
            response_json text not null,
            created_at text not null,
            foreign key (merchant_id) references merchants(id)
        )
        """
    )
    conn.execute(
        """
        create index if not exists idx_merchant_product_operations_owner
        on merchant_product_operations(merchant_id, created_at, operation_id)
        """
    )


def migration_030_product_inventory_operation_receipts(conn: sqlite3.Connection) -> None:
    """merchant_product_operations.operation_kind CHECK 扩展：允许库存更新。

    v29 的 CHECK 只允许 ('exact_product_create','exact_product_money_update')。B 线要为
    **库存写入**提供同事务 operation receipt——现状是库存写入走 legacy
    `PATCH /products/{sku}`（`update_product` 接受 `stock`），**没有可对账的回执**，一旦
    落进 UNKNOWN 就无法查明副作用是否真的发生。SQLite 不能 ALTER CHECK，故重建表
    （复制全列 + 数据 + 索引）；幂等：schema 已含 product_inventory_update 时跳过。

    **已知代价**：v29 把词表写进了 CHECK，于是**每新增一个 operation kind 都要再重建一次
    表**。本次只加 `product_inventory_update`（不预置尚未实现的 kind，避免死词表）；若
    kind 还要继续增长（listing 变更等），应重新评估是否改为枚举表或去掉 CHECK 由应用层
    校验——那时是一次设计决策，不是又一轮机械重建。
    """
    row = conn.execute(
        "select sql from sqlite_master where type='table' and name='merchant_product_operations'"
    ).fetchone()
    if row is None or "product_inventory_update" in (row[0] or ""):
        return
    conn.execute(
        """
        create table merchant_product_operations_v30 (
            operation_id text primary key,
            merchant_id text not null,
            operation_kind text not null
                check(operation_kind in (
                    'exact_product_create','exact_product_money_update','product_inventory_update'
                )),
            sku text not null,
            request_hash text not null,
            status text not null check(status in ('succeeded')),
            response_json text not null,
            created_at text not null,
            foreign key (merchant_id) references merchants(id)
        )
        """
    )
    conn.execute(
        """
        insert into merchant_product_operations_v30(
            operation_id, merchant_id, operation_kind, sku, request_hash,
            status, response_json, created_at
        )
        select
            operation_id, merchant_id, operation_kind, sku, request_hash,
            status, response_json, created_at
        from merchant_product_operations
        """
    )
    conn.execute("drop table merchant_product_operations")
    conn.execute(
        "alter table merchant_product_operations_v30 rename to merchant_product_operations"
    )
    conn.execute(
        """
        create index if not exists idx_merchant_product_operations_owner
        on merchant_product_operations(merchant_id, created_at, operation_id)
        """
    )


def migration_031_listing_pause_and_operation_kind_enum(conn: sqlite3.Connection) -> None:
    """operation kind 词表改枚举表（最后一次重建表）+ products.listing_paused 状态列。

    **为什么是枚举表而不是再一次放宽 CHECK**（东哥 2026-09-22 拍板）：v29/v30 把
    operation kind 词表写进了 CHECK，于是**每新增一个 kind 都要机械重建一次表**
    （v30 已为此付过一次代价，其 docstring 也预警了这一点）。本次为 B 线「上下架」
    （product_listing_change）加 kind 时一并把词表收敛到枚举表
    ``merchant_product_operation_kinds``，``merchant_product_operations.operation_kind``
    去掉 CHECK、改为外键引用——**以后加 kind = 枚举表加行，不再重建表**。

    **FK 的实际生效行为**：``shopping_cli/db/session.py`` 的 ``open_connection`` 对每个
    连接执行 ``pragma foreign_keys = on``，因此经应用入口打开的连接上该外键**真实
    强制**——未播种的 kind 插入会被 IntegrityError 拒绝（与此前 CHECK 的拒绝强度
    一致）。SQLite 默认不开 FK，绕过应用入口的裸连接不强制，但写入路径全部走
    ``db_session``，不接受裸连接写入。

    重建照 ``_v30`` 模式（复制全列 + 数据 + 索引）；幂等：schema 已引用枚举表时跳过。
    迁移顺序保证先播种再重建——FK 开启下 insert select 要求既有行的 kind 全部在枚举
    表中（v30 词表三个 kind + 本次新增的 product_listing_change，共四行）。

    ``products.listing_paused``：上下架此前**没有任何数据模型**（listings/* 全是从
    products 派生的只读投影，发布面随 kiwi-catalog 子系统移除）。新增
    ``listing_paused integer not null default 0`` 作为「暂停销售」的唯一事实来源；
    投影层（``list_publishable_listings``）据此排除暂停商品——这就是「下架」的语义。
    """
    conn.execute(
        """
        create table if not exists merchant_product_operation_kinds (
            kind text primary key
        )
        """
    )
    for kind in (
        "exact_product_create",
        "exact_product_money_update",
        "product_inventory_update",
        "product_listing_change",
    ):
        conn.execute(
            "insert or ignore into merchant_product_operation_kinds(kind) values(?)",
            (kind,),
        )
    row = conn.execute(
        "select sql from sqlite_master where type='table' and name='merchant_product_operations'"
    ).fetchone()
    if row is not None and "merchant_product_operation_kinds" not in (row[0] or ""):
        conn.execute(
            """
            create table merchant_product_operations_v31 (
                operation_id text primary key,
                merchant_id text not null,
                operation_kind text not null,
                sku text not null,
                request_hash text not null,
                status text not null check(status in ('succeeded')),
                response_json text not null,
                created_at text not null,
                foreign key (merchant_id) references merchants(id),
                foreign key (operation_kind) references merchant_product_operation_kinds(kind)
            )
            """
        )
        conn.execute(
            """
            insert into merchant_product_operations_v31(
                operation_id, merchant_id, operation_kind, sku, request_hash,
                status, response_json, created_at
            )
            select
                operation_id, merchant_id, operation_kind, sku, request_hash,
                status, response_json, created_at
            from merchant_product_operations
            """
        )
        conn.execute("drop table merchant_product_operations")
        conn.execute(
            "alter table merchant_product_operations_v31 rename to merchant_product_operations"
        )
        conn.execute(
            """
            create index if not exists idx_merchant_product_operations_owner
            on merchant_product_operations(merchant_id, created_at, operation_id)
            """
        )
    ensure_column(conn, "products", "listing_paused", "integer not null default 0")


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
    Migration(16, "product_source_column", migration_016_product_source_column),
    Migration(17, "product_provenance", migration_017_product_provenance),
    Migration(19, "remove_catalog_subsystem_tables", migration_019_remove_catalog_subsystem_tables),
    Migration(20, "buyer_ledger_buyer_dimension", migration_020_buyer_ledger_buyer_dimension),
    Migration(21, "channel_ingress_stale_marker", migration_021_channel_ingress_stale_marker),
    Migration(22, "product_handoff_destination", migration_022_product_handoff_destination),
    Migration(23, "remove_scheme_a_stub_merchants", migration_023_remove_scheme_a_stub_merchants),
    Migration(24, "product_source_csv_excel", migration_024_product_source_csv_excel),
    Migration(25, "negotiation_decision_idempotency", migration_025_negotiation_decision_idempotency),
    Migration(26, "product_pricing_boundaries", migration_026_product_pricing_boundaries),
    Migration(27, "delivery_times", migration_027_delivery_times),
    Migration(28, "exact_money_authority", migration_028_exact_money_authority),
    Migration(29, "merchant_product_operation_receipts", migration_029_merchant_product_operation_receipts),
    Migration(30, "product_inventory_operation_receipts", migration_030_product_inventory_operation_receipts),
    Migration(31, "listing_pause_and_operation_kind_enum", migration_031_listing_pause_and_operation_kind_enum),
)


def run_migrations(conn: sqlite3.Connection) -> None:
    current_version = schema_user_version(conn)
    for migration in MIGRATIONS:
        if migration.version <= current_version:
            continue
        # SAVEPOINT 包裹：迁移中途崩溃不再留下"user_version 已推进但 DDL
        # 缺失"的中间态（此前无事务，migration_009 的破坏性 dedup 与后续
        # 索引创建之间崩溃会让库处于 fast-path 永不修复的状态）。SAVEPOINT
        # 兼容调用方已有的隐式事务（显式 BEGIN 会冲突）。
        savepoint = f"migrate_{migration.version}"
        conn.execute(f"savepoint {savepoint}")
        try:
            migration.apply(conn)
            _set_schema_user_version(conn, migration.version)
            conn.execute(f"release {savepoint}")
        except Exception:
            conn.execute(f"rollback to {savepoint}")
            conn.execute(f"release {savepoint}")
            raise

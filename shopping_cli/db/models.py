"""SQLite schema for the shopping-cli MVP."""

SCHEMA = [
    """
    create table if not exists meta (
        key text primary key,
        value text not null
    )
    """,
    """
    create table if not exists merchants (
        id text primary key,
        name text not null,
        city text not null default '',
        service_area text not null default '',
        contact text not null default '',
        hours text not null default '',
        automation_boundaries text not null default '',
        tags_json text not null default '[]',
        created_at text not null,
        updated_at text not null
    )
    """,
    """
    create table if not exists products (
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
        active integer not null default 1,
        created_at text not null,
        updated_at text not null,
        foreign key (merchant_id) references merchants(id)
    )
    """,
    """
    create table if not exists delivery_rules (
        merchant_id text primary key,
        service_area text not null default '',
        fee real not null default 0,
        currency text not null default 'CNY',
        eta_minutes integer not null default 0,
        radius_km real not null default 0,
        notes text not null default '',
        created_at text not null,
        updated_at text not null,
        foreign key (merchant_id) references merchants(id)
    )
    """,
    """
    create table if not exists conversations (
        id text primary key,
        buyer_id text not null,
        merchant_id text not null,
        sku text not null default '',
        status text not null,
        next_actor text not null default '',
        created_at text not null,
        updated_at text not null,
        last_sender text not null default '',
        foreign key (merchant_id) references merchants(id)
    )
    """,
    """
    create table if not exists messages (
        id integer primary key autoincrement,
        conversation_id text not null,
        sender text not null,
        intent text not null,
        text text not null,
        structured_payload_json text not null default '{}',
        created_at text not null,
        foreign key (conversation_id) references conversations(id)
    )
    """,
    """
    create table if not exists agents (
        id text primary key,
        type text not null,
        owner_id text not null,
        status text not null,
        capabilities_json text not null default '[]',
        last_seen_at text not null,
        pid integer not null default 0,
        version text not null default '',
        last_error text not null default '',
        checked_count integer not null default 0,
        replied_count integer not null default 0
    )
    """,
    """
    create table if not exists moderation_flags (
        id integer primary key autoincrement,
        conversation_id text not null default '',
        sku text not null default '',
        reason text not null,
        severity text not null default 'review',
        created_at text not null,
        resolved_at text not null default '',
        resolution text not null default '',
        resolved_by text not null default ''
    )
    """,
    """
    create table if not exists api_tokens (
        token text primary key,
        token_hash text not null default '',
        token_prefix text not null default '',
        token_suffix text not null default '',
        role text not null,
        merchant_id text not null default '',
        buyer_id text not null default '',
        agent_id text not null default '',
        conversation_id text not null default '',
        revoked_at text not null default '',
        expires_at text not null default '',
        created_at text not null
    )
    """,
    """
    create table if not exists audit_events (
        id integer primary key autoincrement,
        conversation_id text not null default '',
        actor text not null,
        event text not null,
        details_json text not null default '{}',
        created_at text not null
    )
    """,
    """
    create table if not exists agent_message_processes (
        agent_id text not null,
        message_id integer not null,
        conversation_id text not null,
        idempotency_key text not null,
        status text not null,
        attempts integer not null default 0,
        last_error text not null default '',
        created_at text not null,
        updated_at text not null,
        processed_at text not null default '',
        primary key (agent_id, message_id),
        foreign key (message_id) references messages(id)
    )
    """,
    """
    create table if not exists channel_message_ingresses (
        channel text not null,
        external_user_id text not null,
        external_message_id text not null,
        conversation_id text not null default '',
        message_id integer not null default 0,
        status text not null,
        created_at text not null,
        updated_at text not null,
        primary key (channel, external_user_id, external_message_id)
    )
    """,
]

INDEXES = [
    """
    create index if not exists idx_conversations_merchant_status_updated
    on conversations(merchant_id, status, updated_at desc)
    """,
    """
    create index if not exists idx_conversations_merchant_updated
    on conversations(merchant_id, updated_at desc)
    """,
    """
    create index if not exists idx_conversations_buyer_updated
    on conversations(buyer_id, updated_at desc)
    """,
    """
    create index if not exists idx_conversations_buyer_merchant_sku_created
    on conversations(buyer_id, merchant_id, sku, created_at desc)
    """,
    """
    create index if not exists idx_messages_conversation_id
    on messages(conversation_id, id)
    """,
    """
    create index if not exists idx_moderation_flags_conversation_resolved
    on moderation_flags(conversation_id, resolved_at, id)
    """,
    """
    create index if not exists idx_moderation_flags_conversation_id
    on moderation_flags(conversation_id, id)
    """,
    """
    create index if not exists idx_moderation_flags_queue
    on moderation_flags(resolved_at, created_at desc, id desc)
    """,
    """
    create index if not exists idx_api_tokens_merchant_role_created
    on api_tokens(merchant_id, role, created_at desc)
    """,
    """
    create index if not exists idx_api_tokens_token_hash
    on api_tokens(token_hash)
    """,
    """
    create index if not exists idx_api_tokens_merchant_role_prefix
    on api_tokens(merchant_id, role, token_prefix)
    """,
    """
    create index if not exists idx_agents_owner_id
    on agents(owner_id, id)
    """,
    """
    create index if not exists idx_agent_message_processes_agent_status_updated
    on agent_message_processes(agent_id, status, updated_at, message_id)
    """,
    """
    create index if not exists idx_audit_events_actor_event_id
    on audit_events(actor, event, id desc)
    """,
    """
    create index if not exists idx_audit_events_conversation_id
    on audit_events(conversation_id, id)
    """,
    """
    create index if not exists idx_products_active_merchant
    on products(active, merchant_id)
    """,
    """
    create index if not exists idx_products_active_stock_price
    on products(active, stock, price, sku)
    """,
    """
    create index if not exists idx_merchants_city_lower
    on merchants(lower(city), id)
    """,
]

EXTRA_COLUMNS = {
    "conversations": [
        ("next_actor", "text not null default ''"),
    ],
    "agents": [
        ("pid", "integer not null default 0"),
        ("version", "text not null default ''"),
        ("last_error", "text not null default ''"),
        ("checked_count", "integer not null default 0"),
        ("replied_count", "integer not null default 0"),
    ],
    "moderation_flags": [
        ("resolved_at", "text not null default ''"),
        ("resolution", "text not null default ''"),
        ("resolved_by", "text not null default ''"),
    ],
    "api_tokens": [
        ("token_hash", "text not null default ''"),
        ("token_prefix", "text not null default ''"),
        ("token_suffix", "text not null default ''"),
        ("agent_id", "text not null default ''"),
        ("conversation_id", "text not null default ''"),
        ("revoked_at", "text not null default ''"),
        ("expires_at", "text not null default ''"),
    ],
}

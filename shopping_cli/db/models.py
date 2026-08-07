"""SQLite schema and dataclass models for the shopping-cli MVP."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

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
        source text not null default 'local'
            check(source in ('local','erp')),
        created_at text not null,
        updated_at text not null,
        foreign key (merchant_id) references merchants(id)
    )
    """,
    """
    create table if not exists policies (
        merchant_id text not null,
        code text not null,
        category text not null default '',
        title text not null default '',
        body text not null,
        tags_json text not null default '[]',
        high_risk integer not null default 0,
        active integer not null default 1,
        created_at text not null,
        updated_at text not null,
        primary key (merchant_id, code),
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
        reuse_key text not null default '',
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
    """
    create table if not exists buyer_request_idempotency (
        endpoint text not null,
        token_hash text not null,
        idempotency_key text not null,
        request_hash text not null,
        status text not null,
        response_json text not null default '{}',
        buyer_id text not null default '',
        conversation_id text not null default '',
        message_id integer not null default 0,
        created_at text not null,
        updated_at text not null,
        primary key (endpoint, token_hash, idempotency_key)
    )
    """,
    """
    create table if not exists buyer_bootstrap_rate_limits (
        token_hash text not null,
        window_start text not null,
        request_count integer not null default 0,
        updated_at text not null,
        primary key (token_hash, window_start)
    )
    """,
    """
    create table if not exists merchant_bootstrap_idempotency (
        admin_token_hash text not null,
        idempotency_key text not null,
        request_hash text not null,
        merchant_id text not null default '',
        created_at text not null,
        updated_at text not null,
        primary key (admin_token_hash, idempotency_key)
    )
    """,
    # ── Agent Catalog (v10) ──────────────────────────────────────────
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
    """
    create table if not exists agent_catalog_register_limits (
        canonical_domain text not null,
        window_start text not null,
        request_count integer not null default 0,
        updated_at text not null,
        primary key (canonical_domain, window_start)
    )
    """,
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
    """,
    """
    create table if not exists agent_catalog_write_rate_limits (
        actor_key text not null,
        window_start text not null,
        request_count integer not null default 0,
        updated_at text not null,
        primary key (actor_key, window_start)
    )
    """,
    # ── Agent Trust Observations (v13 / v2.2 Phase 2, §5.7) ────────────
    # Private-only: commercial reputation / protocol trust observations.  They
    # are never exposed through public serializers, search responses, or any
    # public API output (§3.4, §5.7).
    """
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
    """,
    # ── Hosted A2A inbound idempotency (v14 / v2.4-W3, binding rc1 §3.6) ────
    # Authoritative (sender_identity, message_id) KNP idempotency ledger for
    # the shared-host A2A JSON-RPC endpoint.  response_json stores the JSON-RPC
    # result/error part so a replay can rebuild the identical response.
    """
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
    """,
]

INDEXES = [
    """
    create unique index if not exists idx_conversations_unique_open_key
    on conversations(reuse_key)
    where reuse_key != '' and status != 'closed'
    """,
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
    """
    create index if not exists idx_policies_merchant_active_code
    on policies(merchant_id, active, code)
    """,
    """
    create index if not exists idx_buyer_request_idempotency_updated
    on buyer_request_idempotency(updated_at desc)
    """,
    """
    create index if not exists idx_buyer_bootstrap_rate_limits_updated
    on buyer_bootstrap_rate_limits(updated_at desc)
    """,
    # ── Agent Catalog indexes ────────────────────────────────────────
    """
    create index if not exists idx_catalog_agents_merchant
    on catalog_agents(merchant_id)
    """,
    """
    create index if not exists idx_catalog_agents_hosted_runtime
    on catalog_agents(hosted_runtime_agent_id)
    """,
    """
    create index if not exists idx_agent_endpoints_catalog_agent
    on agent_endpoints(catalog_agent_id)
    """,
    """
    create index if not exists idx_agent_capabilities_catalog_agent
    on agent_capabilities(catalog_agent_id)
    """,
    """
    create index if not exists idx_agent_skills_catalog_agent
    on agent_skills(catalog_agent_id)
    """,
    """
    create index if not exists idx_agent_profile_snapshots_catalog_agent
    on agent_profile_snapshots(catalog_agent_id)
    """,
    """
    create index if not exists idx_agent_verifications_catalog_agent
    on agent_verifications(catalog_agent_id)
    """,
    """
    create index if not exists idx_agent_trust_observations_catalog_agent
    on agent_trust_observations(catalog_agent_id)
    """,
]

# ── Dataclass models ────────────────────────────────────────────────────


@dataclass(frozen=True)
class CatalogAgent:
    catalog_agent_id: str
    merchant_id: str
    hosted_runtime_agent_id: str
    display_name: str
    provider_name: str
    canonical_domain: str
    agent_type: str
    source_type: str
    lifecycle_status: str
    verification_status: str
    hosting_mode: str
    first_seen_at: str
    last_seen_at: str
    last_verified_at: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> CatalogAgent:
        return cls(
            catalog_agent_id=str(row["catalog_agent_id"]),
            merchant_id=str(row["merchant_id"] or ""),
            hosted_runtime_agent_id=str(row["hosted_runtime_agent_id"] or ""),
            display_name=str(row["display_name"]),
            provider_name=str(row["provider_name"] or ""),
            canonical_domain=str(row["canonical_domain"] or ""),
            agent_type=str(row["agent_type"] or ""),
            source_type=str(row["source_type"]),
            lifecycle_status=str(row["lifecycle_status"]),
            verification_status=str(row["verification_status"]),
            hosting_mode=str(row["hosting_mode"]),
            first_seen_at=str(row["first_seen_at"]),
            last_seen_at=str(row["last_seen_at"]),
            last_verified_at=str(row["last_verified_at"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


@dataclass(frozen=True)
class AgentEndpoint:
    endpoint_id: int
    catalog_agent_id: str
    kind: str
    url: str
    protocol: str
    protocol_version: str
    preference: int
    auth_summary_json: str
    status: str
    last_checked_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AgentEndpoint:
        return cls(
            endpoint_id=int(row["endpoint_id"]),
            catalog_agent_id=str(row["catalog_agent_id"]),
            kind=str(row["kind"]),
            url=str(row["url"] or ""),
            protocol=str(row["protocol"] or ""),
            protocol_version=str(row["protocol_version"] or ""),
            preference=int(row["preference"]),
            auth_summary_json=str(row["auth_summary_json"] or "{}"),
            status=str(row["status"]),
            last_checked_at=str(row["last_checked_at"] or ""),
        )


@dataclass(frozen=True)
class AgentCapability:
    catalog_agent_id: str
    namespace: str
    capability_id: str
    version: str
    required: int
    source: str
    schema_url: str
    spec_url: str
    last_verified_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AgentCapability:
        return cls(
            catalog_agent_id=str(row["catalog_agent_id"]),
            namespace=str(row["namespace"]),
            capability_id=str(row["capability_id"]),
            version=str(row["version"] or ""),
            required=int(row["required"]),
            source=str(row["source"] or ""),
            schema_url=str(row["schema_url"] or ""),
            spec_url=str(row["spec_url"] or ""),
            last_verified_at=str(row["last_verified_at"] or ""),
        )


@dataclass(frozen=True)
class AgentSkill:
    catalog_agent_id: str
    skill_id: str
    name: str
    description: str
    tags_json: str
    input_modes_json: str
    output_modes_json: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AgentSkill:
        return cls(
            catalog_agent_id=str(row["catalog_agent_id"]),
            skill_id=str(row["skill_id"]),
            name=str(row["name"]),
            description=str(row["description"] or ""),
            tags_json=str(row["tags_json"] or "[]"),
            input_modes_json=str(row["input_modes_json"] or "[]"),
            output_modes_json=str(row["output_modes_json"] or "[]"),
        )


@dataclass(frozen=True)
class AgentProfileSnapshot:
    snapshot_id: int
    catalog_agent_id: str
    profile_type: str
    source_url: str
    etag: str
    last_modified: str
    content_hash: str
    raw_json: str
    fetched_at: str
    fresh_until: str
    validation_status: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AgentProfileSnapshot:
        return cls(
            snapshot_id=int(row["snapshot_id"]),
            catalog_agent_id=str(row["catalog_agent_id"]),
            profile_type=str(row["profile_type"]),
            source_url=str(row["source_url"] or ""),
            etag=str(row["etag"] or ""),
            last_modified=str(row["last_modified"] or ""),
            content_hash=str(row["content_hash"] or ""),
            raw_json=str(row["raw_json"] or "{}"),
            fetched_at=str(row["fetched_at"] or ""),
            fresh_until=str(row["fresh_until"] or ""),
            validation_status=str(row["validation_status"]),
        )


@dataclass(frozen=True)
class AgentVerification:
    verification_id: int
    catalog_agent_id: str
    verification_type: str
    result: str
    evidence_json: str
    checked_at: str
    expires_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AgentVerification:
        return cls(
            verification_id=int(row["verification_id"]),
            catalog_agent_id=str(row["catalog_agent_id"]),
            verification_type=str(row["verification_type"]),
            result=str(row["result"] or ""),
            evidence_json=str(row["evidence_json"] or "{}"),
            checked_at=str(row["checked_at"] or ""),
            expires_at=str(row["expires_at"] or ""),
        )


@dataclass(frozen=True)
class AgentTrustObservation:
    """One private trust observation (§5.7).

    Private-only: never exposed through public serializers, search responses,
    or any public API output.  Commercial reputation and protocol trust stay
    separate from public verification metadata.
    """

    observation_id: int
    catalog_agent_id: str
    kind: str
    value: float
    source: str
    evidence_ref: str
    observed_at: str
    expires_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> AgentTrustObservation:
        return cls(
            observation_id=int(row["observation_id"]),
            catalog_agent_id=str(row["catalog_agent_id"]),
            kind=str(row["kind"]),
            value=float(row["value"]),
            source=str(row["source"] or ""),
            evidence_ref=str(row["evidence_ref"] or ""),
            observed_at=str(row["observed_at"] or ""),
            expires_at=str(row["expires_at"] or ""),
        )

"""Shared API route metadata derived from the executable fallback router."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RouteInfo:
    path: str
    methods: set[str]
    groups: frozenset[str]

    def __init__(self, path: str, methods: set[str], groups: set[str] | None = None):
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "methods", methods)
        object.__setattr__(self, "groups", frozenset(groups or set()))


_ROUTE_GROUPS: dict[str, set[str]] = {
    "/health": {"marketplace"},
    "/merchants": {"merchants"},
    "/merchants/{merchant_id}": {"merchants"},
    "/merchants/{merchant_id}/private-config": {"merchants", "agents"},
    "/merchants/{merchant_id}/token/rotate": {"merchants"},
    "/merchants/{merchant_id}/token/revoke": {"merchants"},
    "/merchants/{merchant_id}/token/recover": {"merchants"},
    "/products": {"merchants"},
    "/products/{sku}": {"merchants"},
    "/search/products": {"marketplace"},
    "/search/merchants": {"marketplace"},
    "/channels/messages": {"marketplace", "conversations"},
    "/buyer/ask": {"marketplace", "conversations"},
    "/conversations": {"conversations"},
    "/conversations/{conversation_id}": {"conversations"},
    "/conversations/{conversation_id}/messages": {"conversations"},
    "/conversations/{conversation_id}/close": {"conversations"},
    "/buyers/{buyer_id}/conversations": {"conversations"},
    "/agents/heartbeat": {"agents"},
    "/agents/tokens": {"agents"},
    "/agents/tokens/revoke": {"agents"},
    "/agents/tokens/rotate": {"agents"},
    "/agents/messages/claim": {"agents"},
    "/agents/messages/complete": {"agents"},
    "/agents/messages/fail": {"agents"},
    "/agents/messages/abandon": {"agents"},
    "/agents/messages/abandon-stale": {"agents"},
    "/agents": {"agents"},
    "/agents/{agent_id}": {"agents"},
    "/merchants/{merchant_id}/agents": {"agents"},
    "/audit/tool-calls": {"agents"},
    "/audit/events": {"agents", "merchants"},
    "/human-review/queue": {"conversations"},
    "/human-review/{review_id}": {"conversations"},
    "/human-review/{review_id}/resolve": {"conversations"},
    "/merchants/{merchant_id}/conversations": {"conversations"},
    "/merchants/{merchant_id}/human-review": {"conversations"},
    "/conversations/{conversation_id}/human-review": {"conversations"},
    "/conversations/{conversation_id}/human-review/resolve": {"conversations"},
    "/capabilities": {"marketplace"},
    "/negotiation/pending-messages": {"agents", "conversations"},
    "/negotiation/claims": {"agents", "conversations"},
    "/negotiation/claims/complete": {"agents", "conversations"},
    "/negotiation/claims/fail": {"agents", "conversations"},
    "/negotiation/claims/abandon": {"agents", "conversations"},
    "/negotiation/claims/heartbeat": {"agents", "conversations"},
    "/negotiation/claims/abandon-stale": {"agents", "conversations"},
    "/negotiation/snapshot": {"agents", "conversations"},
    "/negotiation/decisions": {"agents", "conversations"},
    # ── Agent Catalog v2.1 public read (§10.1) ────────────────────────────────
    "/v1/agent-catalog/agents": {"agent_catalog", "marketplace"},
    "/v1/agent-catalog/agents/search": {"agent_catalog", "marketplace"},
    "/v1/agent-catalog/agents/{catalog_agent_id}": {"agent_catalog", "marketplace"},
    "/v1/agent-catalog/merchants/{merchant_id}/agents": {"agent_catalog", "marketplace"},
    # ── Agent Catalog v2.2 writes (§10.2–§10.4) ────────────────────────────────
    "/v1/agent-catalog/agents/register": {"agent_catalog", "marketplace"},
    "/v1/agent-catalog/agents/{catalog_agent_id}/refresh": {"agent_catalog", "marketplace"},
    "/v1/agent-catalog/agents/{catalog_agent_id}/verify": {"agent_catalog", "marketplace"},
    "/v1/agent-catalog/agents/{catalog_agent_id}/claim": {"agent_catalog", "marketplace"},
    "/v1/agent-catalog/agents/{catalog_agent_id}/suspend": {"agent_catalog", "marketplace"},
    "/v1/agent-catalog/agents/{catalog_agent_id}/reinstate": {"agent_catalog", "marketplace"},
    # ── Hosted A2A publication v2.4-W1 (read-only) ────────────────────────────
    "/v1/hosted/agents/{catalog_agent_id}/agent-card.json": {"agent_catalog", "marketplace"},
    "/v1/hosted/agents/{catalog_agent_id}/ucp": {"agent_catalog", "marketplace"},
    # ── Hosted A2A JSON-RPC endpoint v2.4-W3 ──────────────────────────────────
    "/a2a/agents/{catalog_agent_id}": {"agent_catalog", "marketplace", "a2a"},
    # ── Listing projections（shopping-cli v0.3 §14；只读，Merchant Kiwi 兼容）──
    "/v1/merchant/listings/projections": {"marketplace"},
    "/v1/merchant/listings/{sku}/projection": {"marketplace"},
    # ── ERP 同步（v0.3 §3/#3；写面，merchant/admin token）─────────────────────
    "/v1/merchant/erp/sync": {"merchants"},
}


def route_info() -> list[RouteInfo]:
    # Import lazily to avoid a cycle while app.py constructs its route table.
    from shopping_cli.api.app import _ROUTE_TABLE

    methods_by_path: dict[str, set[str]] = {}
    for entry in _ROUTE_TABLE:
        methods_by_path.setdefault(entry.path_template, set()).update(entry.methods)
    unknown_paths = set(methods_by_path) - set(_ROUTE_GROUPS)
    stale_paths = set(_ROUTE_GROUPS) - set(methods_by_path)
    if unknown_paths or stale_paths:
        raise RuntimeError(
            f"route group metadata is out of sync: unknown={sorted(unknown_paths)}, stale={sorted(stale_paths)}"
        )
    return [RouteInfo(path, methods, _ROUTE_GROUPS[path]) for path, methods in methods_by_path.items()]


def routes_for_group(group: str) -> list[RouteInfo]:
    return [route for route in route_info() if group in route.groups]


# ── kiwi-catalog standalone service（阶段 1 裁剪原型）───────────────────────
# 只暴露 Agent Catalog 域：/v1/agent-catalog/*（注册/验证/搜索/治理）+
# /v1/hosted/*（Agent Card / UCP 发布面）+ /health。 托管协商端点
# （/a2a/agents/{id}，group 含 agent_catalog）被排除——切割分水岭。
# 裁剪是路由层视图；阶段 1 的独立 DB 文件仍是全量 schema 超集（见
# docs/shopping-cli-agent-catalog-extraction-plan-v1.0.md 阶段 1 调整说明）。


def catalog_route_info() -> list[RouteInfo]:
    """Route view for the kiwi-catalog standalone service."""
    return [
        route
        for route in route_info()
        if route.path == "/health"
        or ("agent_catalog" in route.groups and not route.path.startswith("/a2a/"))
    ]


def marketplace_route_info() -> list[RouteInfo]:
    """主 marketplace API 的路由视图（shopping-cli v0.3 MVP #8）。

    排除纯 Agent Catalog 面（/v1/agent-catalog/*——职责已迁移到独立
    kiwi-catalog 服务）；**保留**双组共享路由（/v1/hosted/* Agent Card/UCP
    发布面、/a2a/* A2A 入站——merchant 运行时的对外面）。
    """
    return [
        route
        for route in route_info()
        if not route.path.startswith("/v1/agent-catalog/")
    ]

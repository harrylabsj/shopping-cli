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


def marketplace_route_info() -> list[RouteInfo]:
    """主 marketplace API 的路由视图。

    v3.0 起与 :func:`route_info` 完全一致——Agent Catalog/A2A/hosted 路由
    已随子系统剥离（职责在独立 kiwi-catalog / kiwi 服务）。保留别名以免
    外部调用点（测试）连带修改。
    """
    return route_info()

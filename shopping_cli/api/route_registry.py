"""Shared API route metadata."""

from __future__ import annotations


class RouteInfo:
    def __init__(self, path: str, methods: set[str], groups: set[str] | None = None):
        self.path = path
        self.methods = methods
        self.groups = frozenset(groups or set())


API_ROUTES: tuple[RouteInfo, ...] = (
    RouteInfo("/health", {"GET"}, {"marketplace"}),
    RouteInfo("/merchants", {"GET", "POST"}, {"merchants"}),
    RouteInfo("/merchants/{merchant_id}", {"GET", "PATCH"}, {"merchants"}),
    RouteInfo("/products", {"POST"}, {"merchants"}),
    RouteInfo("/products/{sku}", {"GET", "PATCH"}, {"merchants"}),
    RouteInfo("/search/products", {"GET"}, {"marketplace"}),
    RouteInfo("/search/merchants", {"GET"}, {"marketplace"}),
    RouteInfo("/channels/messages", {"POST"}, {"marketplace", "conversations"}),
    RouteInfo("/buyer/ask", {"POST"}, {"marketplace", "conversations"}),
    RouteInfo("/conversations", {"POST"}, {"conversations"}),
    RouteInfo("/conversations/{conversation_id}", {"GET"}, {"conversations"}),
    RouteInfo("/conversations/{conversation_id}/messages", {"POST"}, {"conversations"}),
    RouteInfo("/conversations/{conversation_id}/close", {"POST"}, {"conversations"}),
    RouteInfo("/buyers/{buyer_id}/conversations", {"GET"}, {"conversations"}),
    RouteInfo("/agents/heartbeat", {"POST"}, {"agents"}),
    RouteInfo("/agents/tokens", {"GET", "POST"}, {"agents"}),
    RouteInfo("/agents/tokens/revoke", {"POST"}, {"agents"}),
    RouteInfo("/agents/tokens/rotate", {"POST"}, {"agents"}),
    RouteInfo("/agents/messages/claim", {"POST"}, {"agents"}),
    RouteInfo("/agents/messages/complete", {"POST"}, {"agents"}),
    RouteInfo("/agents/messages/fail", {"POST"}, {"agents"}),
    RouteInfo("/agents/messages/abandon", {"POST"}, {"agents"}),
    RouteInfo("/agents/messages/abandon-stale", {"POST"}, {"agents"}),
    RouteInfo("/agents", {"GET"}, {"agents"}),
    RouteInfo("/agents/{agent_id}", {"GET"}, {"agents"}),
    RouteInfo("/merchants/{merchant_id}/agents", {"GET"}, {"agents"}),
    RouteInfo("/audit/tool-calls", {"POST"}, {"agents"}),
    RouteInfo("/audit/events", {"GET"}, {"agents", "merchants"}),
    RouteInfo("/human-review/queue", {"GET"}, {"conversations"}),
    RouteInfo("/human-review/{review_id}", {"GET"}, {"conversations"}),
    RouteInfo("/human-review/{review_id}/resolve", {"POST"}, {"conversations"}),
    RouteInfo("/merchants/{merchant_id}/conversations", {"GET"}, {"conversations"}),
    RouteInfo("/merchants/{merchant_id}/human-review", {"GET"}, {"conversations"}),
    RouteInfo("/conversations/{conversation_id}/human-review", {"POST"}, {"conversations"}),
    RouteInfo("/conversations/{conversation_id}/human-review/resolve", {"POST"}, {"conversations"}),
)


def route_info() -> list[RouteInfo]:
    return list(API_ROUTES)


def routes_for_group(group: str) -> list[RouteInfo]:
    return [route for route in route_info() if group in route.groups]

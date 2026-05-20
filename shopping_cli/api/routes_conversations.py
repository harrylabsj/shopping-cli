"""Documented conversation and human-review API route group."""

from __future__ import annotations

from shopping_cli.api.app import RouteInfo, route_info

ROUTE_PATHS = [
    "/buyer/ask",
    "/conversations",
    "/conversations/{conversation_id}",
    "/conversations/{conversation_id}/messages",
    "/conversations/{conversation_id}/close",
    "/buyers/{buyer_id}/conversations",
    "/human-review/queue",
    "/merchants/{merchant_id}/conversations",
    "/merchants/{merchant_id}/human-review",
    "/conversations/{conversation_id}/human-review",
    "/conversations/{conversation_id}/human-review/resolve",
]


def routes() -> list[RouteInfo]:
    wanted = set(ROUTE_PATHS)
    return [route for route in route_info() if route.path in wanted]


def route_paths() -> list[str]:
    return [route.path for route in routes()]

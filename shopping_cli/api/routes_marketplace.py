"""Documented public marketplace API route group."""

from __future__ import annotations

from shopping_cli.api.app import RouteInfo, route_info

ROUTE_PATHS = [
    "/health",
    "/search/products",
    "/search/merchants",
    "/buyer/ask",
]


def routes() -> list[RouteInfo]:
    wanted = set(ROUTE_PATHS)
    return [route for route in route_info() if route.path in wanted]


def route_paths() -> list[str]:
    return [route.path for route in routes()]

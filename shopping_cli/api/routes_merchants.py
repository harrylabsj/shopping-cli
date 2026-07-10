"""Documented merchant and catalog API route group."""

from __future__ import annotations

from shopping_cli.api.route_registry import RouteInfo, routes_for_group


def routes() -> list[RouteInfo]:
    return routes_for_group("merchants")


def route_paths() -> list[str]:
    return [route.path for route in routes()]

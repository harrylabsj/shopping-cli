"""Characterization tests for the pure request dispatch helper.

These lock in the behavior that was historically embedded inline in
``shopping_cli.api.app.handle_request``: route-table iteration order, method
matching, path-parameter forwarding, and the exact error types/messages for
method mismatches and missing routes.
"""

from __future__ import annotations

from typing import Any

import pytest

from shopping_cli.api.request_dispatch import RouteEntry, dispatch_request
from shopping_cli.core.errors import MethodNotAllowedError, NotFoundError


def _echo_handler(db_path: Any, payload: dict[str, Any], query: dict[str, Any], **path_params: str) -> dict[str, Any]:
    return {"path_params": path_params, "payload": payload, "query": query}


def test_dispatch_invokes_matching_handler_and_returns_200() -> None:
    routes = (RouteEntry({"GET"}, "/items/{item_id}", _echo_handler),)
    status, result = dispatch_request(":memory:", "GET", "/items/a-1", {"x": 1}, {"limit": "5"}, routes)
    assert status == 200
    assert result == {"path_params": {"item_id": "a-1"}, "payload": {"x": 1}, "query": {"limit": "5"}}


def test_dispatch_matches_incoming_method_case_insensitively() -> None:
    routes = (RouteEntry({"GET"}, "/items", _echo_handler),)
    status, result = dispatch_request(":memory:", "get", "/items", {}, {}, routes)
    assert status == 200


def test_dispatch_forwards_path_parameters_as_keyword_arguments() -> None:
    captured: dict[str, str] = {}

    def handler(db_path: Any, payload: dict[str, Any], query: dict[str, Any], **path_params: str) -> dict[str, Any]:
        captured.update(path_params)
        return {"ok": True}

    routes = (RouteEntry({"GET"}, "/merchants/{merchant_id}/products/{sku}", handler),)
    status, result = dispatch_request(":memory:", "GET", "/merchants/m-1/products/tea-1", {}, {}, routes)
    assert status == 200
    assert result == {"ok": True}
    assert captured == {"merchant_id": "m-1", "sku": "tea-1"}


def test_dispatch_uses_first_matching_route_in_registration_order() -> None:
    calls: list[str] = []

    def first(db_path: Any, payload: dict[str, Any], query: dict[str, Any], **path_params: str) -> dict[str, Any]:
        calls.append("first")
        return {"via": "first"}

    def second(db_path: Any, payload: dict[str, Any], query: dict[str, Any], **path_params: str) -> dict[str, Any]:
        calls.append("second")
        return {"via": "second"}

    routes = (
        RouteEntry({"GET"}, "/items/{item_id}", first),
        RouteEntry({"GET"}, "/items/{item_id}", second),
    )
    status, result = dispatch_request(":memory:", "GET", "/items/a-1", {}, {}, routes)
    assert status == 200
    assert result == {"via": "first"}
    assert calls == ["first"]


def test_dispatch_raises_method_not_allowed_for_known_path_with_wrong_method() -> None:
    routes = (RouteEntry({"GET"}, "/items/{item_id}", _echo_handler),)
    with pytest.raises(MethodNotAllowedError) as excinfo:
        dispatch_request(":memory:", "POST", "/items/a-1", {}, {}, routes)
    assert str(excinfo.value) == "Method not allowed for POST /items/a-1"


def test_dispatch_raises_not_found_for_missing_route() -> None:
    routes = (RouteEntry({"GET"}, "/items/{item_id}", _echo_handler),)
    with pytest.raises(NotFoundError) as excinfo:
        dispatch_request(":memory:", "GET", "/other", {}, {}, routes)
    assert str(excinfo.value) == "No route for GET /other"


def test_dispatch_raises_not_found_for_empty_route_table() -> None:
    with pytest.raises(NotFoundError) as excinfo:
        dispatch_request(":memory:", "GET", "/items/a-1", {}, {}, ())
    assert str(excinfo.value) == "No route for GET /items/a-1"


def test_dispatch_accepts_list_route_tables() -> None:
    routes = [
        RouteEntry({"GET"}, "/health", lambda db_path, payload, query, **kw: {"ok": True}),
    ]
    status, result = dispatch_request(":memory:", "GET", "/health", {}, {}, routes)
    assert status == 200
    assert result == {"ok": True}

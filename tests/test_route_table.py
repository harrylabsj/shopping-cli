"""Characterization tests for the extracted fallback route table.

``shopping_cli/api/route_table.py`` owns the executable route table, its
handler wrappers and ``resolve_route``.  These tests pin the table's shape,
its static-before-parameter ordering constraints and the route resolution
behavior, so future structural moves cannot silently drop, reorder or
dereference a route.  All tests are pure (no DB, no FastAPI), so they also run
under ``python3 -m unittest discover`` in a no-fastapi environment.
"""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

import shopping_cli.api.fastapi_routes as fastapi_routes_module
import shopping_cli.api.route_table as route_table_module
from shopping_cli.api import app as app_module
from shopping_cli.api.request_dispatch import RouteEntry
from shopping_cli.api.route_table import _ROUTE_TABLE, resolve_route


def _path_index(path: str) -> int:
    for i, entry in enumerate(_ROUTE_TABLE):
        if entry.path_template == path:
            return i
    raise AssertionError(f"route {path!r} not found in _ROUTE_TABLE")


def _methods_by_path() -> dict[str, set[str]]:
    methods_by_path: dict[str, set[str]] = {}
    for entry in _ROUTE_TABLE:
        methods_by_path.setdefault(entry.path_template, set()).update(entry.methods)
    return methods_by_path


class RouteTableCharacterizationTest(unittest.TestCase):
    def test_every_route_entry_is_well_formed(self):
        self.assertGreaterEqual(len(_ROUTE_TABLE), 50)
        for entry in _ROUTE_TABLE:
            with self.subTest(path=entry.path_template):
                self.assertIsInstance(entry, RouteEntry)
                self.assertIsInstance(entry.path_template, str)
                self.assertTrue(entry.path_template.startswith("/"))
                self.assertIsInstance(entry.methods, set)
                self.assertGreaterEqual(len(entry.methods), 1)
                self.assertTrue(all(isinstance(m, str) and m.isupper() for m in entry.methods))
                self.assertTrue(callable(entry.handler))

    def test_route_table_covers_expected_route_groups(self):
        paths = {entry.path_template for entry in _ROUTE_TABLE}
        for expected in (
            "/health",
            "/merchants",
            "/merchants/{merchant_id}",
            "/merchants/{merchant_id}/private-config",
            "/merchants/{merchant_id}/token/rotate",
            "/merchants/{merchant_id}/token/revoke",
            "/merchants/{merchant_id}/token/recover",
            "/merchants/{merchant_id}/conversations",
            "/merchants/{merchant_id}/human-review",
            "/merchants/{merchant_id}/agents",
            "/products",
            "/products/{sku}",
            "/search/products",
            "/search/merchants",
            "/channels/messages",
            "/buyer/ask",
            "/conversations",
            "/conversations/{conversation_id}",
            "/conversations/{conversation_id}/messages",
            "/conversations/{conversation_id}/close",
            "/conversations/{conversation_id}/human-review",
            "/conversations/{conversation_id}/human-review/resolve",
            "/buyers/{buyer_id}/conversations",
            "/agents/heartbeat",
            "/agents/tokens",
            "/agents/tokens/revoke",
            "/agents/tokens/rotate",
            "/agents/messages/claim",
            "/agents/messages/complete",
            "/agents/messages/fail",
            "/agents/messages/abandon",
            "/agents/messages/abandon-stale",
            "/agents",
            "/agents/{agent_id}",
            "/audit/tool-calls",
            "/audit/events",
            "/human-review/queue",
            "/human-review/{review_id}",
            "/human-review/{review_id}/resolve",
            "/capabilities",
            "/negotiation/pending-messages",
            "/negotiation/claims",
            "/negotiation/claims/complete",
            "/negotiation/claims/fail",
            "/negotiation/claims/abandon",
            "/negotiation/claims/heartbeat",
            "/negotiation/claims/abandon-stale",
            "/negotiation/snapshot",
            "/negotiation/decisions",
            "/v1/merchant/listings/projections",
            "/v1/merchant/listings/{sku}/projection",
            "/v1/merchant/erp/sync",
        ):
            self.assertIn(expected, paths, f"missing route {expected!r}")

    def test_static_paths_precede_parameter_siblings(self):
        """顺序匹配约束：静态段必须先于参数兄弟段声明。

        ``_match_path`` 顺序匹配；若参数段先声明，静态段会被当作参数值吞掉。
        """
        self.assertLess(_path_index("/merchants"), _path_index("/merchants/{merchant_id}"))
        self.assertLess(_path_index("/products"), _path_index("/products/{sku}"))
        self.assertLess(_path_index("/agents"), _path_index("/agents/{agent_id}"))
        self.assertLess(_path_index("/agents/tokens"), _path_index("/agents/{agent_id}"))
        self.assertLess(_path_index("/human-review/queue"), _path_index("/human-review/{review_id}"))
        self.assertLess(
            _path_index("/v1/merchant/listings/projections"),
            _path_index("/v1/merchant/listings/{sku}/projection"),
        )

    def test_route_methods_are_pinned(self):
        methods_by_path = _methods_by_path()
        self.assertEqual(methods_by_path["/health"], {"GET"})
        self.assertEqual(methods_by_path["/merchants"], {"GET", "POST"})
        self.assertEqual(methods_by_path["/merchants/{merchant_id}"], {"GET", "PATCH"})
        self.assertEqual(methods_by_path["/products"], {"POST"})
        self.assertEqual(methods_by_path["/products/{sku}"], {"GET", "PATCH"})
        self.assertEqual(methods_by_path["/agents/tokens"], {"GET", "POST"})
        self.assertEqual(methods_by_path["/agents/{agent_id}"], {"GET"})
        self.assertEqual(methods_by_path["/negotiation/claims"], {"POST"})
        self.assertEqual(methods_by_path["/v1/merchant/erp/sync"], {"POST"})

    def test_route_registry_stays_in_sync_with_extracted_table(self):
        from shopping_cli.api.route_registry import route_info

        info = route_info()
        self.assertEqual({r.path for r in info}, {entry.path_template for entry in _ROUTE_TABLE})

    def test_resolve_route_known_path_and_method(self):
        self.assertEqual(resolve_route("GET", "/health"), (True, True))
        self.assertEqual(resolve_route("get", "/health"), (True, True))  # 方法大小写不敏感

    def test_resolve_route_known_path_wrong_method(self):
        self.assertEqual(resolve_route("DELETE", "/health"), (True, False))
        self.assertEqual(resolve_route("POST", "/products/{sku}"), (True, False))

    def test_resolve_route_unknown_path(self):
        self.assertEqual(resolve_route("GET", "/does-not-exist"), (False, False))

    def test_resolve_route_accepts_explicit_route_list(self):
        table = [RouteEntry({"GET"}, "/custom", lambda db_path, payload, query, **kw: "ok")]
        self.assertEqual(resolve_route("GET", "/custom", table), (True, True))
        self.assertEqual(resolve_route("POST", "/custom", table), (True, False))
        self.assertEqual(resolve_route("GET", "/other", table), (False, False))

    def test_resolve_route_skips_non_str_template_fail_closed(self):
        """非 str route 模板 fail-closed：跳过而非强转，正常 str 模板仍匹配。"""

        class _WeirdTemplateRoute:
            def __init__(self, template: object) -> None:
                self.path_template = template
                self.methods = {"GET"}

        class _PathOnlyRoute:
            path = "/legacy"
            methods: set[str] = {"GET"}

        table = [_WeirdTemplateRoute(42), _WeirdTemplateRoute(None), _PathOnlyRoute()]
        self.assertEqual(resolve_route("GET", "/legacy", table), (True, True))
        self.assertEqual(resolve_route("GET", "/42", table), (False, False))

    def test_app_facade_reexports_extracted_symbols(self):
        self.assertIs(app_module._ROUTE_TABLE, route_table_module._ROUTE_TABLE)
        self.assertIs(app_module.resolve_route, route_table_module.resolve_route)
        self.assertIs(app_module.RouteEntry, RouteEntry)
        self.assertIs(app_module.FastAPI, fastapi_routes_module.FastAPI)

    def test_route_entry_is_frozen(self):
        entry = RouteEntry({"GET"}, "/x", lambda **kw: None)
        with self.assertRaises(FrozenInstanceError):
            entry.path_template = "/y"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()

"""Pure request dispatch loop for the fallback route table.

This module owns the route-table entry value and the inline dispatch loop
that was historically embedded in ``shopping_cli.api.app.handle_request``.
It is transport-agnostic: it never touches HTTP, ASGI, FastAPI, validation,
auth, or handlers.  Callers supply the route table and a handler contract in
which each handler receives ``db_path``, ``payload``, ``query`` and any
extracted path parameters as keyword arguments.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from shopping_cli.api.route_matching import match_path as _match_path
from shopping_cli.core.errors import MethodNotAllowedError, NotFoundError

# Route handlers return the JSON response body (a dict).
Handler = Callable[..., Any]


@dataclass(frozen=True)
class RouteEntry:
    methods: set[str]
    path_template: str
    handler: Handler


def dispatch_request(
    db_path: str | Path,
    method: str,
    path: str,
    payload: dict[str, Any],
    query: dict[str, Any],
    routes: Iterable[RouteEntry],
) -> tuple[int, dict[str, Any]]:
    """Dispatch *method*/*path* to the first matching handler in *routes*.

    The route table is evaluated in registration order.  The first route whose
    template matches *path* fixes the path parameters; if its methods contain
    *method* (case-insensitive), the handler is invoked with ``db_path``,
    ``payload``, ``query`` and the extracted path parameters as keyword
    arguments.

    Raises the same errors as the historical inline router:
    ``MethodNotAllowedError`` when a path matches but no method does, and
    ``NotFoundError`` when no route matches the path.
    """
    path_matched = False
    for route in routes:
        path_params = _match_path(route.path_template, path)
        if path_params is None:
            continue
        path_matched = True
        if method.upper() in route.methods:
            result = route.handler(db_path, payload, query, **path_params)
            return 200, result
    if path_matched:
        raise MethodNotAllowedError(f"Method not allowed for {method} {path}")
    raise NotFoundError(f"No route for {method} {path}")

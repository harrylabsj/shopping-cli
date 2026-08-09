"""Marketplace API app factory.

FastAPI is used when installed. The lightweight fallback keeps route metadata
available for local tests in environments where optional API dependencies have
not been installed yet.

装配拆分（move-only，行为不变）：可执行路由表与 handler wrapper 在
``api.route_table``（_ROUTE_TABLE / resolve_route），FastAPI 路由安装与
header 默认值在 ``api.fastapi_routes``（register_fastapi_routes）。本模块
保留公共门面——create_app / handle_request / _ROUTE_TABLE / RouteEntry /
resolve_route / FastAPI / AUTHORIZATION_HEADER / IDEMPOTENCY_KEY_HEADER——
route_registry、fallback ASGI 与既有导入方不变。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from shopping_cli import VERSION
from shopping_cli.api.fallback_asgi import MarketplaceASGIApp
from shopping_cli.api.fastapi_routes import (
    AUTHORIZATION_HEADER,  # noqa: F401 - compat re-export for external importers
    FastAPI,
    Header,  # noqa: F401 - compat re-export for external importers
    IDEMPOTENCY_KEY_HEADER,  # noqa: F401 - compat re-export for external importers
    JSONResponse,  # noqa: F401 - compat re-export for external importers
    Request,  # noqa: F401 - compat re-export for external importers
    RequestValidationError,  # noqa: F401 - compat re-export for external importers
    Response,  # noqa: F401 - compat re-export for external importers
    StarletteHTTPException,  # noqa: F401 - compat re-export for external importers
    register_fastapi_routes,
)
from shopping_cli.api.limits import validate_payload
from shopping_cli.api.request_dispatch import (
    RouteEntry,  # noqa: F401 - compat re-export for external importers
    dispatch_request,
)
from shopping_cli.api.request_limits import (
    ASGIApp as _ASGIApp,  # noqa: F401 - compat re-export for external importers
    ASGIReceive as _ASGIReceive,  # noqa: F401 - compat re-export for external importers
    ASGISend as _ASGISend,  # noqa: F401 - compat re-export for external importers
    RequestBodyLimitMiddleware as _RequestBodyLimitMiddleware,  # noqa: F401 - compat re-export for external importers
)
from shopping_cli.api.route_table import _ROUTE_TABLE, resolve_route
from shopping_cli.core.errors import (
    AuthError,
    ConflictError,
    IdempotencyConflict,
    MethodNotAllowedError,
    NotFoundError,
    PermissionDenied,
    RateLimitError,
    PayloadTooLargeError,
    ShoppingCliError,
    ValidationError,
)

logger = logging.getLogger("shopping-cli")


def handle_request(
    db_path: str | Path,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    payload = payload or {}
    query = query or {}
    try:
        validate_payload(payload)
        return dispatch_request(db_path, method, path, payload, query, _ROUTE_TABLE)
    except AuthError as exc:
        return 403, {"ok": False, "error": str(exc)}
    except PermissionDenied as exc:
        return 403, {"ok": False, "error": str(exc)}
    except IdempotencyConflict as exc:
        return 409, {"ok": False, "error": str(exc)}
    except ConflictError as exc:
        return 409, {"ok": False, "error": str(exc)}
    except NotFoundError as exc:
        return 404, {"ok": False, "error": str(exc)}
    except RateLimitError as exc:
        return 429, {"ok": False, "error": str(exc)}
    except PayloadTooLargeError as exc:
        return 413, {"ok": False, "error": str(exc)}
    except MethodNotAllowedError as exc:
        return 405, {"ok": False, "error": str(exc)}
    except ValidationError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except ShoppingCliError as exc:
        return 400, {"ok": False, "error": str(exc)}
    except Exception:
        logger.exception("unhandled error handling %s %s", method, path)
        return 500, {"ok": False, "error": "internal server error"}


def create_app(db_path: str | Path = "shopping-cli.sqlite") -> Any:
    from shopping_cli.api.route_registry import route_info

    routes = route_info()
    if FastAPI is None:
        return MarketplaceASGIApp(
            db_path,
            route_provider=lambda: routes,
            route_resolver=lambda method, path: resolve_route(method, path, routes=routes),
        )

    app = FastAPI(
        title="shopping-cli Marketplace API",
        version=VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.db_path = str(db_path)
    app.state.fastapi_available = True
    register_fastapi_routes(app, db_path)
    return app

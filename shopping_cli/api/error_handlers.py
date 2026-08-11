"""FastAPI exception-handler registration for the marketplace API.

Centralizes the business exception → HTTP mapping (status code, JSON envelope,
error string) that :func:`shopping_cli.api.app.create_app` used to register
inline, so the transport layer only wires the registration in. This module has
no hard dependency on FastAPI: the response factory and the optional
Starlette/FastAPI exception types are injected by the caller, matching how the
dual API stack isolates optional imports (see ``app.py``'s try/except import
block).
"""

from __future__ import annotations

import logging
from typing import Any

from shopping_cli.api.error_response import build_error_response
from shopping_cli.core.errors import (
    AuthError,
    ConflictError,
    IdempotencyConflict,
    MethodNotAllowedError,
    NotFoundError,
    PayloadTooLargeError,
    PermissionDenied,
    QuotaExceededError,
    RateLimitError,
    ShoppingCliError,
    ValidationError,
)

logger = logging.getLogger("shopping-cli")


def _json_error_response(json_response: Any, status_code: int, error: str) -> Any:
    return build_error_response(status_code, error, json_response)


def register_error_handlers(
    app: Any,
    *,
    json_response: Any = None,
    request_validation_error: Any = None,
    starlette_http_exception: Any = None,
) -> None:
    """Register business and transport error handlers on *app*.

    *app* must expose ``exception_handler`` (a FastAPI app or a test double).
    ``json_response`` is the FastAPI ``JSONResponse`` class, or ``None`` when
    FastAPI is not installed (handlers then return the fallback
    ``SimpleNamespace`` envelope). ``request_validation_error`` and
    ``starlette_http_exception`` are the optional FastAPI/Starlette exception
    types; passing ``None`` skips those handlers, mirroring ``app.py``'s
    conditional registration.

    Registration order is load-bearing: specific business exceptions come
    before their base classes and before the catch-all ``Exception`` handler,
    so a first-match handler walk (as done by the FakeFastAPI test harness)
    resolves a subclass to its own status rather than a base class's.
    """
    @app.exception_handler(AuthError)
    def auth_error_handler(_request: Any, exc: AuthError) -> Any:
        return _json_error_response(json_response, 403, str(exc))

    @app.exception_handler(PermissionDenied)
    def permission_denied_handler(_request: Any, exc: PermissionDenied) -> Any:
        return _json_error_response(json_response, 403, str(exc))

    @app.exception_handler(IdempotencyConflict)
    def idempotency_conflict_handler(_request: Any, exc: IdempotencyConflict) -> Any:
        return _json_error_response(json_response, 409, str(exc))

    @app.exception_handler(ConflictError)
    def conflict_error_handler(_request: Any, exc: ConflictError) -> Any:
        return _json_error_response(json_response, 409, str(exc))

    @app.exception_handler(NotFoundError)
    def not_found_error_handler(_request: Any, exc: NotFoundError) -> Any:
        return _json_error_response(json_response, 404, str(exc))

    @app.exception_handler(QuotaExceededError)
    def quota_exceeded_handler(_request: Any, exc: QuotaExceededError) -> Any:
        return _json_error_response(json_response, 403, str(exc))

    @app.exception_handler(RateLimitError)
    def rate_limit_error_handler(_request: Any, exc: RateLimitError) -> Any:
        return _json_error_response(json_response, 429, str(exc))

    @app.exception_handler(PayloadTooLargeError)
    def payload_too_large_handler(_request: Any, exc: PayloadTooLargeError) -> Any:
        return _json_error_response(json_response, 413, str(exc))

    @app.exception_handler(MethodNotAllowedError)
    def method_not_allowed_handler(_request: Any, exc: MethodNotAllowedError) -> Any:
        return _json_error_response(json_response, 405, str(exc))

    @app.exception_handler(ValidationError)
    def validation_error_handler(_request: Any, exc: ValidationError) -> Any:
        return _json_error_response(json_response, 400, str(exc))

    @app.exception_handler(ShoppingCliError)
    def shopping_cli_error_handler(_request: Any, exc: ShoppingCliError) -> Any:
        return _json_error_response(json_response, 400, str(exc))

    if request_validation_error is not None:  # pragma: no cover - exercised with fastapi installed
        @app.exception_handler(request_validation_error)
        def request_validation_error_handler(request: Any, exc: Exception) -> Any:
            # 不回显 str(exc)：包含 schema 内部结构并回显调用方输入。
            logger.warning("request validation failed on %s: %s", getattr(request, "url", "?"), exc)
            return _json_error_response(json_response, 400, "invalid request body")

    if starlette_http_exception is not None:  # pragma: no cover - exercised with fastapi installed
        @app.exception_handler(starlette_http_exception)
        def http_exception_handler(_request: Any, exc: Any) -> Any:
            status = int(exc.status_code)
            message = "not found" if status == 404 else "method not allowed" if status == 405 else str(exc.detail)
            return _json_error_response(json_response, status, message)

    @app.exception_handler(Exception)
    def unexpected_error_handler(request: Any, exc: Exception) -> Any:
        logger.exception("unhandled error on %s %s", getattr(request, "method", "?"), getattr(request, "url", "?"))
        return _json_error_response(json_response, 500, "internal server error")

"""Characterization tests for :func:`shopping_cli.api.error_handlers.register_error_handlers`.

Locks the exception → HTTP mapping that used to live inline in
``app.py.create_app`` so the refactor into ``error_handlers.py`` is verified
without requiring FastAPI to be installed: business exceptions map to their
status codes with the ``{"ok": false, "error": ...}`` envelope, the response
factory and the optional Starlette/FastAPI exception types are injected (None
skips the conditional handlers), and the transport handlers (request
validation, Starlette 404/405, unexpected 500) keep their pre-move behavior.
"""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

import shopping_cli.api.error_handlers
from shopping_cli.api.error_handlers import register_error_handlers
from shopping_cli.core.errors import (
    AuthError,
    ConflictError,
    IdempotencyConflict,
    MethodNotAllowedError,
    NotFoundError,
    PayloadTooLargeError,
    PermissionDenied,
    RateLimitError,
    ShoppingCliError,
    ValidationError,
)


class _FakeApp:
    """Minimal FastAPI stand-in: records ``exception_handler`` registrations."""

    def __init__(self) -> None:
        self.exception_handlers: dict[type, object] = {}

    def exception_handler(self, exc_type: type) -> object:
        def decorator(func: object) -> object:
            self.exception_handlers[exc_type] = func
            return func

        return decorator


class _FakeValidationError(Exception):
    pass


class _FakeHTTPException(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail


def _decode(response: object) -> tuple[int, dict[str, object]]:
    """Read a fallback SimpleNamespace response (status_code + JSON body)."""
    return response.status_code, json.loads(response.body.decode("utf-8"))


def _capture_response_factory() -> tuple[dict[str, object], object]:
    captured: dict[str, object] = {}

    def factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    return captured, factory


class RegisterErrorHandlersTest(unittest.TestCase):
    def _register(self, **kwargs: object) -> _FakeApp:
        app = _FakeApp()
        register_error_handlers(app, **kwargs)
        return app

    def test_business_exceptions_map_to_status_and_envelope(self):
        cases = [
            (AuthError, 403),
            (PermissionDenied, 403),
            (IdempotencyConflict, 409),
            (ConflictError, 409),
            (NotFoundError, 404),
            (RateLimitError, 429),
            (PayloadTooLargeError, 413),
            (MethodNotAllowedError, 405),
            (ValidationError, 400),
            (ShoppingCliError, 400),
        ]
        app = self._register(json_response=None)
        for exc_type, expected_status in cases:
            with self.subTest(exc_type=exc_type.__name__):
                self.assertIn(exc_type, app.exception_handlers)
                handler = app.exception_handlers[exc_type]
                status, body = _decode(handler(None, exc_type("boom")))
                self.assertEqual(status, expected_status)
                self.assertEqual(body, {"ok": False, "error": "boom"})

    def test_subclass_instances_match_own_handler_first(self):
        # The FakeFastAPI dispatch used by test_public_marketplace walks
        # exception_handlers in registration order with isinstance(); specific
        # subclasses must be registered before their base classes.
        app = self._register(json_response=None)
        entries = list(app.exception_handlers.items())
        for exc, expected_handler in [
            (AuthError("no token"), AuthError),
            (IdempotencyConflict("dup key"), IdempotencyConflict),
            (PermissionDenied("no"), PermissionDenied),
            (ConflictError("conflict"), ConflictError),
        ]:
            with self.subTest(exc=type(exc).__name__):
                handler = next(h for t, h in entries if isinstance(exc, t))
                status, _ = _decode(handler(None, exc))
                self.assertEqual(app.exception_handlers[expected_handler], handler)
                self.assertEqual(status, 403 if isinstance(exc, (AuthError, PermissionDenied)) else 409)

    def test_fallback_response_factory_needs_no_fastapi(self):
        app = self._register(json_response=None)
        handler = app.exception_handlers[ValidationError]
        status, body = _decode(handler(None, ValidationError("bad")))
        self.assertEqual(status, 400)
        self.assertEqual(body, {"ok": False, "error": "bad"})

    def test_injected_response_factory_receives_status_and_content(self):
        captured, factory = _capture_response_factory()
        app = self._register(json_response=factory)
        handler = app.exception_handlers[NotFoundError]
        handler(None, NotFoundError("gone"))
        self.assertEqual(captured["status_code"], 404)
        self.assertEqual(captured["content"], {"ok": False, "error": "gone"})

    def test_request_validation_error_returns_sanitized_400(self):
        app = self._register(json_response=None, request_validation_error=_FakeValidationError)
        self.assertIn(_FakeValidationError, app.exception_handlers)
        handler = app.exception_handlers[_FakeValidationError]
        request = SimpleNamespace(url="http://test/merchants")
        with self.assertLogs("shopping-cli", level="WARNING") as logs:
            status, body = _decode(handler(request, _FakeValidationError("schema internals")))
        self.assertEqual(status, 400)
        self.assertEqual(body, {"ok": False, "error": "invalid request body"})
        self.assertTrue(any("request validation failed on" in message for message in logs.output))

    def test_starlette_http_exception_mapping(self):
        app = self._register(json_response=None, starlette_http_exception=_FakeHTTPException)
        self.assertIn(_FakeHTTPException, app.exception_handlers)
        handler = app.exception_handlers[_FakeHTTPException]
        for status_code, expected_message in (
            (404, "not found"),
            (405, "method not allowed"),
            (422, "detail"),  # any other status echoes str(exc.detail)
        ):
            with self.subTest(status_code=status_code):
                status, body = _decode(handler(None, _FakeHTTPException(status_code, "detail")))
                self.assertEqual(status, status_code)
                self.assertEqual(body, {"ok": False, "error": expected_message})

    def test_unexpected_exception_maps_to_500(self):
        app = self._register(json_response=None)
        self.assertIn(Exception, app.exception_handlers)
        handler = app.exception_handlers[Exception]
        with self.assertLogs("shopping-cli", level="ERROR"):
            status, body = _decode(handler(None, RuntimeError("secret internal detail")))
        self.assertEqual(status, 500)
        self.assertEqual(body, {"ok": False, "error": "internal server error"})

    def test_optional_handlers_skipped_when_types_absent(self):
        app = self._register(json_response=None)
        self.assertNotIn(_FakeValidationError, app.exception_handlers)
        self.assertNotIn(_FakeHTTPException, app.exception_handlers)

    def test_module_has_no_hard_fastapi_dependency(self):
        source = Path(shopping_cli.api.error_handlers.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import fastapi", source)
        self.assertNotIn("from fastapi", source)
        self.assertNotIn("from starlette", source)


if __name__ == "__main__":
    unittest.main()

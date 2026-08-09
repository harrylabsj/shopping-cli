"""Shared synchronous Marketplace API transport used by agents and LLM tools."""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any, cast

HTTPTransport = Callable[
    [str, str, dict[str, Any] | None, dict[str, Any] | None, dict[str, str]],
    dict[str, Any],
]
MAX_HTTP_TIMEOUT_SECONDS = 60.0
MAX_HTTP_RESPONSE_BYTES = 8 * 1024 * 1024


class MarketplaceHTTPError(RuntimeError):
    """Raised when the Marketplace API transport or response is invalid."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never follow redirects for authenticated Marketplace requests.

    urllib's default redirect handler can replay request headers on the next
    hop. Rejecting redirects at the transport boundary prevents a Bearer
    credential from crossing origins (and makes the failure explicit to the
    caller).
    """

    def redirect_request(self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _bounded_read(response: Any) -> bytes:
    """Read a response body without allowing unbounded memory growth."""
    content_length = None
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            content_length = headers.get("Content-Length")
        except AttributeError:
            content_length = None
    if content_length is None and hasattr(response, "getheader"):
        content_length = response.getheader("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_HTTP_RESPONSE_BYTES:
                raise MarketplaceHTTPError("Marketplace API response exceeds 8 MiB limit")
        except (TypeError, ValueError):
            pass
    try:
        raw_body = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
    except TypeError:
        # Small test doubles and embedding transports may expose read() without
        # a size argument; retain compatibility while enforcing the post-read cap.
        raw_body = response.read()
    if len(raw_body) > MAX_HTTP_RESPONSE_BYTES:
        raise MarketplaceHTTPError("Marketplace API response exceeds 8 MiB limit")
    return raw_body


def _url_origin(parts: urllib.parse.SplitResult) -> tuple[str, str, int | None]:
    try:
        port = parts.port
    except ValueError:
        port = None
    return (parts.scheme.lower(), (parts.hostname or "").lower(), port)


def _assert_same_origin(response: Any, request_url: str) -> None:
    """Fail closed when a transport returned a response from another origin.

    urllib exposes the effective URL on responses (``geturl``). The default
    opener refuses redirects via _NoRedirectHandler, but a caller-supplied
    opener may follow a 3xx and replay the Authorization header on the next
    hop. Comparing the effective origin rejects such responses so a
    cross-origin result is never accepted as the Marketplace API's answer.
    """
    geturl = getattr(response, "geturl", None)
    if geturl is None:
        return
    try:
        effective = urllib.parse.urlsplit(geturl())
    except ValueError:
        raise MarketplaceHTTPError("Marketplace API response has an invalid origin") from None
    requested = urllib.parse.urlsplit(request_url)
    if _url_origin(effective) != _url_origin(requested):
        raise MarketplaceHTTPError(
            f"Marketplace API response origin mismatch ({effective.netloc or geturl()})"
        )


def safe_http_timeout(value: Any, default: float = 10.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    if not math.isfinite(number) or number <= 0:
        return default
    return min(number, MAX_HTTP_TIMEOUT_SECONDS)


def validate_marketplace_base_url(base_url: str, allow_insecure_http: bool = False) -> str:
    normalized = str(base_url or "").rstrip("/")
    if not normalized:
        raise ValueError("base_url is required")
    parsed = urllib.parse.urlsplit(normalized)
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    insecure_allowed = allow_insecure_http or str(os.environ.get("SHOPPING_ALLOW_INSECURE_HTTP") or "").lower() in {
        "1", "true", "yes", "on"
    }
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("base_url must use http or https")
    if parsed.scheme == "http" and parsed.hostname not in local_hosts and not insecure_allowed:
        raise ValueError(
            "remote Marketplace API URLs must use https; set SHOPPING_ALLOW_INSECURE_HTTP only for trusted internal networks"
        )
    return normalized


class MarketplaceHTTPClient:
    def __init__(
        self,
        base_url: str,
        auth_token: str,
        *,
        timeout: Any = 10.0,
        opener: Any | None = None,
        transport: HTTPTransport | None = None,
        allow_insecure_http: bool = False,
    ) -> None:
        self.base_url = validate_marketplace_base_url(base_url, allow_insecure_http)
        self.auth_token = str(auth_token or "").strip()
        if not self.auth_token:
            raise ValueError("auth_token is required")
        self.timeout = safe_http_timeout(timeout)
        # Keep the default late-bound so tests and embedding hosts can patch the
        # standard transport after constructing a client.
        self.opener = opener
        self.transport = transport

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        method = method.upper()
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.auth_token}"}
        if self.transport is not None:
            return self.validate_response(self.transport(method, path, payload, query, headers))
        url = f"{self.base_url}/{path.lstrip('/')}"
        clean_query = {key: value for key, value in (query or {}).items() if value not in (None, "")}
        if clean_query:
            url = f"{url}?{urllib.parse.urlencode(clean_query)}"
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            opener = self.opener
            if opener is None:
                opener = urllib.request.build_opener(_NoRedirectHandler())
            # build_opener returns an OpenerDirector (call .open, not
            # __call__); custom test doubles are callables invoked directly.
            # Normalize so the default transport path actually engages the
            # redirect refusal below.
            if hasattr(opener, "open"):
                opener = opener.open
            open_request = cast(Callable[..., Any], opener)
            with open_request(request, timeout=self.timeout) as response:
                status = int(getattr(response, "status", 200))
                if 300 <= status < 400:
                    raise MarketplaceHTTPError(
                        f"Marketplace API redirect refused (HTTP {status})"
                    )
                _assert_same_origin(response, url)
                raw_body = _bounded_read(response)
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            raw_body = _bounded_read(exc)
            raise MarketplaceHTTPError(self.error_message(raw_body, f"Marketplace API returned HTTP {exc.code}")) from exc
        except TimeoutError as exc:
            raise MarketplaceHTTPError(f"Marketplace API request timed out: {exc}") from exc
        except urllib.error.URLError as exc:
            raise MarketplaceHTTPError(f"Marketplace API request failed: {exc.reason}") from exc
        return self.validate_response(self.decode_body(raw_body))

    @staticmethod
    def decode_body(raw_body: bytes) -> dict[str, Any]:
        if not raw_body:
            return {}
        try:
            decoded = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketplaceHTTPError("Marketplace API returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise MarketplaceHTTPError("Marketplace API returned a non-object response")
        return decoded

    @classmethod
    def error_message(cls, raw_body: bytes, fallback: str) -> str:
        try:
            decoded = cls.decode_body(raw_body)
        except MarketplaceHTTPError:
            return fallback
        return str(decoded.get("error") or fallback)

    @staticmethod
    def validate_response(result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise MarketplaceHTTPError("Marketplace API returned a non-object response")
        if result.get("ok") is False:
            raise MarketplaceHTTPError(str(result.get("error") or "Marketplace API request failed"))
        return result

    @staticmethod
    def response_object(result: dict[str, Any], key: str) -> dict[str, Any]:
        value = result.get(key)
        if not isinstance(value, dict):
            raise MarketplaceHTTPError(f"Marketplace API response missing object: {key}")
        return dict(value)

    @staticmethod
    def response_list(result: dict[str, Any], key: str) -> list[Any]:
        value = result.get(key)
        if not isinstance(value, list):
            raise MarketplaceHTTPError(f"Marketplace API response missing list: {key}")
        return list(value)

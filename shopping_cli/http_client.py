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


def _assert_same_origin_request(url: str, base_url: str) -> None:
    """Fail closed before attaching Authorization to a non-same-origin target.

    审查 P1-12：凭据只在目标与 base_url 同源时才附加。请求 URL 由
    base_url 拼接而来，正常情况下必然同源；此检查防御畸形 path / base_url
    使 Authorization 落在第三方 origin 的回归。
    """
    try:
        target = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(base_url)
    except ValueError:
        raise MarketplaceHTTPError("Marketplace API request has an invalid URL") from None
    if _url_origin(target) != _url_origin(base):
        raise MarketplaceHTTPError(
            f"Marketplace API request origin mismatch ({target.netloc or url})"
        )


# 审查 P1-12：携带 Authorization 的请求只能交给"保证不跟随重定向"的 opener。
# 默认 opener 内置 _NoRedirectHandler；受控的 urllib ``OpenerDirector`` 会重建
# 剔除会跟跳的 ``HTTPRedirectHandler``。**任意 callable opener 无法被证明不会
# 内部跟跳并在第二 origin 重放 Bearer**，因此对鉴权请求一律 fail-closed——
# 需要自定义传输的调用方走显式的 ``transport=`` 注入点。


def _resolve_opener(opener: Any) -> Any:
    """把 opener 解析为"绝不跟跳重定向"的可调用对象（P1-12 fail-closed）。

    - ``None`` → 内置默认 opener（含 ``_NoRedirectHandler``，契约由库保证）。
    - 真实 urllib ``OpenerDirector`` → 重建剔除会跟跳的 ``HTTPRedirectHandler``。
    - 其余任意对象（含 callable opener）→ 对鉴权请求抛 ``MarketplaceHTTPError``；
      需要注入自定义传输请使用 ``transport=``。
    """
    if opener is None:
        return urllib.request.build_opener(_NoRedirectHandler())
    if isinstance(opener, urllib.request.OpenerDirector):
        return _redirect_safe_opener(opener)
    raise MarketplaceHTTPError(
        "custom opener must be a urllib.request.OpenerDirector; arbitrary "
        "callable openers cannot guarantee no-redirect behavior, use the "
        "transport= injection point instead"
    )


def _redirect_safe_opener(opener: Any) -> Any:
    """Strip redirect-following handlers from a caller-supplied opener.

    审查 P1-12：自定义 opener 可能是带 ``HTTPRedirectHandler`` 的真实
    urllib ``OpenerDirector``——跟随跨源 3xx 时会把原始请求头（含
    Authorization）重放到下一跳，凭据在 ``_assert_same_origin`` 事后拦截
    之前已经出网。重建 opener：逐个拷贝调用方自己的 handler，仅剔除所有
    会跟跳的 ``HTTPRedirectHandler``，再装入拒绝重定向的
    ``_NoRedirectHandler``。不引入 build_opener 的默认 handler 以免与调用方
    自定义传输冲突。仅对 ``OpenerDirector`` 生效——任意 callable opener 已在
    ``_resolve_opener`` 中被 fail-closed，不会走到这里。
    """
    if not isinstance(opener, urllib.request.OpenerDirector):
        return opener
    # mypy 的 urllib.request.OpenerDirector 类型不声明 .handlers；运行时存在，
    # 用 getattr 读取并防御性校验类型。
    handlers = getattr(opener, "handlers", [])
    if not isinstance(handlers, (list, tuple)):
        return opener
    redirect_free = [
        handler
        for handler in handlers
        if not isinstance(handler, urllib.request.HTTPRedirectHandler)
    ]
    if len(redirect_free) == len(handlers):
        return opener
    rebuilt = urllib.request.OpenerDirector()
    for handler in redirect_free:
        rebuilt.add_handler(handler)
    rebuilt.add_handler(_NoRedirectHandler())
    return rebuilt


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
        url = f"{self.base_url}/{path.lstrip('/')}"
        clean_query = {key: value for key, value in (query or {}).items() if value not in (None, "")}
        if clean_query:
            url = f"{url}?{urllib.parse.urlencode(clean_query)}"
        # 审查 P1-12：先同源校验、再附加 Authorization——凭据绝不落在第三方
        # origin。opener 解析放在 transport 分支**之后**：提供 transport 注入时
        # 完全跳过 opener（transport 是显式替代传输，其行为由调用方负责）。
        _assert_same_origin_request(url, self.base_url)
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self.auth_token}"}
        if self.transport is not None:
            try:
                return self.validate_response(self.transport(method, path, payload, query, headers))
            except urllib.error.HTTPError as exc:
                raw_body = _bounded_read(exc)
                raise MarketplaceHTTPError(
                    self.error_message(raw_body, f"Marketplace API returned HTTP {exc.code}")
                ) from exc
            except TimeoutError as exc:
                raise MarketplaceHTTPError(f"Marketplace API request timed out: {exc}") from exc
            except urllib.error.URLError as exc:
                raise MarketplaceHTTPError(f"Marketplace API request failed: {exc.reason}") from exc
        opener = _resolve_opener(self.opener)
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            # build_opener returns an OpenerDirector (call .open, not
            # __call__); OpenerDirector-based custom openers are normalized the
            # same way. A rejected callable opener never reaches this point
            # (fail-closed in _resolve_opener).
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

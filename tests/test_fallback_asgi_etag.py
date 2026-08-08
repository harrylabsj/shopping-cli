"""fallback ASGI ETag/条件 GET 回归测试（v3.0 内联 compute_etag 后补回）。

原覆盖随 discovery 子系统删除（test_discovery_fetcher.py）——内联实现的
语义必须与条件 GET 行为一致：强引号 hash、`*`/`W/`/逗号列表解析、304
仅限成功 GET。
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from shopping_cli.api.fallback_asgi import MarketplaceASGIApp, compute_etag, etag_matches
from shopping_cli.api.handlers.common import DEFAULT_RESULT_LIMIT, result_limit
from shopping_cli.core.catalog import create_merchant
from shopping_cli.db.session import db_session


def _run(coro):
    return asyncio.run(coro)


def _capture(app, path="/merchants", method="GET", headers=None):
    """Drive the ASGI app and capture the response start + body."""
    received = []

    async def send(message):
        received.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": b"",
        "headers": [
            (b"host", b"test"),
        ]
        + [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    _run(app(scope, receive, send))
    start = next(m for m in received if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in received if m["type"] == "http.response.body"
    )
    status = start["status"]
    response_headers = {k.decode().lower(): v.decode() for k, v in start.get("headers", [])}
    return status, response_headers, body


class FallbackAsgiEtagTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db_file = Path(self.tmp) / "shop.sqlite"
        with db_session(self.db_file) as conn:
            create_merchant(conn, "seller-a", "West Lake Tea", city="Hangzhou")
        self.app = MarketplaceASGIApp(self.db_file)

    def test_etag_helpers_match_weak_and_wildcard(self) -> None:
        etag = compute_etag(b'{"ok": true}')
        self.assertTrue(etag.startswith('"') and etag.endswith('"'))
        self.assertTrue(etag_matches(etag, etag))
        self.assertTrue(etag_matches(f"W/{etag}", etag))
        self.assertTrue(etag_matches("*", etag))
        self.assertTrue(etag_matches(f'"other", {etag}', etag))
        self.assertFalse(etag_matches('"other"', etag))
        self.assertFalse(etag_matches("", etag))
        self.assertFalse(etag_matches("garbage", etag))
        # 同内容同 ETag；不同内容不同 ETag
        self.assertEqual(compute_etag(b"abc"), compute_etag(b"abc"))
        self.assertNotEqual(compute_etag(b"abc"), compute_etag(b"abd"))
        # str 与 bytes 同内容一致
        self.assertEqual(compute_etag("abc"), compute_etag(b"abc"))

    def test_conditional_get_returns_304_with_matching_etag(self) -> None:
        status, headers, _body = _capture(self.app, "/merchants")
        self.assertEqual(status, 200)
        etag = headers["etag"]

        status304, headers304, body304 = _capture(
            self.app, "/merchants", headers={"if-none-match": etag}
        )
        self.assertEqual(status304, 304)
        self.assertEqual(body304, b"")

    def test_conditional_get_with_stale_etag_returns_200(self) -> None:
        _status, headers, _body = _capture(self.app, "/merchants")
        etag = headers["etag"]

        status, _headers, body = _capture(
            self.app, "/merchants", headers={"if-none-match": '"stale-etag"'}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body)

    def test_error_body_never_304s(self) -> None:
        # 404 即使 If-None-Match: * 也不得 304（304 只用于成功表示）。
        status, headers, _body = _capture(
            self.app, "/no-such-route", headers={"if-none-match": "*"}
        )
        self.assertEqual(status, 404)
        self.assertNotEqual(headers.get("etag", ""), "")

    def test_post_never_304s(self) -> None:
        _status, headers, _body = _capture(self.app, "/merchants", method="GET")
        etag = headers["etag"]
        # POST 带匹配 ETag 也不得 304（allow_304 仅限 GET）
        status, _h, body = _capture(
            self.app,
            "/merchants",
            method="POST",
            headers={"if-none-match": etag},
        )
        self.assertNotEqual(status, 304)


if __name__ == "__main__":
    unittest.main()

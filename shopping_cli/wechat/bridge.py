"""Stdlib HTTP bridge from WeChat callbacks to the shopping-cli API."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from shopping_cli.wechat.messages import (
    DEFAULT_WECHAT_CHANNEL,
    MAX_WECHAT_BODY_BYTES,
    WeChatMessageError,
    build_channel_payload,
    normalize_wechat_message,
    parse_wechat_xml_message,
)
from shopping_cli.wechat.replies import (
    UNAVAILABLE_REPLY,
    UNSUPPORTED_MESSAGE_REPLY,
    build_text_xml_response,
    format_wechat_reply,
)
from shopping_cli.wechat.signature import verify_wechat_signature


class ShoppingAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class BridgeConfig:
    wechat_token: str
    shopping_api_url: str = "http://127.0.0.1:8765"
    shopping_channel_token: str = ""
    wechat_channel: str = DEFAULT_WECHAT_CHANNEL
    host: str = "127.0.0.1"
    port: int = 8787
    timeout: float = 10.0

    @classmethod
    def from_env(
        cls,
        *,
        wechat_token: str | None = None,
        shopping_api_url: str | None = None,
        shopping_channel_token: str | None = None,
        wechat_channel: str | None = None,
        host: str | None = None,
        port: str | int | None = None,
        timeout: str | float | None = None,
    ) -> "BridgeConfig":
        return cls(
            wechat_token=str(wechat_token if wechat_token is not None else os.environ.get("WECHAT_TOKEN") or "").strip(),
            shopping_api_url=str(
                shopping_api_url
                if shopping_api_url is not None
                else os.environ.get("SHOPPING_API_URL")
                or os.environ.get("SHOPPING_MARKETPLACE_API_URL")
                or "http://127.0.0.1:8765"
            ).strip(),
            shopping_channel_token=str(
                shopping_channel_token
                if shopping_channel_token is not None
                else os.environ.get("SHOPPING_CHANNEL_TOKEN")
                or ""
            ).strip(),
            wechat_channel=str(
                wechat_channel
                if wechat_channel is not None
                else os.environ.get("SHOPPING_WECHAT_CHANNEL")
                or DEFAULT_WECHAT_CHANNEL
            ).strip()
            or DEFAULT_WECHAT_CHANNEL,
            host=str(host if host is not None else os.environ.get("WECHAT_BRIDGE_HOST") or "127.0.0.1").strip()
            or "127.0.0.1",
            port=_positive_port(port if port is not None else os.environ.get("WECHAT_BRIDGE_PORT") or 8787),
            timeout=_positive_timeout(timeout if timeout is not None else os.environ.get("WECHAT_BRIDGE_TIMEOUT_SECONDS") or 10.0),
        )


def _positive_port(value: str | int) -> int:
    try:
        port = int(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("WECHAT_BRIDGE_PORT must be an integer between 1 and 65535") from exc
    if port <= 0 or port > 65535:
        raise ValueError("WECHAT_BRIDGE_PORT must be an integer between 1 and 65535")
    return port


def _positive_timeout(value: str | float) -> float:
    try:
        timeout = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("WECHAT_BRIDGE_TIMEOUT_SECONDS must be a positive number") from exc
    if timeout <= 0:
        raise ValueError("WECHAT_BRIDGE_TIMEOUT_SECONDS must be a positive number")
    return min(timeout, 60.0)


class ShoppingAPIClient:
    def __init__(
        self,
        base_url: str,
        channel_token: str,
        timeout: float = 10.0,
        opener: Any | None = None,
    ) -> None:
        self.base_url = str(base_url or "").rstrip("/")
        if not self.base_url:
            raise ValueError("shopping_api_url is required")
        self.channel_token = str(channel_token or "").strip()
        if not self.channel_token:
            raise ValueError("shopping_channel_token is required")
        self.timeout = _positive_timeout(timeout)
        self.opener = opener or urllib.request.urlopen

    def channel_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/channels/messages", payload)

    def _request(self, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.channel_token}",
                "Content-Type": "application/json",
            },
            method=method.upper(),
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw_body = response.read()
        except urllib.error.HTTPError as exc:
            raw_body = exc.read()
            raise ShoppingAPIError(_shopping_api_error_message(raw_body, f"shopping API returned HTTP {exc.code}")) from exc
        except TimeoutError as exc:
            raise ShoppingAPIError(f"shopping API request timed out: {exc}") from exc
        except urllib.error.URLError as exc:
            raise ShoppingAPIError(f"shopping API request failed: {exc.reason}") from exc
        return _decode_shopping_api_body(raw_body)


def _decode_shopping_api_body(raw_body: bytes) -> dict[str, Any]:
    if not raw_body:
        return {}
    try:
        decoded = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShoppingAPIError("shopping API returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ShoppingAPIError("shopping API returned a non-object response")
    return decoded


def _shopping_api_error_message(raw_body: bytes, fallback: str) -> str:
    try:
        decoded = _decode_shopping_api_body(raw_body)
    except ShoppingAPIError:
        return fallback
    return str(decoded.get("error") or fallback)


class WeChatBridge:
    def __init__(self, config: BridgeConfig, client: ShoppingAPIClient | None = None) -> None:
        self.config = config
        if not self.config.wechat_token:
            raise ValueError("WECHAT_TOKEN is required")
        self.client = client or ShoppingAPIClient(
            self.config.shopping_api_url,
            self.config.shopping_channel_token,
            timeout=self.config.timeout,
        )

    def verify_query(self, query: dict[str, list[str]]) -> bool:
        return verify_wechat_signature(
            self.config.wechat_token,
            _query_value(query, "signature"),
            _query_value(query, "timestamp"),
            _query_value(query, "nonce"),
        )

    def handle_get(self, path: str, query: dict[str, list[str]]) -> tuple[int, dict[str, str], bytes]:
        if path != "/wechat/callback":
            return _text_response(404, "not found")
        if not self.verify_query(query):
            return _text_response(403, "invalid signature")
        return _text_response(200, _query_value(query, "echostr"))

    def handle_post(self, path: str, query: dict[str, list[str]], body: bytes) -> tuple[int, dict[str, str], bytes]:
        if path != "/wechat/callback":
            return _text_response(404, "not found")
        if not self.verify_query(query):
            return _text_response(403, "invalid signature")
        try:
            message = parse_wechat_xml_message(body)
            normalized = normalize_wechat_message(message, source=self.config.wechat_channel)
        except WeChatMessageError as exc:
            return _text_response(400, str(exc))
        to_user = normalized["external_user_id"]
        from_user = normalized["account_id"]
        if normalized["message_type"] != "text":
            return _xml_response(200, build_text_xml_response(to_user, from_user, UNSUPPORTED_MESSAGE_REPLY))
        try:
            payload = build_channel_payload(normalized, channel=self.config.wechat_channel)
            result = self.client.channel_message(payload)
            reply = format_wechat_reply(result)
        except Exception:
            reply = UNAVAILABLE_REPLY
        return _xml_response(200, build_text_xml_response(to_user, from_user, reply))


def _query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return str(values[-1] if values else "")


def _text_response(status: int, text: str) -> tuple[int, dict[str, str], bytes]:
    return status, {"Content-Type": "text/plain; charset=utf-8"}, str(text or "").encode("utf-8")


def _xml_response(status: int, body: bytes) -> tuple[int, dict[str, str], bytes]:
    return status, {"Content-Type": "application/xml; charset=utf-8"}, body


def make_handler(bridge: WeChatBridge) -> type[BaseHTTPRequestHandler]:
    class WeChatBridgeHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path, query = self._path_and_query()
            status, headers, body = bridge.handle_get(path, query)
            self._send(status, headers, body)

        def do_POST(self) -> None:
            path, query = self._path_and_query()
            try:
                content_length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                content_length = 0
            if content_length > MAX_WECHAT_BODY_BYTES:
                self._send(*_text_response(413, f"WeChat message body must be <= {MAX_WECHAT_BODY_BYTES} bytes"))
                return
            body = self.rfile.read(max(content_length, 0))
            status, headers, response_body = bridge.handle_post(path, query, body)
            self._send(status, headers, response_body)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _path_and_query(self) -> tuple[str, dict[str, list[str]]]:
            parsed = urllib.parse.urlsplit(self.path)
            return parsed.path, urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        def _send(self, status: int, headers: dict[str, str], body: bytes) -> None:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return WeChatBridgeHandler


def serve(config: BridgeConfig) -> None:
    bridge = WeChatBridge(config)
    server = ThreadingHTTPServer((config.host, config.port), make_handler(bridge))
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Serve the shopping-cli WeChat AI bridge.")
    parser.add_argument("--host", default=os.environ.get("WECHAT_BRIDGE_HOST") or "127.0.0.1")
    parser.add_argument("--port", default=os.environ.get("WECHAT_BRIDGE_PORT") or "8787")
    parser.add_argument("--wechat-token", default=os.environ.get("WECHAT_TOKEN") or "")
    parser.add_argument(
        "--shopping-api-url",
        default=os.environ.get("SHOPPING_API_URL") or os.environ.get("SHOPPING_MARKETPLACE_API_URL") or "http://127.0.0.1:8765",
    )
    parser.add_argument("--shopping-channel-token", default=os.environ.get("SHOPPING_CHANNEL_TOKEN") or "")
    parser.add_argument("--channel", default=os.environ.get("SHOPPING_WECHAT_CHANNEL") or DEFAULT_WECHAT_CHANNEL)
    parser.add_argument("--timeout", default=os.environ.get("WECHAT_BRIDGE_TIMEOUT_SECONDS") or "10")
    args = parser.parse_args(argv)
    try:
        config = BridgeConfig.from_env(
            wechat_token=args.wechat_token,
            shopping_api_url=args.shopping_api_url,
            shopping_channel_token=args.shopping_channel_token,
            wechat_channel=args.channel,
            host=args.host,
            port=args.port,
            timeout=args.timeout,
        )
        serve(config)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()

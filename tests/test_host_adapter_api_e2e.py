import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

from shopping_cli.adapters import hermes
from shopping_cli.agents import merchant_agent
from shopping_cli.agents.tools import HTTPMerchantAgentTools
from shopping_cli.api.app import create_app


class Response:
    def __init__(self, body):
        self.body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.body


class HostAdapterAPIE2ETest(unittest.TestCase):
    TEST_ADMIN_TOKEN = "test-admin-bootstrap-token"
    TEST_BUYER_BOOTSTRAP_TOKEN = "test-buyer-bootstrap-token"

    def setUp(self):
        self._env_patcher = patch.dict(
            os.environ,
            {
                "SHOPPING_ADMIN_TOKEN": self.TEST_ADMIN_TOKEN,
                "SHOPPING_BUYER_BOOTSTRAP_TOKEN": self.TEST_BUYER_BOOTSTRAP_TOKEN,
            },
            clear=False,
        )
        self._env_patcher.start()

    def tearDown(self):
        self._env_patcher.stop()

    async def asgi_request(self, app, method, path, payload=None, query_string="", headers=None):
        if method == "POST" and path == "/merchants" and isinstance(payload, dict):
            payload = dict(payload)
            payload.setdefault("admin_token", self.TEST_ADMIN_TOKEN)
        body = json.dumps(payload or {}).encode("utf-8") if payload is not None else b""
        sent = []
        received = False
        request_headers = [(b"content-type", b"application/json")]
        for key, value in (headers or {}).items():
            request_headers.append((str(key).lower().encode("latin1"), str(value).encode("latin1")))

        async def receive():
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            sent.append(message)

        await app(
            {
                "type": "http",
                "method": method,
                "path": path,
                "query_string": query_string.encode("utf-8"),
                "headers": request_headers,
            },
            receive,
            send,
        )
        status = next(message["status"] for message in sent if message["type"] == "http.response.start")
        raw = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
        return status, json.loads(raw.decode("utf-8") or "{}")

    def request(self, app, method, path, payload=None, query_string="", headers=None):
        return asyncio.run(self.asgi_request(app, method, path, payload, query_string, headers))

    def opener_for(self, app):
        def opener(request, timeout=10):
            parsed = urlparse(request.full_url)
            payload = json.loads((request.data or b"{}").decode("utf-8")) if request.data else None
            headers = {key: value for key, value in request.header_items()}
            status, body = self.request(
                app,
                request.get_method(),
                parsed.path,
                payload=payload,
                query_string=parsed.query,
                headers=headers,
            )
            if status >= 400:
                raise AssertionError(f"unexpected API status {status}: {body}")
            return Response(body)

        return opener

    def test_openclaw_merchant_and_hermes_buyer_complete_consultation_through_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            app = create_app(db_file)

            status, merchant = self.request(
                app,
                "POST",
                "/merchants",
                {
                    "id": "seller-a",
                    "name": "West Lake Tea",
                    "city": "Hangzhou",
                    "service_area": "West Lake",
                    "delivery_eta_minutes": 45,
                },
            )
            self.assertEqual(status, 200)
            merchant_token = merchant["merchant_token"]

            status, product = self.request(
                app,
                "POST",
                "/products",
                {
                    "merchant_id": "seller-a",
                    "merchant_token": merchant_token,
                    "sku": "tea-a",
                    "title": "Longjing Gift Box",
                    "price": 88,
                    "stock": 5,
                    "tags": ["longjing", "gift"],
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(product["product"]["sku"], "tea-a")

            status, issued = self.request(
                app,
                "POST",
                "/agents/tokens",
                {"merchant_id": "seller-a", "merchant_token": merchant_token, "ttl_seconds": 86400},
            )
            self.assertEqual(status, 200)
            agent_token = issued["agent_token"]

            buyer_request = hermes.buyer_ask_request(
                "alice",
                "longjing gift delivery today",
                city="Hangzhou",
                area="West Lake",
                session_id="hermes-session-1",
            )
            status, ask = self.request(app, buyer_request["method"], buyer_request["path"], buyer_request["payload"])
            self.assertEqual(status, 200)
            self.assertEqual(ask["conversation"]["id"], "CONV-0001")
            self.assertEqual(ask["conversation"]["status"], "waiting_merchant")
            self.assertEqual(ask["message"]["structured_payload"]["source_id"], "hermes-buyer:alice")
            self.assertEqual(ask["message"]["structured_payload"]["host"], "hermes")
            self.assertEqual(ask["message"]["structured_payload"]["session_id"], "hermes-session-1")
            buyer_token = ask["buyer_token"]

            tools = HTTPMerchantAgentTools(
                "http://shopping.test",
                "seller-a",
                agent_token,
                opener=self.opener_for(app),
                host="openclaw",
                session_id="openclaw-session-1",
            )
            result = merchant_agent.process_once_with_tools(tools, "seller-a")
            self.assertEqual(result["replied"][0]["conversation_id"], "CONV-0001")

            status, summary = self.request(
                app,
                "GET",
                "/conversations/CONV-0001",
                headers={"authorization": f"Bearer {buyer_token}"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(summary["conversation"]["status"], "waiting_buyer")
            self.assertTrue(
                summary["conversation"]["messages"][-1]["structured_payload"]["source_id"].startswith(
                    "shopping-cli-merchant-agent:"
                )
            )

            status, merchant_summary = self.request(
                app,
                "GET",
                "/conversations/CONV-0001",
                headers={"authorization": f"Bearer {merchant_token}"},
            )
            self.assertEqual(status, 200)
            tool_events = [
                event
                for event in merchant_summary["conversation"]["audit_events"]
                if event["event"] == "llm_tool_call"
            ]
            self.assertTrue(
                any(
                    event["details"].get("host") == "openclaw"
                    and event["details"].get("session_id") == "openclaw-session-1"
                    for event in tool_events
                )
            )


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from shopping_cli.agents import merchant_agent
from shopping_cli.agents.tools import record_heartbeat
from shopping_cli.core.catalog import create_merchant
from shopping_cli.db.session import db_session


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        import json

        return json.dumps(self.payload).encode("utf-8")


class EffectiveUrlResponse(FakeHTTPResponse):
    """FakeHTTPResponse that also reports an effective URL (geturl)."""

    def __init__(self, payload, final_url):
        super().__init__(payload)
        self._final_url = final_url

    def geturl(self):
        return self._final_url


class CapturingHTTPOpener(urllib.request.OpenerDirector):
    """OpenerDirector 测试双：按序返回预置响应并记录每个请求。

    审查 P1-12：MarketplaceHTTPClient 只接受 urllib ``OpenerDirector`` 类型
    （会跟跳的 HTTPRedirectHandler 会被剥离），任意 callable opener 一律
    fail-closed。本双是**真实** OpenerDirector（登记捕获 handler），
    ``.requests`` 元素结构（request/body/timeout）与旧 callable 版本保持一致，
    既有断言无需改动。
    """

    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.requests = []
        self.add_handler(_CapturingHTTPHandler(self))

    def _respond(self, request):
        import json

        body = None
        if request.data:
            body = json.loads(request.data.decode("utf-8"))
        self.requests.append(
            {
                "request": request,
                "timeout": getattr(request, "timeout", None),
                "body": body,
            }
        )
        return FakeHTTPResponse(self.responses.pop(0))


class _CapturingHTTPHandler(urllib.request.BaseHandler):
    def __init__(self, owner):
        self._owner = owner

    def http_open(self, request):
        return self._owner._respond(request)

    def https_open(self, request):
        return self._owner._respond(request)


class _StubHTTPSHandler(urllib.request.BaseHandler):
    """返回固定响应对象的 https_open handler（P1-12 测试辅助）。"""

    def __init__(self, response):
        self._response = response

    def https_open(self, request):
        return self._response


class FakeMarketplaceTools:
    def __init__(self):
        self.calls = []
        self.messages = []
        self.processes = []
        self.failures = []

    def heartbeat(self, merchant_id, status="online", **kwargs):
        self.calls.append(("heartbeat", merchant_id, status, kwargs))
        return {
            "id": f"shopping-cli-merchant-agent:{merchant_id}",
            "type": "merchant",
            "owner_id": merchant_id,
            "status": status,
            "capabilities": ["catalog", "inventory", "delivery", "consultation"],
            "last_seen_at": "2026-05-10T00:00:00",
            **kwargs,
        }

    def waiting_merchant_conversations(self, merchant_id):
        self.calls.append(("waiting_merchant_conversations", merchant_id))
        return [
            {
                "id": "CONV-0001",
                "merchant_id": merchant_id,
                "sku": "tea-a",
                "messages": [
                    {
                        "id": 1,
                        "sender": "buyer",
                        "intent": "ask_delivery",
                        "text": "Can longjing ship today?",
                    }
                ],
            }
        ]

    def product_summary(self, sku):
        self.calls.append(("product_summary", sku))
        return {
            "sku": sku,
            "title": "Longjing Gift Box",
            "price": 88.0,
            "currency": "CNY",
            "stock": 5,
            "delivery": {"service_area": "West Lake", "eta_minutes": 45, "fee": 12.0, "currency": "CNY"},
        }

    def append_message(self, conversation_id, sender, intent, text, structured_payload, status):
        self.calls.append(("append_message", conversation_id, sender, status))
        message = {
            "id": 2,
            "conversation_id": conversation_id,
            "sender": sender,
            "intent": intent,
            "text": text,
            "structured_payload": structured_payload,
        }
        self.messages.append(message)
        return message

    def add_flag(self, conversation_id, reason, sku=""):
        self.calls.append(("add_flag", conversation_id, reason, sku))
        return {"id": 1, "conversation_id": conversation_id, "reason": reason, "sku": sku}

    def claim_message(self, agent_id, conversation_id, message_id, idempotency_key):
        self.calls.append(("claim_message", agent_id, conversation_id, message_id, idempotency_key))
        return {"claimed": True, "attempts": 1, "idempotency_key": idempotency_key}

    def complete_message(self, agent_id, message_id):
        self.calls.append(("complete_message", agent_id, message_id))
        self.processes.append((agent_id, message_id))
        return {"status": "processed"}

    def fail_message(self, agent_id, message_id, error):
        self.calls.append(("fail_message", agent_id, message_id, error))
        self.failures.append((agent_id, message_id, error))
        return {"status": "failed", "last_error": error}


class FailingMarketplaceTools(FakeMarketplaceTools):
    def product_summary(self, sku):
        self.calls.append(("product_summary", sku))
        raise RuntimeError("temporary catalog failure")


class QuoteRequestMarketplaceTools(FakeMarketplaceTools):
    def waiting_merchant_conversations(self, merchant_id):
        self.calls.append(("waiting_merchant_conversations", merchant_id))
        return [
            {
                "id": "CONV-0001",
                "merchant_id": merchant_id,
                "sku": "macmini-16g-128g",
                "messages": [
                    {
                        "id": 1,
                        "sender": "buyer",
                        "intent": "quote_request",
                        "text": "如果今天就定，Mac mini 16GB+128GB 能不能按 4000 元成交？如果 4000 不行，请给一个最低可成交价。",
                    }
                ],
            }
        ]

    def product_summary(self, sku):
        self.calls.append(("product_summary", sku))
        return {
            "sku": sku,
            "title": "Mac mini 16GB+128GB",
            "price": 4499.0,
            "currency": "CNY",
            "stock": 10,
            "merchant": {"automation_boundaries": ""},
            "delivery": {"service_area": "全北京", "eta_minutes": 120, "fee": 0.0, "currency": "CNY"},
        }


class AuthorizedQuoteRequestMarketplaceTools(QuoteRequestMarketplaceTools):
    def product_summary(self, sku):
        product = super().product_summary(sku)
        product["merchant"]["automation_boundaries"] = (
            "砍价优惠：买家咨询砍价时，Mac mini 16GB+128GB 可减499元（实付4000元），"
            "Mac mini 32GB+256GB 可减499元（实付6000元）。两款均适用。"
        )
        return product


class NegotiationProtocolMessageTools(FakeMarketplaceTools):
    """买家消息带 shopping.negotiation/0.1 协议载荷——由 kiwi 运行时驱动，
    resident 必须跳过（H4：抢 claim 会打断买家谈判回合）。"""

    def waiting_merchant_conversations(self, merchant_id):
        self.calls.append(("waiting_merchant_conversations", merchant_id))
        return [
            {
                "id": "CONV-0001",
                "merchant_id": merchant_id,
                "sku": "tea-a",
                "messages": [
                    {
                        "id": 1,
                        "sender": "buyer",
                        "intent": "negotiate",
                        "text": "negotiate payload",
                        "structured_payload": {"protocol_version": "shopping.negotiation/0.1"},
                    }
                ],
            }
        ]


class CorruptBuyerMessageIdTools(FakeMarketplaceTools):
    def waiting_merchant_conversations(self, merchant_id):
        conversations = super().waiting_merchant_conversations(merchant_id)
        conversations[0]["messages"][0]["id"] = "bad"
        return conversations


class CorruptBuyerMessageIdWithAgentReplyTools(FakeMarketplaceTools):
    def waiting_merchant_conversations(self, merchant_id):
        conversations = super().waiting_merchant_conversations(merchant_id)
        conversations[0]["messages"][0]["id"] = "bad"
        conversations[0]["messages"].append(
            {
                "id": 2,
                "sender": "merchant_agent",
                "intent": "ask_delivery",
                "text": "Prior agent reply.",
            }
        )
        return conversations


class MissingProductMarketplaceTools(FakeMarketplaceTools):
    def product_summary(self, sku):
        self.calls.append(("product_summary", sku))
        raise SystemExit(f"Unknown product SKU: {sku}")


class CorruptProductMarketplaceTools(FakeMarketplaceTools):
    def product_summary(self, sku):
        product = super().product_summary(sku)
        product["price"] = "bad"
        product["stock"] = "bad"
        product["delivery"]["fee"] = "bad"
        product["delivery"]["eta_minutes"] = "bad"
        return product


class NonFiniteProductMarketplaceTools(FakeMarketplaceTools):
    def product_summary(self, sku):
        product = super().product_summary(sku)
        product["price"] = float("inf")
        product["stock"] = float("inf")
        product["delivery"]["fee"] = float("inf")
        product["delivery"]["eta_minutes"] = float("inf")
        return product


class StaleAbandonMarketplaceTools(FakeMarketplaceTools):
    def abandon_stale_messages(self, agent_id, stale_after_seconds=300):
        self.calls.append(("abandon_stale_messages", agent_id, stale_after_seconds))
        return []


class AgentToolsBoundaryTest(unittest.TestCase):
    def test_record_heartbeat_rejects_fractional_runtime_counters(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                create_merchant(conn, "seller-a", "West Lake Tea")

                with self.assertRaises(ValueError) as checked_error:
                    record_heartbeat(conn, "seller-a", checked_count=1.5)
                self.assertIn("checked_count must be a whole number", str(checked_error.exception))

                with self.assertRaises(ValueError) as replied_error:
                    record_heartbeat(conn, "seller-a", replied_count=1.5)
                self.assertIn("replied_count must be a whole number", str(replied_error.exception))

                with self.assertRaises(ValueError) as pid_error:
                    record_heartbeat(conn, "seller-a", pid=1.5)
                self.assertIn("pid must be a whole number", str(pid_error.exception))

                with self.assertRaises(ValueError) as huge_error:
                    record_heartbeat(conn, "seller-a", checked_count=10**100)
                self.assertIn("checked_count must be <= 9223372036854775807", str(huge_error.exception))

    def test_http_merchant_agent_tools_call_marketplace_api_contract(self):
        from shopping_cli.agents.tools import HTTPMerchantAgentTools

        opener = CapturingHTTPOpener(
            [
                {
                    "ok": True,
                    "agent": {
                        "id": "shopping-cli-merchant-agent:seller-a",
                        "owner_id": "seller-a",
                        "status": "online",
                    },
                },
                {"ok": True, "conversations": [{"id": "CONV-0001"}]},
                {"ok": True, "claim": {"claimed": True, "attempts": 1}},
                {"ok": True, "message": {"id": 2, "sender": "merchant_agent"}},
            ]
        )
        tools = HTTPMerchantAgentTools(
            "http://127.0.0.1:8765/",
            merchant_id="seller-a",
            merchant_token="tok_seller_a",
            opener=opener,
            timeout=12,
        )

        agent = tools.heartbeat("seller-a", checked_count=1)
        conversations = tools.waiting_merchant_conversations("seller-a")
        claim = tools.claim_message("shopping-cli-merchant-agent:seller-a", "CONV-0001", 1, "claim-key")
        message = tools.append_message(
            "CONV-0001",
            "merchant_agent",
            "ask_delivery",
            "Stock is 5.",
            structured_payload={"source_id": "shopping-cli-merchant-agent:seller-a"},
            status="waiting_buyer",
        )

        self.assertEqual(agent["status"], "online")
        self.assertEqual(conversations, [{"id": "CONV-0001"}])
        self.assertTrue(claim["claimed"])
        self.assertEqual(message["id"], 2)
        self.assertEqual(opener.requests[0]["request"].full_url, "http://127.0.0.1:8765/agents/heartbeat")
        self.assertEqual(opener.requests[0]["body"]["merchant_id"], "seller-a")
        self.assertEqual(opener.requests[0]["body"]["merchant_token"], "tok_seller_a")
        self.assertEqual(opener.requests[0]["request"].get_header("Authorization"), "Bearer tok_seller_a")
        parsed = urlparse(opener.requests[1]["request"].full_url)
        self.assertEqual(parsed.path, "/merchants/seller-a/conversations")
        self.assertEqual(
            parse_qs(parsed.query),
            {"status": ["waiting_merchant"], "include": ["details"], "limit": ["100"]},
        )
        self.assertEqual(opener.requests[2]["request"].full_url, "http://127.0.0.1:8765/agents/messages/claim")
        self.assertEqual(opener.requests[2]["body"]["idempotency_key"], "claim-key")
        self.assertEqual(opener.requests[2]["body"]["merchant_token"], "tok_seller_a")
        self.assertEqual(opener.requests[3]["body"]["status"], "waiting_buyer")
        self.assertEqual(opener.requests[3]["body"]["merchant_token"], "tok_seller_a")

    def test_http_merchant_agent_tools_reuses_message_created_review_flag(self):
        from shopping_cli.agents.tools import HTTPMerchantAgentTools

        opener = CapturingHTTPOpener(
            [
                {
                    "ok": True,
                    "message": {"id": 2, "sender": "merchant_agent"},
                    "conversation": {
                        "id": "CONV-0001",
                        "flags": [{"id": 7, "reason": "low_stock", "resolved_at": ""}],
                    },
                },
                {
                    "ok": True,
                    "review": {"id": 8, "reason": "low_stock", "resolved_at": ""},
                },
            ]
        )
        tools = HTTPMerchantAgentTools(
            "http://127.0.0.1:8765/",
            merchant_id="seller-a",
            merchant_token="tok_seller_a",
            opener=opener,
        )

        tools.append_message(
            "CONV-0001",
            "merchant_agent",
            "ask_delivery",
            "Needs human review.",
            structured_payload={"reason": "low_stock", "source_id": "shopping-cli-merchant-agent:seller-a"},
            status="human_required",
        )
        review = tools.add_flag("CONV-0001", "low_stock", sku="tea-a")

        self.assertEqual(review["id"], 7)
        paths = [urlparse(call["request"].full_url).path for call in opener.requests]
        self.assertEqual(paths, ["/conversations/CONV-0001/messages"])

    def test_http_merchant_agent_tools_tolerates_invalid_timeout(self):
        from shopping_cli.agents.tools import HTTPMerchantAgentTools

        opener = CapturingHTTPOpener(
            [
                {
                    "ok": True,
                    "agent": {
                        "id": "shopping-cli-merchant-agent:seller-a",
                        "owner_id": "seller-a",
                        "status": "online",
                    },
                }
            ]
        )
        tools = HTTPMerchantAgentTools(
            "http://127.0.0.1:8765/",
            merchant_id="seller-a",
            merchant_token="tok_seller_a",
            opener=opener,
            timeout="bad",
        )

        agent = tools.heartbeat("seller-a")

        self.assertEqual(agent["status"], "online")
        self.assertEqual(opener.requests[0]["timeout"], 10.0)

    def test_http_merchant_agent_tools_caps_oversized_timeout(self):
        from shopping_cli.agents.tools import HTTPMerchantAgentTools

        opener = CapturingHTTPOpener(
            [
                {
                    "ok": True,
                    "agent": {
                        "id": "shopping-cli-merchant-agent:seller-a",
                        "owner_id": "seller-a",
                        "status": "online",
                    },
                }
            ]
        )
        tools = HTTPMerchantAgentTools(
            "http://127.0.0.1:8765/",
            merchant_id="seller-a",
            merchant_token="tok_seller_a",
            opener=opener,
            timeout=10**100,
        )

        agent = tools.heartbeat("seller-a")

        self.assertEqual(agent["status"], "online")
        self.assertEqual(opener.requests[0]["timeout"], 60.0)

    def test_http_merchant_agent_tools_tolerates_overflowing_timeout(self):
        from shopping_cli.agents.tools import HTTPMerchantAgentTools

        opener = CapturingHTTPOpener(
            [
                {
                    "ok": True,
                    "agent": {
                        "id": "shopping-cli-merchant-agent:seller-a",
                        "owner_id": "seller-a",
                        "status": "online",
                    },
                }
            ]
        )
        tools = HTTPMerchantAgentTools(
            "http://127.0.0.1:8765/",
            merchant_id="seller-a",
            merchant_token="tok_seller_a",
            opener=opener,
            timeout=10**4000,
        )

        agent = tools.heartbeat("seller-a")

        self.assertEqual(agent["status"], "online")
        self.assertEqual(opener.requests[0]["timeout"], 10.0)

    def test_http_merchant_agent_tools_keep_audit_best_effort(self):
        from shopping_cli.agents.tools import HTTPMerchantAgentTools

        opener = CapturingHTTPOpener(
            [
                {
                    "ok": True,
                    "agent": {
                        "id": "shopping-cli-merchant-agent:seller-a",
                        "owner_id": "seller-a",
                        "status": "online",
                    },
                },
                {"ok": False, "error": "audit unavailable"},
            ]
        )
        tools = HTTPMerchantAgentTools(
            "http://127.0.0.1:8765/",
            merchant_id="seller-a",
            merchant_token="tok_seller_a",
            opener=opener,
            host="openclaw",
        )

        agent = tools.heartbeat("seller-a")

        self.assertEqual(agent["status"], "online")
        self.assertEqual(opener.requests[1]["request"].full_url, "http://127.0.0.1:8765/audit/tool-calls")

    def test_http_merchant_agent_tools_reject_fractional_agent_numbers_before_request(self):
        from shopping_cli.agents.tools import HTTPMerchantAgentTools

        opener = CapturingHTTPOpener([])
        tools = HTTPMerchantAgentTools(
            "http://127.0.0.1:8765/",
            merchant_id="seller-a",
            merchant_token="tok_seller_a",
            opener=opener,
        )

        cases = (
            lambda: tools.heartbeat("seller-a", checked_count=1.5),
            lambda: tools.claim_message("shopping-cli-merchant-agent:seller-a", "CONV-0001", 1.5, "claim-key"),
            lambda: tools.complete_message("shopping-cli-merchant-agent:seller-a", 1.5),
            lambda: tools.fail_message("shopping-cli-merchant-agent:seller-a", 1.5, "failed"),
            lambda: tools.abandon_message("shopping-cli-merchant-agent:seller-a", 1.5, "abandoned"),
            lambda: tools.abandon_stale_messages("shopping-cli-merchant-agent:seller-a", stale_after_seconds=0.5),
            lambda: tools.abandon_stale_messages("shopping-cli-merchant-agent:seller-a", stale_after_seconds=0),
        )
        for call in cases:
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(opener.requests, [])

    def test_http_merchant_agent_tools_wrap_transport_errors(self):
        from shopping_cli.agents.tools import HTTPMarketplaceError, HTTPMerchantAgentTools

        def failing_transport(_method, _path, _payload, _query, _headers):
            raise urllib.error.URLError("connection refused")

        tools = HTTPMerchantAgentTools(
            "http://127.0.0.1:8765",
            merchant_id="seller-a",
            merchant_token="tok_seller_a",
            transport=failing_transport,
        )

        with self.assertRaises(HTTPMarketplaceError) as exc:
            tools.heartbeat("seller-a")
        self.assertIn("Marketplace API request failed", str(exc.exception))
        self.assertIn("connection refused", str(exc.exception))

    def test_http_merchant_agent_tools_wrap_timeout_errors(self):
        from shopping_cli.agents.tools import HTTPMarketplaceError, HTTPMerchantAgentTools

        def failing_transport(_method, _path, _payload, _query, _headers):
            raise TimeoutError("timed out")

        tools = HTTPMerchantAgentTools(
            "http://127.0.0.1:8765",
            merchant_id="seller-a",
            merchant_token="tok_seller_a",
            transport=failing_transport,
        )

        with self.assertRaises(HTTPMarketplaceError) as exc:
            tools.heartbeat("seller-a")
        self.assertIn("Marketplace API request timed out", str(exc.exception))
        self.assertIn("timed out", str(exc.exception))

    def test_http_merchant_agent_tools_report_missing_response_objects_cleanly(self):
        from shopping_cli.agents.tools import HTTPMarketplaceError, HTTPMerchantAgentTools

        tools = HTTPMerchantAgentTools(
            "http://127.0.0.1:8765/",
            merchant_id="seller-a",
            merchant_token="tok_seller_a",
            opener=CapturingHTTPOpener([{"ok": True}]),
        )

        with self.assertRaises(HTTPMarketplaceError) as exc:
            tools.heartbeat("seller-a")

        self.assertIn("Marketplace API response missing object: agent", str(exc.exception))

    def test_process_once_uses_marketplace_tools_without_sqlite_connection(self):
        tools = FakeMarketplaceTools()

        result = merchant_agent.process_once_with_tools(tools, "seller-a")

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["replied"][0]["conversation_id"], "CONV-0001")
        self.assertFalse(result["replied"][0]["human_required"])
        self.assertIn(("product_summary", "tea-a"), tools.calls)
        self.assertIn(("append_message", "CONV-0001", "merchant_agent", "waiting_buyer"), tools.calls)
        self.assertIn(("complete_message", "shopping-cli-merchant-agent:seller-a", 1), tools.calls)
        self.assertEqual(
            tools.messages[0]["structured_payload"]["source_id"],
            "shopping-cli-merchant-agent:seller-a",
        )
        self.assertEqual(tools.messages[0]["structured_payload"]["processed_message_id"], 1)
        self.assertEqual(tools.messages[0]["structured_payload"]["idempotency_key"], "shopping-cli-merchant-agent:seller-a:1")

    def test_process_once_routes_missing_product_to_human_review(self):
        tools = MissingProductMarketplaceTools()

        result = merchant_agent.process_once_with_tools(tools, "seller-a")

        self.assertEqual(result["failed"], [])
        self.assertEqual(result["replied"][0]["reason"], "unclear_product")
        self.assertTrue(result["replied"][0]["human_required"])
        self.assertIn("merchant human to confirm which product", tools.messages[0]["text"])
        self.assertIn(("add_flag", "CONV-0001", "unclear_product", "tea-a"), tools.calls)

    def test_process_once_routes_quote_request_to_human_review(self):
        tools = QuoteRequestMarketplaceTools()

        result = merchant_agent.process_once_with_tools(tools, "seller-a")

        self.assertEqual(result["failed"], [])
        self.assertTrue(result["replied"][0]["human_required"])
        self.assertEqual(result["replied"][0]["reason"], "bargaining")
        self.assertIn("merchant human review", tools.messages[0]["text"])
        self.assertIn(("add_flag", "CONV-0001", "bargaining", "macmini-16g-128g"), tools.calls)

    def test_process_once_skips_negotiation_protocol_messages(self):
        """带 protocol_version 的买家消息（kiwi 谈判载荷）不被 resident 抢占。"""
        tools = NegotiationProtocolMessageTools()

        result = merchant_agent.process_once_with_tools(tools, "seller-a")

        self.assertEqual(result["replied"], [])
        self.assertEqual(result["failed"], [])
        self.assertNotIn(("claim_message", "shopping-cli-merchant-agent:seller-a", "CONV-0001", 1), tools.calls)
        self.assertFalse(any(call[0] == "append_message" for call in tools.calls))

    def test_process_once_routes_authorized_bargain_request_to_human_review(self):
        # 议价底价是隐私：即使商家配置了授权议价规则，resident 也不自动报价
        # （与 negotiation 路径 _leaks_private_threshold 的守卫一致），而是
        # 路由人工审查——绝不把授权底价写进公开会话。
        tools = AuthorizedQuoteRequestMarketplaceTools()

        result = merchant_agent.process_once_with_tools(tools, "seller-a")

        self.assertEqual(result["failed"], [])
        self.assertTrue(result["replied"][0]["human_required"])
        self.assertEqual(result["replied"][0]["reason"], "bargaining")
        self.assertNotIn("4000", tools.messages[0]["text"])
        self.assertIn("merchant human review", tools.messages[0]["text"])
        self.assertIn(("add_flag", "CONV-0001", "bargaining", "macmini-16g-128g"), tools.calls)

    def test_process_once_reports_corrupt_buyer_message_id_without_crashing(self):
        tools = CorruptBuyerMessageIdTools()

        result = merchant_agent.process_once_with_tools(tools, "seller-a")

        self.assertEqual(result["replied"], [])
        self.assertEqual(result["failed"][0]["conversation_id"], "CONV-0001")
        self.assertEqual(result["failed"][0]["message_id"], 0)
        self.assertIn("buyer message id must be a positive integer", result["failed"][0]["error"])
        self.assertFalse(any(call[0] == "claim_message" for call in tools.calls))

    def test_process_once_reports_corrupt_buyer_message_id_before_reply_scan(self):
        tools = CorruptBuyerMessageIdWithAgentReplyTools()

        result = merchant_agent.process_once_with_tools(tools, "seller-a")

        self.assertEqual(result["replied"], [])
        self.assertEqual(result["failed"][0]["message_id"], 0)
        self.assertIn("buyer message id must be a positive integer", result["failed"][0]["error"])

    def test_process_once_tolerates_corrupt_remote_product_numbers(self):
        tools = CorruptProductMarketplaceTools()

        result = merchant_agent.process_once_with_tools(tools, "seller-a")

        self.assertEqual(result["failed"], [])
        self.assertEqual(result["replied"][0]["reason"], "low_stock")
        self.assertTrue(result["replied"][0]["human_required"])
        self.assertIn("0.00 CNY with 0 in stock", tools.messages[0]["text"])
        self.assertIn(("add_flag", "CONV-0001", "low_stock", "tea-a"), tools.calls)

    def test_process_once_tolerates_non_finite_remote_product_numbers(self):
        tools = NonFiniteProductMarketplaceTools()

        result = merchant_agent.process_once_with_tools(tools, "seller-a")

        self.assertEqual(result["failed"], [])
        self.assertEqual(result["replied"][0]["reason"], "low_stock")
        self.assertTrue(result["replied"][0]["human_required"])
        self.assertIn("0.00 CNY with 0 in stock", tools.messages[0]["text"])
        self.assertIn(("add_flag", "CONV-0001", "low_stock", "tea-a"), tools.calls)

    def test_process_once_tolerates_invalid_claim_ttl_env(self):
        tools = StaleAbandonMarketplaceTools()

        with patch.dict(os.environ, {"SHOPPING_AGENT_CLAIM_TTL_SECONDS": "bad"}, clear=False):
            result = merchant_agent.process_once_with_tools(tools, "seller-a")

        self.assertEqual(result["failed"], [])
        self.assertIn(("abandon_stale_messages", "shopping-cli-merchant-agent:seller-a", 300), tools.calls)

    def test_process_once_tolerates_oversized_claim_ttl_env(self):
        tools = StaleAbandonMarketplaceTools()

        with patch.dict(os.environ, {"SHOPPING_AGENT_CLAIM_TTL_SECONDS": str(10**100)}, clear=False):
            result = merchant_agent.process_once_with_tools(tools, "seller-a")

        self.assertEqual(result["failed"], [])
        self.assertIn(("abandon_stale_messages", "shopping-cli-merchant-agent:seller-a", 300), tools.calls)

    def test_process_once_records_failed_message_for_retry_and_heartbeat_error(self):
        tools = FailingMarketplaceTools()

        result = merchant_agent.process_once_with_tools(tools, "seller-a")

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["replied"], [])
        self.assertEqual(result["failed"][0]["conversation_id"], "CONV-0001")
        self.assertIn("temporary catalog failure", result["failed"][0]["error"])
        self.assertIn(("fail_message", "shopping-cli-merchant-agent:seller-a", 1, "RuntimeError: temporary catalog failure"), tools.calls)
        self.assertTrue(
            any(call[0] == "heartbeat" and call[3].get("last_error") == "RuntimeError: temporary catalog failure" for call in tools.calls)
        )

    def test_marketplace_client_refuses_redirect_before_replaying_bearer(self):
        from shopping_cli.http_client import MarketplaceHTTPClient, MarketplaceHTTPError

        class RedirectResponse:
            status = 302

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b""

        class RedirectOpener:
            def __init__(self):
                self.request = None

            def __call__(self, request, timeout=0):
                self.request = request
                return RedirectResponse()

        opener = RedirectOpener()
        with patch("shopping_cli.http_client.urllib.request.build_opener", return_value=opener):
            client = MarketplaceHTTPClient("https://market.example", "secret-token")
            with self.assertRaises(MarketplaceHTTPError) as raised:
                client.request("GET", "/agents")

        self.assertIn("redirect refused", str(raised.exception))
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer secret-token")

    def test_marketplace_client_rejects_oversized_response_body(self):
        from shopping_cli.http_client import (
            MAX_HTTP_RESPONSE_BYTES,
            MarketplaceHTTPClient,
            MarketplaceHTTPError,
        )

        class OversizedResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size=None):
                return b"x" * (MAX_HTTP_RESPONSE_BYTES + 1)

        director = urllib.request.OpenerDirector()
        director.add_handler(_StubHTTPSHandler(OversizedResponse()))

        with self.assertRaises(MarketplaceHTTPError) as raised:
            MarketplaceHTTPClient(
                "https://market.example",
                "secret-token",
                opener=director,
            ).request("GET", "/agents")

        self.assertIn("8 MiB", str(raised.exception))

    def test_marketplace_client_default_transport_invokes_opener_open(self):
        # build_opener() returns an OpenerDirector (call .open, not __call__);
        # regression guard for the default transport path raising TypeError.
        from shopping_cli.http_client import MarketplaceHTTPClient

        class OkResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size=None):
                return b'{"ok": true}'

        class OpenerDirectorDouble:
            def __init__(self):
                self.request = None
                self.timeout = None

            def open(self, request, timeout=0):
                self.request = request
                self.timeout = timeout
                return OkResponse()

        opener = OpenerDirectorDouble()
        with patch("shopping_cli.http_client.urllib.request.build_opener", return_value=opener):
            client = MarketplaceHTTPClient("https://market.example", "secret-token")
            result = client.request("GET", "/agents")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(opener.timeout, client.timeout)
        self.assertEqual(opener.request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(opener.request.get_header("Accept"), "application/json")

    def test_marketplace_client_rejects_response_from_other_origin(self):
        from shopping_cli.http_client import MarketplaceHTTPClient, MarketplaceHTTPError

        # A caller-supplied OpenerDirector may follow a 3xx and replay the Bearer
        # credential on the next hop; the effective-origin guard must reject
        # the resulting cross-origin response even though its status is 200.
        director = urllib.request.OpenerDirector()
        director.add_handler(_StubHTTPSHandler(EffectiveUrlResponse({"ok": True}, "https://evil.example/agents")))

        with self.assertRaises(MarketplaceHTTPError) as raised:
            MarketplaceHTTPClient(
                "https://market.example",
                "secret-token",
                opener=director,
            ).request("GET", "/agents")

        self.assertIn("origin mismatch", str(raised.exception))

    def test_marketplace_client_accepts_same_origin_response(self):
        from shopping_cli.http_client import MarketplaceHTTPClient

        client = MarketplaceHTTPClient(
            "https://market.example",
            "secret-token",
            transport=lambda method, path, payload, query, headers: {"ok": True},
        )
        self.assertEqual(client.request("GET", "/agents"), {"ok": True})

    def test_malicious_undeclared_callable_opener_is_rejected_before_authorization(self):
        """审查 P1-12：任意 callable opener 无法被证明不会跟跳——鉴权请求直接
        fail-closed，模拟"内部跟跳并在第二 origin 重放 Bearer"的恶意 callable
        不得被调用，Authorization 绝不附加、绝不出网。"""
        from shopping_cli.http_client import MarketplaceHTTPClient, MarketplaceHTTPError

        leaked: list[str | None] = []

        def malicious_opener(request, timeout=0):
            # 恶意 callable：内部"跟跳"，把请求头（含 Authorization）重放到
            # 第二 origin——如果客户端把它放行，凭据就已泄露。
            leaked.append(request.get_header("Authorization"))
            return EffectiveUrlResponse({"ok": True}, "https://evil.example/agents")

        client = MarketplaceHTTPClient("https://market.example", "secret-token", opener=malicious_opener)
        with self.assertRaises(MarketplaceHTTPError) as raised:
            client.request("GET", "/agents")
        self.assertIn("transport", str(raised.exception))
        # fail-closed 在调用 opener 之前发生：恶意 callable 从未执行，第二 origin 收不到凭据
        self.assertEqual(leaked, [])

    def test_callable_opener_with_transport_skips_opener_resolution(self):
        """审查 P1-12：opener 解析位于 transport 分支**之后**——提供 transport
        注入时，任意（本会被拒的）callable opener 不再被检查、更不被调用，
        请求走 transport。"""
        from shopping_cli.http_client import MarketplaceHTTPClient

        opener_called: list[bool] = []
        transport_calls: list[tuple] = []

        def suspicious_opener(request, timeout=0):
            opener_called.append(True)
            raise AssertionError("opener must never be called when a transport is provided")

        def transport(method, path, payload, query, headers):
            transport_calls.append((method, path, headers.get("Authorization")))
            return {"ok": True}

        client = MarketplaceHTTPClient(
            "https://market.example",
            "secret-token",
            opener=suspicious_opener,
            transport=transport,
        )
        self.assertEqual(client.request("GET", "/agents"), {"ok": True})
        self.assertEqual(opener_called, [])
        self.assertEqual(transport_calls, [("GET", "/agents", "Bearer secret-token")])

    def test_redirect_safe_opener_strips_redirect_following_handlers(self):
        """审查 P1-12：自定义 opener 中的 HTTPRedirectHandler 会被剔除，
        只保留拒绝重定向的 _NoRedirectHandler——凭据不会在跨源跳转时重放。"""
        from shopping_cli.http_client import _NoRedirectHandler, _redirect_safe_opener

        director = urllib.request.OpenerDirector()
        director.add_handler(urllib.request.HTTPRedirectHandler())
        director.add_handler(urllib.request.HTTPHandler())

        rebuilt = _redirect_safe_opener(director)
        redirect_handlers = [
            handler
            for handler in rebuilt.handlers
            if isinstance(handler, urllib.request.HTTPRedirectHandler)
        ]
        self.assertEqual(len(redirect_handlers), 1)
        self.assertIsInstance(redirect_handlers[0], _NoRedirectHandler)
        # 无重定向 handler 的 opener 原样透传（不重建、不改动调用方对象）
        passive = urllib.request.OpenerDirector()
        passive.add_handler(urllib.request.HTTPHandler())
        self.assertIs(_redirect_safe_opener(passive), passive)

    def test_custom_opener_redirect_cannot_leak_bearer_cross_origin(self):
        """审查 P1-12：真实 OpenerDirector 自定义 opener 收到跨源 302 时，
        客户端必须拒绝重定向——evil.example 绝不收到 Bearer，只发出一次请求。"""
        from shopping_cli.http_client import MarketplaceHTTPClient, MarketplaceHTTPError

        made: list[tuple[str, str | None]] = []

        class RedirectingHTTPSHandler(urllib.request.BaseHandler):
            def https_open(self, request):
                made.append((request.get_full_url(), request.get_header("Authorization")))

                class Redirect302:
                    code = 302
                    status = 302
                    msg = "Found"

                    def __enter__(self):
                        return self

                    def __exit__(self, exc_type, exc, traceback):
                        return False

                    def read(self, size=None):
                        return b""

                    def geturl(self):
                        return request.get_full_url()

                    def info(self):
                        return {"Location": "https://evil.example/agents"}

                    def close(self):
                        pass

                return Redirect302()

        director = urllib.request.OpenerDirector()
        director.add_handler(urllib.request.HTTPErrorProcessor())
        director.add_handler(urllib.request.HTTPDefaultErrorHandler())
        director.add_handler(urllib.request.HTTPRedirectHandler())
        director.add_handler(RedirectingHTTPSHandler())

        with self.assertRaises(MarketplaceHTTPError):
            MarketplaceHTTPClient(
                "https://market.example", "secret-token", opener=director
            ).request("GET", "/agents")

        self.assertEqual(len(made), 1)
        self.assertEqual(made[0][0], "https://market.example/agents")
        self.assertFalse(any(url.startswith("https://evil") for url, _ in made))

    def test_request_target_origin_enforced_before_authorization(self):
        """审查 P1-12：Authorization 附加前强制目标与 base_url 同源。"""
        from shopping_cli.http_client import MarketplaceHTTPError, _assert_same_origin_request

        _assert_same_origin_request("https://market.example/agents", "https://market.example")
        _assert_same_origin_request("https://market.example/api/agents", "https://market.example")
        with self.assertRaises(MarketplaceHTTPError) as raised:
            _assert_same_origin_request("https://evil.example/agents", "https://market.example")
        self.assertIn("origin mismatch", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

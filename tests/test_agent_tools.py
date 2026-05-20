import os
import tempfile
import unittest
import urllib.error
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


class CapturingHTTPOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=0):
        import json

        body = None
        if request.data:
            body = json.loads(request.data.decode("utf-8"))
        self.requests.append({"request": request, "timeout": timeout, "body": body})
        return FakeHTTPResponse(self.responses.pop(0))


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
        self.assertEqual(parse_qs(parsed.query), {"status": ["waiting_merchant"]})
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

        def failing_opener(_request, timeout=0):
            raise urllib.error.URLError("connection refused")

        tools = HTTPMerchantAgentTools(
            "http://127.0.0.1:8765",
            merchant_id="seller-a",
            merchant_token="tok_seller_a",
            opener=failing_opener,
        )

        with self.assertRaises(HTTPMarketplaceError) as exc:
            tools.heartbeat("seller-a")
        self.assertIn("Marketplace API request failed", str(exc.exception))
        self.assertIn("connection refused", str(exc.exception))

    def test_http_merchant_agent_tools_wrap_timeout_errors(self):
        from shopping_cli.agents.tools import HTTPMarketplaceError, HTTPMerchantAgentTools

        def failing_opener(_request, timeout=0):
            raise TimeoutError("timed out")

        tools = HTTPMerchantAgentTools(
            "http://127.0.0.1:8765",
            merchant_id="seller-a",
            merchant_token="tok_seller_a",
            opener=failing_opener,
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


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shopping_cli.core.catalog import create_merchant, create_product
from shopping_cli.core.conversations import append_message, conversation_summary, ensure_conversation
from shopping_cli.db.session import db_session
from shopping_cli.llm.dispatcher import (
    HTTPMarketplaceError,
    HTTPMarketplaceToolDispatcher,
    MarketplaceToolDispatcher,
    dispatch_marketplace_tool,
)
from shopping_cli.llm.prompts import buyer_system_prompt, merchant_system_prompt
from shopping_cli.llm.providers import OpenAICompatibleProvider, provider_from_env
from shopping_cli.llm.runner import run_marketplace_tool_loop
from shopping_cli.llm.tools import marketplace_tool_schemas


class LlmContractTest(unittest.TestCase):
    def seed_consultation(self, db_file: Path) -> None:
        with db_session(db_file) as conn:
            create_merchant(
                conn,
                merchant_id="seller-a",
                name="West Lake Tea",
                city="Hangzhou",
                service_area="West Lake",
                delivery_eta_minutes=45,
            )
            create_product(
                conn,
                merchant_id="seller-a",
                sku="tea-a",
                title="Longjing Gift Box",
                price=88,
                stock=5,
                tags=["longjing", "gift"],
            )
            conversation = ensure_conversation(conn, "alice", "seller-a", "tea-a")
            append_message(conn, conversation["id"], "buyer", "ask_delivery", "Can this deliver today?")

    def test_marketplace_tool_schemas_are_openai_function_tools(self):
        tools = marketplace_tool_schemas()
        names = [tool["function"]["name"] for tool in tools]

        self.assertEqual(
            names,
            [
                "catalog_search",
                "conversation_send",
                "conversation_summarize",
                "human_review_flag",
                "merchant_reply",
            ],
        )
        self.assertNotIn("create_order", names)
        self.assertNotIn("charge_payment", names)
        for tool in tools:
            self.assertEqual(tool["type"], "function")
            parameters = tool["function"]["parameters"]
            self.assertEqual(parameters["type"], "object")
            self.assertFalse(parameters["additionalProperties"])

    def test_openai_compatible_provider_builds_payload_with_tools(self):
        calls = []

        def fake_transport(url, headers, payload, timeout):
            calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
            return {"choices": [{"message": {"content": "consultation reply"}}]}

        provider = OpenAICompatibleProvider(
            base_url="https://llm.example/v1/",
            api_key="secret-token",
            model="shopping-test-model",
            timeout=12,
            max_tokens=512,
            transport=fake_transport,
        )

        response = provider.complete(
            [
                {"role": "system", "content": "Stay inside MVP consultation boundaries."},
                {"role": "user", "content": "Can this merchant deliver today?"},
            ],
            tools=marketplace_tool_schemas(),
        )

        self.assertEqual(response.content, "consultation reply")
        self.assertEqual(calls[0]["url"], "https://llm.example/v1/chat/completions")
        self.assertEqual(calls[0]["headers"]["authorization"], "Bearer secret-token")
        self.assertEqual(calls[0]["timeout"], 12)
        payload = calls[0]["payload"]
        self.assertEqual(payload["model"], "shopping-test-model")
        self.assertEqual(payload["max_tokens"], 512)
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["messages"][1]["content"], "Can this merchant deliver today?")
        self.assertEqual(payload["tools"][0]["function"]["name"], "catalog_search")
        self.assertNotIn("secret-token", str(payload))

    def test_openai_compatible_provider_tolerates_invalid_numeric_options(self):
        calls = []

        def fake_transport(url, headers, payload, timeout):
            calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
            return {"choices": [{"message": {"content": "consultation reply"}}]}

        provider = OpenAICompatibleProvider(
            base_url="https://llm.example/v1/",
            api_key="secret-token",
            model="shopping-test-model",
            timeout="bad",
            max_tokens="bad",
            transport=fake_transport,
        )

        response = provider.complete([{"role": "user", "content": "Can this merchant deliver today?"}])

        self.assertEqual(response.content, "consultation reply")
        self.assertEqual(calls[0]["timeout"], 30)
        self.assertNotIn("max_tokens", calls[0]["payload"])

    def test_openai_compatible_provider_tolerates_non_finite_numeric_options(self):
        calls = []

        def fake_transport(url, headers, payload, timeout):
            calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
            return {"choices": [{"message": {"content": "consultation reply"}}]}

        try:
            provider = OpenAICompatibleProvider(
                base_url="https://llm.example/v1/",
                api_key="secret-token",
                model="shopping-test-model",
                timeout=float("inf"),
                max_tokens=float("nan"),
                transport=fake_transport,
            )
        except OverflowError as exc:
            self.fail(f"provider should tolerate non-finite numeric options: {exc}")

        response = provider.complete([{"role": "user", "content": "Can this merchant deliver today?"}])

        self.assertEqual(response.content, "consultation reply")
        self.assertEqual(calls[0]["timeout"], 30)
        self.assertNotIn("max_tokens", calls[0]["payload"])

    def test_openai_compatible_provider_caps_oversized_numeric_options(self):
        calls = []

        def fake_transport(url, headers, payload, timeout):
            calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout})
            return {"choices": [{"message": {"content": "consultation reply"}}]}

        provider = OpenAICompatibleProvider(
            base_url="https://llm.example/v1/",
            api_key="secret-token",
            model="shopping-test-model",
            timeout=10**100,
            max_tokens=10**100,
            transport=fake_transport,
        )

        response = provider.complete([{"role": "user", "content": "Can this merchant deliver today?"}])

        self.assertEqual(response.content, "consultation reply")
        self.assertEqual(calls[0]["timeout"], 300)
        self.assertEqual(calls[0]["payload"]["max_tokens"], 32768)

    def test_openai_compatible_provider_reports_invalid_default_transport_json_cleanly(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"\xff"

        provider = OpenAICompatibleProvider(
            base_url="https://llm.example/v1/",
            api_key="secret-token",
            model="shopping-test-model",
        )

        with patch("shopping_cli.llm.providers.urllib.request.urlopen", return_value=FakeResponse()):
            with self.assertRaises(ValueError) as raised:
                provider.complete([{"role": "user", "content": "Can this merchant deliver today?"}])

        self.assertIn("LLM provider returned invalid JSON", str(raised.exception))

    def test_provider_from_env_reads_openai_compatible_settings(self):
        env = {
            "SHOPPING_LLM_BASE_URL": "https://llm.example/custom",
            "SHOPPING_LLM_API_KEY": "env-token",
            "SHOPPING_LLM_MODEL": "env-model",
            "SHOPPING_LLM_TIMEOUT_SECONDS": "9",
            "SHOPPING_LLM_MAX_TOKENS": "2048",
        }
        with patch.dict(os.environ, env, clear=False):
            provider = provider_from_env(transport=lambda *_args: {"choices": [{"message": {"content": "ok"}}]})

        self.assertEqual(provider.base_url, "https://llm.example/custom")
        self.assertEqual(provider.api_key, "env-token")
        self.assertEqual(provider.model, "env-model")
        self.assertEqual(provider.timeout, 9)
        self.assertEqual(provider.max_tokens, 2048)

    def test_llm_tool_loop_dispatches_tool_calls_and_returns_final_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)
            calls = []

            def fake_transport(_url, _headers, payload, _timeout):
                calls.append(payload)
                if len(calls) == 1:
                    return {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "call_catalog",
                                            "type": "function",
                                            "function": {
                                                "name": "catalog_search",
                                                "arguments": "{\"query\":\"longjing\",\"city\":\"Hangzhou\"}",
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                return {"choices": [{"message": {"role": "assistant", "content": "Longjing Gift Box is available."}}]}

            provider = OpenAICompatibleProvider(
                base_url="https://llm.example/v1",
                api_key="secret-token",
                model="shopping-test-model",
                transport=fake_transport,
            )
            dispatcher = MarketplaceToolDispatcher(db_file, source_id="llm-loop", actor="alice", token_scope="buyer")

            result = run_marketplace_tool_loop(
                provider,
                dispatcher,
                [{"role": "user", "content": "Find longjing near Hangzhou."}],
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["content"], "Longjing Gift Box is available.")
            self.assertEqual(result["tool_results"][0]["tool"], "catalog_search")
            self.assertEqual(calls[0]["tools"][0]["function"]["name"], "catalog_search")
            tool_message = calls[1]["messages"][-1]
            self.assertEqual(tool_message["role"], "tool")
            self.assertEqual(tool_message["tool_call_id"], "call_catalog")
            self.assertIn("tea-a", tool_message["content"])

    def test_llm_tool_loop_retries_transient_provider_failures_before_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)
            calls = []

            def flaky_transport(_url, _headers, payload, _timeout):
                calls.append(payload)
                if len(calls) == 1:
                    raise TimeoutError("temporary provider timeout")
                return {"choices": [{"message": {"role": "assistant", "content": "Recovered response."}}]}

            provider = OpenAICompatibleProvider(
                base_url="https://llm.example/v1",
                api_key="secret-token",
                model="shopping-test-model",
                transport=flaky_transport,
            )
            dispatcher = MarketplaceToolDispatcher(db_file, source_id="llm-loop", actor="alice", token_scope="buyer")

            result = run_marketplace_tool_loop(
                provider,
                dispatcher,
                [{"role": "user", "content": "Find longjing near Hangzhou."}],
                provider_retries=1,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["content"], "Recovered response.")
            self.assertEqual(len(calls), 2)

    def test_llm_tool_loop_tolerates_invalid_runtime_numeric_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)

            provider = OpenAICompatibleProvider(
                base_url="https://llm.example/v1",
                api_key="secret-token",
                model="shopping-test-model",
                transport=lambda *_args: {"choices": [{"message": {"role": "assistant", "content": "Recovered response."}}]},
            )
            dispatcher = MarketplaceToolDispatcher(db_file, source_id="llm-loop", actor="alice", token_scope="buyer")

            result = run_marketplace_tool_loop(
                provider,
                dispatcher,
                [{"role": "user", "content": "Find longjing near Hangzhou."}],
                max_steps="bad",
                max_tool_calls="bad",
                provider_retries="bad",
                provider_retry_delay_seconds="bad",
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["content"], "Recovered response.")

    def test_llm_tool_loop_tolerates_non_finite_runtime_integer_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)

            provider = OpenAICompatibleProvider(
                base_url="https://llm.example/v1",
                api_key="secret-token",
                model="shopping-test-model",
                transport=lambda *_args: {"choices": [{"message": {"role": "assistant", "content": "Recovered response."}}]},
            )
            dispatcher = MarketplaceToolDispatcher(db_file, source_id="llm-loop", actor="alice", token_scope="buyer")

            try:
                result = run_marketplace_tool_loop(
                    provider,
                    dispatcher,
                    [{"role": "user", "content": "Find longjing near Hangzhou."}],
                    max_steps=float("inf"),
                    max_tool_calls=float("nan"),
                    provider_retries=float("inf"),
                )
            except OverflowError as exc:
                self.fail(f"non-finite runtime integer options should fall back safely: {exc}")

            self.assertTrue(result["ok"])
            self.assertEqual(result["content"], "Recovered response.")

    def test_llm_tool_loop_caps_oversized_provider_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)
            calls = []

            def flaky_transport(*_args):
                calls.append(True)
                if len(calls) <= 8:
                    raise TimeoutError("temporary provider timeout")
                return {"choices": [{"message": {"role": "assistant", "content": "Recovered response."}}]}

            provider = OpenAICompatibleProvider(
                base_url="https://llm.example/v1",
                api_key="secret-token",
                model="shopping-test-model",
                transport=flaky_transport,
            )
            dispatcher = MarketplaceToolDispatcher(db_file, source_id="llm-loop", actor="alice", token_scope="buyer")

            result = run_marketplace_tool_loop(
                provider,
                dispatcher,
                [{"role": "user", "content": "Find longjing near Hangzhou."}],
                provider_retries=10**100,
            )

            self.assertFalse(result["ok"])
            self.assertIn("TimeoutError", result["error"])
            self.assertLess(len(calls), 9)

    def test_llm_tool_loop_caps_oversized_max_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)
            calls = []

            def looping_transport(*_args):
                calls.append(True)
                if len(calls) <= 20:
                    return {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": f"call-{len(calls)}",
                                            "type": "function",
                                            "function": {
                                                "name": "catalog_search",
                                                "arguments": "{\"query\":\"longjing\"}",
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                return {"choices": [{"message": {"role": "assistant", "content": "Recovered response."}}]}

            provider = OpenAICompatibleProvider(
                base_url="https://llm.example/v1",
                api_key="secret-token",
                model="shopping-test-model",
                transport=looping_transport,
            )
            dispatcher = MarketplaceToolDispatcher(db_file, source_id="llm-loop", actor="alice", token_scope="buyer")

            result = run_marketplace_tool_loop(
                provider,
                dispatcher,
                [{"role": "user", "content": "Find longjing near Hangzhou."}],
                max_steps=10**100,
            )

            self.assertFalse(result["ok"])
            self.assertIn("LLM tool loop exceeded max_steps", result["error"])
            self.assertLess(len(calls), 20)

    def test_llm_tool_loop_tolerates_nan_retry_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)
            calls = []

            def flaky_transport(*_args):
                calls.append(True)
                if len(calls) == 1:
                    raise TimeoutError("temporary provider timeout")
                return {"choices": [{"message": {"role": "assistant", "content": "Recovered response."}}]}

            provider = OpenAICompatibleProvider(
                base_url="https://llm.example/v1",
                api_key="secret-token",
                model="shopping-test-model",
                transport=flaky_transport,
            )
            dispatcher = MarketplaceToolDispatcher(db_file, source_id="llm-loop", actor="alice", token_scope="buyer")

            try:
                result = run_marketplace_tool_loop(
                    provider,
                    dispatcher,
                    [{"role": "user", "content": "Find longjing near Hangzhou."}],
                    provider_retries=1,
                    provider_retry_delay_seconds="nan",
                )
            except ValueError as exc:
                self.fail(f"nan retry delay should be treated as no delay: {exc}")

            self.assertTrue(result["ok"])
            self.assertEqual(result["content"], "Recovered response.")
            self.assertEqual(len(calls), 2)

    def test_llm_tool_loop_tolerates_overflowing_retry_delay(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)
            calls = []

            def flaky_transport(*_args):
                calls.append(True)
                if len(calls) == 1:
                    raise TimeoutError("temporary provider timeout")
                return {"choices": [{"message": {"role": "assistant", "content": "Recovered response."}}]}

            provider = OpenAICompatibleProvider(
                base_url="https://llm.example/v1",
                api_key="secret-token",
                model="shopping-test-model",
                transport=flaky_transport,
            )
            dispatcher = MarketplaceToolDispatcher(db_file, source_id="llm-loop", actor="alice", token_scope="buyer")

            result = run_marketplace_tool_loop(
                provider,
                dispatcher,
                [{"role": "user", "content": "Find longjing near Hangzhou."}],
                provider_retries=1,
                provider_retry_delay_seconds=10**4000,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["content"], "Recovered response.")
            self.assertEqual(len(calls), 2)

    def test_llm_tool_loop_reports_malformed_provider_choices_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)

            provider = OpenAICompatibleProvider(
                base_url="https://llm.example/v1",
                api_key="secret-token",
                model="shopping-test-model",
                transport=lambda *_args: {"choices": "bad"},
            )
            dispatcher = MarketplaceToolDispatcher(db_file, source_id="llm-loop", actor="alice", token_scope="buyer")

            result = run_marketplace_tool_loop(
                provider,
                dispatcher,
                [{"role": "user", "content": "Find longjing near Hangzhou."}],
            )

            self.assertFalse(result["ok"])
            self.assertIn("LLM provider choices must be a list", result["error"])

    def test_llm_tool_loop_stops_before_exceeding_tool_call_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)

            def fake_transport(_url, _headers, _payload, _timeout):
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_catalog_1",
                                        "type": "function",
                                        "function": {
                                            "name": "catalog_search",
                                            "arguments": "{\"query\":\"longjing\",\"city\":\"Hangzhou\"}",
                                        },
                                    },
                                    {
                                        "id": "call_catalog_2",
                                        "type": "function",
                                        "function": {
                                            "name": "catalog_search",
                                            "arguments": "{\"query\":\"gift\",\"city\":\"Hangzhou\"}",
                                        },
                                    },
                                ],
                            }
                        }
                    ]
                }

            provider = OpenAICompatibleProvider(
                base_url="https://llm.example/v1",
                api_key="secret-token",
                model="shopping-test-model",
                transport=fake_transport,
            )
            dispatcher = MarketplaceToolDispatcher(db_file, source_id="llm-loop", actor="alice", token_scope="buyer")

            result = run_marketplace_tool_loop(
                provider,
                dispatcher,
                [{"role": "user", "content": "Find longjing near Hangzhou."}],
                max_tool_calls=1,
            )

            self.assertFalse(result["ok"])
            self.assertIn("tool call budget", result["error"])
            self.assertEqual(len(result["tool_results"]), 1)

    def test_llm_tool_loop_returns_deterministic_fallback_on_tool_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)

            def fake_transport(_url, _headers, _payload, _timeout):
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_bad",
                                        "type": "function",
                                        "function": {"name": "create_order", "arguments": "{}"},
                                    }
                                ],
                            }
                        }
                    ]
                }

            provider = OpenAICompatibleProvider(
                base_url="https://llm.example/v1",
                api_key="secret-token",
                model="shopping-test-model",
                transport=fake_transport,
            )
            dispatcher = MarketplaceToolDispatcher(db_file, source_id="llm-loop", actor="alice", token_scope="buyer")

            result = run_marketplace_tool_loop(provider, dispatcher, [{"role": "user", "content": "Create an order."}])

            self.assertFalse(result["ok"])
            self.assertIn("human should review", result["content"])
            self.assertIn("create_order", result["error"])

    def test_system_prompts_include_mvp_guardrails(self):
        buyer_prompt = buyer_system_prompt()
        merchant_prompt = merchant_system_prompt("Catalog and delivery only.")
        combined = f"{buyer_prompt}\n{merchant_prompt}".lower()

        self.assertIn("consultation only", combined)
        self.assertIn("do not create orders", combined)
        self.assertIn("do not reserve stock", combined)
        self.assertIn("do not charge", combined)
        self.assertIn("refund", combined)
        self.assertIn("human review", combined)
        self.assertIn("pending merchant", combined)
        self.assertIn("not failures", combined)
        self.assertIn("catalog and delivery only", merchant_prompt.lower())

    def test_marketplace_tool_dispatcher_executes_catalog_conversation_and_summary_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)
            dispatcher = MarketplaceToolDispatcher(db_file, source_id="llm-test")

            catalog = dispatcher.dispatch("catalog_search", {"query": "longjing", "city": "Hangzhou"})
            self.assertEqual(catalog["tool"], "catalog_search")
            self.assertEqual(catalog["result"]["results"][0]["sku"], "tea-a")

            sent = dispatcher.dispatch(
                "conversation_send",
                {
                    "conversation_id": "CONV-0001",
                    "sender": "buyer_cli",
                    "intent": "ask_stock",
                    "text": "How many are available?",
                },
            )
            self.assertEqual(sent["result"]["message"]["sender"], "buyer_cli")
            self.assertEqual(sent["result"]["conversation"]["status"], "waiting_merchant")
            self.assertTrue(sent["result"]["pending"])
            self.assertEqual(sent["result"]["next_action"], "Wait for merchant agent response.")
            self.assertEqual(sent["result"]["message"]["structured_payload"]["source_id"], "llm-test")

            summary = dispatcher.dispatch("conversation_summarize", {"conversation_id": "CONV-0001"})
            self.assertEqual(summary["result"]["summary"]["conversation"]["id"], "CONV-0001")
            self.assertTrue(summary["result"]["summary"]["pending"])
            self.assertEqual(summary["result"]["summary"]["next_action"], "Wait for merchant agent response.")
            self.assertTrue(summary["result"]["summary"]["no_order_created"])

    def test_marketplace_tool_dispatcher_handles_human_review_and_merchant_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)

            review = dispatch_marketplace_tool(
                db_file,
                "human_review_flag",
                {"conversation_id": "CONV-0001", "reason": "bargaining", "severity": "review"},
                source_id="llm-merchant",
            )
            self.assertEqual(review["result"]["conversation"]["status"], "human_required")
            self.assertEqual(review["result"]["review"]["reason"], "bargaining")

            reply = dispatch_marketplace_tool(
                db_file,
                "merchant_reply",
                {
                    "conversation_id": "CONV-0001",
                    "intent": "ask_delivery",
                    "text": "A merchant human must confirm this request.",
                    "human_required": True,
                    "reason": "low_stock",
                },
                source_id="llm-merchant",
            )
            self.assertEqual(reply["result"]["message"]["sender"], "merchant_agent")
            self.assertEqual(reply["result"]["conversation"]["status"], "human_required")
            self.assertTrue(any(flag["reason"] == "low_stock" for flag in reply["result"]["conversation"]["flags"]))

    def test_human_review_flag_routes_using_normalized_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)

            review = dispatch_marketplace_tool(
                db_file,
                "human_review_flag",
                {"conversation_id": "CONV-0001", "reason": " suspicious_content ", "severity": " urgent "},
                source_id="llm-merchant",
            )

            self.assertEqual(review["result"]["review"]["reason"], "suspicious_content")
            self.assertEqual(review["result"]["review"]["severity"], "urgent")
            self.assertEqual(review["result"]["conversation"]["next_actor"], "operator")

    def test_merchant_reply_routes_human_review_using_normalized_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)

            reply = dispatch_marketplace_tool(
                db_file,
                "merchant_reply",
                {
                    "conversation_id": "CONV-0001",
                    "intent": "support",
                    "text": "A human operator must review this.",
                    "human_required": True,
                    "reason": " suspicious_content ",
                },
                source_id="llm-merchant",
            )

            self.assertEqual(reply["result"]["message"]["structured_payload"]["reason"], "suspicious_content")
            self.assertEqual(reply["result"]["flags"][0]["reason"], "suspicious_content")
            self.assertEqual(reply["result"]["conversation"]["next_actor"], "operator")

    def test_marketplace_tool_dispatcher_rejects_unknown_or_disallowed_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)
            with self.assertRaises(SystemExit):
                dispatch_marketplace_tool(db_file, "create_order", {"conversation_id": "CONV-0001"})
            with self.assertRaises(SystemExit):
                dispatch_marketplace_tool(
                    db_file,
                    "conversation_send",
                    {
                        "conversation_id": "CONV-0001",
                        "sender": "merchant_agent",
                        "intent": "ask_stock",
                        "text": "Not allowed through buyer send tool.",
                    },
                )

            with db_session(db_file) as conn:
                conversation = conversation_summary(conn, "CONV-0001")
            self.assertEqual([message["sender"] for message in conversation["messages"]], ["buyer"])

    def test_marketplace_tool_dispatcher_enforces_scope_and_audits_tool_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)

            buyer_dispatcher = MarketplaceToolDispatcher(
                db_file,
                source_id="hermes-buyer",
                host="hermes",
                session_id="sess-buyer",
                actor="alice",
                token_scope="buyer",
            )
            sent = buyer_dispatcher.dispatch(
                "conversation_send",
                {
                    "conversation_id": "CONV-0001",
                    "sender": "buyer",
                    "intent": "ask_stock",
                    "text": "Any stock left?",
                },
            )
            self.assertEqual(sent["result"]["message"]["sender"], "buyer")

            with self.assertRaises(SystemExit):
                buyer_dispatcher.dispatch(
                    "merchant_reply",
                    {
                        "conversation_id": "CONV-0001",
                        "intent": "ask_stock",
                        "text": "Buyer scope should not reply as merchant.",
                    },
                )

            merchant_dispatcher = MarketplaceToolDispatcher(
                db_file,
                source_id="openclaw-merchant",
                host="openclaw",
                session_id="sess-merchant",
                actor="seller-a",
                token_scope="merchant_agent",
            )
            reply = merchant_dispatcher.dispatch(
                "merchant_reply",
                {
                    "conversation_id": "CONV-0001",
                    "intent": "ask_stock",
                    "text": "Stock is 5.",
                },
            )
            self.assertEqual(reply["result"]["conversation"]["status"], "waiting_buyer")

            with db_session(db_file) as conn:
                events = conversation_summary(conn, "CONV-0001")["audit_events"]
            tool_events = [event for event in events if event["event"] == "llm_tool_call"]
            self.assertEqual([event["details"]["status"] for event in tool_events], ["ok", "denied", "ok"])
            self.assertEqual(tool_events[0]["details"]["host"], "hermes")
            self.assertEqual(tool_events[0]["details"]["session_id"], "sess-buyer")
            self.assertEqual(tool_events[0]["details"]["actor"], "alice")
            self.assertEqual(tool_events[0]["details"]["token_scope"], "buyer")
            self.assertEqual(tool_events[1]["details"]["tool"], "merchant_reply")
            self.assertIn("not allowed", tool_events[1]["details"]["error"])
            self.assertEqual(tool_events[2]["details"]["host"], "openclaw")
            self.assertEqual(tool_events[2]["details"]["token_scope"], "merchant_agent")

    def test_marketplace_tool_dispatcher_rejects_cross_merchant_conversation_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)
            with db_session(db_file) as conn:
                create_merchant(
                    conn,
                    merchant_id="seller-b",
                    name="River Tea",
                    city="Hangzhou",
                    service_area="West Lake",
                    delivery_eta_minutes=30,
                )

            dispatcher = MarketplaceToolDispatcher(
                db_file,
                source_id="openclaw-merchant-b",
                host="openclaw",
                session_id="sess-merchant-b",
                actor="seller-b",
                token_scope="merchant_agent",
            )

            with self.assertRaises(SystemExit):
                dispatcher.dispatch(
                    "merchant_reply",
                    {
                        "conversation_id": "CONV-0001",
                        "intent": "ask_stock",
                        "text": "seller-b must not reply to seller-a conversations.",
                    },
                )
            with self.assertRaises(SystemExit):
                dispatcher.dispatch(
                    "human_review_flag",
                    {"conversation_id": "CONV-0001", "reason": "cross_merchant", "severity": "review"},
                )
            with self.assertRaises(SystemExit):
                dispatcher.dispatch("conversation_summarize", {"conversation_id": "CONV-0001"})

            with db_session(db_file) as conn:
                conversation = conversation_summary(conn, "CONV-0001")
            self.assertEqual([message["sender"] for message in conversation["messages"]], ["buyer"])
            self.assertEqual(conversation["flags"], [])

    def test_marketplace_tool_dispatcher_rejects_cross_buyer_conversation_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            self.seed_consultation(db_file)
            with db_session(db_file) as conn:
                ensure_conversation(conn, "bob", "seller-a", "tea-a")

            dispatcher = MarketplaceToolDispatcher(
                db_file,
                source_id="hermes-buyer-bob",
                host="hermes",
                session_id="sess-buyer-bob",
                actor="bob",
                token_scope="buyer",
            )

            with self.assertRaises(SystemExit):
                dispatcher.dispatch(
                    "conversation_send",
                    {
                        "conversation_id": "CONV-0001",
                        "sender": "buyer",
                        "intent": "ask_stock",
                        "text": "bob must not write to alice conversations.",
                    },
                )
            with self.assertRaises(SystemExit):
                dispatcher.dispatch("conversation_summarize", {"conversation_id": "CONV-0001"})

            with db_session(db_file) as conn:
                conversation = conversation_summary(conn, "CONV-0001")
            self.assertEqual([message["sender"] for message in conversation["messages"]], ["buyer"])

    def test_http_marketplace_tool_dispatcher_calls_api_with_bearer_token(self):
        calls = []

        def fake_transport(method, path, payload, query, headers):
            calls.append(
                {
                    "method": method,
                    "path": path,
                    "payload": payload,
                    "query": query,
                    "headers": headers,
                }
            )
            if path == "/search/products":
                return {"ok": True, "results": [{"sku": "tea-a", "title": "Longjing Gift Box"}]}
            if path == "/conversations/CONV-0001/messages":
                return {
                    "ok": True,
                    "message": {"id": 2, "sender": "buyer", "structured_payload": {"source_id": "hermes-buyer"}},
                    "conversation": {"id": "CONV-0001", "status": "waiting_merchant"},
                }
            if path == "/audit/tool-calls":
                return {"ok": True, "event": {"event": "llm_tool_call", "details": payload}}
            raise AssertionError(f"unexpected API path: {path}")

        dispatcher = HTTPMarketplaceToolDispatcher(
            "http://127.0.0.1:8765",
            auth_token="buyer-token",
            source_id="hermes-buyer",
            host="hermes",
            session_id="sess-buyer",
            actor="alice",
            token_scope="buyer",
            transport=fake_transport,
        )

        catalog = dispatcher.dispatch("catalog_search", {"query": "longjing", "city": "Hangzhou"})
        sent = dispatcher.dispatch(
            "conversation_send",
            {
                "conversation_id": "CONV-0001",
                "sender": "buyer",
                "intent": "ask_stock",
                "text": "Any stock left?",
            },
        )

        self.assertEqual(catalog["result"]["results"][0]["sku"], "tea-a")
        self.assertEqual(sent["result"]["message"]["sender"], "buyer")
        self.assertEqual(calls[0]["method"], "GET")
        self.assertEqual(calls[0]["path"], "/search/products")
        self.assertEqual(calls[0]["query"]["query"], "longjing")
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer buyer-token")
        self.assertEqual(calls[1]["path"], "/audit/tool-calls")
        self.assertEqual(calls[1]["payload"]["tool"], "catalog_search")
        self.assertEqual(calls[1]["payload"]["status"], "ok")
        self.assertEqual(calls[2]["method"], "POST")
        self.assertEqual(calls[2]["payload"]["source_id"], "hermes-buyer")
        self.assertEqual(calls[2]["headers"]["Authorization"], "Bearer buyer-token")
        self.assertEqual(calls[3]["path"], "/audit/tool-calls")
        self.assertEqual(calls[3]["payload"]["tool"], "conversation_send")
        self.assertEqual(calls[3]["payload"]["conversation_id"], "CONV-0001")
        self.assertEqual(calls[3]["payload"]["status"], "ok")

    def test_http_marketplace_tool_dispatcher_tolerates_invalid_timeout(self):
        dispatcher = HTTPMarketplaceToolDispatcher(
            "http://127.0.0.1:8765",
            auth_token="buyer-token",
            actor="alice",
            token_scope="buyer",
            timeout="bad",
            transport=lambda _method, _path, _payload, _query, _headers: {"ok": True},
        )

        self.assertEqual(dispatcher.timeout, 10.0)

    def test_http_marketplace_tool_dispatcher_caps_oversized_timeout(self):
        dispatcher = HTTPMarketplaceToolDispatcher(
            "http://127.0.0.1:8765",
            auth_token="buyer-token",
            actor="alice",
            token_scope="buyer",
            timeout=10**100,
            transport=lambda _method, _path, _payload, _query, _headers: {"ok": True},
        )

        self.assertEqual(dispatcher.timeout, 60.0)

    def test_http_marketplace_tool_dispatcher_tolerates_overflowing_timeout(self):
        dispatcher = HTTPMarketplaceToolDispatcher(
            "http://127.0.0.1:8765",
            auth_token="buyer-token",
            actor="alice",
            token_scope="buyer",
            timeout=10**4000,
            transport=lambda _method, _path, _payload, _query, _headers: {"ok": True},
        )

        self.assertEqual(dispatcher.timeout, 10.0)

    def test_http_marketplace_tool_dispatcher_wraps_timeout_errors(self):
        dispatcher = HTTPMarketplaceToolDispatcher(
            "http://127.0.0.1:8765",
            auth_token="buyer-token",
            actor="alice",
            token_scope="buyer",
        )

        with patch("shopping_cli.llm.dispatcher.urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(SystemExit) as raised:
                dispatcher.dispatch("conversation_summarize", {"conversation_id": "CONV-0001"})

        self.assertIn("Marketplace API request timed out", str(raised.exception))
        self.assertIn("timed out", str(raised.exception))

    def test_http_marketplace_tool_dispatcher_reports_malformed_conversation_response_cleanly(self):
        dispatcher = HTTPMarketplaceToolDispatcher(
            "http://127.0.0.1:8765",
            auth_token="buyer-token",
            actor="alice",
            token_scope="buyer",
            transport=lambda _method, _path, _payload, _query, _headers: {"ok": True},
        )

        with self.assertRaises(HTTPMarketplaceError) as raised:
            dispatcher.dispatch("conversation_summarize", {"conversation_id": "CONV-0001"})
        self.assertIn("Marketplace API response missing object: conversation", str(raised.exception))

    def test_http_marketplace_tool_dispatcher_reports_malformed_message_response_cleanly(self):
        def fake_transport(_method, path, _payload, _query, _headers):
            if path == "/audit/tool-calls":
                return {"ok": True, "event": {"event": "llm_tool_call"}}
            return {"ok": True, "conversation": {"id": "CONV-0001"}}

        dispatcher = HTTPMarketplaceToolDispatcher(
            "http://127.0.0.1:8765",
            auth_token="merchant-token",
            actor="shopping-cli-merchant-agent:seller-a",
            token_scope="merchant_agent",
            transport=fake_transport,
        )

        with self.assertRaises(HTTPMarketplaceError) as raised:
            dispatcher.dispatch(
                "merchant_reply",
                {"conversation_id": "CONV-0001", "intent": "answer", "text": "Available."},
            )
        self.assertIn("Marketplace API response missing object: message", str(raised.exception))

    def test_http_marketplace_tool_dispatcher_reuses_message_created_review_flag(self):
        calls = []

        def fake_transport(method, path, payload, query, headers):
            calls.append({"method": method, "path": path, "payload": payload, "query": query, "headers": headers})
            if path == "/conversations/CONV-0001/messages":
                return {
                    "ok": True,
                    "message": {"id": 2, "sender": "merchant_agent", "structured_payload": payload["structured_payload"]},
                    "conversation": {
                        "id": "CONV-0001",
                        "status": "human_required",
                        "flags": ["malformed", {"id": 1, "reason": "low_confidence", "resolved_at": None}],
                    },
                }
            if path == "/conversations/CONV-0001/human-review":
                return {
                    "ok": True,
                    "review": {"id": 2, "reason": "low_confidence", "resolved_at": None},
                    "conversation": {"id": "CONV-0001", "status": "human_required", "flags": []},
                }
            if path == "/audit/tool-calls":
                return {"ok": True, "event": {"event": "llm_tool_call"}}
            raise AssertionError(f"unexpected API path: {path}")

        dispatcher = HTTPMarketplaceToolDispatcher(
            "http://127.0.0.1:8765",
            auth_token="merchant-token",
            actor="shopping-cli-merchant-agent:seller-a",
            token_scope="merchant_agent",
            transport=fake_transport,
        )

        result = dispatcher.dispatch(
            "merchant_reply",
            {
                "conversation_id": "CONV-0001",
                "intent": "support",
                "text": "Needs human review.",
                "human_required": True,
                "reason": "low_confidence",
            },
        )

        self.assertEqual([flag["id"] for flag in result["result"]["flags"]], [1])
        self.assertNotIn("/conversations/CONV-0001/human-review", [call["path"] for call in calls])

    def test_http_marketplace_tool_dispatcher_preserves_denial_when_audit_fails(self):
        dispatcher = HTTPMarketplaceToolDispatcher(
            "http://127.0.0.1:8765",
            auth_token="buyer-token",
            actor="alice",
            token_scope="buyer",
            transport=lambda _method, _path, _payload, _query, _headers: {
                "ok": False,
                "error": "audit unavailable",
            },
        )

        with self.assertRaises(SystemExit) as raised:
            dispatcher.dispatch(
                "merchant_reply",
                {"conversation_id": "CONV-0001", "intent": "support", "text": "Not allowed."},
            )

        self.assertIn("not allowed", str(raised.exception))

    def test_http_marketplace_tool_dispatcher_enforces_scope_before_api_call(self):
        calls = []
        dispatcher = HTTPMarketplaceToolDispatcher(
            "http://127.0.0.1:8765",
            auth_token="buyer-token",
            actor="alice",
            token_scope="buyer",
            transport=lambda method, path, payload, query, headers: calls.append(
                {"method": method, "path": path, "payload": payload, "query": query, "headers": headers}
            )
            or {"ok": True, "event": {"event": "llm_tool_call", "details": payload}},
        )

        with self.assertRaises(SystemExit):
            dispatcher.dispatch(
                "merchant_reply",
                {"conversation_id": "CONV-0001", "intent": "support", "text": "Not allowed."},
            )
        with self.assertRaises(SystemExit):
            dispatcher.dispatch("create_order", {})
        self.assertEqual([call["path"] for call in calls], ["/audit/tool-calls", "/audit/tool-calls"])
        self.assertEqual([call["payload"]["tool"] for call in calls], ["merchant_reply", "create_order"])
        self.assertEqual([call["payload"]["status"] for call in calls], ["denied", "denied"])


if __name__ == "__main__":
    unittest.main()

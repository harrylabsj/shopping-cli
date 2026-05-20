import json
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import shopping  # noqa: E402


class ShoppingCliMvpTest(unittest.TestCase):
    def run_cli(self, db_file, *args):
        output = StringIO()
        with redirect_stdout(output):
            shopping.main(["--db", str(db_file), *args])
        return output.getvalue()

    def run_cli_with_input(self, db_file, input_text, *args):
        output = StringIO()
        with patch("sys.stdin", StringIO(input_text)), redirect_stdout(output):
            shopping.main(["--db", str(db_file), *args])
        return output.getvalue()

    def read_rows(self, db_file, table):
        conn = sqlite3.connect(db_file)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(f"select * from {table}")]
        finally:
            conn.close()

    def seed_longjing_shop(self, db_file):
        self.run_cli(
            db_file,
            "merchant",
            "create",
            "--id",
            "seller-a",
            "--name",
            "West Lake Tea",
            "--city",
            "Hangzhou",
            "--service-area",
            "西湖附近",
            "--contact",
            "wechat:westlake",
            "--hours",
            "09:00-21:00",
            "--delivery-fee",
            "12",
            "--delivery-eta-minutes",
            "45",
            "--automation-boundaries",
            "Agent may answer catalog, stock, price, delivery, and substitution questions.",
            "--tags",
            "tea,gift,龙井",
        )
        self.run_cli(
            db_file,
            "product",
            "add",
            "--merchant",
            "seller-a",
            "--sku",
            "tea-a",
            "--title",
            "西湖龙井礼盒",
            "--price",
            "88",
            "--stock",
            "5",
            "--category",
            "tea",
            "--tags",
            "longjing,gift,龙井,礼盒",
            "--delivery-attributes",
            "same-city,courier",
        )

    def test_longjing_consultation_records_intent_without_order_or_payment_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping-cli.sqlite"
            self.seed_longjing_shop(db_file)

            ask = json.loads(
                self.run_cli(
                    db_file,
                    "buyer",
                    "ask",
                    "--buyer",
                    "alice",
                    "--text",
                    "我在西湖附近，今天想买两盒龙井礼盒，能送吗？",
                    "--city",
                    "Hangzhou",
                    "--area",
                    "西湖附近",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(ask["conversation"]["id"], "CONV-0001")
            self.assertEqual(ask["conversation"]["status"], "waiting_merchant")
            self.assertTrue(ask["pending"])
            self.assertEqual(ask["next_action"], "Wait for merchant agent response.")
            self.assertEqual(ask["candidates"][0]["sku"], "tea-a")
            self.assertIn("no order", " ".join(ask["warnings"]).lower())

            agent = json.loads(
                self.run_cli(
                    db_file,
                    "agent",
                    "run",
                    "--merchant",
                    "seller-a",
                    "--once",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(agent["replied"][0]["conversation_id"], "CONV-0001")
            self.assertFalse(agent["replied"][0]["human_required"])

            summary = json.loads(
                self.run_cli(
                    db_file,
                    "buyer",
                    "summarize",
                    "--conversation",
                    "CONV-0001",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(summary["option"]["sku"], "tea-a")
            self.assertEqual(summary["option"]["stock"], 5)
            self.assertEqual(summary["option"]["delivery"]["eta_minutes"], 45)
            self.assertTrue(summary["no_order_created"])
            self.assertTrue(summary["no_stock_reserved"])
            self.assertIn("payment", " ".join(summary["warnings"]).lower())

            intent = json.loads(
                self.run_cli(
                    db_file,
                    "buyer",
                    "intent",
                    "--conversation",
                    "CONV-0001",
                    "--intent",
                    "purchase_intent",
                    "--text",
                    "我想继续购买，请商家人工确认。",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(intent["message"]["intent"], "purchase_intent")
            self.assertEqual(intent["conversation"]["status"], "waiting_merchant")
            self.assertTrue(intent["pending"])
            self.assertEqual(intent["next_action"], "Wait for merchant agent response.")

            messages = self.read_rows(db_file, "messages")
            self.assertEqual([row["intent"] for row in messages], ["ask_delivery", "ask_delivery", "purchase_intent"])
            payloads = [json.loads(row["structured_payload_json"]) for row in messages]
            self.assertEqual(payloads[0]["source_id"], "buyer-cli")
            self.assertEqual(payloads[1]["source_id"], "shopping-cli-merchant-agent:seller-a")
            self.assertEqual(payloads[2]["source_id"], "buyer-cli")
            conn = sqlite3.connect(db_file)
            try:
                tables = {
                    row[0]
                    for row in conn.execute("select name from sqlite_master where type = 'table'")
                }
            finally:
                conn.close()
            self.assertNotIn("orders", tables)
            self.assertNotIn("payments", tables)

    def test_channel_ingest_opens_and_continues_buyer_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping-cli.sqlite"
            self.seed_longjing_shop(db_file)

            opened = json.loads(
                self.run_cli(
                    db_file,
                    "channel",
                    "ingest",
                    "--channel",
                    "whatsapp",
                    "--external-user",
                    "+15550001111",
                    "--external-message-id",
                    "wa-msg-1",
                    "--text",
                    "今天想买龙井礼盒，西湖附近能送吗？",
                    "--city",
                    "Hangzhou",
                    "--area",
                    "西湖附近",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(opened["buyer_id"], "whatsapp:+15550001111")
            self.assertEqual(opened["conversation"]["id"], "CONV-0001")
            self.assertEqual(opened["conversation"]["status"], "waiting_merchant")
            self.assertEqual(opened["message"]["structured_payload"]["source_id"], "channel:whatsapp")
            self.assertEqual(opened["message"]["structured_payload"]["channel"], "whatsapp")
            self.assertEqual(opened["message"]["structured_payload"]["external_user_id"], "+15550001111")

            retried_open = json.loads(
                self.run_cli(
                    db_file,
                    "channel",
                    "ingest",
                    "--channel",
                    "whatsapp",
                    "--external-user",
                    "+15550001111",
                    "--external-message-id",
                    "wa-msg-1",
                    "--text",
                    "今天想买龙井礼盒，西湖附近能送吗？",
                    "--city",
                    "Hangzhou",
                    "--area",
                    "西湖附近",
                    "--format",
                    "json",
                )
            )
            self.assertTrue(retried_open["idempotent"])
            self.assertEqual(retried_open["message"]["id"], opened["message"]["id"])
            self.assertEqual(len(retried_open["conversation"]["messages"]), 1)

            continued = json.loads(
                self.run_cli(
                    db_file,
                    "channel",
                    "ingest",
                    "--channel",
                    "whatsapp",
                    "--external-user",
                    "+15550001111",
                    "--conversation",
                    "CONV-0001",
                    "--text",
                    "再确认一下库存。",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(continued["conversation"]["id"], "CONV-0001")
            self.assertEqual([message["sender"] for message in continued["conversation"]["messages"]], ["buyer", "buyer"])
            self.assertEqual(
                continued["conversation"]["messages"][1]["structured_payload"]["source_id"],
                "channel:whatsapp",
            )

            delivered = json.loads(
                self.run_cli(
                    db_file,
                    "channel",
                    "ingest",
                    "--channel",
                    "whatsapp",
                    "--external-user",
                    "+15550001111",
                    "--conversation",
                    "CONV-0001",
                    "--external-message-id",
                    "wa-msg-2",
                    "--text",
                    "今天能送到吗？",
                    "--format",
                    "json",
                )
            )
            retried = json.loads(
                self.run_cli(
                    db_file,
                    "channel",
                    "ingest",
                    "--channel",
                    "whatsapp",
                    "--external-user",
                    "+15550001111",
                    "--conversation",
                    "CONV-0001",
                    "--external-message-id",
                    "wa-msg-2",
                    "--text",
                    "今天能送到吗？",
                    "--format",
                    "json",
                )
            )
            self.assertTrue(retried["idempotent"])
            self.assertEqual(retried["message"]["id"], delivered["message"]["id"])
            self.assertEqual(len(retried["conversation"]["messages"]), 3)

    def test_channel_ingest_normalizes_channel_names_for_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping-cli.sqlite"
            self.seed_longjing_shop(db_file)

            opened = json.loads(
                self.run_cli(
                    db_file,
                    "channel",
                    "ingest",
                    "--channel",
                    " Telegram ",
                    "--external-user",
                    "@alice",
                    "--external-message-id",
                    "tg-msg-1",
                    "--text",
                    "longjing gift delivery today",
                    "--city",
                    "Hangzhou",
                    "--format",
                    "json",
                )
            )
            retried = json.loads(
                self.run_cli(
                    db_file,
                    "channel",
                    "ingest",
                    "--channel",
                    "telegram",
                    "--external-user",
                    "@alice",
                    "--external-message-id",
                    "tg-msg-1",
                    "--text",
                    "longjing gift delivery today",
                    "--city",
                    "Hangzhou",
                    "--format",
                    "json",
                )
            )

            self.assertEqual(opened["buyer_id"], "telegram:@alice")
            self.assertEqual(opened["message"]["structured_payload"]["source_id"], "channel:telegram")
            self.assertTrue(retried["idempotent"])
            self.assertEqual(retried["message"]["id"], opened["message"]["id"])
            self.assertEqual(len(retried["conversation"]["messages"]), 1)

    def test_channel_ingest_retries_no_match_without_creating_later_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping-cli.sqlite"

            first = json.loads(
                self.run_cli(
                    db_file,
                    "channel",
                    "ingest",
                    "--channel",
                    "telegram",
                    "--external-user",
                    "@alice",
                    "--external-message-id",
                    "tg-no-match",
                    "--text",
                    "longjing gift delivery today",
                    "--city",
                    "Hangzhou",
                    "--format",
                    "json",
                )
            )
            self.seed_longjing_shop(db_file)
            retried = json.loads(
                self.run_cli(
                    db_file,
                    "channel",
                    "ingest",
                    "--channel",
                    "telegram",
                    "--external-user",
                    "@alice",
                    "--external-message-id",
                    "tg-no-match",
                    "--text",
                    "longjing gift delivery today",
                    "--city",
                    "Hangzhou",
                    "--format",
                    "json",
                )
            )

            self.assertFalse(first["idempotent"])
            self.assertEqual(first["candidates"], [])
            self.assertIsNone(first["conversation"])
            self.assertTrue(retried["idempotent"])
            self.assertEqual(retried["candidates"], [])
            self.assertIsNone(retried["conversation"])

    def test_bargaining_is_marked_for_human_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping-cli.sqlite"
            self.seed_longjing_shop(db_file)

            self.run_cli(
                db_file,
                "buyer",
                "ask",
                "--buyer",
                "alice",
                "--text",
                "龙井礼盒两盒能便宜一点吗？可以私下优惠吗？",
                "--city",
                "Hangzhou",
            )
            agent = json.loads(
                self.run_cli(
                    db_file,
                    "agent",
                    "run",
                    "--merchant",
                    "seller-a",
                    "--once",
                    "--format",
                    "json",
                )
            )
            self.assertTrue(agent["replied"][0]["human_required"])

            review = json.loads(
                self.run_cli(
                    db_file,
                    "merchant",
                    "human-review",
                    "--merchant",
                    "seller-a",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(review["conversations"][0]["status"], "human_required")
            self.assertEqual(review["conversations"][0]["flags"][0]["reason"], "bargaining")

    def test_low_stock_and_unclear_delivery_require_human_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping-cli.sqlite"
            self.run_cli(db_file, "merchant", "create", "--id", "seller-low", "--name", "Low Stock Tea")
            self.run_cli(
                db_file,
                "product",
                "add",
                "--merchant",
                "seller-low",
                "--sku",
                "tea-low",
                "--title",
                "Rare Longjing",
                "--price",
                "188",
                "--stock",
                "1",
                "--tags",
                "longjing",
            )
            self.run_cli(
                db_file,
                "buyer",
                "ask",
                "--buyer",
                "alice",
                "--text",
                "rare longjing stock and delivery today",
                "--format",
                "json",
            )

            agent = json.loads(
                self.run_cli(
                    db_file,
                    "agent",
                    "run",
                    "--merchant",
                    "seller-low",
                    "--once",
                    "--format",
                    "json",
                )
            )
            self.assertTrue(agent["replied"][0]["human_required"])
            self.assertEqual(agent["replied"][0]["reason"], "low_stock")

            review = json.loads(
                self.run_cli(
                    db_file,
                    "merchant",
                    "human-review",
                    "--merchant",
                    "seller-low",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(review["conversations"][0]["flags"][0]["reason"], "low_stock")

            self.run_cli(db_file, "merchant", "create", "--id", "seller-delivery", "--name", "Delivery Unknown Tea")
            self.run_cli(
                db_file,
                "product",
                "add",
                "--merchant",
                "seller-delivery",
                "--sku",
                "tea-delivery",
                "--title",
                "Delivery Longjing",
                "--price",
                "88",
                "--stock",
                "5",
                "--tags",
                "delivery-longjing",
            )
            self.run_cli(
                db_file,
                "buyer",
                "ask",
                "--buyer",
                "bob",
                "--text",
                "delivery-longjing delivery today",
                "--format",
                "json",
            )

            delivery_agent = json.loads(
                self.run_cli(
                    db_file,
                    "agent",
                    "run",
                    "--merchant",
                    "seller-delivery",
                    "--once",
                    "--format",
                    "json",
                )
            )
            self.assertTrue(delivery_agent["replied"][0]["human_required"])
            self.assertEqual(delivery_agent["replied"][0]["reason"], "unclear_delivery")

    def test_suspicious_content_requires_human_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping-cli.sqlite"
            self.seed_longjing_shop(db_file)
            self.run_cli(
                db_file,
                "buyer",
                "ask",
                "--buyer",
                "alice",
                "--text",
                "longjing gift counterfeit fake id request",
                "--city",
                "Hangzhou",
            )
            agent = json.loads(
                self.run_cli(
                    db_file,
                    "agent",
                    "run",
                    "--merchant",
                    "seller-a",
                    "--once",
                    "--format",
                    "json",
                )
            )
            self.assertTrue(agent["replied"][0]["human_required"])
            self.assertEqual(agent["replied"][0]["reason"], "suspicious_content")

    def test_low_confidence_language_requires_human_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping-cli.sqlite"
            self.seed_longjing_shop(db_file)
            self.run_cli(
                db_file,
                "buyer",
                "ask",
                "--buyer",
                "alice",
                "--text",
                "maybe longjing gift not sure what I need",
                "--city",
                "Hangzhou",
            )
            agent = json.loads(
                self.run_cli(
                    db_file,
                    "agent",
                    "run",
                    "--merchant",
                    "seller-a",
                    "--once",
                    "--format",
                    "json",
                )
            )
            self.assertTrue(agent["replied"][0]["human_required"])
            self.assertEqual(agent["replied"][0]["reason"], "low_confidence")

    def test_conversation_cli_covers_message_close_and_human_review_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping-cli.sqlite"
            self.seed_longjing_shop(db_file)

            created = json.loads(
                self.run_cli(
                    db_file,
                    "conversation",
                    "create",
                    "--buyer",
                    "alice",
                    "--merchant",
                    "seller-a",
                    "--sku",
                    "tea-a",
                    "--intent",
                    "ask_stock",
                    "--text",
                    "Is tea-a available?",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(created["conversation"]["status"], "waiting_merchant")

            replied = json.loads(
                self.run_cli(
                    db_file,
                    "conversation",
                    "message",
                    "--conversation",
                    "CONV-0001",
                    "--sender",
                    "merchant_agent",
                    "--intent",
                    "ask_stock",
                    "--text",
                    "Stock is 5.",
                    "--status",
                    "waiting_buyer",
                    "--source-id",
                    "manual-agent",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(replied["message"]["structured_payload"]["source_id"], "manual-agent")

            review = json.loads(
                self.run_cli(
                    db_file,
                    "conversation",
                    "human-review",
                    "--conversation",
                    "CONV-0001",
                    "--reason",
                    "low_confidence",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(review["conversation"]["status"], "human_required")

            resolved = json.loads(
                self.run_cli(
                    db_file,
                    "conversation",
                    "resolve-review",
                    "--conversation",
                    "CONV-0001",
                    "--action",
                    "reply",
                    "--sender",
                    "merchant",
                    "--text",
                    "Human reviewed.",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(resolved["conversation"]["status"], "waiting_buyer")
            self.assertIsNotNone(resolved["reviews"][0]["resolved_at"])

            closed = json.loads(
                self.run_cli(
                    db_file,
                    "conversation",
                    "close",
                    "--conversation",
                    "CONV-0001",
                    "--sender",
                    "operator",
                    "--text",
                    "Closed.",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(closed["conversation"]["status"], "closed")

    def test_cli_exposes_agent_lists_and_global_human_review_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping-cli.sqlite"
            self.seed_longjing_shop(db_file)
            created = json.loads(
                self.run_cli(
                    db_file,
                    "conversation",
                    "create",
                    "--buyer",
                    "alice",
                    "--merchant",
                    "seller-a",
                    "--sku",
                    "tea-a",
                    "--intent",
                    "ask_stock",
                    "--text",
                    "Can I get a private discount?",
                    "--format",
                    "json",
                )
            )
            updated_at = created["conversation"]["updated_at"]
            self.run_cli(db_file, "agent", "heartbeat", "--merchant", "seller-a")

            agents = json.loads(self.run_cli(db_file, "agent", "list", "--format", "json"))
            self.assertEqual(agents["agents"][0]["owner_id"], "seller-a")
            agent = json.loads(
                self.run_cli(
                    db_file,
                    "agent",
                    "show",
                    "--agent",
                    "shopping-cli-merchant-agent:seller-a",
                    "--format",
                    "json",
                )
            )
            self.assertEqual(agent["agent"]["id"], "shopping-cli-merchant-agent:seller-a")

            filtered = json.loads(
                self.run_cli(
                    db_file,
                    "conversation",
                    "list",
                    "--buyer",
                    "alice",
                    "--updated-since",
                    updated_at,
                    "--format",
                    "json",
                )
            )
            self.assertEqual(filtered["conversations"][0]["id"], "CONV-0001")

            self.run_cli(
                db_file,
                "conversation",
                "human-review",
                "--conversation",
                "CONV-0001",
                "--reason",
                "low_confidence",
            )
            queue = json.loads(self.run_cli(db_file, "human-review", "queue", "--format", "json"))
            self.assertEqual(queue["reviews"][0]["conversation_id"], "CONV-0001")
            self.assertEqual(queue["reviews"][0]["merchant_id"], "seller-a")

    def test_legacy_import_ignores_orders_and_imports_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping-cli.sqlite"
            legacy_file = Path(tmp) / "shopping.json"
            legacy_file.write_text(
                json.dumps(
                    {
                        "merchants": {
                            "seller-a": {
                                "id": "seller-a",
                                "name": "West Lake Tea",
                                "city": "Hangzhou",
                                "contact": "wechat:westlake",
                                "tags": ["tea"],
                            }
                        },
                        "products": {
                            "tea-a": {
                                "sku": "tea-a",
                                "merchant_id": "seller-a",
                                "title": "Longjing Gift Box",
                                "price": 88,
                                "currency": "CNY",
                                "stock": 5,
                                "category": "tea",
                                "tags": ["longjing", "gift"],
                                "shipping": "same-city courier",
                            }
                        },
                        "orders": {"ORD-0001": {"status": "draft"}},
                        "payments": {"PAY-0001": {"status": "held_by_psp"}},
                    }
                ),
                encoding="utf-8",
            )

            imported = json.loads(
                self.run_cli(
                    db_file,
                    "legacy",
                    "import",
                    "--from-json",
                    str(legacy_file),
                    "--format",
                    "json",
                )
            )
            self.assertEqual(imported["imported"], {"merchants": 1, "products": 1})

            search = json.loads(
                self.run_cli(db_file, "search", "products", "--query", "longjing", "--format", "json")
            )
            self.assertEqual(search["results"][0]["sku"], "tea-a")
            conn = sqlite3.connect(db_file)
            try:
                tables = {
                    row[0]
                    for row in conn.execute("select name from sqlite_master where type = 'table'")
                }
            finally:
                conn.close()
            self.assertNotIn("orders", tables)
            self.assertNotIn("payments", tables)

    def test_buyer_chat_repl_opens_conversation_and_records_followups(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping-cli.sqlite"
            self.seed_longjing_shop(db_file)

            output = self.run_cli_with_input(
                db_file,
                "我在西湖附近，想买龙井礼盒，今天能送吗？\n还有库存吗？\n/intent purchase_intent 我想继续购买\n/summary\n/quit\n",
                "buyer",
                "chat",
                "--buyer",
                "alice",
                "--city",
                "Hangzhou",
                "--area",
                "西湖附近",
                "--format",
                "json",
            )

            events = [json.loads(line) for line in output.splitlines() if line.strip()]
            self.assertEqual([event["event"] for event in events], ["ask", "message", "intent", "summary", "quit"])
            self.assertEqual(events[0]["conversation"]["id"], "CONV-0001")
            self.assertEqual(events[1]["message"]["intent"], "ask_stock")
            self.assertEqual(events[2]["message"]["intent"], "purchase_intent")
            self.assertTrue(events[3]["summary"]["no_order_created"])

            messages = self.read_rows(db_file, "messages")
            payloads = [json.loads(row["structured_payload_json"]) for row in messages]
            self.assertEqual([row["intent"] for row in messages], ["ask_delivery", "ask_stock", "purchase_intent"])
            self.assertEqual(payloads[1]["source_id"], "buyer-chat")
            self.assertEqual(payloads[2]["source_id"], "buyer-chat")

    def test_buyer_chat_history_exports_multiturn_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping-cli.sqlite"
            self.seed_longjing_shop(db_file)

            output = self.run_cli_with_input(
                db_file,
                "我想买龙井礼盒，今天能送吗？\n还有库存吗？\n/history\n/summary\n/quit\n",
                "buyer",
                "chat",
                "--buyer",
                "alice",
                "--city",
                "Hangzhou",
                "--format",
                "json",
            )

            events = [json.loads(line) for line in output.splitlines() if line.strip()]
            self.assertEqual([event["event"] for event in events], ["ask", "message", "history", "summary", "quit"])
            history = events[2]
            self.assertEqual(history["conversation"]["id"], "CONV-0001")
            self.assertEqual([message["sender"] for message in history["messages"]], ["buyer", "buyer"])
            self.assertEqual([message["intent"] for message in history["messages"]], ["ask_delivery", "ask_stock"])
            self.assertEqual(events[3]["summary"]["conversation"]["messages"], history["messages"])

    def test_buyer_chat_summary_and_history_cover_human_review_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping-cli.sqlite"
            self.seed_longjing_shop(db_file)

            output = self.run_cli_with_input(
                db_file,
                "龙井礼盒两盒能便宜一点吗？可以私下优惠吗？\n/quit\n",
                "buyer",
                "chat",
                "--buyer",
                "alice",
                "--city",
                "Hangzhou",
                "--format",
                "json",
            )
            events = [json.loads(line) for line in output.splitlines() if line.strip()]
            conversation_id = events[0]["conversation"]["id"]
            agent = json.loads(
                self.run_cli(
                    db_file,
                    "agent",
                    "run",
                    "--merchant",
                    "seller-a",
                    "--once",
                    "--format",
                    "json",
                )
            )
            self.assertTrue(agent["replied"][0]["human_required"])

            output = self.run_cli_with_input(
                db_file,
                "/summary\n/history\n/quit\n",
                "buyer",
                "chat",
                "--buyer",
                "alice",
                "--conversation",
                conversation_id,
                "--format",
                "json",
            )
            events = [json.loads(line) for line in output.splitlines() if line.strip()]
            self.assertEqual([event["event"] for event in events], ["summary", "history", "quit"])
            self.assertEqual(events[0]["summary"]["conversation"]["status"], "human_required")
            self.assertEqual(events[0]["summary"]["next_action"], "Wait for merchant human review.")
            self.assertTrue(any("Human review flag: bargaining" == warning for warning in events[0]["summary"]["warnings"]))
            self.assertEqual(events[1]["conversation"]["status"], "human_required")
            self.assertEqual([message["sender"] for message in events[1]["messages"]], ["buyer", "merchant_agent"])

    def test_mvp_cli_does_not_expose_order_or_payment_commands(self):
        help_text = self.run_cli(Path(tempfile.gettempdir()) / "unused-shopping-cli.sqlite", "--help")
        self.assertNotIn(" order ", help_text)
        self.assertNotIn("payment", help_text.lower())
        errors = StringIO()
        with self.assertRaises(SystemExit):
            with redirect_stderr(errors):
                shopping.main(["--db", str(Path(tempfile.gettempdir()) / "unused-shopping-cli.sqlite"), "order", "create"])

    def test_nested_help_shows_subcommand_options(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "shopping.py"), "buyer", "chat", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("Run a lightweight buyer chat REPL", result.stdout)
        self.assertIn("--conversation", result.stdout)
        self.assertNotIn("{merchant,delivery,product,buyer,agent,db}", result.stdout)


if __name__ == "__main__":
    unittest.main()

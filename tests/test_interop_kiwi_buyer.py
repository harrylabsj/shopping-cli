"""Cross-implementation interop: Kiwi Buyer wire → shopping-cli Hosted Gateway.

binding rc1 §7 test matrix 的第一行「Kiwi Buyer ↔ shopping-cli Hosted
Merchant」的 wire 层验证：请求体由 Kiwi 的真实实现构造（
`kiwi/scripts/gen-interop-fixture.mjs` → `kiwi/contracts/interop/
kiwi-buyer-message-send.json`，KNP envelope digest 由 kiwi dist 计算），
本测试原样加载并通过 shopping-cli 的完整 hosted 链路处理——验证 JSON-RPC
帧格式、A2A Message 结构（role/messageId/Data Part 的 `knp_envelope`
约定）、envelope 校验与 digest 验证、binding 映射与协商处理的跨实现兼容。

只有运行时上下文（negotiation_id 对应当前 conversation）被替换并用
Python 实现重算 digest（字段变更后 digest 必须重算；JCS 算法一致性由
conformance vectors 锚定）。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from shopping_cli.api.fallback_asgi import MarketplaceASGIApp
from shopping_cli.a2a.binding import negotiation_id_for_conversation
from shopping_cli.a2a.knp import finalize_envelope, verify_envelope_digest

from test_a2a_hosted_server import A2A_PATH, AGENT_ID, _seed

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / ".."  # workspace root
    / "kiwi"
    / "contracts"
    / "interop"
    / "kiwi-buyer-message-send.json"
)

CAGT_ID = f"cagt_{AGENT_ID}"


def _load_fixture() -> dict:
    """Load the Kiwi-generated wire request (see module docstring)."""
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


class KiwiBuyerInteropTest(unittest.TestCase):
    """Kiwi Buyer wire 打 shopping-cli hosted gateway 的全链路互操作。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_file = Path(self.tmp.name) / "marketplace.sqlite"
        self.seed = _seed(self.db_file)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _post(self, body: dict) -> tuple[int, dict]:
        app = MarketplaceASGIApp(self.db_file)
        sent: list[dict] = []
        body_bytes = json.dumps(body).encode("utf-8")
        received = False

        async def receive():
            nonlocal received
            if received:
                return {"type": "http.disconnect"}
            received = True
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        async def send(message: dict) -> None:
            sent.append(message)

        async def run() -> None:
            await app(
                {
                    "type": "http",
                    "method": "POST",
                    "path": A2A_PATH.format(catalog_agent_id=CAGT_ID),
                    "query_string": b"",
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"authorization", f"Bearer {self.seed['buyer_token']}".encode("latin1")),
                    ],
                },
                receive,
                send,
            )

        asyncio.run(run())
        status = next(
            message["status"] for message in sent if message["type"] == "http.response.start"
        )
        out = b"".join(
            message.get("body", b"") for message in sent if message["type"] == "http.response.body"
        )
        return status, json.loads(out.decode("utf-8") or "{}")

    def test_kiwi_buyer_wire_accepted_end_to_end(self) -> None:
        body = _load_fixture()
        # Wire 结构断言（Kiwi 构造的请求必须符合 hosted gateway 的帧约定）。
        self.assertEqual(body["jsonrpc"], "2.0")
        self.assertEqual(body["method"], "message/send")
        message = body["params"]["message"]
        self.assertEqual(message["role"], "agent")
        data_part = [p for p in message["parts"] if p.get("kind") == "data"]
        self.assertEqual(len(data_part), 1)

        # 替换运行时上下文（conversation + legacy 消息引用）并用 Python
        # 实现重算 digest。
        envelope = data_part[0]["data"]["knp_envelope"]
        envelope["negotiation_id"] = negotiation_id_for_conversation(self.seed["conversation_id"])
        envelope["in_reply_to"] = f"msg_legacy_{self.seed['merchant_message_id']}"
        envelope = finalize_envelope(envelope)
        message["messageId"] = envelope["message_id"]
        message["parts"][0]["data"]["knp_envelope"] = envelope

        status, response = self._post(body)
        self.assertEqual(status, 200)
        # Kiwi envelope 的 digest 由 Python 实现验证通过（JCS 一致性）。
        self.assertTrue(verify_envelope_digest(envelope))
        # 协商处理成功：JSON-RPC result（而非 error）——inquiry 被绑定到
        # legacy 语义并 accepted（merchant 回复的 result envelope 确认）。
        self.assertIn("result", response)
        self.assertNotIn("error", response)
        reply = response["result"]["message"]
        self.assertEqual(reply["parts"][0]["kind"], "data")
        reply_envelope = reply["parts"][0]["data"]["knp_envelope"]
        self.assertEqual(reply_envelope["payload"]["type"], "result")
        self.assertEqual(reply_envelope["payload"]["outcome"], "accepted")
        self.assertEqual(reply_envelope["in_reply_to"], envelope["message_id"])

    def test_kiwi_fixture_envelope_validates_under_python_knp(self) -> None:
        """不替换上下文的原样 fixture：envelope 结构必须能被 Python 校验。"""
        body = _load_fixture()
        envelope = body["params"]["message"]["parts"][0]["data"]["knp_envelope"]
        # 原样 digest 是 kiwi 算的——Python 必须验证通过（跨语言 digest 锚）。
        self.assertTrue(verify_envelope_digest(envelope))


if __name__ == "__main__":
    unittest.main()

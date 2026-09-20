import json
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs

from shopping_cli.wechat.bridge import BridgeConfig, ShoppingAPIClient, WeChatBridge
from shopping_cli.wechat.messages import (
    build_channel_payload,
    normalize_wechat_message,
    parse_wechat_xml_message,
)
from shopping_cli.wechat.replies import (
    CONSULTATION_WARNING,
    build_text_xml_response,
    format_wechat_reply,
)
from shopping_cli.wechat.signature import verify_wechat_signature, wechat_signature


def text_message_xml(
    content="longjing delivery today",
    msg_type="text",
    to_user="gh_seller",
    from_user="openid_alice",
    msg_id="10001",
):
    return f"""
    <xml>
      <ToUserName>{to_user}</ToUserName>
      <FromUserName>{from_user}</FromUserName>
      <CreateTime>1718000000</CreateTime>
      <MsgType>{msg_type}</MsgType>
      <Content>{content}</Content>
      <MsgId>{msg_id}</MsgId>
    </xml>
    """.encode("utf-8")


class FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def close(self):
        return None


class CapturingOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=0):
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.requests.append({"request": request, "timeout": timeout, "body": body})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return FakeHTTPResponse(response)


class FakeBridgeClient:
    def __init__(self, result=None, error=None):
        self.result = result or {
            "ok": True,
            "selected": {
                "sku": "tea-a",
                "title": "Longjing Gift Box",
                "price": 88,
                "currency": "CNY",
                "stock": 5,
                "delivery": {"service_area": "West Lake", "eta_minutes": 45, "fee": 12, "currency": "CNY"},
            },
            "conversation": {"id": "CONV-0001", "status": "waiting_merchant"},
        }
        self.error = error
        self.payloads = []

    def channel_message(self, payload):
        self.payloads.append(dict(payload))
        if self.error is not None:
            raise self.error
        return self.result


class WeChatBridgeTest(unittest.TestCase):
    def signed_query(self, token="secret-token", timestamp="1718000000", nonce="nonce-1", echostr="ok"):
        signature = wechat_signature(token, timestamp, nonce)
        return parse_qs(f"signature={signature}&timestamp={timestamp}&nonce={nonce}&echostr={echostr}")

    def bridge(self, client=None):
        return WeChatBridge(
            BridgeConfig(
                wechat_token="secret-token",
                shopping_api_url="http://127.0.0.1:8765",
                shopping_channel_token="channel-token",
            ),
            client=client or FakeBridgeClient(),
        )

    def test_wechat_signature_uses_sorted_sha1_material(self):
        signature = wechat_signature("token", "3", "1")

        self.assertEqual(signature, wechat_signature("token", "1", "3"))
        self.assertTrue(verify_wechat_signature("token", signature, "3", "1"))
        self.assertFalse(verify_wechat_signature("token", "bad", "3", "1"))

    def test_parse_and_normalize_text_xml_message(self):
        message = parse_wechat_xml_message(text_message_xml())
        normalized = normalize_wechat_message(message)

        self.assertEqual(normalized["account_id"], "gh_seller")
        self.assertEqual(normalized["external_user_id"], "openid_alice")
        self.assertEqual(normalized["external_message_id"], "10001")
        self.assertEqual(normalized["message_type"], "text")
        self.assertEqual(normalized["text"], "longjing delivery today")

    def test_build_channel_payload_preserves_message_id_for_idempotency(self):
        normalized = normalize_wechat_message(parse_wechat_xml_message(text_message_xml()))

        payload = build_channel_payload(normalized, channel="wechat-ai", city="Hangzhou", area="West Lake")

        self.assertEqual(
            payload,
            {
                "channel": "wechat-ai",
                "external_user_id": "openid_alice",
                "external_message_id": "10001",
                "text": "longjing delivery today",
                "city": "Hangzhou",
                "area": "West Lake",
            },
        )

    def test_format_product_match_reply_is_transaction_safe(self):
        reply = format_wechat_reply(
            {
                "ok": True,
                "selected": {
                    "title": "Longjing Gift Box",
                    "price": 88,
                    "currency": "CNY",
                    "stock": 5,
                    "delivery": {"service_area": "West Lake", "eta_minutes": 45, "fee": 12, "currency": "CNY"},
                },
            }
        )

        self.assertIn("Longjing Gift Box", reply)
        self.assertIn("库存：5", reply)
        self.assertIn(CONSULTATION_WARNING, reply)
        self.assertNotIn("已下单", reply)
        self.assertNotIn("已锁库存", reply)
        self.assertLessEqual(len(reply), 600)

    def test_format_no_match_and_human_review_replies(self):
        no_match = format_wechat_reply({"ok": True, "candidates": [], "conversation": None})
        self.assertIn("暂时没找到匹配商品", no_match)
        self.assertIn(CONSULTATION_WARNING, no_match)

        human = format_wechat_reply({"ok": True, "conversation": {"id": "CONV-0001", "status": "human_required"}})
        self.assertIn("需要商家人工确认", human)
        self.assertIn(CONSULTATION_WARNING, human)

    def test_text_xml_response_escapes_content_and_swaps_users(self):
        raw = build_text_xml_response("openid_alice", "gh_seller", "A&B < C", create_time=123)
        root = ET.fromstring(raw)

        self.assertEqual(root.findtext("ToUserName"), "openid_alice")
        self.assertEqual(root.findtext("FromUserName"), "gh_seller")
        self.assertEqual(root.findtext("CreateTime"), "123")
        self.assertEqual(root.findtext("MsgType"), "text")
        self.assertEqual(root.findtext("Content"), "A&B < C")

    def test_shopping_api_client_posts_channel_message_with_bearer_token(self):
        opener = CapturingOpener([{"ok": True, "candidates": []}])
        client = ShoppingAPIClient("http://shopping.test/", "channel-token", opener=opener)

        result = client.channel_message({"channel": "wechat-ai", "external_user_id": "openid", "text": "hello"})

        self.assertTrue(result["ok"])
        request = opener.requests[0]["request"]
        self.assertEqual(request.full_url, "http://shopping.test/channels/messages")
        self.assertEqual(request.get_header("Authorization"), "Bearer channel-token")
        self.assertEqual(opener.requests[0]["body"]["external_user_id"], "openid")

    def test_shopping_api_client_wraps_http_errors(self):
        error = urllib.error.HTTPError(
            "http://shopping.test/channels/messages",
            403,
            "Forbidden",
            {},
            FakeHTTPResponse({"ok": False, "error": "invalid channel token"}),
        )
        client = ShoppingAPIClient("http://shopping.test", "channel-token", opener=CapturingOpener([error]))

        with self.assertRaises(Exception) as raised:
            client.channel_message({"channel": "wechat-ai", "external_user_id": "openid", "text": "hello"})

        self.assertIn("invalid channel token", str(raised.exception))

    def test_get_callback_validates_url_ownership(self):
        bridge = self.bridge()

        status, headers, body = bridge.handle_get("/wechat/callback", self.signed_query(echostr="hello"))

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(body.decode("utf-8"), "hello")

    def test_get_callback_rejects_invalid_signature(self):
        bridge = self.bridge()
        query = parse_qs("signature=bad&timestamp=1718000000&nonce=nonce-1&echostr=hello")

        status, _headers, body = bridge.handle_get("/wechat/callback", query)

        self.assertEqual(status, 403)
        self.assertIn("invalid signature", body.decode("utf-8"))

    def test_post_callback_maps_text_message_to_channel_ingress_and_xml_reply(self):
        client = FakeBridgeClient()
        bridge = self.bridge(client=client)

        status, headers, body = bridge.handle_post("/wechat/callback", self.signed_query(), text_message_xml())
        root = ET.fromstring(body)

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/xml; charset=utf-8")
        self.assertEqual(client.payloads[0]["channel"], "wechat-official-account")
        self.assertEqual(client.payloads[0]["external_user_id"], "openid_alice")
        self.assertEqual(client.payloads[0]["external_message_id"], "10001")
        self.assertIn("Longjing Gift Box", root.findtext("Content"))
        self.assertIn("尚未下单", root.findtext("Content"))

    def test_post_callback_returns_safe_reply_for_unsupported_message_type(self):
        client = FakeBridgeClient()
        bridge = self.bridge(client=client)

        status, _headers, body = bridge.handle_post(
            "/wechat/callback",
            self.signed_query(),
            text_message_xml(msg_type="image", content=""),
        )
        root = ET.fromstring(body)

        self.assertEqual(status, 200)
        self.assertEqual(client.payloads, [])
        self.assertIn("暂时只支持文字咨询", root.findtext("Content"))

    def test_post_callback_returns_safe_reply_when_shopping_api_fails(self):
        client = FakeBridgeClient(error=RuntimeError("connection refused"))
        bridge = self.bridge(client=client)

        status, _headers, body = bridge.handle_post("/wechat/callback", self.signed_query(), text_message_xml())
        root = ET.fromstring(body)

        self.assertEqual(status, 200)
        self.assertIn("商家助手暂时不可用", root.findtext("Content"))
        self.assertIn("尚未下单", root.findtext("Content"))


if __name__ == "__main__":
    unittest.main()

"""WeChat message parsing and channel payload mapping."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

MAX_WECHAT_BODY_BYTES = 1_048_576
DEFAULT_WECHAT_CHANNEL = "wechat-official-account"


class WeChatMessageError(ValueError):
    pass


def _tag_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def parse_wechat_xml_message(raw_body: bytes) -> dict[str, str]:
    if len(raw_body or b"") > MAX_WECHAT_BODY_BYTES:
        raise WeChatMessageError(f"WeChat message body must be <= {MAX_WECHAT_BODY_BYTES} bytes")
    try:
        root = ET.fromstring(raw_body or b"")
    except ET.ParseError as exc:
        raise WeChatMessageError("invalid WeChat XML message") from exc
    if _tag_name(root.tag) != "xml":
        raise WeChatMessageError("WeChat XML root must be xml")
    message: dict[str, str] = {}
    for child in list(root):
        message[_tag_name(child.tag)] = str(child.text or "").strip()
    return message


def normalize_wechat_message(
    message: dict[str, str],
    source: str = DEFAULT_WECHAT_CHANNEL,
) -> dict[str, str]:
    msg_type = str(message.get("MsgType") or "").strip().lower()
    if not msg_type:
        raise WeChatMessageError("WeChat message type is required")
    normalized = {
        "source": str(source or DEFAULT_WECHAT_CHANNEL).strip() or DEFAULT_WECHAT_CHANNEL,
        "account_id": str(message.get("ToUserName") or "").strip(),
        "external_user_id": str(message.get("FromUserName") or "").strip(),
        "external_message_id": str(message.get("MsgId") or message.get("CreateTime") or "").strip(),
        "message_type": msg_type,
        "text": str(message.get("Content") or "").strip() if msg_type == "text" else "",
    }
    if not normalized["account_id"]:
        raise WeChatMessageError("WeChat account id is required")
    if not normalized["external_user_id"]:
        raise WeChatMessageError("WeChat external user id is required")
    return normalized


def build_channel_payload(
    normalized: dict[str, str],
    channel: str = "",
    conversation_id: str = "",
    city: str = "",
    area: str = "",
) -> dict[str, Any]:
    text = str(normalized.get("text") or "").strip()
    if not text:
        raise WeChatMessageError("text is required")
    payload: dict[str, Any] = {
        "channel": str(channel or normalized.get("source") or DEFAULT_WECHAT_CHANNEL).strip() or DEFAULT_WECHAT_CHANNEL,
        "external_user_id": str(normalized.get("external_user_id") or "").strip(),
        "external_message_id": str(normalized.get("external_message_id") or "").strip(),
        "text": text,
    }
    if not payload["external_user_id"]:
        raise WeChatMessageError("external_user_id is required")
    if conversation_id:
        payload["conversation_id"] = str(conversation_id)
    if city:
        payload["city"] = str(city)
    if area:
        payload["area"] = str(area)
    return payload


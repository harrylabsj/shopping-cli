"""WeChat-safe buyer reply formatting."""

from __future__ import annotations

import time
from typing import Any
from xml.sax.saxutils import escape

MAX_WECHAT_REPLY_CHARS = 600
CONSULTATION_WARNING = "这是售前咨询，尚未下单、未锁库存、未收款。"
UNSUPPORTED_MESSAGE_REPLY = f"暂时只支持文字咨询。{CONSULTATION_WARNING}"
UNAVAILABLE_REPLY = f"商家助手暂时不可用，请稍后再试或等待人工回复。{CONSULTATION_WARNING}"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (OverflowError, TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return max(int(value), 0)
    except (OverflowError, TypeError, ValueError):
        return default


def _trim_reply(text: str) -> str:
    text = "\n".join(line.rstrip() for line in str(text or "").strip().splitlines()).strip()
    if len(text) <= MAX_WECHAT_REPLY_CHARS:
        return text
    return text[: MAX_WECHAT_REPLY_CHARS - 1].rstrip() + "…"


def _delivery_text(product: dict[str, Any]) -> str:
    delivery = product.get("delivery") if isinstance(product.get("delivery"), dict) else {}
    service_area = _text((delivery or {}).get("service_area"))
    eta_minutes = _safe_int((delivery or {}).get("eta_minutes"))
    fee = _safe_number((delivery or {}).get("fee"))
    currency = _text((delivery or {}).get("currency") or product.get("currency") or "CNY")
    if not service_area:
        return "配送需商家确认"
    return f"{service_area}，约 {eta_minutes} 分钟，配送费 {fee:.2f} {currency}"


def _format_product_match(product: dict[str, Any]) -> str:
    title = _text(product.get("title") or product.get("sku") or "匹配商品")
    price = _safe_number(product.get("price"))
    currency = _text(product.get("currency") or "CNY")
    stock = _safe_int(product.get("stock"))
    return _trim_reply(
        "\n".join(
            [
                f"找到：{title}",
                f"价格：{price:.2f} {currency}",
                f"库存：{stock}",
                f"配送：{_delivery_text(product)}",
                "",
                CONSULTATION_WARNING,
                "需要继续确认可直接回复。",
            ]
        )
    )


def _human_review_needed(result: dict[str, Any]) -> bool:
    if result.get("human_required") is True or result.get("requires_human_review") is True:
        return True
    conversation = result.get("conversation")
    if isinstance(conversation, dict) and conversation.get("status") == "human_required":
        return True
    flags = conversation.get("flags") if isinstance(conversation, dict) else []
    return bool(flags)


def format_wechat_reply(result: dict[str, Any]) -> str:
    if not isinstance(result, dict) or result.get("ok") is False:
        return UNAVAILABLE_REPLY
    if _human_review_needed(result):
        return _trim_reply(f"这个问题需要商家人工确认，我已转给商家。\n\n{CONSULTATION_WARNING}")
    selected = result.get("selected")
    if isinstance(selected, dict):
        return _format_product_match(selected)
    candidates = result.get("candidates")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        return _format_product_match(candidates[0])
    if isinstance(candidates, list) and not candidates:
        return _trim_reply(f"暂时没找到匹配商品。请补充商品名、预算、收货区域，或等待商家人工确认。\n\n{CONSULTATION_WARNING}")
    if isinstance(result.get("conversation"), dict):
        return _trim_reply(f"已收到，我会继续转给商家/商家助手处理。\n\n当前仍是售前咨询，尚未形成交易承诺。")
    return UNAVAILABLE_REPLY


def build_text_xml_response(to_user: str, from_user: str, text: str, create_time: int | None = None) -> bytes:
    timestamp = int(create_time if create_time is not None else time.time())
    body = (
        "<xml>"
        f"<ToUserName>{escape(_text(to_user))}</ToUserName>"
        f"<FromUserName>{escape(_text(from_user))}</FromUserName>"
        f"<CreateTime>{timestamp}</CreateTime>"
        "<MsgType>text</MsgType>"
        f"<Content>{escape(_trim_reply(text))}</Content>"
        "</xml>"
    )
    return body.encode("utf-8")


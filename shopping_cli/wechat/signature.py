"""WeChat callback signature helpers."""

from __future__ import annotations

import hashlib
import hmac


def wechat_signature(token: str, timestamp: str, nonce: str) -> str:
    parts = sorted([str(token or ""), str(timestamp or ""), str(nonce or "")])
    material = "".join(parts).encode("utf-8")
    return hashlib.sha1(material).hexdigest()


def verify_wechat_signature(token: str, signature: str, timestamp: str, nonce: str) -> bool:
    expected = wechat_signature(token, timestamp, nonce)
    return hmac.compare_digest(expected, str(signature or "").strip().lower())


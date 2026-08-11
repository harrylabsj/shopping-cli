"""Token issuance, lookup, and summary helpers."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from shopping_cli.core.errors import AuthError, ValidationError
from shopping_cli.core.harness import append_audit_event
from shopping_cli.core.tokens import is_sha256_digest, token_digest, token_matches, token_prefix, token_suffix
from shopping_cli.db.session import now_iso

# ── kiwi-catalog 门户代理凭据（免费档商家）──────────────────────────────
# 配置 KIWI_CATALOG_PROXY_TOKEN 后，kiwi-catalog 门户后端以免密共享密钥作为
# 免费档商家的 Bearer 调本服务。catalog 是身份权威且为受信服务，该凭据可对
# 任意 merchant_id 生效（免费档商家 id 与审批商家同一 id 空间）；命中后返回
# catalog_proxy 哨兵，由 create_product 施加免费商品额度闸门。
# 未配置/为空时代理分支整体关闭（fail-closed）。
CATALOG_PROXY_ROLE = "catalog_proxy"
_CATALOG_PROXY_TOKEN_ENV = "KIWI_CATALOG_PROXY_TOKEN"


def catalog_proxy_token() -> str:
    """读取门户代理共享密钥；未配置返回 ""（代理分支关闭）。"""
    return str(os.environ.get(_CATALOG_PROXY_TOKEN_ENV) or "")

# ── 统一令牌（方案A）：catalog 做身份权威 ────────────────────────────────
# 配置 KIWI_CATALOG_AUTH_URL 后，shopping-cli 的商家 token 校验先查本地，
# 未命中则调 kiwi-catalog 的 /v1/merchants/{id}/token/validate（商家 owner
# token 通用）。带进程内缓存（TTL 内轮换/吊销有延迟，可接受）。
_CATALOG_AUTH_URL_ENV = "KIWI_CATALOG_AUTH_URL"
_CATALOG_CACHE_TTL_SECONDS = 300
_catalog_validation_cache: dict[str, tuple[float, bool]] = {}
_catalog_validation_lock = threading.Lock()


def _catalog_validate_merchant_token(merchant_id: str, token: str) -> bool:
    """调 kiwi-catalog 校验 owner token；带缓存；未配置/不可达 → False。"""
    base = (os.environ.get(_CATALOG_AUTH_URL_ENV) or "").rstrip("/")
    if not base or not token:
        return False
    key = f"{merchant_id}:{hashlib.sha256(str(token).encode()).hexdigest()}"
    now = time.monotonic()
    with _catalog_validation_lock:
        hit = _catalog_validation_cache.get(key)
        if hit is not None and now - hit[0] < _CATALOG_CACHE_TTL_SECONDS:
            return hit[1]
    req = urllib.request.Request(
        f"{base}/v1/merchants/{urllib.parse.quote(merchant_id)}/token/validate",
        data=json.dumps({"token": str(token)}).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
            valid = bool(body.get("ok") and body.get("valid"))
    except Exception:  # noqa: BLE001 —— 网络/解析失败按无效处理（fail-closed）
        valid = False
    with _catalog_validation_lock:
        _catalog_validation_cache[key] = (now, valid)
    return valid

DEFAULT_BUYER_TOKEN_TTL_SECONDS = 86400
DEFAULT_MERCHANT_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60


def default_merchant_agent_id(merchant_id: str) -> str:
    return f"shopping-cli-merchant-agent:{merchant_id}"


def token_is_expired(expires_at: str) -> bool:
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(str(expires_at))
    except (TypeError, ValueError):
        return True
    try:
        current = datetime.now(tz=expires.tzinfo) if expires.tzinfo is not None else datetime.now()
        return expires <= current
    except TypeError:
        return True


def expires_at_from_ttl(ttl_seconds: Any, positive_whole_seconds: Any) -> str:
    seconds = positive_whole_seconds(ttl_seconds, "ttl_seconds")
    if seconds is None:
        return ""
    try:
        expires_at = datetime.now() + timedelta(seconds=seconds)
    except OverflowError as exc:
        raise ValidationError("ttl_seconds is too large") from exc
    return expires_at.replace(microsecond=0).isoformat()


def agent_token_summary(row: Any) -> dict[str, Any]:
    token_key = str(row["token"])
    prefix = str(row["token_prefix"] or token_key[:24])
    suffix = str(row["token_suffix"] or token_key[-6:])
    revoked = bool(row["revoked_at"])
    expired = token_is_expired(row["expires_at"])
    return {
        "token_prefix": prefix,
        "token_suffix": suffix,
        "token_role": row["role"],
        "merchant_id": row["merchant_id"],
        "agent_id": row["agent_id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "revoked": revoked,
        "expired": expired,
        "active": not revoked and not expired,
    }


def agent_token_row(conn: Any, token: str) -> Any:
    stored = str(token or "")
    digest = stored if is_sha256_digest(stored) else token_digest(stored)
    return conn.execute(
        """
        select token, token_hash, token_prefix, token_suffix, role, merchant_id, agent_id, created_at, expires_at, revoked_at
        from api_tokens
        where token = ? or token_hash = ?
        """,
        (digest, digest),
    ).fetchone()


def resolve_agent_token(conn: Any, merchant_id: str, token: Any = "", token_prefix_value: Any = "") -> str:
    resolved = str(token or "")
    if resolved:
        row = agent_token_row(conn, resolved)
        if row is None or row["role"] != "agent" or row["merchant_id"] != merchant_id:
            raise AuthError("invalid agent token")
        return str(row["token"])
    prefix = str(token_prefix_value or "")
    if not prefix:
        raise ValueError("token or token_prefix is required")
    rows = conn.execute(
        """
        select token from api_tokens
        where merchant_id = ? and role = 'agent' and (token_prefix like ? or token like ?)
        order by created_at desc, token
        limit 2
        """,
        (merchant_id, f"{prefix}%", f"{prefix}%"),
    ).fetchall()
    if not rows:
        raise AuthError("invalid agent token")
    if len(rows) > 1:
        raise ValueError("token_prefix is ambiguous")
    return str(rows[0]["token"])


def require_api_token(conn: Any, token: Any, missing_error: str = "authorization token required") -> Any:
    raw_token = str(token or "")
    if not raw_token:
        raise AuthError(missing_error)
    digest = token_digest(raw_token)
    row = conn.execute(
        """
        select role, merchant_id, buyer_id, agent_id, conversation_id, revoked_at, expires_at
        from api_tokens
        where token_hash = ?
        """,
        (digest,),
    ).fetchone()
    if row is None:
        raise AuthError("invalid authorization token")
    if row["revoked_at"]:
        raise AuthError("revoked authorization token")
    if token_is_expired(row["expires_at"]):
        raise AuthError("expired authorization token")
    return row


def _ensure_merchant_exists(conn: Any, merchant_id: str) -> None:
    """catalog 背书的商家在 shopping-cli 不存在时补建最小商家行（方案A 引导）。

    商家在 catalog 审批后即合法；首次用 owner token 管理商品时自动落一个
    shopping-cli 商家行，避免「Unknown merchant」404。
    """
    row = conn.execute("select 1 from merchants where id = ?", (merchant_id,)).fetchone()
    if row is not None:
        return
    now = now_iso()
    conn.execute(
        "insert into merchants(id, name, city, service_area, contact, hours,"
        " automation_boundaries, tags_json, created_at, updated_at)"
        " values (?, ?, '', '', '', '', '', '[]', ?, ?)",
        (merchant_id, merchant_id, now, now),
    )


def require_merchant_token(conn: Any, merchant_id: str, token: Any) -> Any:
    # 1) 本地 merchant token（shopping-cli 自身签发）
    try:
        row = require_api_token(conn, token, "merchant token required")
        if row is not None and row["role"] == "merchant" and row["merchant_id"] == merchant_id:
            return row
    except AuthError:
        pass
    # 2) kiwi-catalog 门户代理凭据（免费档商家；任意 merchant_id）
    presented = str(token or "")
    proxy_secret = catalog_proxy_token()
    if proxy_secret and presented and token_matches(presented, proxy_secret):
        _ensure_merchant_exists(conn, merchant_id)
        return {"role": CATALOG_PROXY_ROLE, "merchant_id": merchant_id}
    # 3) 跨服务：kiwi-catalog owner token（方案A，配置了 KIWI_CATALOG_AUTH_URL 时通用）
    if _catalog_validate_merchant_token(merchant_id, presented):
        _ensure_merchant_exists(conn, merchant_id)
        return None
    raise AuthError("invalid merchant token")


def require_agent_or_merchant_token(conn: Any, merchant_id: str, agent_id: str, token: Any) -> Any:
    if agent_id != default_merchant_agent_id(merchant_id):
        raise AuthError(f"Agent {agent_id} cannot act for merchant {merchant_id}")
    row = require_api_token(conn, token, "agent or merchant token required")
    if row is None or row["merchant_id"] != merchant_id:
        raise AuthError("invalid agent or merchant token")
    if row["role"] == "merchant":
        return row
    if row["role"] == "agent" and row["agent_id"] == agent_id:
        return row
    raise AuthError("invalid agent or merchant token")


def require_conversation_read_token(conn: Any, conversation: dict[str, Any], token: Any) -> None:
    row = require_api_token(conn, token, "conversation read token required")
    if (
        row["role"] == "buyer"
        and row["buyer_id"] == conversation["buyer_id"]
        and row["conversation_id"] == conversation["id"]
    ):
        return
    if row["role"] == "merchant" and row["merchant_id"] == conversation["merchant_id"]:
        return
    if row["role"] == "agent" and row["merchant_id"] == conversation["merchant_id"]:
        return
    raise AuthError("invalid conversation read token")


def require_buyer_conversation_token(conn: Any, conversation: dict[str, Any], token: Any) -> None:
    row = require_api_token(conn, token, "buyer conversation token required")
    if (
        row["role"] == "buyer"
        and row["buyer_id"] == conversation["buyer_id"]
        and row["conversation_id"] == conversation["id"]
    ):
        return
    raise AuthError("invalid buyer conversation token")


def require_buyer_read_token(conn: Any, buyer_id: str, token: Any) -> Any:
    row = require_api_token(conn, token, "buyer conversation read token required")
    if row["role"] == "buyer" and row["buyer_id"] == buyer_id:
        return row
    raise AuthError("invalid buyer conversation read token")


def require_merchant_read_token(conn: Any, merchant_id: str, token: Any) -> Any:
    row = require_api_token(conn, token, "merchant conversation read token required")
    if row["role"] == "merchant" and row["merchant_id"] == merchant_id:
        return row
    if row["role"] == "agent" and row["merchant_id"] == merchant_id:
        return row
    raise AuthError("invalid merchant conversation read token")


def append_agent_token_audit(conn: Any, merchant_id: str, event: str, details: dict[str, Any]) -> None:
    append_audit_event(conn, "", merchant_id, event, details)


def issue_merchant_token(conn: Any, merchant_id: str) -> str:
    token = f"shopping_merchant_{secrets.token_urlsafe(24)}"
    store_merchant_token(conn, token, merchant_id)
    return token


def store_merchant_token(conn: Any, token: str, merchant_id: str, *, ignore_conflict: bool = False) -> None:
    digest = token_digest(token)
    try:
        ttl_seconds = int(str(os.environ.get("SHOPPING_MERCHANT_TOKEN_TTL_SECONDS") or DEFAULT_MERCHANT_TOKEN_TTL_SECONDS))
    except ValueError:
        ttl_seconds = DEFAULT_MERCHANT_TOKEN_TTL_SECONDS
    ttl_seconds = min(max(ttl_seconds, 3600), 365 * 24 * 60 * 60)
    expires_at = (datetime.now() + timedelta(seconds=ttl_seconds)).replace(microsecond=0).isoformat()
    insert = "insert or ignore" if ignore_conflict else "insert"
    conn.execute(
        f"""
        {insert} into api_tokens(token, token_hash, token_prefix, token_suffix, role, merchant_id, buyer_id, agent_id, expires_at, created_at)
        values (?, ?, ?, ?, 'merchant', ?, '', '', ?, ?)
        """,
        (digest, digest, token_prefix(token), token_suffix(token), merchant_id, expires_at, now_iso()),
    )


def ensure_merchant_token(conn: Any, token: str, merchant_id: str) -> str:
    store_merchant_token(conn, token, merchant_id, ignore_conflict=True)
    return token


def rotate_merchant_token(conn: Any, merchant_id: str, *, actor: str = "admin") -> str:
    revoked_at = now_iso()
    conn.execute(
        """
        update api_tokens set revoked_at = ?
        where merchant_id = ? and role = 'merchant' and revoked_at = ''
        """,
        (revoked_at, merchant_id),
    )
    token = issue_merchant_token(conn, merchant_id)
    append_audit_event(
        conn,
        "",
        actor,
        "merchant_token_rotated",
        {"merchant_id": merchant_id, "revoked_at": revoked_at, "token_prefix": token_prefix(token)},
    )
    return token


def revoke_merchant_tokens(conn: Any, merchant_id: str, *, actor: str = "admin") -> int:
    revoked_at = now_iso()
    cursor = conn.execute(
        "update api_tokens set revoked_at = ? where merchant_id = ? and role = 'merchant' and revoked_at = ''",
        (revoked_at, merchant_id),
    )
    count = int(cursor.rowcount or 0)
    append_audit_event(conn, "", actor, "merchant_tokens_revoked", {"merchant_id": merchant_id, "revoked_count": count})
    return count


def issue_agent_token(conn: Any, merchant_id: str, agent_id: str, ttl_seconds: Any = None, positive_whole_seconds: Any = None) -> tuple[str, str]:
    if positive_whole_seconds is None:
        raise ValueError("positive_whole_seconds helper is required")
    token = f"shopping_agent_{secrets.token_urlsafe(24)}"
    digest = token_digest(token)
    expires_at = expires_at_from_ttl(ttl_seconds, positive_whole_seconds)
    conn.execute(
        """
        insert into api_tokens(token, token_hash, token_prefix, token_suffix, role, merchant_id, buyer_id, agent_id, expires_at, created_at)
        values (?, ?, ?, ?, 'agent', ?, '', ?, ?, ?)
        """,
        (digest, digest, token_prefix(token), token_suffix(token), merchant_id, agent_id, expires_at, now_iso()),
    )
    return token, expires_at


def issue_buyer_token(conn: Any, buyer_id: str, conversation_id: str) -> str:
    token = f"shopping_buyer_{secrets.token_urlsafe(24)}"
    store_buyer_token(conn, token, buyer_id, conversation_id)
    return token


def ensure_buyer_token(conn: Any, buyer_id: str, conversation_id: str, token: str) -> str:
    store_buyer_token(conn, token, buyer_id, conversation_id, ignore_conflict=True)
    return token


def store_buyer_token(
    conn: Any,
    token: str,
    buyer_id: str,
    conversation_id: str,
    ignore_conflict: bool = False,
) -> None:
    digest = token_digest(token)
    try:
        ttl_seconds = int(str(os.environ.get("SHOPPING_BUYER_TOKEN_TTL_SECONDS") or DEFAULT_BUYER_TOKEN_TTL_SECONDS))
    except ValueError:
        ttl_seconds = DEFAULT_BUYER_TOKEN_TTL_SECONDS
    ttl_seconds = min(max(ttl_seconds, 60), 30 * 24 * 60 * 60)
    expires_at = (datetime.now() + timedelta(seconds=ttl_seconds)).replace(microsecond=0).isoformat()
    insert = "insert or ignore" if ignore_conflict else "insert"
    conn.execute(
        f"""
        {insert} into api_tokens(
            token, token_hash, token_prefix, token_suffix, role,
            merchant_id, buyer_id, agent_id, conversation_id, expires_at, created_at
        )
        values (?, ?, ?, ?, 'buyer', '', ?, '', ?, ?, ?)
        """,
        (digest, digest, token_prefix(token), token_suffix(token), buyer_id, conversation_id, expires_at, now_iso()),
    )


def revoke_buyer_tokens_for_conversation(conn: Any, conversation_id: str) -> int:
    cursor = conn.execute(
        """
        update api_tokens set revoked_at = ?
        where role = 'buyer' and conversation_id = ? and revoked_at = ''
        """,
        (now_iso(), conversation_id),
    )
    return int(cursor.rowcount or 0)

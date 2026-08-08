"""shopping.negotiation/0.1 authoritative negotiation services.

All write operations are server-authoritative: role and owner identity are
derived from the API token, never from client-declared fields. The policy gate
re-checks conversation state, next_actor, claim ownership, catalog facts and
merchant private thresholds before any message is written. Negotiation never
creates orders, payments or inventory reservations.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from shopping_cli.agents import merchant_agent
from shopping_cli.core import negotiation as protocol
from shopping_cli.core.catalog import product_summary
from shopping_cli.core.conversations import (
    add_flag,
    append_message,
    conversation_messages,
    require_conversation,
)
from shopping_cli.core.errors import AuthError, ConflictError, IdempotencyConflict, NotFoundError, ValidationError
from shopping_cli.core.harness import (
    abandon_agent_message,
    abandon_stale_agent_messages,
    agent_message_process_summary,
    append_audit_event,
    claim_agent_message,
    complete_agent_message,
    conversation_audit_events,
    fail_agent_message,
)
from shopping_cli.core.policies import list_policies
from shopping_cli.db.session import now_iso
from shopping_cli.services import tokens as token_service
from shopping_cli.services.conversations import append_conversation_closed_audit

MAX_PENDING_MESSAGES = 100
MAX_SNAPSHOT_MESSAGES = 50
NEGOTIATION_INTENT = "negotiate"

# Stale-claim recovery (M3): public TTL boundary for the negotiation API.
# The default matches core.harness; the cap rejects unreasonably large
# values instead of silently coercing them.
DEFAULT_STALE_TTL_SECONDS = 300
MAX_STALE_TTL_SECONDS = 86_400

_POLICY_AUDIT_EVENTS = {
    protocol.AUDIT_POLICY_ACCEPTED: "accepted",
    protocol.AUDIT_POLICY_DENIED: "rejected_retryable",
    protocol.AUDIT_HUMAN_REQUIRED: "human_required",
}

# Words that turn a quoted number into a private-threshold disclosure.
_THRESHOLD_TERMS = (
    "最低价",
    "底价",
    "最低可成交",
    "底线",
    "成本价",
    "lowest price",
    "floor price",
    "min price",
    "minimum price",
    "cost price",
)


@dataclass(frozen=True)
class NegotiationActor:
    """Server-derived identity behind an API token. Never client-declared."""

    role: str  # "buyer" | "merchant"
    owner_id: str  # buyer_id or merchant_id bound to the token
    agent_id: str  # claim identity used in agent_message_processes
    conversation_id: str = ""  # buyer tokens are bound to one conversation


def require_negotiation_actor(conn: sqlite3.Connection, token: Any) -> NegotiationActor:
    row = token_service.require_api_token(conn, token, "negotiation token required")
    role = str(row["role"] or "")
    if role == "buyer":
        buyer_id = str(row["buyer_id"] or "")
        conversation_id = str(row["conversation_id"] or "")
        if not buyer_id or not conversation_id:
            raise AuthError("buyer token is not bound to a conversation")
        return NegotiationActor(
            role="buyer",
            owner_id=buyer_id,
            agent_id=protocol.buyer_agent_identity(buyer_id),
            conversation_id=conversation_id,
        )
    if role == "merchant":
        merchant_id = str(row["merchant_id"] or "")
        if not merchant_id:
            raise AuthError("merchant token is not bound to a merchant")
        return NegotiationActor(
            role="merchant",
            owner_id=merchant_id,
            agent_id=token_service.default_merchant_agent_id(merchant_id),
        )
    if role == "agent":
        merchant_id = str(row["merchant_id"] or "")
        agent_id = str(row["agent_id"] or "")
        if not merchant_id or agent_id != token_service.default_merchant_agent_id(merchant_id):
            raise AuthError("agent token cannot act as a negotiation merchant agent")
        return NegotiationActor(role="merchant", owner_id=merchant_id, agent_id=agent_id)
    raise AuthError("token role cannot negotiate")


def require_actor_conversation(conn: sqlite3.Connection, actor: NegotiationActor, conversation_id: str) -> sqlite3.Row:
    conversation = require_conversation(conn, conversation_id)
    if actor.role == "buyer":
        if conversation["buyer_id"] != actor.owner_id or actor.conversation_id != conversation_id:
            raise AuthError(f"Buyer {actor.owner_id} cannot access conversation {conversation_id}")
    elif conversation["merchant_id"] != actor.owner_id:
        raise AuthError(f"Merchant {actor.owner_id} cannot access conversation {conversation_id}")
    return conversation


def _counterpart_senders(role: str) -> set[str]:
    return protocol.MERCHANT_SENDERS if role == "buyer" else protocol.BUYER_SENDERS


def _latest_counterpart_message(conn: sqlite3.Connection, conversation_id: str, role: str) -> dict[str, Any] | None:
    senders = _counterpart_senders(role)
    for message in reversed(conversation_messages(conn, conversation_id)):
        if message["sender"] in senders:
            return message
    return None


def _require_counterpart_message(
    conn: sqlite3.Connection,
    actor: NegotiationActor,
    conversation_id: str,
    message_id: int,
) -> dict[str, Any]:
    if message_id <= 0:
        raise ValidationError("message_id must be greater than 0")
    row = conn.execute(
        "select id, conversation_id, sender from messages where id = ?",
        (message_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown message: {message_id}")
    if row["conversation_id"] != conversation_id:
        raise ValidationError(f"Message {message_id} does not belong to conversation {conversation_id}")
    if row["sender"] not in _counterpart_senders(actor.role):
        raise ValidationError(f"Message {message_id} was not sent by the negotiation counterpart")
    return {"id": int(row["id"]), "conversation_id": row["conversation_id"], "sender": row["sender"]}


def _require_own_turn(conversation: sqlite3.Row, actor: NegotiationActor) -> None:
    if protocol.role_for_next_actor(str(conversation["next_actor"] or "")) != actor.role:
        raise ConflictError(f"Conversation {conversation['id']} is not waiting for the {actor.role}")


def _require_active_claim(
    conn: sqlite3.Connection,
    actor: NegotiationActor,
    conversation_id: str,
    message_id: int,
) -> dict[str, Any]:
    try:
        process = agent_message_process_summary(conn, actor.agent_id, message_id)
    except NotFoundError as exc:
        raise ConflictError(f"Message {message_id} is not claimed by this agent") from exc
    if process["conversation_id"] != conversation_id:
        raise ConflictError(f"Claim for message {message_id} belongs to a different conversation")
    if process["status"] != "processing":
        raise ConflictError(f"Claim for message {message_id} is not processing (status: {process['status']})")
    return process


def _process_row(conn: sqlite3.Connection, agent_id: str, message_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "select status from agent_message_processes where agent_id = ? and message_id = ?",
        (agent_id, message_id),
    ).fetchone()


def list_pending_messages(conn: sqlite3.Connection, actor: NegotiationActor) -> list[dict[str, Any]]:
    """Messages whose conversation next_actor is this role and whose latest
    counterpart message this agent has not processed yet. Fail closed: a buyer
    token only ever sees its bound conversation; a merchant only its own."""
    if actor.role == "merchant":
        rows = conn.execute(
            """
            select id, status from conversations
            where merchant_id = ? and status = 'waiting_merchant'
            order by updated_at desc limit ?
            """,
            (actor.owner_id, MAX_PENDING_MESSAGES),
        ).fetchall()
    else:
        if not actor.conversation_id:
            return []
        rows = conn.execute(
            """
            select id, status from conversations
            where id = ? and buyer_id = ? and status = 'waiting_buyer'
            """,
            (actor.conversation_id, actor.owner_id),
        ).fetchall()
    pending: list[dict[str, Any]] = []
    for row in rows:
        conversation_id = str(row["id"])
        message = _latest_counterpart_message(conn, conversation_id, actor.role)
        if message is None:
            continue
        process = _process_row(conn, actor.agent_id, int(message["id"]))
        if process is not None and process["status"] in {"processing", "processed"}:
            continue
        pending.append(
            {
                "conversation_id": conversation_id,
                "message_id": int(message["id"]),
                "conversation_status": str(row["status"]),
                "sender_role": "buyer" if actor.role == "merchant" else "merchant",
                "preview": protocol.truncate_text(message["text"], 200),
                "created_at": str(message["created_at"]),
            }
        )
    return pending


def claim_message(
    conn: sqlite3.Connection,
    actor: NegotiationActor,
    conversation_id: str,
    message_id: int,
    idempotency_key: str,
) -> dict[str, Any]:
    if not idempotency_key.strip():
        raise ValidationError("idempotency_key is required")
    conversation = require_actor_conversation(conn, actor, conversation_id)
    _require_own_turn(conversation, actor)
    _require_counterpart_message(conn, actor, conversation_id, message_id)
    latest = _latest_counterpart_message(conn, conversation_id, actor.role)
    if latest is None or int(latest["id"]) != message_id:
        raise ConflictError(f"Message {message_id} is not the latest counterpart message")
    return claim_agent_message(conn, actor.agent_id, conversation_id, message_id, idempotency_key)


def complete_claim(conn: sqlite3.Connection, actor: NegotiationActor, message_id: int) -> dict[str, Any]:
    process = agent_message_process_summary(conn, actor.agent_id, message_id)
    require_actor_conversation(conn, actor, process["conversation_id"])
    return complete_agent_message(conn, actor.agent_id, message_id)


def fail_claim(conn: sqlite3.Connection, actor: NegotiationActor, message_id: int, error: str) -> dict[str, Any]:
    process = agent_message_process_summary(conn, actor.agent_id, message_id)
    require_actor_conversation(conn, actor, process["conversation_id"])
    return fail_agent_message(conn, actor.agent_id, message_id, error or "agent failure")


def abandon_claim(conn: sqlite3.Connection, actor: NegotiationActor, message_id: int, error: str) -> dict[str, Any]:
    process = agent_message_process_summary(conn, actor.agent_id, message_id)
    require_actor_conversation(conn, actor, process["conversation_id"])
    return abandon_agent_message(conn, actor.agent_id, message_id, error or "agent abandoned claim")


def _strict_ttl_seconds(value: Any) -> int:
    """Fail-closed TTL parsing for the public negotiation boundary.

    Accepts only a whole positive number (int, integral finite float, or a
    decimal string). bools, fractions, non-finite floats, nonpositive and
    unreasonably large values are rejected — never silently coerced.
    """
    if value is None:
        return DEFAULT_STALE_TTL_SECONDS
    if isinstance(value, bool):
        raise ValidationError("ttl_seconds must be a whole number")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValidationError("ttl_seconds must be a whole number")
        number = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise ValidationError("ttl_seconds must be a whole number")
        number = int(text)
    else:
        raise ValidationError("ttl_seconds must be a whole number")
    if number <= 0:
        raise ValidationError("ttl_seconds must be greater than 0")
    if number > MAX_STALE_TTL_SECONDS:
        raise ValidationError(f"ttl_seconds must be <= {MAX_STALE_TTL_SECONDS}")
    return number


def heartbeat_claims(
    conn: sqlite3.Connection, actor: NegotiationActor, message_id: int | None = None
) -> dict[str, Any]:
    """Refresh updated_at on the actor's OWN currently-processing claims.

    Used by long-running agent turns so a healthy claim is never considered
    stale. Never touches settled (processed/failed/abandoned) claims, never
    revives stale work, and never touches another identity's claims: the
    update is scoped by the token-derived agent_id, and a message-scoped
    heartbeat additionally passes require_actor_conversation (a buyer token
    stays bound to its own conversation).
    """
    now = now_iso()
    conversation_id = ""
    if message_id is not None:
        if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
            raise ValidationError("message_id must be a positive whole number")
        process = agent_message_process_summary(conn, actor.agent_id, message_id)
        conversation_id = str(process["conversation_id"])
        require_actor_conversation(conn, actor, conversation_id)
        cursor = conn.execute(
            """
            update agent_message_processes
            set updated_at = ?
            where agent_id = ? and message_id = ? and status = ?
            """,
            (now, actor.agent_id, message_id, "processing"),
        )
        refreshed = cursor.rowcount
    else:
        # buyer 批量心跳必须限定在其 token 绑定的会话内——buyer agent id 是
        # 按 buyer 恒定的，A 会话 token 不应能续命 B 会话的 claim（否则
        # 崩溃的 claim 被永久续命、永不回收）。
        if actor.role == "buyer" and actor.conversation_id:
            cursor = conn.execute(
                """
                update agent_message_processes
                set updated_at = ?
                where agent_id = ? and status = ? and message_id in (
                    select id from messages where conversation_id = ?
                )
                """,
                (now, actor.agent_id, "processing", actor.conversation_id),
            )
        else:
            cursor = conn.execute(
                """
                update agent_message_processes
                set updated_at = ?
                where agent_id = ? and status = ?
                """,
                (now, actor.agent_id, "processing"),
            )
        refreshed = cursor.rowcount
    append_audit_event(
        conn,
        conversation_id,
        actor.agent_id,
        "agent_message_heartbeat",
        {
            "refreshed": refreshed,
            **({"message_id": message_id} if message_id is not None else {}),
        },
    )
    return {"status": "ok", "refreshed": refreshed, "at": now}


def abandon_stale_claims(conn: sqlite3.Connection, actor: NegotiationActor, ttl_seconds: Any = None) -> dict[str, Any]:
    """Abandon the actor's OWN stale processing claims (crash recovery).

    Scoped strictly by the token-derived agent_id: a buyer token can only
    ever affect claims of its own buyer identity (and those only exist for
    its bound conversation), never another buyer or the merchant. Each
    abandoned claim is audited by core.harness with reason
    stale_processing_claim and stays reclaimable.
    """
    ttl = _strict_ttl_seconds(ttl_seconds)
    # buyer 的 abandon 同样限定在其绑定会话内（与 heartbeat 一致）。
    abandoned = abandon_stale_agent_messages(
        conn,
        actor.agent_id,
        ttl,
        conversation_id=actor.conversation_id if actor.role == "buyer" else "",
    )
    return {
        "abandoned": len(abandoned),
        "message_ids": [int(process["message_id"]) for process in abandoned],
        "ttl_seconds": ttl,
        "at": now_iso(),
    }


def _snapshot_message(message: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": int(message["id"]),
        "sender_role": "buyer" if message["sender"] in protocol.BUYER_SENDERS else "merchant",
        # DB rows store naive local time; emit explicit-offset RFC 3339 so the
        # frozen schema (and Kiwi's strict Ajv date-time check) accepts it.
        "created_at": protocol.normalize_db_timestamp(message["created_at"]),
        "public_message": protocol.truncate_text(message["text"], 2000),
    }
    payload = message.get("structured_payload") or {}
    decision = payload.get("decision") if payload.get("protocol_version") == protocol.PROTOCOL_VERSION else None
    if isinstance(decision, dict):
        action = decision.get("action")
        if action in protocol.DECISION_ACTIONS:
            entry["action"] = action
        proposal = decision.get("proposal")
        entry["proposal"] = proposal if isinstance(proposal, dict) else None
    else:
        entry["proposal"] = None
    return entry


def _policy_ref_summary(conn: sqlite3.Connection, merchant_id: str) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for policy in list_policies(conn, merchant_id, limit=32):
        refs.append(
            {
                "ref": protocol.truncate_text(f"policy:{policy['code']}", 128),
                "summary": protocol.truncate_text(policy["title"] or policy["body"], 1000),
            }
        )
    return refs


def _own_policy_results(conn: sqlite3.Connection, conversation_id: str, role: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for event in conversation_audit_events(conn, conversation_id):
        mapped = _POLICY_AUDIT_EVENTS.get(event["event"])
        if mapped is None:
            continue
        details = event.get("details") or {}
        if details.get("role") != role:
            continue
        reason_codes = [
            protocol.truncate_text(code, 64) for code in details.get("reason_codes") or [] if str(code or "").strip()
        ][:32]
        results.append(
            {
                "result": mapped,
                "reason_codes": reason_codes,
                "public_reason": protocol.truncate_text(details.get("public_reason"), 1000),
            }
        )
    return results[-32:]


def _load_conversation_product(conn: sqlite3.Connection, conversation: sqlite3.Row) -> dict[str, Any]:
    sku = str(conversation["sku"] or "").strip()
    if not sku:
        raise ConflictError(f"Conversation {conversation['id']} is not bound to a product")
    try:
        product = product_summary(conn, sku)
    except NotFoundError as exc:
        raise ConflictError(f"Conversation {conversation['id']} product {sku} is unavailable") from exc
    if product["merchant_id"] != conversation["merchant_id"]:
        raise ConflictError(f"Conversation {conversation['id']} product does not belong to the merchant")
    return product


def build_snapshot(
    conn: sqlite3.Connection,
    actor: NegotiationActor,
    conversation_id: str,
    message_id: int,
) -> dict[str, Any]:
    """Authoritative, role-trimmed snapshot. Contains no private thresholds:
    merchant automation boundaries and any buyer-side private config are never
    read here, and the result is validated against the frozen schema."""
    conversation = require_actor_conversation(conn, actor, conversation_id)
    _require_own_turn(conversation, actor)
    _require_counterpart_message(conn, actor, conversation_id, message_id)
    _require_active_claim(conn, actor, conversation_id, message_id)
    product = _load_conversation_product(conn, conversation)

    observed_at = protocol.now_rfc3339()
    stock_qty = int(product["stock"])
    stock_status = "available" if stock_qty > 2 else "low" if stock_qty > 0 else "out_of_stock"
    delivery_rule = product.get("delivery") or {}
    eta_minutes = int(delivery_rule.get("eta_minutes") or 60)
    now = datetime.now(timezone.utc)
    eta_start = now.timestamp() + eta_minutes * 60
    eta_end = now.timestamp() + (eta_minutes + 120) * 60

    messages = [_snapshot_message(message) for message in conversation_messages(conn, conversation_id)]
    messages = messages[-MAX_SNAPSHOT_MESSAGES:]
    current_proposal = None
    open_issues: list[str] = []
    for message in reversed(messages):
        if current_proposal is None and isinstance(message.get("proposal"), dict):
            current_proposal = message["proposal"]
    for message in reversed(conversation_messages(conn, conversation_id)):
        payload = message.get("structured_payload") or {}
        decision = payload.get("decision") if payload.get("protocol_version") == protocol.PROTOCOL_VERSION else None
        if isinstance(decision, dict):
            issues = decision.get("open_issues")
            if isinstance(issues, list):
                open_issues = [protocol.truncate_text(issue, 500) for issue in issues if str(issue or "").strip()][:32]
            break

    snapshot: dict[str, Any] = {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "conversation": {
            "id": str(conversation["id"]),
            "status": str(conversation["status"]),
            "next_actor": protocol.snapshot_next_actor(str(conversation["next_actor"] or "")),
        },
        "role": actor.role,
        "in_reply_to_message_id": message_id,
        "product": {
            "sku": protocol.truncate_text(product["sku"], 128),
            "title": protocol.truncate_text(product["title"], 500),
            "currency": protocol.truncate_text(product["currency"], 8),
            "list_price": float(product["price"]),
        },
        "stock": {
            "status": stock_status,
            "quantity": stock_qty,
            "observed_at": observed_at,
            "reserved": False,
            "source": {"backend": "local_marketplace", "observed_at": observed_at},
        },
        "delivery": {
            "eta_start": datetime.fromtimestamp(eta_start, timezone.utc).isoformat(timespec="seconds"),
            "eta_end": datetime.fromtimestamp(eta_end, timezone.utc).isoformat(timespec="seconds"),
            "fee": float(delivery_rule.get("fee") or 0),
        },
        "after_sales_policies": _policy_ref_summary(conn, str(conversation["merchant_id"])),
        "messages": messages,
        "current_proposal": current_proposal,
        "open_issues": open_issues,
        "policy_results": _own_policy_results(conn, conversation_id, actor.role),
    }
    description = protocol.truncate_text(product.get("description"), 2000)
    if description:
        snapshot["product"]["description"] = description
    notes = protocol.truncate_text(delivery_rule.get("notes"), 500)
    if notes:
        snapshot["delivery"]["notes"] = notes
    protocol.validate_contract("snapshot", snapshot)
    return snapshot


@dataclass(frozen=True)
class GateOutcome:
    result: str  # "accepted" | "rejected_retryable" | "human_required"
    reason_codes: tuple[str, ...]
    public_reason: str


_ACCEPT = GateOutcome("accepted", (), "")


def _reject(code: str, public_reason: str) -> GateOutcome:
    return GateOutcome("rejected_retryable", (code,), public_reason)


def _human(code: str, public_reason: str) -> GateOutcome:
    return GateOutcome("human_required", (code,), public_reason)


def _check_proposal_facts(
    conn: sqlite3.Connection,
    conversation: sqlite3.Row,
    proposal: dict[str, Any],
) -> GateOutcome:
    """Fact checks shared by both roles, re-read from the latest catalog data.

    Beyond purchase feasibility (quantity <= stock) this verifies the
    proposal's observed stock quantity/status against the authoritative
    server-side stock, so a stale or forged inventory observation is never
    written into a public structured message."""
    sku = str(conversation["sku"] or "").strip()
    if not sku or proposal["sku"] != sku:
        return _reject("unknown_product", "磋商商品与会话商品不一致，请基于最新快照中的商品报价。")
    try:
        product = product_summary(conn, sku)
    except NotFoundError:
        return _reject("unknown_product", "商品当前不可用，请重新获取快照。")
    if product["merchant_id"] != conversation["merchant_id"]:
        return _reject("unknown_product", "商品不属于该商家，请重新获取快照。")
    if proposal["currency"] != product["currency"]:
        return _reject("currency_mismatch", f"币种必须为 {product['currency']}。")
    stock_qty = int(product["stock"])
    if stock_qty <= 0:
        return _reject("insufficient_stock", "商品当前无库存，请重新获取快照后再报价。")
    if int(proposal["quantity"]) > stock_qty:
        return _reject("insufficient_stock", f"当前可售库存为 {stock_qty} 件，请调整数量。")
    # Authoritative inventory cross-check: the observed stock carried by the
    # proposal must match the latest server-side stock, otherwise a forged
    # observation would be written into the public structured message.
    expected_status = "available" if stock_qty > 2 else "low" if stock_qty > 0 else "out_of_stock"
    observed = proposal["stock"]
    if int(observed["quantity"]) != stock_qty or observed["status"] != expected_status:
        return _reject(
            "stale_inventory",
            f"库存观察与服务端最新库存（{stock_qty} 件，{expected_status}）不一致，请重新获取快照后再报价。",
        )
    now = datetime.now(timezone.utc)
    observed_at = protocol.parse_rfc3339(proposal["stock"]["observed_at"])
    valid_until = protocol.parse_rfc3339(proposal["valid_until"])
    eta_start = protocol.parse_rfc3339(proposal["delivery"]["eta_start"])
    eta_end = protocol.parse_rfc3339(proposal["delivery"]["eta_end"])
    if observed_at is None or valid_until is None or eta_start is None or eta_end is None:
        return _reject("invalid_timestamp", "时间字段必须是带时区的 RFC 3339 时间戳。")
    if valid_until <= now:
        return _reject("quote_expired", "报价有效期已过期，请重新获取快照后再报价。")
    if eta_end < eta_start:
        return _reject("invalid_delivery", "配送时效结束时间早于开始时间，请修正。")
    valid_refs = {
        f"policy:{policy['code']}" for policy in list_policies(conn, str(conversation["merchant_id"]), limit=100)
    }
    unknown_refs = [ref for ref in proposal["after_sales_policy_refs"] if ref not in valid_refs]
    if unknown_refs:
        return _reject("unknown_policy_ref", f"售后政策引用不存在或已失效: {unknown_refs[0]}。")
    return _ACCEPT


def _normalize_digits(text: str) -> str:
    """全角→半角数字并去掉空白/千分位分隔符，供底价匹配使用。"""
    table = str.maketrans("０１２３４５６７８９．", "0123456789.")
    return re.sub(r"[\s,，]", "", text.translate(table))


def _leaks_private_threshold(public_message: str, floor_str: str) -> bool:
    try:
        floor_value = float(floor_str)
    except (TypeError, ValueError):
        return False
    candidates = {floor_str, f"{floor_value:.2f}"}
    if floor_value.is_integer():
        candidates.add(str(int(floor_value)))
    normalized_candidates = {_normalize_digits(c) for c in candidates}
    lowered = public_message.lower()
    if not any(term in lowered or term in public_message for term in _THRESHOLD_TERMS):
        return False
    normalized_message = _normalize_digits(public_message)
    return any(c in public_message for c in candidates) or any(
        n in normalized_message for n in normalized_candidates
    )


def _merchant_gate(
    conn: sqlite3.Connection,
    conversation: sqlite3.Row,
    decision: dict[str, Any],
) -> GateOutcome:
    proposal = decision.get("proposal")
    if decision["action"] in {"propose", "counter"} and proposal is None:
        return _reject("missing_proposal", "propose/counter 必须携带结构化 proposal。")
    product = product_summary(conn, str(conversation["sku"]))
    floor_str = merchant_agent._authorized_bargain_amount(product)
    # 泄漏守卫对每个 merchant 决策运行——ask/decline 等无 proposal 的决策
    # 也可能携带泄露底价的 public_message（此前该分支直接放行）。
    if floor_str and _leaks_private_threshold(str(decision.get("public_message") or ""), floor_str):
        return _human("private_threshold_leak", "公开消息可能泄露商家私有价格边界，需要人工处理。")
    if proposal is None:
        return _ACCEPT
    facts = _check_proposal_facts(conn, conversation, proposal)
    if facts.result != "accepted":
        return facts
    observed_at = protocol.parse_rfc3339(proposal["stock"]["observed_at"])
    assert observed_at is not None  # guaranteed by _check_proposal_facts
    age = (datetime.now(timezone.utc) - observed_at).total_seconds()
    if age > protocol.STOCK_OBSERVATION_MAX_AGE_SECONDS or age < -60:
        return _reject("stale_inventory", "库存观察时间已过期，请重新获取快照后再报价。")
    unit_price = float(proposal["unit_price"])
    if floor_str:
        if unit_price < float(floor_str):
            return _human("below_floor", "报价低于商家授权的自动磋商范围，需要人工处理。")
    elif unit_price < float(product["price"]):
        return _human("unauthorized_discount", "该折扣没有商家授权规则，需要人工处理。")
    return _ACCEPT


def _buyer_gate(
    conn: sqlite3.Connection,
    conversation: sqlite3.Row,
    decision: dict[str, Any],
) -> GateOutcome:
    proposal = decision.get("proposal")
    if decision["action"] in {"propose", "counter"} and proposal is None:
        return _reject("missing_proposal", "propose/counter 必须携带结构化 proposal。")
    if proposal is None:
        return _ACCEPT
    # Non-binding boundary: the buyer side checks structure, facts and expiry
    # only. Private budgets live in the Kiwi profile and are never sent here.
    # A buyer proposal is a non-binding intent, but an accepted one is still
    # written into the public structured message, so the same authoritative
    # stock-observation consistency (quantity/status vs latest server stock)
    # is enforced for buyers as for merchants.
    return _check_proposal_facts(conn, conversation, proposal)


def _find_decision_replay(
    conn: sqlite3.Connection,
    actor: NegotiationActor,
    conversation_id: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    for message in conversation_messages(conn, conversation_id):
        payload = message.get("structured_payload") or {}
        if (
            payload.get("protocol_version") == protocol.PROTOCOL_VERSION
            and payload.get("idempotency_key") == idempotency_key
            and payload.get("agent_id") == actor.agent_id
        ):
            return {"message_id": int(message["id"]), "decision": payload.get("decision")}
    return None


def _policy_result(
    conversation_id: str,
    result: str,
    next_actor: str,
    reason_codes: list[str],
    public_reason: str,
    retries_remaining: int,
    message_id: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "result": result,
        "conversation_id": conversation_id,
        "next_actor": next_actor,
        "reason_codes": reason_codes[:32],
        "public_reason": protocol.truncate_text(public_reason, 1000),
        "retries_remaining": max(0, retries_remaining),
    }
    if message_id is not None:
        payload["message_id"] = message_id
    protocol.validate_contract("policy-result", payload)
    return payload


def submit_decision(
    conn: sqlite3.Connection,
    actor: NegotiationActor,
    decision: Any,
    idempotency_key: str,
) -> dict[str, Any]:
    """The single write intent of shopping.negotiation/0.1.

    Fixed order: schema -> conversation/identity -> idempotent replay ->
    next_actor -> claim -> role policy gate -> atomic write + audit.

    Idempotent replay is checked right after identity, *before* turn/claim
    state, so a client that lost the accepted response can safely retry with
    the same key even after the turn advanced and the claim was completed.
    Replay only ever returns an already-written accepted decision that
    belongs to this same actor (agent_id) and conversation with a
    byte-identical payload; it never produces a new write, and a different
    token/owner can never read another actor's result through it (the
    conversation is ownership-checked and the stored payload is matched on
    the caller's agent_id). Rejections never write messages; conflicts fail
    closed with 409.
    """
    if not isinstance(decision, dict):
        raise ValidationError("decision must be an object")
    protocol.validate_contract("decision", decision)
    if not idempotency_key.strip():
        raise ValidationError("idempotency_key is required")
    conversation_id = str(decision["conversation_id"])
    conversation = require_actor_conversation(conn, actor, conversation_id)

    # Idempotent replay is checked before turn/claim state so a client that
    # lost the accepted response can safely retry with the same key, even
    # after the turn advanced and the claim was completed.
    replay = _find_decision_replay(conn, actor, conversation_id, idempotency_key)
    if replay is not None:
        if protocol.canonical_json(replay["decision"]) != protocol.canonical_json(decision):
            raise IdempotencyConflict(f"idempotency key {idempotency_key} was already used with a different decision")
        refreshed = require_conversation(conn, conversation_id)
        return _policy_result(
            conversation_id,
            "accepted",
            protocol.snapshot_next_actor(str(refreshed["next_actor"] or "")),
            list(decision["reason_codes"]),
            "决策已接受并写入会话（幂等重放，未重复写入）。",
            0,
            message_id=int(replay["message_id"]),
        )

    _require_own_turn(conversation, actor)
    message_id = int(decision["in_reply_to_message_id"])
    _require_counterpart_message(conn, actor, conversation_id, message_id)
    process = _require_active_claim(conn, actor, conversation_id, message_id)

    gate = (
        _merchant_gate(conn, conversation, decision)
        if actor.role == "merchant"
        else _buyer_gate(conn, conversation, decision)
    )
    attempts = int(process["attempts"])
    retries_remaining = max(0, protocol.MAX_DECISION_ATTEMPTS - attempts)
    audit_details = {
        "role": actor.role,
        "action": decision["action"],
        "idempotency_key": idempotency_key,
        "in_reply_to_message_id": message_id,
        "protocol_version": protocol.PROTOCOL_VERSION,
    }

    if decision["action"] == "escalate" or decision["request_human_review"]:
        reason_codes = list(dict.fromkeys([*decision["reason_codes"], "agent_requested_human_review"]))[:32]
        add_flag(
            conn,
            conversation_id,
            reason="agent_requested_human_review",
            sku=str(conversation["sku"] or ""),
        )
        append_audit_event(
            conn,
            conversation_id,
            actor.agent_id,
            protocol.AUDIT_HUMAN_REQUIRED,
            {**audit_details, "reason_codes": reason_codes, "public_reason": "Agent 请求人工介入。"},
        )
        return _policy_result(conversation_id, "human_required", "none", reason_codes, "该请求需要人工处理。", 0)

    if gate.result == "human_required":
        add_flag(conn, conversation_id, reason=gate.reason_codes[0], sku=str(conversation["sku"] or ""))
        append_audit_event(
            conn,
            conversation_id,
            actor.agent_id,
            protocol.AUDIT_HUMAN_REQUIRED,
            {**audit_details, "reason_codes": list(gate.reason_codes), "public_reason": gate.public_reason},
        )
        return _policy_result(conversation_id, "human_required", "none", list(gate.reason_codes), gate.public_reason, 0)

    if gate.result == "rejected_retryable":
        append_audit_event(
            conn,
            conversation_id,
            actor.agent_id,
            protocol.AUDIT_POLICY_DENIED,
            {**audit_details, "reason_codes": list(gate.reason_codes), "public_reason": gate.public_reason},
        )
        return _policy_result(
            conversation_id,
            "rejected_retryable",
            actor.role,
            list(gate.reason_codes),
            gate.public_reason,
            retries_remaining,
        )

    public_message = str(decision["public_message"]).strip()
    if not public_message:
        append_audit_event(
            conn,
            conversation_id,
            actor.agent_id,
            protocol.AUDIT_POLICY_DENIED,
            {**audit_details, "reason_codes": ["empty_public_message"], "public_reason": "公开消息不能为空。"},
        )
        return _policy_result(
            conversation_id,
            "rejected_retryable",
            actor.role,
            ["empty_public_message"],
            "公开消息不能为空。",
            retries_remaining,
        )

    sender = "merchant_agent" if actor.role == "merchant" else "buyer"
    if decision["action"] == "decline":
        status = "closed"
    else:
        status = "waiting_buyer" if actor.role == "merchant" else "waiting_merchant"
    structured_payload = {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "idempotency_key": idempotency_key,
        "agent_id": actor.agent_id,
        "role": actor.role,
        "source_id": actor.agent_id,
        "decision": decision,
    }
    message = append_message(
        conn,
        conversation_id,
        sender=sender,
        intent=NEGOTIATION_INTENT,
        text=public_message,
        structured_payload=structured_payload,
        status=status,
    )
    if status == "closed":
        append_conversation_closed_audit(conn, conversation_id, sender, "")
    append_audit_event(
        conn,
        conversation_id,
        actor.agent_id,
        protocol.AUDIT_DECISION_SUBMITTED,
        {**audit_details, "message_id": int(message["id"])},
    )
    append_audit_event(
        conn,
        conversation_id,
        actor.agent_id,
        protocol.AUDIT_POLICY_ACCEPTED,
        {**audit_details, "message_id": int(message["id"]), "reason_codes": list(decision["reason_codes"])},
    )
    refreshed = require_conversation(conn, conversation_id)
    return _policy_result(
        conversation_id,
        "accepted",
        protocol.snapshot_next_actor(str(refreshed["next_actor"] or "")),
        list(decision["reason_codes"]),
        "决策已接受并写入会话。",
        retries_remaining,
        message_id=int(message["id"]),
    )

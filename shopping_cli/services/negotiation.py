"""shopping.negotiation/0.1 authoritative negotiation services.

All write operations are server-authoritative: role and owner identity are
derived from the API token, never from client-declared fields. The policy gate
re-checks conversation state, next_actor, claim ownership, catalog facts and
merchant private thresholds before any message is written. Negotiation never
creates orders, payments or inventory reservations.
"""

from __future__ import annotations

import math
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
from shopping_cli.db.session import decode_json, now_iso
from shopping_cli.services import tokens as token_service
from shopping_cli.services.conversations import append_conversation_closed_audit
from shopping_cli.services.negotiation_gates import (
    buyer_gate as _buyer_gate,
    check_proposal_facts as _check_proposal_facts,
)
from shopping_cli.services.negotiation_message import (
    decision_sender as _decision_sender,
    decision_status as _decision_status,
    decision_structured_payload as _decision_structured_payload,
)
from shopping_cli.services.negotiation_policy_helpers import (
    leaks_private_threshold as _leaks_private_threshold,
)
from shopping_cli.services.negotiation_policy_result import (
    ACCEPTED_OUTCOME as _ACCEPT,
    GateOutcome,
    build_policy_result as _policy_result,
    human_required as _human,
    rejected as _reject,
)
from shopping_cli.services.negotiation_snapshot import snapshot_message as _snapshot_message
from shopping_cli.services.negotiation_snapshot_projection import (
    latest_open_issues as _latest_open_issues,
    latest_proposal as _latest_proposal,
    project_delivery as _project_delivery,
    project_product as _project_product,
    project_stock as _project_stock,
)

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
    delivery_rule = product.get("delivery") or {}
    now = datetime.now(timezone.utc)

    messages = [_snapshot_message(message) for message in conversation_messages(conn, conversation_id)]
    messages = messages[-MAX_SNAPSHOT_MESSAGES:]
    current_proposal = _latest_proposal(messages)
    open_issues = _latest_open_issues(conversation_messages(conn, conversation_id))

    snapshot: dict[str, Any] = {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "conversation": {
            "id": str(conversation["id"]),
            "status": str(conversation["status"]),
            "next_actor": protocol.snapshot_next_actor(str(conversation["next_actor"] or "")),
        },
        "role": actor.role,
        "in_reply_to_message_id": message_id,
        "product": _project_product(product),
        "stock": _project_stock(stock_qty, observed_at),
        "delivery": _project_delivery(delivery_rule, now=now),
        "after_sales_policies": _policy_ref_summary(conn, str(conversation["merchant_id"])),
        "messages": messages,
        "current_proposal": current_proposal,
        "open_issues": open_issues,
        "policy_results": _own_policy_results(conn, conversation_id, actor.role),
    }
    protocol.validate_contract("snapshot", snapshot)
    return snapshot


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


def _find_decision_replay(
    conn: sqlite3.Connection,
    actor: NegotiationActor,
    conversation_id: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    # 反向扫描最近 100 条：replay 目标恒是刚写入的 decision 消息（幂等
    # 重试发生在提交后不久），首次命中即返回——避免每次 submit 全量
    # 扫描会话（O(n)，长会话下每次决策都拖慢）。
    rows = conn.execute(
        "select id, structured_payload_json from messages "
        "where conversation_id = ? order by id desc limit 100",
        (conversation_id,),
    ).fetchall()
    for row in rows:
        payload = decode_json(row["structured_payload_json"], {})
        if (
            payload.get("protocol_version") == protocol.PROTOCOL_VERSION
            and payload.get("idempotency_key") == idempotency_key
            and payload.get("agent_id") == actor.agent_id
        ):
            return {"message_id": int(row["id"]), "decision": payload.get("decision")}
    return None


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
    # 决策提交时对端消息必须仍是最新——claim 之后到达的新买家消息会使
    # 本决策回答旧问题（跨消息跳答）。拦截后客户端重 claim 最新消息即可；
    # 幂等 replay 在回合检查之前，不受影响。
    latest = _latest_counterpart_message(conn, conversation_id, actor.role)
    if latest is None or int(latest["id"]) != message_id:
        raise ConflictError(f"Message {message_id} is no longer the latest counterpart message")
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

    sender = _decision_sender(actor.role)
    status = _decision_status(actor.role, decision["action"])
    structured_payload = _decision_structured_payload(actor.agent_id, actor.role, decision, idempotency_key)
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

"""Catalog and marketplace HTTP handlers."""

from __future__ import annotations

from pathlib import Path
import hashlib
import hmac
import json
import sqlite3
from typing import Any

from shopping_cli import VERSION
from shopping_cli.config import deployment_profile_from, production_config_checks, validate_production_config
from shopping_cli.core import catalog
from shopping_cli.core.money_authority import (
    CURRENCY_TABLE_VERSION,
    exact_product_money,
    money_mode,
    update_product_money_exact,
)
from shopping_cli.api import auth as api_auth
from shopping_cli.api import idempotency
from shopping_cli.core.errors import (
    AuthError,
    ConflictError,
    IdempotencyConflict,
    NotFoundError,
    ValidationError,
)
from shopping_cli.core.harness import append_audit_event
from shopping_cli.core.tokens import token_digest
from shopping_cli.db.session import db_session, now_iso
from shopping_cli.services import tokens as token_service

from .common import (
    bool_from_query,
    public_merchant_summary,
    public_product_summary,
    require_field,
    result_limit,
    result_offset,
)
from shopping_cli.core import catalog_views

def health(db_path: str | Path) -> dict[str, Any]:
    profile = deployment_profile_from()
    checks: dict[str, Any] = production_config_checks()
    checks["database"] = "ok"
    ok = True
    try:
        validate_production_config()
    except ValueError:
        ok = False
    try:
        with db_session(db_path):
            pass
    except Exception:
        checks["database"] = "error"
        ok = False
    return {
        "ok": ok,
        "service": "shopping-cli-marketplace",
        "version": VERSION,
        "storage": "sqlite",
        "deployment_profile": profile,
        "checks": checks,
    }


def merchant_list(conn: Any, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    return catalog.list_merchants(conn, limit=int(limit), offset=int(offset))


def _server_idempotency_key(admin_token_hash: str, merchant_id: str) -> str:
    """Derive a server-side idempotency key so every bootstrap is replayable."""
    return f"auto:{admin_token_hash}:{merchant_id}"


def create_merchant(db_path: str | Path, payload: dict[str, Any], require_admin_token: Any) -> dict[str, Any]:
    require_admin_token(payload)
    merchant_id = str(require_field(payload, "id"))
    name = str(require_field(payload, "name"))
    admin_token = api_auth.payload_admin_token(payload)
    admin_token_hash = token_digest(admin_token)
    # Always use a deterministic idempotency key: honour the client-supplied key
    # when present, otherwise derive one from admin_token + merchant_id so every
    # bootstrap is replayable even when the response was lost mid-flight.
    client_key = idempotency.idempotency_key_from_payload(payload)
    idempotency_key = client_key or _server_idempotency_key(admin_token_hash, merchant_id)
    request_hash = idempotency.request_hash(
        {
            "id": merchant_id,
            "name": name,
            "city": str(payload.get("city") or ""),
            "service_area": str(payload.get("service_area") or ""),
            "contact": str(payload.get("contact") or ""),
            "hours": str(payload.get("hours") or ""),
            "automation_boundaries": str(payload.get("automation_boundaries") or ""),
            "tags": payload.get("tags") or [],
            "delivery_fee": payload.get("delivery_fee", 0),
            "delivery_eta_minutes": payload.get("delivery_eta_minutes", 0),
            "delivery_radius_km": payload.get("delivery_radius_km", 0),
        }
    )

    def replay(conn: Any) -> dict[str, Any] | None:
        row = conn.execute(
            """
            select request_hash, merchant_id from merchant_bootstrap_idempotency
            where admin_token_hash = ? and idempotency_key = ?
            """,
            (admin_token_hash, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if str(row["request_hash"]) != request_hash:
            raise IdempotencyConflict("idempotency key was reused with a different request")
        replay_merchant_id = str(row["merchant_id"] or merchant_id)
        token = deterministic_merchant_token(admin_token, idempotency_key, replay_merchant_id)
        token_row = conn.execute(
            "select revoked_at from api_tokens where token_hash = ?",
            (token_digest(token),),
        ).fetchone()
        if token_row is not None and str(token_row["revoked_at"] or ""):
            raise ConflictError(
                "merchant bootstrap token was rotated or revoked; "
                "use POST /merchants/{merchant_id}/token/recover to recover"
            )
        token_service.ensure_merchant_token(conn, token, replay_merchant_id)
        return {
            "ok": True,
            "merchant": catalog.merchant_summary(conn, replay_merchant_id),
            "merchant_token": token,
            "idempotent": True,
        }

    with db_session(db_path) as conn:
        replayed = replay(conn)
        if replayed is not None:
            return replayed
        try:
            conn.execute(
                """
                insert into merchant_bootstrap_idempotency(
                    admin_token_hash, idempotency_key, request_hash, merchant_id, created_at, updated_at
                ) values (?, ?, ?, ?, datetime('now'), datetime('now'))
                """,
                (admin_token_hash, idempotency_key, request_hash, merchant_id),
            )
        except sqlite3.IntegrityError:
            replayed = replay(conn)
            if replayed is not None:
                return replayed
            raise
        try:
            merchant = catalog.create_merchant(
                conn,
                merchant_id=merchant_id,
                name=name,
                city=str(payload.get("city") or ""),
                service_area=str(payload.get("service_area") or ""),
                contact=str(payload.get("contact") or ""),
                hours=str(payload.get("hours") or ""),
                automation_boundaries=str(payload.get("automation_boundaries") or ""),
                tags=payload.get("tags") or [],
                delivery_fee=payload.get("delivery_fee", 0),
                delivery_eta_minutes=payload.get("delivery_eta_minutes", 0),
                delivery_radius_km=payload.get("delivery_radius_km", 0),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(f"Merchant already exists: {merchant_id}") from exc
        token = deterministic_merchant_token(admin_token, idempotency_key, merchant["id"])
        token_service.ensure_merchant_token(conn, token, merchant["id"])
        return {"ok": True, "merchant": merchant, "merchant_token": token}


def deterministic_merchant_token(admin_token: str, idempotency_key: str, merchant_id: str) -> str:
    material = f"merchant-bootstrap\n{idempotency_key}\n{merchant_id}"
    digest = hmac.new(admin_token.encode("utf-8"), material.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"shopping_merchant_{digest}"


def rotate_merchant_token(
    db_path: str | Path,
    merchant_id: str,
    payload: dict[str, Any],
    require_admin_token: Any,
) -> dict[str, Any]:
    with db_session(db_path) as conn:
        catalog.require_merchant(conn, merchant_id)
        actor = "admin"
        try:
            require_admin_token(payload)
        except AuthError:
            token_row = token_service.require_merchant_token(conn, merchant_id, api_auth.payload_token(payload))
            actor = str(token_row["merchant_id"])
        token = token_service.rotate_merchant_token(conn, merchant_id, actor=actor)
        return {"ok": True, "merchant_id": merchant_id, "merchant_token": token, "rotated": True}


def recover_merchant_token(
    db_path: str | Path,
    merchant_id: str,
    payload: dict[str, Any],
    require_admin_token: Any,
) -> dict[str, Any]:
    """Admin recovery: issue a fresh merchant token when the bootstrap token was lost.

    This is the explicit recovery path promised by the bootstrap replay error.
    It revokes all existing merchant tokens and issues a replacement, so the
    caller does not need the lost token.
    """
    require_admin_token(payload)
    with db_session(db_path) as conn:
        catalog.require_merchant(conn, merchant_id)
        token = token_service.rotate_merchant_token(conn, merchant_id, actor="admin")
        return {
            "ok": True,
            "merchant_id": merchant_id,
            "merchant_token": token,
            "recovered": True,
        }


def revoke_merchant_tokens(
    db_path: str | Path,
    merchant_id: str,
    payload: dict[str, Any],
    require_admin_token: Any,
) -> dict[str, Any]:
    require_admin_token(payload)
    with db_session(db_path) as conn:
        catalog.require_merchant(conn, merchant_id)
        count = token_service.revoke_merchant_tokens(conn, merchant_id, actor="admin")
        return {"ok": True, "merchant_id": merchant_id, "revoked_count": count}


def update_merchant(
    db_path: str | Path,
    merchant_id: str,
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        merchant = catalog.update_merchant(
            conn,
            merchant_id=merchant_id,
            name=payload.get("name"),
            city=payload.get("city"),
            service_area=payload.get("service_area"),
            contact=payload.get("contact"),
            hours=payload.get("hours"),
            automation_boundaries=payload.get("automation_boundaries"),
            tags=payload.get("tags") if "tags" in payload else None,
            delivery_fee=payload.get("delivery_fee"),
            delivery_eta_minutes=payload.get("delivery_eta_minutes"),
            delivery_radius_km=payload.get("delivery_radius_km"),
        )
        return {"ok": True, "merchant": merchant}


def get_merchant(db_path: str | Path, merchant_id: str) -> dict[str, Any]:
    with db_session(db_path) as conn:
        return {"ok": True, "merchant": public_merchant_summary(catalog.merchant_summary(conn, merchant_id))}


def get_merchant_private_config(
    db_path: str | Path,
    merchant_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Return private automation policy to an authenticated merchant/agent."""
    with db_session(db_path) as conn:
        merchant = catalog.require_merchant(conn, merchant_id)
        token_row = token_service.require_api_token(
            conn,
            api_auth.payload_token(payload),
            "merchant or agent token required",
        )
        if token_row["merchant_id"] != merchant_id or token_row["role"] not in {"merchant", "agent"}:
            raise AuthError("invalid merchant or agent token")
        actor = str(token_row["agent_id"] or merchant_id)
        version = str(merchant["updated_at"] or "")
        append_audit_event(
            conn,
            "",
            actor,
            "merchant_automation_boundaries_loaded",
            {"merchant_id": merchant_id, "version": version},
        )
        return {
            "ok": True,
            "merchant_id": merchant_id,
            "automation_boundaries": str(merchant["automation_boundaries"] or ""),
            "version": version,
        }

def list_merchants(db_path: str | Path, query: dict[str, Any] | None = None) -> dict[str, Any]:
    query = query or {}
    with db_session(db_path) as conn:
        return {
            "ok": True,
            "results": [
                public_merchant_summary(merchant)
                for merchant in merchant_list(
                    conn,
                    limit=result_limit(query.get("limit")),
                    offset=result_offset(query.get("offset")),
                )
            ],
        }


def create_product(
    db_path: str | Path,
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    with db_session(db_path) as conn:
        merchant_id = str(require_field(payload, "merchant_id"))
        require_merchant_token(conn, merchant_id, payload)
        product = catalog.create_product(
            conn,
            merchant_id=merchant_id,
            sku=str(require_field(payload, "sku")),
            title=str(require_field(payload, "title")),
            price=require_field(payload, "price"),
            stock=require_field(payload, "stock"),
            currency=str(payload.get("currency") or "CNY"),
            category=str(payload.get("category") or ""),
            tags=payload.get("tags") or [],
            description=str(payload.get("description") or ""),
            delivery_attributes=payload.get("delivery_attributes") or [],
            handoff_destination=str(payload.get("handoff_destination") or ""),
            floor_price=payload.get("floor_price", 0.0),
            max_discount_percent=payload.get("max_discount_percent", 0.0),
            promotions=payload.get("promotions"),
        )
        return {"ok": True, "product": product}


def update_product(
    db_path: str | Path,
    sku: str,
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    with db_session(db_path) as conn:
        existing = catalog.product_summary(conn, sku)
        merchant_id = str(payload.get("merchant_id") or existing["merchant_id"])
        require_merchant_token(conn, merchant_id, payload)
        product = catalog.update_product(
            conn,
            sku=sku,
            merchant_id=merchant_id,
            title=payload.get("title"),
            price=payload.get("price"),
            stock=payload.get("stock"),
            currency=payload.get("currency"),
            category=payload.get("category"),
            tags=payload.get("tags") if "tags" in payload else None,
            description=payload.get("description"),
            delivery_attributes=payload.get("delivery_attributes") if "delivery_attributes" in payload else None,
            handoff_destination=payload.get("handoff_destination")
            if "handoff_destination" in payload
            else None,
            floor_price=payload.get("floor_price"),
            max_discount_percent=payload.get("max_discount_percent"),
            promotions=payload.get("promotions") if "promotions" in payload else None,
        )
        return {"ok": True, "product": product}


def _exact_product_projection(conn: Any, merchant_id: str, sku: str) -> dict[str, Any]:
    product = catalog.product_summary(conn, sku)
    if str(product.get("merchant_id") or "") != merchant_id:
        raise NotFoundError(f"Unknown product SKU for merchant: {sku}")
    money = exact_product_money(conn, merchant_id, sku)
    # listing_paused 只进 v1 面（owner 鉴权）投影：销售状态是商家的事实字段，
    # v1 写面（上下架端点、执行后回读 verifyAfter）都依赖它能被读回；公开读面
    # （public_product_summary）不携带。
    paused_row = conn.execute(
        "select listing_paused from products where sku=?", (sku,)
    ).fetchone()
    return {
        "sku": str(product["sku"]),
        "merchant_id": merchant_id,
        "title": str(product["title"]),
        "description": str(product.get("description") or ""),
        "category": str(product.get("category") or ""),
        "tags": list(product.get("tags") or []),
        "stock": int(product.get("stock") or 0),
        "listing_paused": bool(paused_row["listing_paused"]) if paused_row else False,
        "currency": money.currency,
        "price_minor": money.price_minor,
        "currency_table_version": CURRENCY_TABLE_VERSION,
        "authority_version": money.authority_version,
        "delivery_attributes": list(product.get("delivery_attributes") or []),
        "handoff_destination": str(product.get("handoff_destination") or ""),
    }


def list_products_exact(
    db_path: str | Path,
    query: dict[str, Any],
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    merchant_id = str(require_field(query, "merchant_id"))
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        mode, authority_version = money_mode(conn, merchant_id)
        if mode != "EXACT_MINOR":
            raise ConflictError("exact money is not authoritative for this merchant")
        limit = result_limit(query.get("limit"), default=50)
        offset = result_offset(query.get("offset"))
        rows = conn.execute(
            "select sku from products where merchant_id=? order by sku limit ? offset ?",
            (merchant_id, limit + 1, offset),
        ).fetchall()
        items = [_exact_product_projection(conn, merchant_id, str(row["sku"])) for row in rows[:limit]]
        total = int(
            conn.execute(
                "select count(*) count from products where merchant_id=?",
                (merchant_id,),
            ).fetchone()["count"]
        )
        return {
            "ok": True,
            "merchant_id": merchant_id,
            "money_mode": mode,
            "authority_version": authority_version,
            "currency_table_version": CURRENCY_TABLE_VERSION,
            "items": items,
            "total": total,
            "next_offset": offset + limit if len(rows) > limit else None,
        }


def get_product_exact(
    db_path: str | Path,
    sku: str,
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    merchant_id = str(require_field(payload, "merchant_id"))
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        return {"ok": True, "product": _exact_product_projection(conn, merchant_id, sku)}


def _operation_id(payload: dict[str, Any]) -> str:
    value = str(require_field(payload, "operation_id")).strip()
    if len(value) > 200:
        raise ValidationError("operation_id must be <= 200 characters")
    return value


def _operation_projection(row: Any) -> dict[str, Any]:
    return {
        "operation_id": str(row["operation_id"]),
        "merchant_id": str(row["merchant_id"]),
        "operation_kind": str(row["operation_kind"]),
        "sku": str(row["sku"]),
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
    }


def _operation_replay(
    conn: Any,
    *,
    operation_id: str,
    merchant_id: str,
    request_hash: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        "select * from merchant_product_operations where operation_id=?",
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    if str(row["merchant_id"]) != merchant_id or str(row["request_hash"]) != request_hash:
        raise IdempotencyConflict("operation_id was reused with a different merchant or request")
    response = json.loads(str(row["response_json"]))
    if not isinstance(response, dict):
        raise ConflictError("stored operation receipt is invalid")
    return {**response, "operation": _operation_projection(row), "idempotent": True}


def _record_product_operation(
    conn: Any,
    *,
    operation_id: str,
    merchant_id: str,
    operation_kind: str,
    sku: str,
    request_hash: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    created_at = now_iso()
    conn.execute(
        """
        insert into merchant_product_operations(
            operation_id, merchant_id, operation_kind, sku, request_hash,
            status, response_json, created_at
        ) values(?, ?, ?, ?, ?, 'succeeded', ?, ?)
        """,
        (
            operation_id,
            merchant_id,
            operation_kind,
            sku,
            request_hash,
            json.dumps(response, ensure_ascii=False, sort_keys=True),
            created_at,
        ),
    )
    row = conn.execute(
        "select * from merchant_product_operations where operation_id=?",
        (operation_id,),
    ).fetchone()
    return _operation_projection(row)


def get_product_operation_exact(
    db_path: str | Path,
    operation_id: str,
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    merchant_id = str(require_field(payload, "merchant_id"))
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        row = conn.execute(
            "select * from merchant_product_operations where operation_id=? and merchant_id=?",
            (operation_id, merchant_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Unknown merchant product operation: {operation_id}")
        response = json.loads(str(row["response_json"]))
        return {
            "ok": True,
            "operation": _operation_projection(row),
            "result": response if isinstance(response, dict) else {},
        }


def create_product_exact_api(
    db_path: str | Path,
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    merchant_id = str(require_field(payload, "merchant_id"))
    operation_id = _operation_id(payload)
    sku = str(require_field(payload, "sku"))
    _require_exact_currency_table(payload)
    request_hash = idempotency.request_hash(
        {
            "operation_kind": "exact_product_create",
            "merchant_id": merchant_id,
            "sku": sku,
            "title": str(require_field(payload, "title")),
            "price_minor": str(require_field(payload, "price_minor")),
            "floor_price_minor": str(payload.get("floor_price_minor") or "0"),
            "stock": int(require_field(payload, "stock")),
            "currency": str(payload.get("currency") or "CNY"),
            "currency_table_version": str(payload.get("currency_table_version") or ""),
            "expected_authority_version": int(require_field(payload, "expected_authority_version")),
            "category": str(payload.get("category") or ""),
            "tags": payload.get("tags") or [],
            "description": str(payload.get("description") or ""),
            "delivery_attributes": payload.get("delivery_attributes") or [],
            "handoff_destination": str(payload.get("handoff_destination") or ""),
        }
    )
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        conn.execute("begin immediate")
        replay = _operation_replay(
            conn,
            operation_id=operation_id,
            merchant_id=merchant_id,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        product = catalog.create_product_exact(
            conn,
            merchant_id,
            sku,
            str(require_field(payload, "title")),
            str(require_field(payload, "price_minor")),
            int(require_field(payload, "stock")),
            expected_authority_version=int(require_field(payload, "expected_authority_version")),
            floor_price_minor=str(payload.get("floor_price_minor") or "0"),
            currency=str(payload.get("currency") or "CNY"),
            category=str(payload.get("category") or ""),
            tags=payload.get("tags") or [],
            description=str(payload.get("description") or ""),
            delivery_attributes=payload.get("delivery_attributes") or [],
            handoff_destination=str(payload.get("handoff_destination") or ""),
        )
        response = {
            "ok": True,
            "product": _exact_product_projection(conn, merchant_id, str(product["sku"])),
            "idempotent": False,
        }
        response["operation"] = _record_product_operation(
            conn,
            operation_id=operation_id,
            merchant_id=merchant_id,
            operation_kind="exact_product_create",
            sku=sku,
            request_hash=request_hash,
            response=response,
        )
        return response


def update_product_inventory_exact_api(
    db_path: str | Path,
    sku: str,
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    """v1 库存写入：与副作用**同事务**落 operation receipt。

    **为什么需要新端点**：库存写入此前只走 legacy ``PATCH /products/{sku}``
    （``update_product`` 接受 ``stock``），**没有可对账的回执**——一旦落进 UNKNOWN
    就无法查明副作用是否真的发生过。不能拿**当前**库存值去猜**历史**操作的结果。

    与 exact 商品写入的口径一致：统一前置（``currency_table_version``）、商家鉴权、
    ``begin immediate`` 原子、相同 ``operation_id`` 幂等重放、不同请求冲突拒绝、回执
    与效果同事务。

    **不要求 ``expected_authority_version``**：金额权威（``merchant_money_authority``）
    只管钱，库存不在其管辖内。这一点与改价端点不同，是刻意的。
    """
    merchant_id = str(require_field(payload, "merchant_id"))
    operation_id = _operation_id(payload)
    _require_exact_currency_table(payload)
    stock = int(require_field(payload, "stock"))
    request_hash = idempotency.request_hash(
        {
            "operation_kind": "product_inventory_update",
            "merchant_id": merchant_id,
            "sku": sku,
            "stock": stock,
        }
    )
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        conn.execute("begin immediate")
        replay = _operation_replay(
            conn,
            operation_id=operation_id,
            merchant_id=merchant_id,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        # set_stock 自带归属校验、搜索索引同步与审计留痕——失败即抛，回执不会落库，
        # 于是「有回执」严格等价于「效果已提交」。
        catalog.set_stock(conn, sku, stock, merchant_id)
        response = {
            "ok": True,
            "product": _exact_product_projection(conn, merchant_id, sku),
            "idempotent": False,
        }
        response["operation"] = _record_product_operation(
            conn,
            operation_id=operation_id,
            merchant_id=merchant_id,
            operation_kind="product_inventory_update",
            sku=sku,
            request_hash=request_hash,
            response=response,
        )
        return response


def update_product_listing_exact_api(
    db_path: str | Path,
    sku: str,
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    """v1 上下架写入（暂停/恢复销售）：与副作用**同事务**落 operation receipt。

    **为什么需要新端点**：上下架此前在数据模型里**根本不存在**——``listings/*``
    全是从 products 派生的只读投影，没有任何 paused 状态可写；kiwi 侧工具只能
    fail-closed 报「不可得」（刻意不用库存写零伪装下架，语义不同且会污染库存
    事实）。没有真实状态可写，就没有可对账的回执——外部写一旦落进 UNKNOWN 就
    无法查明副作用是否真的发生过。

    与库存端点同一口径：统一前置（``currency_table_version``）、商家鉴权、
    ``begin immediate`` 原子、相同 ``operation_id`` 幂等重放、不同请求冲突拒绝、
    回执与效果同事务。响应投影携带 ``listing_paused`` 目标态，使下游对账
    （queryOutcome）能核对回执与请求语义一致，而非只看状态 succeeded。

    **不要求 ``expected_authority_version``**：金额权威只管钱，销售状态不在其
    管辖内（与库存端点同理，刻意）。
    """
    merchant_id = str(require_field(payload, "merchant_id"))
    operation_id = _operation_id(payload)
    _require_exact_currency_table(payload)
    paused_raw = require_field(payload, "paused")
    if not isinstance(paused_raw, bool):
        # 语义字段必须严格 bool：宽松的 truthy 转换（如 "false"→True）会让
        # 商家「恢复销售」被静默执行成「暂停销售」，方向反了比不写更糟。
        raise ValidationError("paused must be a boolean")
    paused = paused_raw
    request_hash = idempotency.request_hash(
        {
            "operation_kind": "product_listing_change",
            "merchant_id": merchant_id,
            "sku": sku,
            "paused": paused,
        }
    )
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        conn.execute("begin immediate")
        replay = _operation_replay(
            conn,
            operation_id=operation_id,
            merchant_id=merchant_id,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        # set_listing_paused 自带归属校验、搜索索引同步与审计留痕——失败即抛，
        # 回执不会落库，于是「有回执」严格等价于「效果已提交」。
        catalog.set_listing_paused(conn, sku, paused, merchant_id)
        # 投影自带 listing_paused（v1 面统一携带），下游对账据此核对 paused 目标态。
        response = {
            "ok": True,
            "product": _exact_product_projection(conn, merchant_id, sku),
            "idempotent": False,
        }
        response["operation"] = _record_product_operation(
            conn,
            operation_id=operation_id,
            merchant_id=merchant_id,
            operation_kind="product_listing_change",
            sku=sku,
            request_hash=request_hash,
            response=response,
        )
        return response


def update_product_money_exact_api(
    db_path: str | Path,
    sku: str,
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    merchant_id = str(require_field(payload, "merchant_id"))
    operation_id = _operation_id(payload)
    _require_exact_currency_table(payload)
    request_hash = idempotency.request_hash(
        {
            "operation_kind": "exact_product_money_update",
            "merchant_id": merchant_id,
            "sku": sku,
            "price_minor": str(require_field(payload, "price_minor")),
            "currency_table_version": str(payload.get("currency_table_version") or ""),
            "expected_authority_version": int(require_field(payload, "expected_authority_version")),
        }
    )
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        conn.execute("begin immediate")
        replay = _operation_replay(
            conn,
            operation_id=operation_id,
            merchant_id=merchant_id,
            request_hash=request_hash,
        )
        if replay is not None:
            return replay
        current = exact_product_money(conn, merchant_id, sku)
        update_product_money_exact(
            conn,
            merchant_id=merchant_id,
            sku=sku,
            expected_authority_version=int(require_field(payload, "expected_authority_version")),
            price_minor=str(require_field(payload, "price_minor")),
            # The HTTP Workbench path changes the public price only. Private floor
            # remains server-side and cannot enter a general candidate/action snapshot.
            floor_price_minor=current.floor_price_minor,
        )
        response = {
            "ok": True,
            "product": _exact_product_projection(conn, merchant_id, sku),
            "idempotent": False,
        }
        response["operation"] = _record_product_operation(
            conn,
            operation_id=operation_id,
            merchant_id=merchant_id,
            operation_kind="exact_product_money_update",
            sku=sku,
            request_hash=request_hash,
            response=response,
        )
        return response


def _require_exact_currency_table(payload: dict[str, Any]) -> None:
    if str(payload.get("currency_table_version") or "") != CURRENCY_TABLE_VERSION:
        raise ValidationError(
            f"currency_table_version must be {CURRENCY_TABLE_VERSION}"
        )


def _owner_merchant_from_payload(conn: Any, payload: dict[str, Any] | None) -> str:
    """从 Authorization Bearer token 解析合法商户/agent 身份的 merchant_id。

    读路径（GET /products/{sku}、/search/products）的精确库存只向商品所属
    商户本人开放（design v0.3 §7 private inventory，审查 P2-1）。匿名或
    token 无效/吊销/过期一律返回 ""——公开读继续可用，只是投影降级为
    availability_hint，不因无效凭据拒绝公开读。
    """
    token = str((payload or {}).get("_auth_token") or "")
    if not token:
        return ""
    try:
        row = token_service.require_api_token(conn, token, "invalid merchant read token")
    except AuthError:
        return ""
    if row is None:
        return ""
    try:
        role = str(row["role"] or "")
        merchant_id = str(row["merchant_id"] or "")
    except (KeyError, IndexError):
        return ""
    if role not in ("merchant", "agent"):
        return ""
    return merchant_id


def get_product(db_path: str | Path, sku: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    with db_session(db_path) as conn:
        product = catalog.product_summary(conn, sku)
        owner = _owner_merchant_from_payload(conn, payload)
        if owner and owner == str(product.get("merchant_id") or ""):
            return {"ok": True, "product": catalog_views.merchant_product_summary(product)}
        return {"ok": True, "product": catalog_views.public_product_summary(product)}


def search_products(
    db_path: str | Path, query: dict[str, Any], payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    max_price = query.get("max_price")
    with db_session(db_path) as conn:
        owner = _owner_merchant_from_payload(conn, payload)
        results = [
            (
                catalog_views.merchant_product_summary(product)
                if owner and owner == str(product.get("merchant_id") or "")
                else public_product_summary(product)
            )
            for product in catalog.search_products(
                conn,
                query=str(query.get("query") or ""),
                city=str(query.get("city") or ""),
                area=str(query.get("area") or ""),
                max_price=max_price if str(max_price or "") else None,
                include_out_of_stock=bool_from_query(query.get("include_out_of_stock")),
                limit=result_limit(query.get("limit"), default=10),
                offset=result_offset(query.get("offset")),
            )
        ]
        return {"ok": True, "results": results}


def search_merchants(db_path: str | Path, query: dict[str, Any]) -> dict[str, Any]:
    with db_session(db_path) as conn:
        return {
            "ok": True,
            "results": [
                public_merchant_summary(merchant)
                for merchant in catalog.search_merchants(
                    conn,
                    query=str(query.get("query") or ""),
                    city=str(query.get("city") or ""),
                    limit=result_limit(query.get("limit"), default=10),
                    offset=result_offset(query.get("offset")),
                )
            ],
        }

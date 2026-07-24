"""Catalog and marketplace HTTP handlers."""

from __future__ import annotations

from pathlib import Path
import hashlib
import hmac
import sqlite3
from typing import Any

from shopping_cli import VERSION
from shopping_cli.config import deployment_profile_from, production_config_checks, validate_production_config
from shopping_cli.core import catalog
from shopping_cli.api import auth as api_auth
from shopping_cli.api import idempotency
from shopping_cli.core.errors import AuthError, ConflictError, IdempotencyConflict
from shopping_cli.core.harness import append_audit_event
from shopping_cli.core.tokens import token_digest
from shopping_cli.db.session import db_session
from shopping_cli.services import tokens as token_service

from .common import (
    bool_from_query,
    public_merchant_summary,
    public_product_summary,
    require_field,
    result_limit,
    result_offset,
)


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


def create_merchant(db_path: str | Path, payload: dict[str, Any], require_admin_token: Any) -> dict[str, Any]:
    require_admin_token(payload)
    merchant_id = str(require_field(payload, "id"))
    name = str(require_field(payload, "name"))
    idempotency_key = idempotency.idempotency_key_from_payload(payload)
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
    admin_token = api_auth.payload_admin_token(payload)
    admin_token_hash = token_digest(admin_token)

    def replay(conn: Any) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
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
                "merchant bootstrap token was rotated; use the admin token recovery endpoint"
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
        if idempotency_key:
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
        token = (
            deterministic_merchant_token(admin_token, idempotency_key, merchant["id"])
            if idempotency_key
            else token_service.issue_merchant_token(conn, merchant["id"])
        )
        if idempotency_key:
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
        )
        return {"ok": True, "product": product}


def get_product(db_path: str | Path, sku: str) -> dict[str, Any]:
    with db_session(db_path) as conn:
        return {"ok": True, "product": public_product_summary(catalog.product_summary(conn, sku))}


def search_products(db_path: str | Path, query: dict[str, Any]) -> dict[str, Any]:
    max_price = query.get("max_price")
    with db_session(db_path) as conn:
        return {
            "ok": True,
            "results": [
                public_product_summary(product)
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
            ],
        }


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

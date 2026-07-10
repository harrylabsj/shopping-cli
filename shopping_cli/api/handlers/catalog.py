"""Catalog and marketplace HTTP handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shopping_cli import VERSION
from shopping_cli.config import deployment_profile_from, production_config_checks, validate_production_config
from shopping_cli.core import catalog
from shopping_cli.db.session import db_session
from shopping_cli.services import tokens as token_service

from .common import (
    bool_from_query,
    float_or_none,
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
    with db_session(db_path) as conn:
        merchant = catalog.create_merchant(
            conn,
            merchant_id=str(require_field(payload, "id")),
            name=str(require_field(payload, "name")),
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
        token = token_service.issue_merchant_token(conn, merchant["id"])
        return {"ok": True, "merchant": merchant, "merchant_token": token}


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
            delivery_fee=float_or_none(payload.get("delivery_fee")),
            delivery_eta_minutes=payload.get("delivery_eta_minutes"),
            delivery_radius_km=float_or_none(payload.get("delivery_radius_km")),
        )
        return {"ok": True, "merchant": merchant}


def get_merchant(db_path: str | Path, merchant_id: str) -> dict[str, Any]:
    with db_session(db_path) as conn:
        return {"ok": True, "merchant": public_merchant_summary(catalog.merchant_summary(conn, merchant_id))}


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
            price=float_or_none(payload.get("price")),
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

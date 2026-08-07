"""Listing 投影只读 API（shopping-cli v0.3 §14；Merchant Kiwi-compatible）。

- GET /v1/merchant/listings/projections —— 可发布投影列表（public-only）；
- GET /v1/merchant/listings/{sku}/projection —— 单条投影。

公开读免鉴权（沿用 products 现状）；发布动作只经 CLI 不进 API（评审：
写面保持 push-first 单向，API 不暴露发布副作用）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shopping_cli.core.errors import NotFoundError, ValidationError
from shopping_cli.db.session import db_session
from shopping_cli.listings.projection import (
    ProjectionError,
    list_publishable_listings,
    project_product_listing,
)


def list_listing_projections(db_path: str | Path, query: dict[str, Any]) -> dict[str, Any]:
    """GET /v1/merchant/listings/projections —— 可发布投影（public-only）。"""
    merchant_id = str(query.get("merchant_id") or "").strip()
    with db_session(db_path) as conn:
        projections = list_publishable_listings(conn, merchant_id=merchant_id)
        return {"ok": True, "results": projections, "count": len(projections)}


def get_listing_projection(db_path: str | Path, sku: str) -> dict[str, Any]:
    """GET /v1/merchant/listings/{sku}/projection —— 单条投影。"""
    sku = str(sku).strip()
    if not sku:
        raise ValidationError("sku is required")
    with db_session(db_path) as conn:
        try:
            projection = project_product_listing(conn, sku)
        except ProjectionError as exc:
            raise NotFoundError(str(exc)) from exc
        return {"ok": True, "projection": projection}

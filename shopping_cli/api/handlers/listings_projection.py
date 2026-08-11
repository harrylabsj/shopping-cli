"""Listing 投影只读 API（shopping-cli v0.3 §14；Merchant Kiwi-compatible）。

- GET /v1/merchant/listings/projections —— 可发布投影列表（public-only）；
- GET /v1/merchant/listings/{sku}/projection —— 单条投影。

公开读免鉴权（沿用 products 现状）；发布动作只经 CLI 不进 API（评审：
写面保持 push-first 单向，API 不暴露发布副作用）。

handoff_destination 是商家私有成交入口（审查 P2-B）：匿名/非本人读一律
剥离；仅持商品所属商户本人有效 token 的调用保留（kiwi merchant agent
发布/成交取数路径）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shopping_cli.api.handlers.catalog import _owner_merchant_from_payload
from shopping_cli.core.errors import NotFoundError, ValidationError
from shopping_cli.db.session import db_session
from shopping_cli.listings.projection import (
    ProjectionError,
    list_publishable_listings,
    project_product_listing,
)


def list_listing_projections(
    db_path: str | Path, query: dict[str, Any], payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """GET /v1/merchant/listings/projections —— 可发布投影（public-only）。

    必须带 ``merchant_id`` 过滤（审查 P3-01：无过滤的匿名枚举会暴露全部
    商家的可发布投影）；匿名或 token 不属于该商户本人时剥离
    handoff_destination（审查 P2-B）。
    """
    merchant_id = str(query.get("merchant_id") or "").strip()
    if not merchant_id:
        raise ValidationError("merchant_id is required")
    with db_session(db_path) as conn:
        owner = _owner_merchant_from_payload(conn, payload)
        projections = list_publishable_listings(conn, merchant_id=merchant_id)
        if not (owner and owner == merchant_id):
            for projection in projections:
                projection.pop("handoff_destination", None)
        return {"ok": True, "results": projections, "count": len(projections)}


def get_listing_projection(db_path: str | Path, sku: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET /v1/merchant/listings/{sku}/projection —— 单条投影。"""
    sku = str(sku).strip()
    if not sku:
        raise ValidationError("sku is required")
    with db_session(db_path) as conn:
        try:
            projection = project_product_listing(conn, sku)
        except ProjectionError as exc:
            raise NotFoundError(str(exc)) from exc
        owner = _owner_merchant_from_payload(conn, payload)
        row = conn.execute("select merchant_id from products where sku = ?", (sku,)).fetchone()
        if not (owner and row is not None and owner == str(row["merchant_id"] or "")):
            projection.pop("handoff_destination", None)
        return {"ok": True, "projection": projection}

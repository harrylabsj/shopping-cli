"""PublicListingProjection（shopping-cli v0.3 §14/§16；DoD #1-#3）。

public-only 白名单投影：把 products 行压缩为可公开的 discovery projection。
**绝不输出** cost / floor price / 私密库存 / credentials / 客户特定条款
（v0.3 §16 不得输出清单；DoD #2 有回归测试锁定）。

availability_hint / price_range_hint 必须携带 provenance 并注明只是
discovery hint（v0.3 §14）——权威值以本地 products 行为准（v3.0 起
发布面已随 kiwi-catalog 子系统迁移至独立服务）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from shopping_cli.data_sources.erp_source import SOURCE_LOCAL


class ProjectionError(Exception):
    """投影失败（fail-closed：未知 SKU / 私有字段泄漏风险抛本异常）。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hint_price_range(price: float, currency: str) -> str:
    """price_range_hint：粗粒度公开价格提示（不是报价）。"""
    return f"{currency} {price:.0f}"


def _product_row(conn: sqlite3.Connection, sku: str) -> dict[str, Any]:
    row = conn.execute(
        "select * from products where sku = ?", (str(sku).strip(),)
    ).fetchone()
    if row is None:
        raise ProjectionError(f"no product with sku {sku}")
    return dict(row)


def project_product_listing(
    conn: sqlite3.Connection,
    sku: str,
    *,
    merchant_id: str = "",
    include_private: bool = False,
) -> dict[str, Any]:
    """ProductListing projection（v0.4 §4 wire 形状的 public-only 子集）。

    Returns canonical publish payload（原 kiwi-catalog POST /v1/listings/publish 输入；
    的输入；owner_agent_id 由发布方（Merchant Kiwi）在 publish 时绑定——
    projection 不持有 agent 身份）。

    审查 S-M3：``handoff_destination``（KTH 成交入口）是商家私有字段，投影层
    **缺省剥离**（``include_private=False``），仅 owner 显式 opt-in 保留。此前
    剥离依赖调用方，CLI ``--format json`` 原样输出即泄漏成交入口。
    """
    row = _product_row(conn, sku)
    if merchant_id and row["merchant_id"] != merchant_id:
        raise ProjectionError(f"product {sku} belongs to merchant {row['merchant_id']}, not {merchant_id}")

    source = str(row["source"] or SOURCE_LOCAL)
    authority = "LOCAL_AUTHORITATIVE" if source == SOURCE_LOCAL else "UPSTREAM_PROXY"
    now = _now_iso()

    projection: dict[str, Any] = {
        "listing_type": "product",
        "source_product_ref": str(row["sku"]),
        "source_revision": str(row.get("source_revision") or f"local:{row.get('updated_at', '')}"),
        "title": str(row["title"]),
        "category": str(row.get("category") or ""),
        "regions": [],
        "tags": json.loads(row.get("tags_json") or "[]"),
        "commercial_hints": {
            "price_range_hint": _hint_price_range(float(row["price"]), str(row.get("currency") or "CNY")),
            "availability_hint": "in_stock" if int(row.get("stock", 0)) > 0 else "out_of_stock",
            "moq": 1,
            "supports_bulk_quote": True,
        },
    }
    # 每商品成交入口（KTH destination_ref）：商家自行维护，publish 时同步进
    # catalog listing 的 handoff_destination_ref。审查 S-M3：私有字段，缺省
    # 剥离；仅 owner（include_private=True）保留。
    if include_private:
        projection["handoff_destination"] = str(row.get("handoff_destination") or "")
    description = str(row.get("description") or "")
    if description:
        projection["summary"] = description

    # provenance 标注：availability/price 是 discovery hint，不是权威事实（v0.3 §14）。
    # 只存在于 projection（Merchant Kiwi 可见）（wire commercial_hints 七键白名单，v0.4 §4.1）。
    projection["_provenance"] = {
        "authority": authority,
        "source_revision": projection["source_revision"],
        "observed_at": str(row.get("observed_at") or now),
        "fresh_until": str(row.get("fresh_until") or ""),
        "note": "discovery hint only; authoritative value in the local product record",
    }
    return projection


def project_capability_listing(
    conn: sqlite3.Connection,
    capability_ref: str,
    *,
    merchant_id: str = "",
    title: str = "",
    summary: str = "",
    category: str = "manufacturing-services",
) -> dict[str, Any]:
    """CapabilityListing projection（v0.4 §5：publisher_listing_key=capability_ref，
    不虚构 SKU；本轮为 Merchant 手工声明的供给能力）。"""
    if not capability_ref.strip():
        raise ProjectionError("capability_ref must not be empty")
    projection: dict[str, Any] = {
        "listing_type": "capability",
        "publisher_listing_key": str(capability_ref).strip(),
        "title": str(title).strip() or str(capability_ref).strip(),
        "category": str(category).strip(),
        "commercial_hints": {
            "supports_customization": True,
            "moq": 1,
        },
    }
    if summary.strip():
        projection["summary"] = str(summary).strip()
    return projection


def strip_provenance(projection: dict[str, Any]) -> dict[str, Any]:
    """发布前剥离 provenance 元数据（wire commercial_hints 白名单，v0.4 §4.1）。"""
    result = {key: value for key, value in projection.items() if key != "_provenance"}
    return result


def list_publishable_listings(
    conn: sqlite3.Connection,
    *,
    merchant_id: str = "",
    include_private: bool = False,
) -> list[dict[str, Any]]:
    """可发布的商品清单（active=1；products.active=0 即 withdraw 信号，DoD #5）。

    Returns projection dicts（不含 withdraw 项）。审查 S-M3：私有字段
    （handoff_destination）缺省剥离，owner 显式 include_private=True 保留。
    """
    rows = conn.execute(
        "select * from products where active = 1 order by sku",
    ).fetchall()
    projections: list[dict[str, Any]] = []
    for row in rows:
        if merchant_id and row["merchant_id"] != merchant_id:
            continue
        projections.append(
            project_product_listing(
                conn, row["sku"], merchant_id=merchant_id, include_private=include_private
            )
        )
    return projections

"""CommerceDataSource 本地适配器（shopping-cli v0.3 §4/§5；MVP #3/#4）。

- ``LocalCommerceDataSource``：读 products 表（source='local' 行）——
  LOCAL_AUTHORITATIVE；
- ``ErpCommerceDataSource``：读 products 表（source='erp' 行，由
  erp_source.sync_erp_products 同步）——UPSTREAM_PROXY_WRITE（适配器只读，
  写意图 fail-closed）。

同字段双源冲突 → AuthorityConflictError（v0.3 §5：不静默合并冲突权威源）。
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
from typing import Any

from shopping_cli.commerce.data_source import (
    LOCAL_AUTHORITATIVE,
    UPSTREAM_PROXY_WRITE,
    AuthorityConflictError,
    CommerceField,
    ProductFact,
)
from shopping_cli.data_sources.erp_source import AUTHORITY_ERP, SOURCE_ERP, SOURCE_LOCAL


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _minor_from_yuan(price: float) -> int:
    """元 → minor（两位小数；kiwi 侧 data-source.ts 的 元→minor 约定）。

    审查 BUG-04：Decimal(str()) 精确校验——int(round(price*100)) 会把
    19.995 静默改写后进入报价；无法精确表达或超出两位小数精度 → fail-closed
    （抛 ValueError，绝不静默舍入）。
    """
    from decimal import Decimal, InvalidOperation

    try:
        amount = Decimal(str(price))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"price is not a finite decimal: {price!r}") from exc
    scaled = amount * 100
    if scaled != scaled.to_integral_value():
        raise ValueError(f"price {price!r} exceeds 2-decimal precision (lossy)")
    return int(scaled)


def _lead_days_from_eta(conn: sqlite3.Connection, merchant_id: str) -> int:
    """从 delivery_rules.eta_minutes 派生 lead_days（products 无该列——
    此前恒 0 是谎报）。eta 向上取整为天；无配送规则 → 0。"""
    row = conn.execute(
        "select eta_minutes from delivery_rules where merchant_id = ?",
        (merchant_id,),
    ).fetchone()
    eta_minutes = int(row["eta_minutes"] or 0) if row is not None else 0
    if eta_minutes <= 0:
        return 0
    return max(1, math.ceil(eta_minutes / 1440))


class LocalCommerceDataSource:
    """本地录入数据源（LOCAL_AUTHORITATIVE，v0.3 §5）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _row(self, sku: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "select * from products where sku = ? and source = ?", (sku, SOURCE_LOCAL)
        ).fetchone()
        return dict(row) if row is not None else None

    def get_product(self, sku: str) -> ProductFact | None:
        row = self._row(sku)
        if row is None:
            return None
        return ProductFact(
            sku=row["sku"],
            title=row["title"],
            category=row["category"],
            price_minor=_minor_from_yuan(row["price"]),
            currency=row["currency"],
            stock=row["stock"],
            merchant_id=row["merchant_id"],
        )

    def search_products(self, query: str = "", limit: int = 50) -> list[ProductFact]:
        limit = max(1, min(int(limit), 100))
        if query:
            rows = self._conn.execute(
                "select * from products where source = ? and (title like ? or sku like ?)"
                " order by updated_at desc limit ?",
                (SOURCE_LOCAL, f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "select * from products where source = ? order by updated_at desc limit ?",
                (SOURCE_LOCAL, limit),
            ).fetchall()
        return [self._to_fact(dict(row)) for row in rows]

    def _to_fact(self, row: dict[str, Any]) -> ProductFact:
        return ProductFact(
            sku=row["sku"],
            title=row["title"],
            category=row["category"],
            price_minor=_minor_from_yuan(row["price"]),
            currency=row["currency"],
            stock=row["stock"],
            merchant_id=row["merchant_id"],
        )

    def get_inventory(self, sku: str) -> CommerceField[int] | None:
        row = self._row(sku)
        if row is None:
            return None
        return CommerceField(
            value=int(row["stock"]),
            authority_source=SOURCE_LOCAL,
            source_revision=row["source_revision"],
            observed_at=row["observed_at"],
            fresh_until=row["fresh_until"],
            write_mode=LOCAL_AUTHORITATIVE,
        )

    def get_price(self, sku: str) -> CommerceField[int] | None:
        row = self._row(sku)
        if row is None:
            return None
        return CommerceField(
            value=_minor_from_yuan(row["price"]),
            authority_source=SOURCE_LOCAL,
            source_revision=row["source_revision"],
            observed_at=row["observed_at"],
            fresh_until=row["fresh_until"],
            write_mode=LOCAL_AUTHORITATIVE,
        )

    def get_delivery(self, sku: str) -> CommerceField[dict[str, Any]] | None:
        row = self._row(sku)
        if row is None:
            return None
        return CommerceField(
            value={"lead_days": _lead_days_from_eta(self._conn, str(row["merchant_id"]))},
            authority_source=SOURCE_LOCAL,
            source_revision=row["source_revision"],
            observed_at=row["observed_at"],
            fresh_until=row["fresh_until"],
            write_mode=LOCAL_AUTHORITATIVE,
        )

    def get_public_listing(self) -> dict[str, Any]:
        return {
            "source": "shopping-cli",
            "authority": LOCAL_AUTHORITATIVE,
            "count": self._conn.execute(
                "select count(*) from products where source = ?", (SOURCE_LOCAL,)
            ).fetchone()[0],
        }

    def health(self) -> dict[str, Any]:
        return {"ok": True, "source": SOURCE_LOCAL}

    # ── 写意图（v0.3 §4；本轮 fail-closed）─────────────────────────────────

    def draft_product_change(self, sku: str, changes: dict[str, Any]) -> None:
        raise NotImplementedError("draft write intent is not implemented in this milestone")

    def draft_inventory_change(self, sku: str, changes: dict[str, Any]) -> None:
        raise NotImplementedError("draft write intent is not implemented in this milestone")

    def apply_product_change(self, sku: str, changes: dict[str, Any]) -> None:
        raise NotImplementedError("apply write intent is not implemented in this milestone")

    def apply_inventory_change(self, sku: str, changes: dict[str, Any]) -> None:
        raise NotImplementedError("apply write intent is not implemented in this milestone")


class ErpCommerceDataSource:
    """ERP 同步缓存数据源（UPSTREAM_PROXY_WRITE；适配器只读，v0.3 §5）。

    事实在 ERP，本地是缓存：write_mode=UPSTREAM_PROXY_WRITE 但 apply 走
    fail-closed（本轮未实现 ERP 写回）。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _row(self, sku: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "select * from products where sku = ? and source = ?", (sku, SOURCE_ERP)
        ).fetchone()
        return dict(row) if row is not None else None

    def get_product(self, sku: str) -> ProductFact | None:
        row = self._row(sku)
        if row is None:
            return None
        return ProductFact(
            sku=row["sku"],
            title=row["title"],
            category=row["category"],
            price_minor=_minor_from_yuan(row["price"]),
            currency=row["currency"],
            stock=row["stock"],
            merchant_id=row["merchant_id"],
        )

    def search_products(self, query: str = "", limit: int = 50) -> list[ProductFact]:
        limit = max(1, min(int(limit), 100))
        if query:
            rows = self._conn.execute(
                "select * from products where source = ? and (title like ? or sku like ?)"
                " order by updated_at desc limit ?",
                (SOURCE_ERP, f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "select * from products where source = ? order by updated_at desc limit ?",
                (SOURCE_ERP, limit),
            ).fetchall()
        return [self._to_fact(dict(row)) for row in rows]

    def _to_fact(self, row: dict[str, Any]) -> ProductFact:
        return ProductFact(
            sku=row["sku"],
            title=row["title"],
            category=row["category"],
            price_minor=_minor_from_yuan(row["price"]),
            currency=row["currency"],
            stock=row["stock"],
            merchant_id=row["merchant_id"],
        )

    def get_inventory(self, sku: str) -> CommerceField[int] | None:
        row = self._row(sku)
        if row is None:
            return None
        return CommerceField(
            value=int(row["stock"]),
            authority_source=AUTHORITY_ERP,
            source_revision=row["source_revision"],
            observed_at=row["observed_at"],
            fresh_until=row["fresh_until"],
            write_mode=UPSTREAM_PROXY_WRITE,
        )

    def get_price(self, sku: str) -> CommerceField[int] | None:
        row = self._row(sku)
        if row is None:
            return None
        return CommerceField(
            value=_minor_from_yuan(row["price"]),
            authority_source=AUTHORITY_ERP,
            source_revision=row["source_revision"],
            observed_at=row["observed_at"],
            fresh_until=row["fresh_until"],
            write_mode=UPSTREAM_PROXY_WRITE,
        )

    def get_delivery(self, sku: str) -> CommerceField[dict[str, Any]] | None:
        row = self._row(sku)
        if row is None:
            return None
        return CommerceField(
            value={"lead_days": _lead_days_from_eta(self._conn, str(row["merchant_id"]))},
            authority_source=AUTHORITY_ERP,
            source_revision=row["source_revision"],
            observed_at=row["observed_at"],
            fresh_until=row["fresh_until"],
            write_mode=UPSTREAM_PROXY_WRITE,
        )

    def get_public_listing(self) -> dict[str, Any]:
        return {
            "source": "shopping-cli/erp",
            "authority": AUTHORITY_ERP,
            "count": self._conn.execute(
                "select count(*) from products where source = ?", (SOURCE_ERP,)
            ).fetchone()[0],
        }

    def health(self) -> dict[str, Any]:
        return {"ok": True, "source": SOURCE_ERP}

    # ── 写意图（v0.3 §4；UPSTREAM_PROXY_WRITE 但本轮适配器只读，fail-closed）──

    def draft_product_change(self, sku: str, changes: dict[str, Any]) -> None:
        raise NotImplementedError("draft write intent is not implemented in this milestone")

    def draft_inventory_change(self, sku: str, changes: dict[str, Any]) -> None:
        raise NotImplementedError("draft write intent is not implemented in this milestone")

    def apply_product_change(self, sku: str, changes: dict[str, Any]) -> None:
        raise NotImplementedError("apply write intent is not implemented in this milestone")

    def apply_inventory_change(self, sku: str, changes: dict[str, Any]) -> None:
        raise NotImplementedError("apply write intent is not implemented in this milestone")


def resolve_field(
    field_name: str,
    sources: dict[str, CommerceField],
) -> CommerceField:
    """双源同一字段解析：单源直接返回；双源冲突 → AuthorityConflictError。

    v0.3 §5：两个配置源对同一字段都声明权威且 policy 未定义优先级 →
    authority_conflict → fail-closed / operator review（不静默合并）。
    """
    present = {name: field for name, field in sources.items() if field is not None}
    if len(present) == 0:
        raise AuthorityConflictError(f"no configured source provides field {field_name}")
    if len(present) == 1:
        return next(iter(present.values()))
    authorities = {
        name: field.write_mode for name, field in present.items()
    }
    raise AuthorityConflictError(
        f"field {field_name!r} has conflicting authorities: {authorities}"
    )

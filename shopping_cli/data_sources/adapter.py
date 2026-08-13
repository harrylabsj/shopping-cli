"""Adapter SDK（Issue 14 / §6.3）：稳定 REST/ERP adapter 接口 + 注册表。

shopping-cli 是 Merchant Commerce Data & Operations Hub——外部商品事实（ERP、
CSV/Excel、未来 REST 连接器）经**数据源适配器**接入本地 ``products`` 表，Kiwi
merchant 只消费 shopping-cli 的开放层。

本模块定义稳定的适配器契约（不按品牌堆连接器代码）：

* ``DataSourceAdapter`` Protocol——任何适配器实现 ``sync(ctx) -> SyncReport``；
* ``SyncContext``——连接、授权边界（跨租户）、时钟、适配器配置；
* ``SyncReport``——可审计结果（fetched/upserted/skipped/conflicts/errors）；
* 注册表 + ``run(name, ctx)``——CLI / API 统一按名字执行适配器。

权威语义（data hub v0.2.1 §5）：source='local' 本地录入 = LOCAL_AUTHORITATIVE；
外部源（erp / csv_excel）= UPSTREAM_PROXY（本地是缓存）。同 SKU 冲突绝不静默
合并冲突权威源。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

SOURCE_LOCAL = "local"
AUTHORITY_LOCAL = "LOCAL_AUTHORITATIVE"
AUTHORITY_UPSTREAM = "UPSTREAM_PROXY"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SyncContext:
    """适配器执行上下文。"""

    conn: sqlite3.Connection
    default_merchant_id: str = ""
    allowed_merchant_id: str = ""
    now: Callable[[], str] = _now_iso
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class SyncReport:
    """一次适配器同步的可审计结果。"""

    source: str
    authority: str = AUTHORITY_UPSTREAM
    fetched: int = 0
    upserted: int = 0
    skipped: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "upserted": self.upserted,
            "skipped": self.skipped,
            "conflicts": self.conflicts,
            "errors": self.errors,
            "source": self.source,
            "authority": self.authority,
        }


class AdapterError(Exception):
    """适配器执行失败（fail-closed：任何校验/结构/IO 错误抛本异常）。"""


class DataSourceAdapter(Protocol):
    """适配器契约：``name`` + ``sync(ctx)``。"""

    name: str
    description: str

    def sync(self, ctx: SyncContext) -> SyncReport: ...


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

_ADAPTERS: dict[str, DataSourceAdapter] = {}


def register(adapter: DataSourceAdapter) -> DataSourceAdapter:
    """注册适配器（同名字符串参数不覆盖——重复注册即显式错误）。"""
    if adapter.name in _ADAPTERS:
        raise AdapterError(f"adapter {adapter.name!r} already registered")
    _ADAPTERS[adapter.name] = adapter
    return adapter


def registered_adapters() -> dict[str, DataSourceAdapter]:
    """按名字返回已注册适配器（拷贝，避免外部篡改）。"""
    return dict(_ADAPTERS)


def run(name: str, ctx: SyncContext) -> SyncReport:
    """按名字执行适配器。未知名字 → AdapterError（fail-closed）。"""
    adapter = _ADAPTERS.get(name)
    if adapter is None:
        raise AdapterError(
            f"unknown adapter {name!r} (registered: {', '.join(sorted(_ADAPTERS)) or 'none'})"
        )
    return adapter.sync(ctx)


def resolve_merchant_or_raise(conn: sqlite3.Connection, merchant_id: str) -> None:
    """FK 防护：归属 merchant 必须真实存在（否则裸 IntegrityError 中止同步）。"""
    exists = conn.execute("select 1 from merchants where id = ?", (merchant_id,)).fetchone()
    if exists is None:
        raise AdapterError(f"unknown merchant {merchant_id!r}")


def upsert_product_row(
    conn: sqlite3.Connection,
    *,
    sku: str,
    merchant_id: str,
    title: str,
    description: str,
    category: str,
    price: float,
    currency: str,
    stock: int,
    source: str,
    revision: str,
    now_ts: str,
    fresh_until: str,
) -> None:
    """共享 upsert（ERP / CSV-Excel 同款语义：source 标注 + provenance 回填）。"""
    conn.execute(
        """
        insert into products(
            sku, merchant_id, title, description, category, tags_json,
            price, currency, stock, delivery_attributes_json, active,
            source, source_revision, observed_at, fresh_until,
            created_at, updated_at
        ) values (?, ?, ?, ?, ?, '[]', ?, ?, ?, '[]', 1, ?, ?, ?, ?, ?, ?)
        on conflict(sku) do update set
            merchant_id=excluded.merchant_id,
            title=excluded.title,
            description=excluded.description,
            category=excluded.category,
            price=excluded.price,
            currency=excluded.currency,
            stock=excluded.stock,
            source=excluded.source,
            source_revision=excluded.source_revision,
            observed_at=excluded.observed_at,
            fresh_until=excluded.fresh_until,
            updated_at=excluded.updated_at
        """,
        (
            sku,
            merchant_id,
            title,
            description,
            category,
            price,
            currency,
            stock,
            source,
            revision,
            now_ts,
            fresh_until,
            now_ts,
            now_ts,
        ),
    )

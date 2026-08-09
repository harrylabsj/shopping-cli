"""CommerceDataSource Python Protocol（shopping-cli v0.3 §4/§5）。

镜像 kiwi 仓 ``src/commerce/data-source.ts`` 的接口签名（契约锚点）：read
面（searchProducts/getProduct/getInventory/getPrice/getDelivery/
getPublicListing）+ 写意图（draft/apply——本轮只落签名 + fail-closed 拒绝，
v0.3 §4 明确 apply 非普适，评审"本轮不做"清单）。每个 field/source 声明
write mode：

* LOCAL_AUTHORITATIVE —— shopping-cli 本地权威（录入即事实）；
* UPSTREAM_PROXY_WRITE —— 只能调用权威源适配器（本轮 ERP 适配器只读同步，
  apply 拒绝）；
* READ_ONLY —— mutation 必须 fail-closed，不得用本地 shadow 冒充权威源已变。

同字段两个配置源都声明权威且无优先级 → authority_conflict（fail-closed，
不静默合并——v0.3 §5）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# ── write mode（v0.3 §4）───────────────────────────────────────────────────

LOCAL_AUTHORITATIVE = "LOCAL_AUTHORITATIVE"
UPSTREAM_PROXY_WRITE = "UPSTREAM_PROXY_WRITE"
READ_ONLY = "READ_ONLY"

WRITE_MODES: tuple[str, ...] = (LOCAL_AUTHORITATIVE, UPSTREAM_PROXY_WRITE, READ_ONLY)


class AuthorityConflictError(Exception):
    """两个配置源对同一字段都声明权威且无优先级（fail-closed，v0.3 §5）。"""


@dataclass(frozen=True)
class CommerceField:
    """per-field provenance（v0.3 §5 记录形状）。"""

    value: Any
    authority_source: str
    source_revision: str = ""
    observed_at: str = ""
    fresh_until: str = ""
    write_mode: str = READ_ONLY


@dataclass
class ProductFact:
    """public-only 商品事实（禁止成本/底价/私密库存——v0.3 §7）。"""

    sku: str
    title: str = ""
    category: str = ""
    price_minor: int | None = None
    currency: str = "CNY"
    stock: int | None = None
    delivery_lead_days: int | None = None
    merchant_id: str = ""


@runtime_checkable
class CommerceDataSource(Protocol):
    """Merchant 商业事实层的统一读取边界（kiwi data-source.ts 镜像）。"""

    # ── read（权威面）──────────────────────────────────────────────────────

    def search_products(self, query: str = "", limit: int = 50) -> list[ProductFact]: ...

    def get_product(self, sku: str) -> ProductFact | None: ...

    def get_inventory(self, sku: str) -> CommerceField[int] | None: ...

    def get_price(self, sku: str) -> CommerceField[int] | None: ...

    def get_delivery(self, sku: str) -> CommerceField[dict[str, Any]] | None: ...

    def get_public_listing(self) -> dict[str, Any]: ...

    def health(self) -> dict[str, Any]: ...

    # ── 写意图（v0.3 §4；本轮只落签名 + fail-closed）──────────────────────

    def draft_product_change(self, sku: str, changes: dict[str, Any]) -> None:
        """落 draft（不触权威源）。本轮不支持 → NotImplementedError（fail-closed）。"""
        raise NotImplementedError("draft write intent is not implemented in this milestone")

    def draft_inventory_change(self, sku: str, changes: dict[str, Any]) -> None:
        raise NotImplementedError("draft write intent is not implemented in this milestone")

    def apply_product_change(self, sku: str, changes: dict[str, Any]) -> None:
        raise NotImplementedError("apply write intent is not implemented in this milestone")

    def apply_inventory_change(self, sku: str, changes: dict[str, Any]) -> None:
        raise NotImplementedError("apply write intent is not implemented in this milestone")

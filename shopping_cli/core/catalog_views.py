"""Pure public catalog projections."""

from __future__ import annotations

from typing import Any


def public_merchant_summary(merchant: dict[str, Any]) -> dict[str, Any]:
    """Remove merchant-private fields before a summary crosses the API boundary."""
    summary = dict(merchant)
    summary.pop("contact", None)
    summary.pop("automation_boundaries", None)
    return summary


def public_product_summary(product: dict[str, Any]) -> dict[str, Any]:
    """Project a product and recursively apply the merchant public projection.

    design v0.3 §7：精确库存是私密 inventory，公开投影只携带 availability
    提示（审查 P2-1：此前 /products/{sku} 与 /search/products 匿名原样
    出网精确 stock，可逐 SKU 枚举全平台库存）。需要精确库存的商家走
    merchant_product_summary（handler 层 owner 鉴权门）。

    handoff_destination 是商家私有成交入口（谈判达成后由商家 Agent 点对点
    交给买家），公开/匿名投影一律剥离（审查 P2-B），同款纪律参照
    public_merchant_summary 剥离 contact；认证商家读走 merchant_product_summary。

    对已投影数据幂等：含 ``stock`` 时替换为 availability_hint；不含则保留
    已有 hint（审查 P1-02：幂等回放响应二次投影不丢失 in_stock）。
    """
    summary = dict(product)
    summary.pop("handoff_destination", None)
    if "stock" in summary:
        stock = summary.pop("stock", None)
        summary["availability_hint"] = (
            "in_stock" if isinstance(stock, (int, float)) and stock > 0 else "out_of_stock"
        )
    elif "availability_hint" not in summary:
        summary["availability_hint"] = "out_of_stock"
    merchant = summary.get("merchant")
    if isinstance(merchant, dict):
        summary["merchant"] = public_merchant_summary(merchant)
    return summary


def merchant_product_summary(product: dict[str, Any]) -> dict[str, Any]:
    """Merchant-owned read projection：保留精确库存（owner 鉴权门在 handler）。

    与完整 summary 一致，但同样剔除 merchant 私有字段；handler 层必须把
    本投影限定在商品所属商户本人（design v0.3 §7 private inventory）。
    """
    summary = dict(product)
    merchant = summary.get("merchant")
    if isinstance(merchant, dict):
        summary["merchant"] = public_merchant_summary(merchant)
    return summary

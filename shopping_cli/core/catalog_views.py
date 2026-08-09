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
    """Project a product and recursively apply the merchant public projection."""
    summary = dict(product)
    merchant = summary.get("merchant")
    if isinstance(merchant, dict):
        summary["merchant"] = public_merchant_summary(merchant)
    return summary

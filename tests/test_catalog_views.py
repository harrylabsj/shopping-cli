from shopping_cli.core.catalog_views import (
    merchant_product_summary,
    public_merchant_summary,
    public_product_summary,
)


def test_public_merchant_summary_strips_private_fields() -> None:
    merchant = {"name": "Acme", "contact": "secret", "automation_boundaries": {"floor": 9}}
    assert public_merchant_summary(merchant) == {"name": "Acme"}
    assert merchant["contact"] == "secret"


def test_public_product_summary_projects_nested_merchant() -> None:
    product = {"sku": "sku-1", "merchant": {"name": "Acme", "contact": "secret"}}
    # 审查 P2-1：公开投影只携带 availability_hint，精确 stock 不下发
    assert public_product_summary(product) == {
        "sku": "sku-1",
        "merchant": {"name": "Acme"},
        "availability_hint": "out_of_stock",
    }


def test_public_product_summary_hides_exact_stock() -> None:
    """审查 P2-1：公开投影不得携带精确库存（design v0.3 §7 private
    inventory）；精确库存只经 merchant_product_summary（owner 鉴权门）。"""
    product = {"sku": "sku-1", "stock": 42, "merchant": {"name": "Acme"}}
    public = public_product_summary(product)
    assert "stock" not in public
    assert public["availability_hint"] == "in_stock"
    merchant_view = merchant_product_summary(product)
    assert merchant_view["stock"] == 42
    assert "availability_hint" not in merchant_view

from shopping_cli.core.catalog_views import public_merchant_summary, public_product_summary


def test_public_merchant_summary_strips_private_fields() -> None:
    merchant = {"name": "Acme", "contact": "secret", "automation_boundaries": {"floor": 9}}
    assert public_merchant_summary(merchant) == {"name": "Acme"}
    assert merchant["contact"] == "secret"


def test_public_product_summary_projects_nested_merchant() -> None:
    product = {"sku": "sku-1", "merchant": {"name": "Acme", "contact": "secret"}}
    assert public_product_summary(product) == {"sku": "sku-1", "merchant": {"name": "Acme"}}

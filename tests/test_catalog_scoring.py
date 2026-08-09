from shopping_cli.core.catalog_scoring import merchant_match_score, product_match_score


def test_product_match_score_preserves_cjk_bigram_and_stock_weight() -> None:
    in_stock = product_match_score("今天想买龙井礼盒", "西湖龙井礼盒", 3, 100)
    out_of_stock = product_match_score("今天想买龙井礼盒", "西湖龙井礼盒", 0, 100)

    assert in_stock > out_of_stock
    assert in_stock == 25.9


def test_merchant_match_score_is_case_insensitive_and_supports_cjk() -> None:
    assert merchant_match_score("杭州茶", "Acme 杭州茶馆") == merchant_match_score(
        "杭州茶", "acme 杭州茶馆"
    )
    assert merchant_match_score("杭州茶", "杭州茶馆") > 0

from shopping_cli.services.negotiation_policy_helpers import (
    leaks_private_threshold,
    normalize_digits,
)


def test_normalize_digits_preserves_threshold_matching_semantics() -> None:
    assert normalize_digits(" １，２３４．５０ ") == "1234.50"


def test_leaks_private_threshold_detects_explicit_and_full_width_disclosure() -> None:
    assert leaks_private_threshold("我们的最低价是 99", "99") is True
    assert leaks_private_threshold("我们的最低价是９９", "99") is True
    assert leaks_private_threshold("欢迎咨询商品库存", "99") is False
    assert leaks_private_threshold("最低价可谈", "not-a-number") is False

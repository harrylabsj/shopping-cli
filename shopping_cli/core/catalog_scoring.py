"""Pure scoring strategies used by catalog search re-ranking."""

from __future__ import annotations

from shopping_cli.core.catalog_text import cjk_bigrams, tokenize


def product_match_score(query: str, searchable: str, stock: int, price: float) -> float:
    """Score a product against normalized searchable text and numeric facts."""
    query_lower = query.lower()
    searchable_lower = searchable.lower()
    query_tokens = tokenize(query_lower)
    product_tokens = tokenize(searchable_lower)
    score = 0.0
    for token in query_tokens:
        if token in searchable_lower:
            score += 10
    for token in product_tokens:
        if len(token) >= 2 and token in query_lower:
            score += 8
    # CJK bigrams catch substring matches when full-word tokens don't overlap.
    for bigram in cjk_bigrams(query_lower):
        if bigram in searchable_lower:
            score += 7
    if stock > 0:
        score += 5
    score -= price / 1000
    return round(score, 4)


def merchant_match_score(query: str, searchable: str) -> float:
    """Score a merchant against normalized searchable text."""
    query_lower = query.lower()
    searchable_lower = searchable.lower()
    query_tokens = tokenize(query_lower)
    merchant_tokens = tokenize(searchable_lower)
    score = 0.0
    for token in query_tokens:
        if token in searchable_lower:
            score += 10
    for token in merchant_tokens:
        if len(token) >= 2 and token in query_lower:
            score += 8
    for bigram in cjk_bigrams(query_lower):
        if bigram in searchable_lower:
            score += 7
    return round(score, 4)

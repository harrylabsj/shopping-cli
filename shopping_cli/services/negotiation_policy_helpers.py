"""Pure negotiation-policy text helpers.

These functions only normalize public text and detect private-threshold
disclosure. They do not access SQLite or make a negotiation decision.
"""

from __future__ import annotations

import re


THRESHOLD_TERMS = (
    "最低价",
    "底价",
    "最低可成交",
    "底线",
    "成本价",
    "lowest price",
    "floor price",
    "min price",
    "minimum price",
    "cost price",
)


def normalize_digits(text: str) -> str:
    """Normalize full-width digits and remove whitespace/thousands separators."""
    table = str.maketrans("０１２３４５６７８９．", "0123456789.")
    return re.sub(r"[\s,，]", "", text.translate(table))


def leaks_private_threshold(public_message: str, floor_str: str) -> bool:
    """Return whether a public message appears to disclose a private floor."""
    try:
        floor_value = float(floor_str)
    except (TypeError, ValueError):
        return False
    candidates = {floor_str, f"{floor_value:.2f}"}
    if floor_value.is_integer():
        candidates.add(str(int(floor_value)))
    normalized_candidates = {normalize_digits(candidate) for candidate in candidates}
    lowered = public_message.lower()
    if not any(term in lowered or term in public_message for term in THRESHOLD_TERMS):
        return False
    normalized_message = normalize_digits(public_message)
    return any(candidate in public_message for candidate in candidates) or any(
        candidate in normalized_message for candidate in normalized_candidates
    )

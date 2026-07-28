"""Shared persistence limits for user-controlled text and collections."""

from __future__ import annotations

import math
from typing import Any

from shopping_cli.core.errors import ValidationError

MAX_SHORT_TEXT_CHARS = 1024
MAX_PERSISTED_TEXT_CHARS = 65536
MAX_COLLECTION_ITEMS = 100


def bounded_text(value: Any, field: str, maximum: int = MAX_PERSISTED_TEXT_CHARS) -> str:
    text = str(value or "")
    if len(text) > maximum:
        raise ValidationError(f"{field} must be <= {maximum} characters")
    return text


def bounded_string_list(values: list[str], field: str) -> list[str]:
    if len(values) > MAX_COLLECTION_ITEMS:
        raise ValidationError(f"{field} must contain at most {MAX_COLLECTION_ITEMS} items")
    return [bounded_text(value, field, MAX_SHORT_TEXT_CHARS) for value in values]


# ---------------------------------------------------------------------------
# Safe numeric coercion — used everywhere to tolerate malformed / missing
# inputs without surfacing low-level exceptions to callers.
# ---------------------------------------------------------------------------

def safe_int(
    value: Any,
    *,
    default: int = 0,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    """Coerce *value* to int, clamping to [*minimum*, *maximum*].

    Bools, non-finite floats, and unparseable values return *default*.
    """
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (OverflowError, TypeError, ValueError):
        return default
    number = max(number, minimum)
    if maximum is not None:
        number = min(number, maximum)
    return number


def safe_float(
    value: Any,
    *,
    default: float = 0.0,
    minimum: float = 0.0,
    maximum: float | None = None,
    allow_zero: bool = True,
) -> float:
    """Coerce *value* to float, clamping to [*minimum*, *maximum*].

    Bools, non-finite values, and unparseable values return *default*.
    When *allow_zero* is False, the result is also treated as invalid
    if it is <= 0 (useful for ``safe_positive_float`` semantics).
    """
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    if not allow_zero and number <= 0.0:
        return default
    number = max(number, minimum)
    if maximum is not None:
        number = min(number, maximum)
    return number


# Convenience aliases matching the original per-module helpers.
# New code should call safe_int / safe_float directly.

def safe_non_negative_int(value: Any) -> int:
    return safe_int(value)


def safe_positive_int(value: Any, default: int, maximum: int | None = None) -> int:
    return safe_int(value, default=default, minimum=1, maximum=maximum)


def safe_non_negative_float(value: Any) -> float:
    return safe_float(value)


def safe_non_negative_float_with_max(value: Any, default: float, maximum: float | None = None) -> float:
    return safe_float(value, default=default, maximum=maximum)


def safe_positive_float(value: Any, default: float, maximum: float | None = None) -> float:
    return safe_float(value, default=default, allow_zero=False, maximum=maximum)

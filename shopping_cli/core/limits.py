"""Shared persistence limits for user-controlled text and collections."""

from __future__ import annotations

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

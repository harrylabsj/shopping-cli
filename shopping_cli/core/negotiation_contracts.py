"""Frozen shopping.negotiation/0.1 contract validation, time normalization and
JSON canonicalization helpers.

The JSON schemas under ``shopping_cli/contracts/shopping.negotiation/0.1/`` are
verbatim, versioned copies of the frozen Kiwi contract. They ship inside the
package so the runtime never depends on a sibling checkout. Validation uses a
self-contained JSON Schema subset validator (no third-party jsonschema
dependency): it supports exactly the keywords the frozen schemas use —
``type`` (including type lists), ``enum``, ``const``, ``required``,
``additionalProperties: false``, ``properties``, ``items``, ``minLength``,
``maxLength``, ``minimum``, ``maximum``, ``minItems``, ``maxItems`` and local
``$ref`` (``#/$defs/...``). ``format: date-time`` is enforced strictly, in a
way compatible with Ajv + ajv-formats (the Kiwi validator): the value must be
an RFC 3339 date-time with an explicit offset (``Z`` or ``±HH:MM``); naive
timestamps and impossible dates are rejected.

Move-only extraction from :mod:`shopping_cli.core.negotiation`: this leaf
module owns contract validation, RFC 3339 time normalization and JSON
canonicalization. It never touches SQLite, claims, turn/state transitions,
negotiation writes or policy decisions, and depends only on
:class:`shopping_cli.core.errors.ValidationError`. The parent module re-exports
these names unchanged so the public ``core.negotiation`` surface, error
types/messages, schema-loader cache behavior and call signatures are preserved.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from shopping_cli.core.errors import ValidationError

PROTOCOL_VERSION = "shopping.negotiation/0.1"

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts" / "shopping.negotiation" / "0.1"

# Shape compatible with ajv-formats full-mode ``date-time``: explicit offset
# required (Z or ±HH:MM), ``T``/space/lowercase separator allowed. The leap
# second ``23:59:60`` accepted by ajv-formats is rejected here (stricter, and
# never produced by this codebase).
_RFC3339_DATETIME_RE = re.compile(
    r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])[t\s]"
    r"([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d+)?"
    r"(z|[+-]([01]\d|2[0-3]):[0-5]\d)$",
    re.IGNORECASE,
)


def is_rfc3339_datetime(value: Any) -> bool:
    """Strict RFC 3339 date-time check compatible with Ajv + ajv-formats.

    Naive timestamps (no offset) and impossible dates (e.g. Feb 30) are
    rejected, matching the Kiwi-side Ajv ``format: date-time`` validation.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not _RFC3339_DATETIME_RE.fullmatch(text):
        return False
    parsed = parse_rfc3339(text)
    return parsed is not None and parsed.tzinfo is not None


def normalize_db_timestamp(value: Any) -> str:
    """Normalize a stored DB timestamp to RFC 3339 with an explicit offset.

    Existing rows store naive local time (``db.session.now_iso``); those are
    interpreted in the service's local timezone and re-emitted with an
    explicit offset so the frozen snapshot schema (and Kiwi's Ajv) accept
    them. Unparseable values fall back to the current UTC time.
    """
    text = str(value or "").strip()
    parsed: datetime | None = None
    if text:
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
    if parsed is None:
        return now_rfc3339()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.isoformat(timespec="seconds")


@lru_cache(maxsize=None)
def load_contract_schema(name: str) -> dict[str, Any]:
    """Load one frozen contract schema (``capabilities``/``decision``/``policy-result``/``snapshot``)."""
    path = CONTRACTS_DIR / f"{name}.schema.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"frozen negotiation contract schema is missing: {path}") from exc
    schema = json.loads(raw)
    if not isinstance(schema, dict):
        raise RuntimeError(f"frozen negotiation contract schema is not an object: {path}")
    return schema


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValidationError(f"unsupported schema reference: {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            raise ValidationError(f"unsupported schema reference: {ref}")
        node = node[part]
    if not isinstance(node, dict):
        raise ValidationError(f"unsupported schema reference: {ref}")
    return node


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return False


def _validate(value: Any, schema: dict[str, Any], root: dict[str, Any], path: str) -> None:
    if "$ref" in schema:
        _validate(value, _resolve_ref(str(schema["$ref"]), root), root, path)
        return
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(_schema_type_matches(value, str(option)) for option in expected):
            raise ValidationError(f"{path} must be one of types: {', '.join(str(option) for option in expected)}")
    elif isinstance(expected, str) and expected:
        if not _schema_type_matches(value, expected):
            raise ValidationError(f"{path} must be {expected}")
    if "const" in schema and value != schema["const"]:
        raise ValidationError(f"{path} must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(str(item) for item in schema["enum"])
        raise ValidationError(f"{path} must be one of: {allowed}")
    if isinstance(value, str):
        if schema.get("format") == "date-time" and not is_rfc3339_datetime(value):
            raise ValidationError(f"{path} must be an RFC 3339 date-time with an explicit offset (Z or ±HH:MM)")
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            raise ValidationError(f"{path} is shorter than minLength {schema['minLength']}")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise ValidationError(f"{path} is longer than maxLength {schema['maxLength']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError(f"{path} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError(f"{path} must be <= {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            raise ValidationError(f"{path} must contain at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise ValidationError(f"{path} must contain at most {schema['maxItems']} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate(item, item_schema, root, f"{path}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                raise ValidationError(f"{path}.{name} is required")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ValidationError(f"{path} contains unsupported field: {extras[0]}")
        for name, child in properties.items():
            if name in value and isinstance(child, dict):
                _validate(value[name], child, root, f"{path}.{name}")


def validate_contract(name: str, value: Any) -> None:
    """Validate ``value`` against a frozen contract schema, raising ValidationError."""
    schema = load_contract_schema(name)
    _validate(value, schema, schema, name)


def capabilities_report() -> dict[str, Any]:
    """Authoritative protocol/capability advertisement for the LocalMarketplace backend.

    ``orders`` is always false: shopping.negotiation/0.1 never creates orders,
    payments or inventory reservations.
    """
    return {
        "protocol_versions": [PROTOCOL_VERSION],
        "backend": "local_marketplace",
        "capabilities": {
            "catalog_read": True,
            "inventory_read": True,
            "consultation_read": True,
            "consultation_write": True,
            "price_negotiate": True,
            "webhook": False,
            "orders": False,
        },
    }


def parse_rfc3339(value: Any) -> datetime | None:
    """Parse an RFC 3339 / ISO 8601 timestamp; return None when unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

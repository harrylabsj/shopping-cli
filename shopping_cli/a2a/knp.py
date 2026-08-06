"""KNP/1.0 negotiation envelope — domain model, validation and content digest.

Pure-service layer for the KNP/1.0 Negotiation Envelope (sub-spec §8, base
spec §19).  It ports the semantics of the Kiwi runtime's TypeScript
implementation (``kiwi/src/negotiation/domain/envelope.ts`` +
``jcs.ts``) into a self-contained Python module; it is NOT a code copy.

Rules enforced here (all fail-closed):

* ``protocol_version`` accepts only ``"1.0"``; anything else raises
  ``KnpValidationError(code="protocol_version_unsupported")``.
* ``actor`` accepts only ``buyer|merchant`` — system/internal events must
  not impersonate a commercial role (§8.1).
* ``action`` must be in the frozen KNP/1.0 action vocabulary.
* ``created_at`` must be an RFC 3339 timestamp with explicit offset.
* ``payload`` must be an object.
* ``digest`` must match ``sha256:<64 lowercase hex>``.
* ``clarification_response`` must reference the clarified message via
  ``in_reply_to`` (§14).

The envelope digest (binding rc1 §3.6, KNP §19.2) is computed over every
field except the ``digest`` itself and the four transport signature fields,
RFC 8785 JCS canonicalized + SHA-256, lowercase hex with a ``sha256:``
prefix.  Key order is irrelevant and optional fields that are absent do not
affect the digest.

Binding: docs/a2a/shopping-cli-a2a-binding-1.0-rc1.md §3.6, §4
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

KNP_PROTOCOL_VERSION = "1.0"

# KNP/1.0 action vocabulary (sub-spec §8.2).  Frozen — matches kiwi
# ``objects.ts`` ``KNP_ACTIONS`` exactly.
KNP_ACTIONS: tuple[str, ...] = (
    "inquiry",
    "rfq",
    "offer",
    "counter_offer",
    "conditional_offer",
    "clarification",
    "clarification_response",
    "accept_nonbinding",
    "withdraw",
    "decline",
    "cancel",
)

KNP_ACTORS: tuple[str, ...] = ("buyer", "merchant")

# KNP-undefined transport signature fields.  Excluded from the digest input
# (KNP §19.2) — they are transport concerns, not protocol content.
TRANSPORT_SIGNATURE_FIELDS: frozenset[str] = frozenset(
    {
        "signature",
        "transport_signature",
        "http_message_signature",
        "x_message_signature",
    }
)

# RFC 3339 with explicit offset (uppercase T / Z per KNP, matching kiwi).
_KNP_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# JavaScript Number switches to exponent notation at |x| >= 1e21 (RFC 8785
# number serialization is Number.toString()).  Python's repr uses the same
# shortest-round-trip digits but a different exponent threshold, so integer
# floats below this bound are serialized as plain integers to match JS/JCS.
_JCS_EXPONENT_THRESHOLD = 10**21
_JCS_EXPONENT_RE = re.compile(r"^(.+?)[eE]([+-]?)(\d+)$")


class KnpValidationError(Exception):
    """Fail-closed KNP envelope validation failure.

    ``code`` is a machine-readable protocol error code (KNP §18 vocabulary,
    default ``schema_invalid``); ``path`` is a JSON-Pointer-style field path.
    """

    def __init__(self, code: str, message: str, path: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


def _schema_error(path: str, message: str) -> KnpValidationError:
    return KnpValidationError("schema_invalid", message, path)


# ---------------------------------------------------------------------------
# Validation primitives (mirror kiwi common.ts semantics)
# ---------------------------------------------------------------------------


def _require_object(value: Any, path: str) -> dict[str, Any]:
    if value is None or not isinstance(value, dict):
        raise _schema_error(path, f"{path} must be an object")
    return value


def _require_non_empty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or value == "":
        raise _schema_error(path, f"{path} must be a non-empty string")
    return value


def _require_enum(value: Any, allowed: tuple[str, ...], path: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _schema_error(path, f"{path} must be one of {'|'.join(allowed)}")
    return value


def _require_rfc3339(value: Any, path: str) -> str:
    s = _require_non_empty_string(value, path)
    if _KNP_RFC3339_RE.fullmatch(s) is None:
        raise _schema_error(path, f"{path} must be an RFC 3339 timestamp")
    return s


def _require_digest(value: Any, path: str) -> str:
    s = _require_non_empty_string(value, path)
    if _DIGEST_RE.fullmatch(s) is None:
        raise _schema_error(path, f"{path} must be sha256:<64 lowercase hex>")
    return s


def _optional_string(obj: Mapping[str, Any], key: str) -> str | None:
    value = obj.get(key)
    if value is None:
        return None
    return _require_non_empty_string(value, f"/{key}")


# ---------------------------------------------------------------------------
# RFC 8785 JCS (JSON Canonicalization Scheme)
#
# Self-contained implementation — no third-party dependency.  The shared
# ``shopping_cli.core.negotiation.canonical_json`` sorts keys and compacts
# separators but is NOT JCS: it serializes numbers with Python's json
# (e.g. ``1e-07`` instead of JCS ``1e-7``, ``850.0`` instead of ``850``) and
# does not preserve ``-0``.  KNP content digests therefore use this module.
#
# Known, documented divergence: the exponent *threshold* follows JavaScript
# ``Number.prototype.toString()`` only for values KNP actually uses
# (integers for money, finite floats on defensive paths).  Python repr
# switches to exponent form at a slightly different magnitude for very
# small floats; the golden digest vectors in the tests cover the values the
# protocol admits.
# ---------------------------------------------------------------------------


def canonical_number(value: float) -> str:
    """RFC 8785 §3.2.2.2 number serialization for a finite float."""
    if not math.isfinite(value):
        raise _schema_error("/", f"JCS: cannot canonicalize non-finite number {value}")
    if value == 0 and math.copysign(1.0, value) < 0:
        return "-0"
    # JS has a single numeric type: an integer-valued float serializes as
    # the plain integer (String(850.0) === "850").  Below the 1e21 exponent
    # boundary this is exactly what JCS requires.
    if value.is_integer() and abs(value) < _JCS_EXPONENT_THRESHOLD:
        return str(int(value))
    serialized = repr(value)
    match = _JCS_EXPONENT_RE.fullmatch(serialized)
    if match is not None:
        mantissa, sign, digits = match.groups()
        normalized_sign = "-" if sign == "-" else ""
        normalized_digits = digits.lstrip("0") or "0"
        return f"{mantissa}e{normalized_sign}{normalized_digits}"
    return serialized


def _canonical_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        # ensure_ascii=False: JS JSON.stringify emits UTF-8, never \uXXXX.
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return canonical_number(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical_value(item) for item in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value)
        return "{" + ",".join(
            f"{json.dumps(key, ensure_ascii=False, separators=(',', ':'))}:"
            f"{_canonical_value(value[key])}"
            for key in keys
        ) + "}"
    raise _schema_error(
        "/",
        f"JCS: cannot canonicalize {type(value).__name__}",
    )


def jcs_canonicalize(value: Any) -> str:
    """RFC 8785 JCS canonical serialization of a JSON-compatible value."""
    return _canonical_value(value)


def sha256_hex(text: str) -> str:
    """Lowercase hex SHA-256 of a UTF-8 string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_digest(value: Any) -> str:
    """Content-addressed digest of a structured value, ``sha256:`` prefixed."""
    return f"sha256:{sha256_hex(jcs_canonicalize(value))}"


def compute_envelope_digest(fields: Mapping[str, Any]) -> str:
    """Envelope digest (§19.2): all fields except ``digest`` and transport
    signatures, JCS canonicalized + SHA-256.

    Optional fields that are absent from the mapping simply do not
    participate; fields present with a JSON ``null`` value are kept (they
    are JSON data, unlike JavaScript ``undefined``).
    """
    clean: dict[str, Any] = {}
    for key, value in fields.items():
        if key == "digest" or key in TRANSPORT_SIGNATURE_FIELDS:
            continue
        clean[key] = value
    return content_digest(clean)


# ---------------------------------------------------------------------------
# Envelope model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NegotiationEnvelope:
    """Validated KNP/1.0 negotiation envelope.

    ``payload`` is the validated action payload as a plain JSON object.
    ``in_reply_to`` / ``public_message`` are optional envelope fields.
    """

    capability: str
    protocol_version: str
    negotiation_id: str
    exchange_id: str
    message_id: str
    actor: str
    action: str
    created_at: str
    payload: dict[str, Any]
    digest: str
    in_reply_to: str | None = None
    public_message: str | None = None

    #: ignored for digest computation; retained so dataclass equality works.
    _transport_signatures: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def as_dict(self) -> dict[str, Any]:
        """Wire form: optional fields absent (not ``None``), so the digest of
        this dict equals the digest computed at finalize time."""
        data: dict[str, Any] = {
            "capability": self.capability,
            "protocol_version": self.protocol_version,
            "negotiation_id": self.negotiation_id,
            "exchange_id": self.exchange_id,
            "message_id": self.message_id,
            "actor": self.actor,
            "action": self.action,
            "created_at": self.created_at,
            "payload": self.payload,
            "digest": self.digest,
        }
        if self.in_reply_to is not None:
            data["in_reply_to"] = self.in_reply_to
        if self.public_message is not None:
            data["public_message"] = self.public_message
        return data


def finalize_envelope(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Compute the content digest over the unsigned fields and return the
    complete envelope (Kiwi ``finalizeEnvelope``)."""
    return {**dict(fields), "digest": compute_envelope_digest(fields)}


def validate_envelope(value: Any) -> NegotiationEnvelope:
    """Schema-validate a KNP/1.0 envelope, fail-closed.

    Raises ``KnpValidationError`` on any structural violation; digest content
    consistency is verified separately by ``verify_envelope_digest``.
    """
    obj = _require_object(value, "/")
    capability = _require_non_empty_string(obj.get("capability"), "/capability")
    protocol_version = _require_non_empty_string(obj.get("protocol_version"), "/protocol_version")
    if protocol_version != KNP_PROTOCOL_VERSION:
        raise KnpValidationError(
            "protocol_version_unsupported",
            f"unsupported protocol_version {protocol_version}; this runtime implements "
            f"{KNP_PROTOCOL_VERSION}",
            "/protocol_version",
        )
    negotiation_id = _require_non_empty_string(obj.get("negotiation_id"), "/negotiation_id")
    exchange_id = _require_non_empty_string(obj.get("exchange_id"), "/exchange_id")
    message_id = _require_non_empty_string(obj.get("message_id"), "/message_id")
    actor = _require_enum(obj.get("actor"), KNP_ACTORS, "/actor")
    action = _require_enum(obj.get("action"), KNP_ACTIONS, "/action")
    created_at = _require_rfc3339(obj.get("created_at"), "/created_at")
    payload = _require_object(obj.get("payload"), "/payload")
    digest = _require_digest(obj.get("digest"), "/digest")
    in_reply_to = _optional_string(obj, "in_reply_to")
    public_message = _optional_string(obj, "public_message")

    # §14: clarification_response must reference the clarified message.
    if action == "clarification_response" and in_reply_to is None:
        raise _schema_error("/in_reply_to", "clarification_response requires in_reply_to")

    return NegotiationEnvelope(
        capability=capability,
        protocol_version=KNP_PROTOCOL_VERSION,
        negotiation_id=negotiation_id,
        exchange_id=exchange_id,
        message_id=message_id,
        actor=actor,
        action=action,
        created_at=created_at,
        payload=payload,
        digest=digest,
        in_reply_to=in_reply_to,
        public_message=public_message,
    )


def verify_envelope_digest(envelope: Any) -> bool:
    """Recompute the digest over the unsigned fields and compare.

    Accepts either a validated ``NegotiationEnvelope`` or a plain mapping.
    Returns ``False`` (never raises) when the digest was tampered with or is
    in the wrong format.
    """
    fields = envelope.as_dict() if isinstance(envelope, NegotiationEnvelope) else envelope
    if not isinstance(fields, Mapping):
        return False
    actual = fields.get("digest")
    if not isinstance(actual, str):
        return False
    return compute_envelope_digest(fields) == actual


__all__ = [
    "KNP_ACTIONS",
    "KNP_ACTORS",
    "KNP_PROTOCOL_VERSION",
    "KnpValidationError",
    "NegotiationEnvelope",
    "TRANSPORT_SIGNATURE_FIELDS",
    "compute_envelope_digest",
    "content_digest",
    "finalize_envelope",
    "jcs_canonicalize",
    "sha256_hex",
    "validate_envelope",
    "verify_envelope_digest",
]

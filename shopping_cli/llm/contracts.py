"""Declarative LLM marketplace tool contracts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Any


BUYER_SCOPES = {"buyer", "buyer_cli"}
MERCHANT_SCOPES = {"merchant", "merchant_agent"}
PRIVILEGED_CONVERSATION_SCOPES = {"local_trusted", "operator"}
SOURCE_OWNER_PREFIXES = ("shopping-cli-merchant-agent:", "shopping-cli-buyer-agent:", "merchant:", "buyer:")
CONVERSATION_SENDERS = {"buyer", "buyer_cli"}


class ToolContractError(ValueError):
    """Raised when a tool call violates the declarative tool contract."""


@dataclass(frozen=True)
class ToolSchema:
    name: str
    description: str
    parameters: dict[str, Any]

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": deepcopy(self.parameters),
            },
        }


@dataclass(frozen=True)
class ToolContract:
    schema: ToolSchema
    allowed_scopes: frozenset[str]
    handler_name: str

    @property
    def name(self) -> str:
        return self.schema.name


@dataclass(frozen=True)
class PreparedToolCall:
    contract: ToolContract
    arguments: dict[str, Any]


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


TOOL_CONTRACTS: tuple[ToolContract, ...] = (
    ToolContract(
        schema=ToolSchema(
            name="catalog_search",
            description="Search local merchant catalog data for consultation candidates.",
            parameters=_object_schema(
                {
                    "query": {"type": "string"},
                    "city": {"type": "string"},
                    "area": {"type": "string"},
                    "max_price": {"type": "number"},
                    "include_out_of_stock": {"type": "boolean"},
                },
                ["query"],
            ),
        ),
        allowed_scopes=frozenset({"local_trusted", "buyer", "buyer_cli", "merchant", "merchant_agent", "operator"}),
        handler_name="_dispatch_catalog_search",
    ),
    ToolContract(
        schema=ToolSchema(
            name="conversation_send",
            description="Append a buyer-side consultation message. This never creates an order or payment.",
            parameters=_object_schema(
                {
                    "conversation_id": {"type": "string"},
                    "sender": {"type": "string", "enum": sorted(CONVERSATION_SENDERS)},
                    "intent": {"type": "string"},
                    "text": {"type": "string"},
                },
                ["conversation_id", "sender", "intent", "text"],
            ),
        ),
        allowed_scopes=frozenset({"local_trusted", "buyer", "buyer_cli"}),
        handler_name="_dispatch_conversation_send",
    ),
    ToolContract(
        schema=ToolSchema(
            name="conversation_summarize",
            description="Summarize one consultation, including missing facts and MVP warnings.",
            parameters=_object_schema({"conversation_id": {"type": "string"}}, ["conversation_id"]),
        ),
        allowed_scopes=frozenset({"local_trusted", "buyer", "buyer_cli", "merchant", "merchant_agent", "operator"}),
        handler_name="_dispatch_conversation_summarize",
    ),
    ToolContract(
        schema=ToolSchema(
            name="human_review_flag",
            description="Flag a consultation for merchant human review when the request needs escalation.",
            parameters=_object_schema(
                {
                    "conversation_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "severity": {"type": "string", "enum": ["review", "block"]},
                },
                ["conversation_id", "reason"],
            ),
        ),
        allowed_scopes=frozenset({"local_trusted", "merchant", "merchant_agent", "operator"}),
        handler_name="_dispatch_human_review_flag",
    ),
    ToolContract(
        schema=ToolSchema(
            name="merchant_reply",
            description="Append a merchant-agent consultation reply within public merchant rules.",
            parameters=_object_schema(
                {
                    "conversation_id": {"type": "string"},
                    "intent": {"type": "string"},
                    "text": {"type": "string"},
                    "human_required": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                ["conversation_id", "intent", "text"],
            ),
        ),
        allowed_scopes=frozenset({"local_trusted", "merchant", "merchant_agent"}),
        handler_name="_dispatch_merchant_reply",
    ),
)

TOOL_CONTRACT_BY_NAME = {contract.name: contract for contract in TOOL_CONTRACTS}


def marketplace_tool_contracts() -> tuple[ToolContract, ...]:
    return TOOL_CONTRACTS


def marketplace_tool_schema_objects() -> list[ToolSchema]:
    return [contract.schema for contract in TOOL_CONTRACTS]


def marketplace_tool_schemas() -> list[dict[str, Any]]:
    return [contract.schema.as_openai_tool() for contract in TOOL_CONTRACTS]


def require_tool_contract(tool_name: str, token_scope: str) -> ToolContract:
    contract = TOOL_CONTRACT_BY_NAME.get(tool_name)
    if contract is None:
        raise ToolContractError(f"Unknown or disallowed marketplace tool: {tool_name}")
    if token_scope not in contract.allowed_scopes:
        raise ToolContractError(f"tool {tool_name} is not allowed for token scope {token_scope}")
    return contract


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
    return False


def _validate_schema(value: Any, schema: dict[str, Any], path: str) -> None:
    expected = str(schema.get("type") or "")
    if expected and not _schema_type_matches(value, expected):
        raise ToolContractError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(str(item) for item in schema["enum"])
        raise ToolContractError(f"{path} must be one of: {allowed}")
    if expected == "object":
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        for name in required:
            if name not in value:
                raise ToolContractError(f"{path}.{name} is required")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ToolContractError(f"{path} contains unsupported field: {extras[0]}")
        for name, child in properties.items():
            if name in value:
                _validate_schema(value[name], child, f"{path}.{name}")
    elif expected == "array" and "items" in schema:
        for index, item in enumerate(value):
            _validate_schema(item, schema["items"], f"{path}[{index}]")


def normalize_tool_arguments(tool_name: str, arguments: Any) -> dict[str, Any]:
    contract = TOOL_CONTRACT_BY_NAME.get(tool_name)
    if contract is None:
        raise ToolContractError(f"Unknown or disallowed marketplace tool: {tool_name}")
    if arguments is None:
        arguments = {}
    _validate_schema(arguments, contract.schema.parameters, "arguments")
    normalized = dict(arguments)
    if tool_name == "conversation_send":
        sender = str(normalized["sender"])
        if sender not in CONVERSATION_SENDERS:
            raise ToolContractError("conversation_send only supports buyer or buyer_cli senders")
        normalized["sender"] = sender
    return normalized


def prepare_tool_call(tool_name: str, token_scope: str, arguments: Any) -> PreparedToolCall:
    contract = require_tool_contract(tool_name, token_scope)
    return PreparedToolCall(contract=contract, arguments=normalize_tool_arguments(tool_name, arguments))

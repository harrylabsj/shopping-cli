"""Typed tool schemas exposed to optional LLM providers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


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


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def marketplace_tool_schema_objects() -> list[ToolSchema]:
    return [
        ToolSchema(
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
        ToolSchema(
            name="conversation_send",
            description="Append a buyer-side consultation message. This never creates an order or payment.",
            parameters=_object_schema(
                {
                    "conversation_id": {"type": "string"},
                    "sender": {"type": "string", "enum": ["buyer", "buyer_cli"]},
                    "intent": {"type": "string"},
                    "text": {"type": "string"},
                },
                ["conversation_id", "sender", "intent", "text"],
            ),
        ),
        ToolSchema(
            name="conversation_summarize",
            description="Summarize one consultation, including missing facts and MVP warnings.",
            parameters=_object_schema({"conversation_id": {"type": "string"}}, ["conversation_id"]),
        ),
        ToolSchema(
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
        ToolSchema(
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
    ]


def marketplace_tool_schemas() -> list[dict[str, Any]]:
    return [tool.as_openai_tool() for tool in marketplace_tool_schema_objects()]

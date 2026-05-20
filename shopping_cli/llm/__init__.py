"""LLM provider and tool-schema contracts for optional runtime adapters."""

from shopping_cli.llm.dispatcher import MarketplaceToolDispatcher, dispatch_marketplace_tool
from shopping_cli.llm.prompts import buyer_system_prompt, merchant_system_prompt
from shopping_cli.llm.providers import LLMResponse, OpenAICompatibleProvider, provider_from_env
from shopping_cli.llm.tools import marketplace_tool_schemas

__all__ = [
    "LLMResponse",
    "MarketplaceToolDispatcher",
    "OpenAICompatibleProvider",
    "buyer_system_prompt",
    "dispatch_marketplace_tool",
    "marketplace_tool_schemas",
    "merchant_system_prompt",
    "provider_from_env",
]

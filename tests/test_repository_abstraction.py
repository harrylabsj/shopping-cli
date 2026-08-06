"""Repository abstraction contract tests (§19.2, v3.0-P3).

固化 CatalogRepository / ConversationRepository / AuditRepository 契约与
SQLite 现状实现函数的映射关系，防止接口漂移：

1. Protocol 声明的每个方法都有 SQLite 实现函数（显式映射表 —— 本表同时
   是 `docs/shopping-cli-a2a-postgres-adapter-seam-v1.0.md` 接缝点清单的
   可执行版本）。
2. 反向检查：`sqlite_repository` 的全部公开函数（除 ID 生成纯函数
   ``new_catalog_agent_id``，它不读不写）必须被映射表覆盖 —— 新增实现
   函数必须补进映射表与 Protocol。

契约方法名与 SQLite 实现函数名允许不同（如 Protocol ``search`` ↔
``search_catalog_agents``）；签名差异（connection 注入 vs 值参数）是
PG 适配接缝文档的主题，不在此断言。
"""

from __future__ import annotations

import inspect
import unittest

from shopping_cli.agent_catalog import repository, sqlite_repository
from shopping_cli.core import conversations as core_conversations
from shopping_cli.core import harness as core_harness
from shopping_cli.services import conversations as services_conversations

# Protocol 方法名 → SQLite 现状实现函数名（catalog 域）。
_CATALOG_MAPPING: dict[str, str] = {
    "upsert_catalog_agent": "upsert_catalog_agent",
    "require_catalog_agent": "require_catalog_agent",
    "get_catalog_agent": "get_catalog_agent_with_merchant",
    "get_catalog_agent_by_domain": "get_catalog_agent_by_domain",
    "list_catalog_agents": "list_catalog_agents",
    "list_catalog_agents_by_merchant": "list_catalog_agents_by_merchant",
    "set_verification_status": "set_verification_status",
    "set_catalog_agent_merchant": "set_catalog_agent_merchant",
    "list_capabilities": "list_capabilities",
    "upsert_capabilities": "replace_capabilities",
    "list_endpoints": "list_endpoints",
    "upsert_profile_endpoints": "upsert_profile_endpoints",
    "replace_skills": "replace_skills",
    "list_skills": "list_skills",
    "insert_profile_snapshot": "insert_profile_snapshot",
    "latest_profile_snapshot": "latest_profile_snapshot",
    "list_profile_snapshots": "list_profile_snapshots",
    "insert_verification": "insert_verification",
    "list_verifications": "list_verifications",
    "insert_trust_observation": "insert_trust_observation",
    "list_trust_observations": "list_trust_observations",
    "count_trust_observations": "count_trust_observations",
    "trust_observation_counts_by_kind": "trust_observation_counts_by_kind",
    "search": "search_catalog_agents",
    "append_audit": "append_catalog_audit",
    "enforce_catalog_register_domain_limit": "enforce_catalog_register_domain_limit",
}

_CONVERSATION_MAPPING: dict[str, str] = {
    "ensure_conversation": "ensure_conversation",
    "require_conversation": "require_conversation",
    "append_message": "append_message",
    "conversation_messages": "conversation_messages",
    # close 的现状实现在 use-case 层（services/conversations.py，直接 SQL
    # 更新 conversations 状态 + moderation 门禁）；core 层只有 append/flag。
    "close_conversation": "close_conversation",
}
_CONVERSATION_IMPL_MODULES = (core_conversations, services_conversations)

_AUDIT_MAPPING: dict[str, str] = {
    "append_event": "append_audit_event",
    "conversation_audit_events": "conversation_audit_events",
}

# sqlite_repository 中非持久化操作的函数，反向检查豁免：
# - new_catalog_agent_id: ID 生成纯函数（不读不写）；
# - now_iso: 从 db.session re-export 的时间工具（inspect 误报为本地函数）。
_NON_PERSISTENCE_FUNCTIONS = frozenset({"new_catalog_agent_id", "now_iso"})


def _protocol_methods(protocol: type) -> set[str]:
    return {name for name in dir(protocol) if not name.startswith("_")}


def _public_functions(module: object) -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(module, inspect.isfunction)
        if not name.startswith("_")
    }


class CatalogRepositoryMappingTest(unittest.TestCase):
    def test_every_protocol_method_has_sqlite_implementation(self) -> None:
        missing = sorted(set(_CATALOG_MAPPING) - _protocol_methods(repository.CatalogRepository))
        self.assertEqual(missing, [])
        for protocol_method, impl_name in _CATALOG_MAPPING.items():
            self.assertTrue(
                callable(getattr(sqlite_repository, impl_name, None)),
                f"{impl_name!r} (impl of {protocol_method!r}) not found in sqlite_repository",
            )

    def test_every_sqlite_catalog_function_is_covered(self) -> None:
        uncovered = sorted(
            _public_functions(sqlite_repository)
            - set(_CATALOG_MAPPING.values())
            - _NON_PERSISTENCE_FUNCTIONS
        )
        self.assertEqual(
            uncovered, [], "sqlite_repository functions missing from the CatalogRepository mapping"
        )


class ConversationRepositoryMappingTest(unittest.TestCase):
    def test_every_protocol_method_has_sqlite_implementation(self) -> None:
        missing = sorted(set(_CONVERSATION_MAPPING) - _protocol_methods(repository.ConversationRepository))
        self.assertEqual(missing, [])
        for protocol_method, impl_name in _CONVERSATION_MAPPING.items():
            found = any(
                callable(getattr(module, impl_name, None))
                for module in _CONVERSATION_IMPL_MODULES
            )
            self.assertTrue(
                found,
                f"{impl_name!r} (impl of {protocol_method!r}) not found in any conversation module",
            )


class AuditRepositoryMappingTest(unittest.TestCase):
    def test_every_protocol_method_has_sqlite_implementation(self) -> None:
        missing = sorted(set(_AUDIT_MAPPING) - _protocol_methods(repository.AuditRepository))
        self.assertEqual(missing, [])
        for protocol_method, impl_name in _AUDIT_MAPPING.items():
            self.assertTrue(
                callable(getattr(core_harness, impl_name, None)),
                f"{impl_name!r} (impl of {protocol_method!r}) not found in core.harness",
            )


if __name__ == "__main__":
    unittest.main()

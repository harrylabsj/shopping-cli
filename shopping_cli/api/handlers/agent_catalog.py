"""Agent Catalog public read API handlers (§10.1).

v2.1 scope: public read-only.  Registration/refresh/verify/claim belong to
Phase 2 and are NOT implemented here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shopping_cli.agent_catalog.serializers import catalog_search_result
from shopping_cli.agent_catalog.sqlite_repository import (
    get_catalog_agent_with_merchant,
    list_capabilities,
    list_catalog_agents as _list_catalog_agents,
    list_catalog_agents_by_merchant as _list_catalog_agents_by_merchant,
    list_endpoints,
)
from shopping_cli.core.errors import NotFoundError
from shopping_cli.db.session import db_session
from shopping_cli.services.agent_catalog import search_catalog_agents as _search_catalog_agents_service

from .common import result_limit


def _serialize_row(row: dict[str, Any], conn: Any) -> dict[str, Any]:
    """Serialize a catalog agent row + merchant join through public serializer."""
    cagt_id = str(row.get("catalog_agent_id", ""))
    caps = list_capabilities(conn, cagt_id)
    eps = list_endpoints(conn, cagt_id)
    merchant: dict[str, Any] = {
        "id": row.get("merchant_id", ""),
        "name": row.get("merchant_name", ""),
        "city": row.get("merchant_city", ""),
        "service_area": row.get("merchant_service_area", ""),
        "tags_json": row.get("merchant_tags_json", "[]"),
    }
    return catalog_search_result(
        catalog_agent=row,
        merchant=merchant,
        capabilities=caps,
        endpoints=eps,
    )


def list_catalog_agents(db_path: str | Path, query: dict[str, Any]) -> dict[str, Any]:
    """GET /v1/agent-catalog/agents — paginated list."""
    limit = result_limit(query.get("limit"), default=20)
    cursor = str(query.get("cursor") or "").strip()
    with db_session(db_path) as conn:
        rows, next_cursor = _list_catalog_agents(conn, limit=limit, cursor=cursor)
        results = [_serialize_row(row, conn) for row in rows]
        return {
            "ok": True,
            "results": results,
            "next_cursor": next_cursor,
        }


def get_catalog_agent(db_path: str | Path, catalog_agent_id: str) -> dict[str, Any]:
    """GET /v1/agent-catalog/agents/{catalog_agent_id} — detail."""
    with db_session(db_path) as conn:
        row = get_catalog_agent_with_merchant(conn, str(catalog_agent_id).strip())
        if row is None:
            raise NotFoundError(f"Unknown catalog agent: {catalog_agent_id}")
        return {
            "ok": True,
            "catalog_agent": _serialize_row(row, conn),
        }


def search_agent_catalog(db_path: str | Path, query: dict[str, Any]) -> dict[str, Any]:
    """GET /v1/agent-catalog/agents/search — filtered search (§8.2)."""
    limit = result_limit(query.get("limit"), default=20)
    with db_session(db_path) as conn:
        result = _search_catalog_agents_service(
            conn=conn,
            q=str(query.get("q") or ""),
            category=str(query.get("category") or ""),
            skill=str(query.get("skill") or ""),
            capability=str(query.get("capability") or ""),
            protocol=str(query.get("protocol") or ""),
            hosting_mode=str(query.get("hosting_mode") or ""),
            verification_status=str(query.get("verification_status") or ""),
            verified_after=str(query.get("verified_after") or ""),
            limit=limit,
            cursor=str(query.get("cursor") or "").strip(),
        )
        result["ok"] = True
        return result


def list_merchant_catalog_agents(
    db_path: str | Path, merchant_id: str, query: dict[str, Any]
) -> dict[str, Any]:
    """GET /v1/agent-catalog/merchants/{merchant_id}/agents — paginated list."""
    limit = result_limit(query.get("limit"), default=20)
    cursor = str(query.get("cursor") or "").strip()
    with db_session(db_path) as conn:
        rows, next_cursor = _list_catalog_agents_by_merchant(
            conn, merchant_id=str(merchant_id).strip(), limit=limit, cursor=cursor
        )
        results = [_serialize_row(row, conn) for row in rows]
        return {
            "ok": True,
            "results": results,
            "next_cursor": next_cursor,
        }

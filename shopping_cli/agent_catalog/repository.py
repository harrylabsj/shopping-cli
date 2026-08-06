"""Abstract CatalogRepository — persistence-abstraction boundary (§19.2).

All catalog write/read paths MUST go through this interface so the
discovery plane can gain an independent persistence adapter later
(e.g. PostgresCatalogRepository) without touching service logic.
"""

from __future__ import annotations

from typing import Any, Protocol


class CatalogRepository(Protocol):
    """Persistence contract for the Commerce Agent Catalog.

    SQLite is the MVP implementation; a Postgres adapter may be added
    when the catalog becomes a public multi-instance service.
    """

    def upsert_catalog_agent(
        self,
        catalog_agent_id: str,
        merchant_id: str,
        hosted_runtime_agent_id: str,
        display_name: str,
        provider_name: str,
        canonical_domain: str,
        agent_type: str,
        source_type: str,
        lifecycle_status: str,
        verification_status: str,
        hosting_mode: str,
    ) -> dict[str, Any]:
        """Insert or update a catalog_agents row.  Returns the row as a dict."""
        ...

    def get_catalog_agent(self, catalog_agent_id: str) -> dict[str, Any] | None:
        """Return the catalog_agents row joined with merchant name, or None."""
        ...

    def list_capabilities(self, catalog_agent_id: str) -> list[dict[str, Any]]:
        """Return all agent_capabilities rows for a catalog agent."""
        ...

    def upsert_capabilities(
        self,
        catalog_agent_id: str,
        capabilities: list[dict[str, Any]],
    ) -> None:
        """Replace all capabilities for a catalog agent atomically."""
        ...

    def list_endpoints(self, catalog_agent_id: str) -> list[dict[str, Any]]:
        """Return all agent_endpoints rows for a catalog agent."""
        ...

    def search(
        self,
        q: str,
        category: str,
        skill: str,
        capability: str,
        protocol: str,
        hosting_mode: str,
        verification_status: str,
        verified_after: str,
        limit: int,
        cursor: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Search catalog agents with hard filters and deterministic ordering.

        Returns (results, next_cursor).  next_cursor is None when there are
        no more pages.
        """
        ...

    def append_audit(
        self,
        catalog_agent_id: str,
        actor: str,
        event: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Write an audit event scoped to a catalog agent."""
        ...

"""SQLite-backed CatalogRepository — MVP persistence adapter.

Follows the same patterns as shopping_cli/core/catalog.py and
shopping_cli/services/agents.py: raw sqlite3.Connection with row_factory
already set to sqlite3.Row by the session layer.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from shopping_cli.core.errors import NotFoundError
from shopping_cli.db.session import now_iso


def _row_to_dict(row: sqlite3.Row, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    d = dict(row)
    if overrides:
        d.update(overrides)
    return d


# ── catalog_agents ──────────────────────────────────────────────────────────


def _insert_catalog_agent(
    conn: sqlite3.Connection,
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
    ts = now_iso()
    # Empty-string FK values must become None to satisfy SQLite FK pragma:
    # a non-NULL '' value triggers the FK check and fails when no matching
    # parent row exists.
    _mrc = merchant_id or None
    _hri = hosted_runtime_agent_id or None
    conn.execute(
        """
        insert into catalog_agents(
            catalog_agent_id, merchant_id, hosted_runtime_agent_id,
            display_name, provider_name, canonical_domain, agent_type,
            source_type, lifecycle_status, verification_status, hosting_mode,
            first_seen_at, last_seen_at, last_verified_at, created_at, updated_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            catalog_agent_id,
            _mrc,
            _hri,
            display_name,
            provider_name,
            canonical_domain,
            agent_type,
            source_type,
            lifecycle_status,
            verification_status,
            hosting_mode,
            ts,
            ts,
            ts if verification_status == "commerce_verified" else "",
            ts,
            ts,
        ),
    )
    return require_catalog_agent(conn, catalog_agent_id)


def _update_catalog_agent(
    conn: sqlite3.Connection,
    catalog_agent_id: str,
    **fields: Any,
) -> dict[str, Any]:
    allowed = {
        "merchant_id",
        "hosted_runtime_agent_id",
        "display_name",
        "provider_name",
        "canonical_domain",
        "agent_type",
        "source_type",
        "lifecycle_status",
        "verification_status",
        "hosting_mode",
        "last_seen_at",
        "last_verified_at",
    }
    updates: list[str] = []
    values: list[Any] = []
    for col, val in fields.items():
        if col in allowed and val is not None:
            updates.append(f"{col} = ?")
            values.append(val)
    if not updates:
        return require_catalog_agent(conn, catalog_agent_id)
    updates.append("updated_at = ?")
    values.append(now_iso())
    values.append(catalog_agent_id)
    conn.execute(
        f"update catalog_agents set {', '.join(updates)} where catalog_agent_id = ?",
        values,
    )
    return require_catalog_agent(conn, catalog_agent_id)


def upsert_catalog_agent(
    conn: sqlite3.Connection,
    catalog_agent_id: str,
    merchant_id: str = "",
    hosted_runtime_agent_id: str = "",
    display_name: str = "",
    provider_name: str = "",
    canonical_domain: str = "",
    agent_type: str = "",
    source_type: str = "hosted",
    lifecycle_status: str = "active",
    verification_status: str = "discovered",
    hosting_mode: str = "unknown",
) -> dict[str, Any]:
    existing = conn.execute(
        "select catalog_agent_id from catalog_agents where catalog_agent_id = ?",
        (catalog_agent_id,),
    ).fetchone()
    if existing is None:
        return _insert_catalog_agent(
            conn,
            catalog_agent_id=catalog_agent_id,
            merchant_id=merchant_id,
            hosted_runtime_agent_id=hosted_runtime_agent_id,
            display_name=display_name,
            provider_name=provider_name,
            canonical_domain=canonical_domain,
            agent_type=agent_type,
            source_type=source_type,
            lifecycle_status=lifecycle_status,
            verification_status=verification_status,
            hosting_mode=hosting_mode,
        )
    return _update_catalog_agent(
        conn,
        catalog_agent_id,
        merchant_id=merchant_id,
        hosted_runtime_agent_id=hosted_runtime_agent_id,
        display_name=display_name,
        provider_name=provider_name,
        canonical_domain=canonical_domain,
        agent_type=agent_type,
        source_type=source_type,
        lifecycle_status=lifecycle_status,
        verification_status=verification_status,
        hosting_mode=hosting_mode,
        last_seen_at=now_iso(),
    )


def require_catalog_agent(conn: sqlite3.Connection, catalog_agent_id: str) -> dict[str, Any]:
    row = conn.execute(
        "select * from catalog_agents where catalog_agent_id = ?", (catalog_agent_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown catalog agent: {catalog_agent_id}")
    return _row_to_dict(row)


def get_catalog_agent_with_merchant(
    conn: sqlite3.Connection, catalog_agent_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        select ca.*, m.name as merchant_name, m.city as merchant_city,
               m.service_area as merchant_service_area,
               m.tags_json as merchant_tags_json
        from catalog_agents ca
        left join merchants m on m.id = ca.merchant_id
        where ca.catalog_agent_id = ?
        """,
        (catalog_agent_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


# ── agent_capabilities ──────────────────────────────────────────────────────


def list_capabilities(conn: sqlite3.Connection, catalog_agent_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select * from agent_capabilities
        where catalog_agent_id = ?
        order by namespace, capability_id
        """,
        (catalog_agent_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def replace_capabilities(
    conn: sqlite3.Connection,
    catalog_agent_id: str,
    capabilities: list[dict[str, Any]],
) -> None:
    """Atomically replace all capabilities for a catalog agent."""
    conn.execute(
        "delete from agent_capabilities where catalog_agent_id = ?",
        (catalog_agent_id,),
    )
    ts = now_iso()
    for cap in capabilities:
        conn.execute(
            """
            insert into agent_capabilities(
                catalog_agent_id, namespace, capability_id, version,
                required, source, schema_url, spec_url, last_verified_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                catalog_agent_id,
                cap["namespace"],
                cap["capability_id"],
                cap.get("version", ""),
                int(cap.get("required", 0)),
                cap.get("source", ""),
                cap.get("schema_url", ""),
                cap.get("spec_url", ""),
                cap.get("last_verified_at", ts),
            ),
        )


# ── agent_endpoints ─────────────────────────────────────────────────────────


def list_endpoints(conn: sqlite3.Connection, catalog_agent_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "select * from agent_endpoints where catalog_agent_id = ? order by preference desc, endpoint_id",
        (catalog_agent_id,),
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ── Search ──────────────────────────────────────────────────────────────────


def search_catalog_agents(
    conn: sqlite3.Connection,
    q: str = "",
    category: str = "",
    skill: str = "",
    capability: str = "",
    protocol: str = "",
    hosting_mode: str = "",
    verification_status: str = "",
    verified_after: str = "",
    limit: int = 20,
    cursor: str = "",
) -> tuple[list[dict[str, Any]], str | None]:
    """Hard-filtered, deterministically-ordered catalog agent search.

    Returns (results, next_cursor).  next_cursor is None at the last page.
    """
    clauses: list[str] = []
    params: list[Any] = []

    # ── hard filters ────────────────────────────────────────────────────
    if hosting_mode:
        clauses.append("ca.hosting_mode = ?")
        params.append(hosting_mode)

    if verification_status:
        clauses.append("ca.verification_status = ?")
        params.append(verification_status)

    if verified_after:
        clauses.append("ca.last_verified_at >= ?")
        params.append(verified_after)

    # q: free-text search across display_name, provider_name, canonical_domain
    if q:
        clauses.append(
            "(ca.display_name like ? or ca.provider_name like ? or ca.canonical_domain like ?)"
        )
        like_q = f"%{q}%"
        params.extend([like_q, like_q, like_q])

    # category: match against merchant tags or products.category via subquery
    if category:
        clauses.append(
            """(
            exists (select 1 from merchants m2 where m2.id = ca.merchant_id and m2.tags_json like ?)
            or
            exists (select 1 from products p where p.merchant_id = ca.merchant_id and p.category = ? and p.active = 1)
            )"""
        )
        params.extend([f"%{category}%", category])

    # capability: match against agent_capabilities
    if capability:
        clauses.append(
            """exists (
            select 1 from agent_capabilities ac
            where ac.catalog_agent_id = ca.catalog_agent_id
              and (ac.capability_id = ? or ac.namespace || ':' || ac.capability_id = ?)
            )"""
        )
        params.extend([capability, capability])

    # skill: match against agent_skills
    if skill:
        clauses.append(
            """exists (
            select 1 from agent_skills ask
            where ask.catalog_agent_id = ca.catalog_agent_id
              and (ask.skill_id = ? or ask.name like ?)
            )"""
        )
        params.extend([skill, f"%{skill}%"])

    # protocol: match against agent_endpoints
    if protocol:
        clauses.append(
            """exists (
            select 1 from agent_endpoints ae
            where ae.catalog_agent_id = ca.catalog_agent_id
              and (ae.protocol = ? or ae.protocol_version = ?)
            )"""
        )
        params.extend([protocol, protocol])

    # ── cursor (keyset pagination on catalog_agent_id) ──────────────────
    if cursor:
        clauses.append("ca.catalog_agent_id > ?")
        params.append(cursor)

    where = ""
    if clauses:
        where = "where " + " and ".join(clauses)

    # ── deterministic ordering (§8.3) ───────────────────────────────────
    # Priority: verification_status rank → last_verified_at desc → display_name → catalog_agent_id
    order = """
    order by
        case ca.verification_status
            when 'commerce_verified' then 0
            when 'agent_verified' then 1
            when 'domain_verified' then 2
            when 'profile_valid' then 3
            when 'discovered' then 4
            when 'stale' then 5
            when 'unreachable' then 6
            when 'suspended' then 7
            when 'rejected' then 8
            else 9
        end,
        ca.last_verified_at desc,
        ca.display_name,
        ca.catalog_agent_id
    """

    sql = f"""
        select ca.*, m.name as merchant_name, m.city as merchant_city,
               m.service_area as merchant_service_area,
               m.tags_json as merchant_tags_json
        from catalog_agents ca
        left join merchants m on m.id = ca.merchant_id
        {where}
        {order}
        limit ?
    """
    params.append(limit + 1)  # fetch one extra to detect next page

    rows = conn.execute(sql, params).fetchall()

    has_more = len(rows) > limit
    result_rows = rows[:limit]
    next_cursor: str | None = None
    if has_more and result_rows:
        next_cursor = str(result_rows[-1]["catalog_agent_id"])

    results = [_row_to_dict(r) for r in result_rows]
    return results, next_cursor


# ── List (paginated) ─────────────────────────────────────────────────────────


def list_catalog_agents(
    conn: sqlite3.Connection,
    limit: int = 20,
    cursor: str = "",
) -> tuple[list[dict[str, Any]], str | None]:
    """Paginated list of all catalog agents, deterministically ordered."""
    clauses: list[str] = []
    params: list[Any] = []

    if cursor:
        clauses.append("ca.catalog_agent_id > ?")
        params.append(cursor)

    where = ""
    if clauses:
        where = "where " + " and ".join(clauses)

    order = """
    order by
        case ca.verification_status
            when 'commerce_verified' then 0
            when 'agent_verified' then 1
            when 'domain_verified' then 2
            when 'profile_valid' then 3
            when 'discovered' then 4
            when 'stale' then 5
            when 'unreachable' then 6
            when 'suspended' then 7
            when 'rejected' then 8
            else 9
        end,
        ca.last_verified_at desc,
        ca.display_name,
        ca.catalog_agent_id
    """

    sql = f"""
        select ca.*, m.name as merchant_name, m.city as merchant_city,
               m.service_area as merchant_service_area,
               m.tags_json as merchant_tags_json
        from catalog_agents ca
        left join merchants m on m.id = ca.merchant_id
        {where}
        {order}
        limit ?
    """
    params.append(limit + 1)

    rows = conn.execute(sql, params).fetchall()

    has_more = len(rows) > limit
    result_rows = rows[:limit]
    next_cursor: str | None = None
    if has_more and result_rows:
        next_cursor = str(result_rows[-1]["catalog_agent_id"])

    results = [_row_to_dict(r) for r in result_rows]
    return results, next_cursor


def list_catalog_agents_by_merchant(
    conn: sqlite3.Connection,
    merchant_id: str,
    limit: int = 20,
    cursor: str = "",
) -> tuple[list[dict[str, Any]], str | None]:
    """Paginated list of catalog agents for a specific merchant."""
    clauses: list[str] = ["ca.merchant_id = ?"]
    params: list[Any] = [merchant_id]

    if cursor:
        clauses.append("ca.catalog_agent_id > ?")
        params.append(cursor)

    order = """
    order by
        case ca.verification_status
            when 'commerce_verified' then 0
            when 'agent_verified' then 1
            when 'domain_verified' then 2
            when 'profile_valid' then 3
            when 'discovered' then 4
            when 'stale' then 5
            when 'unreachable' then 6
            when 'suspended' then 7
            when 'rejected' then 8
            else 9
        end,
        ca.last_verified_at desc,
        ca.display_name,
        ca.catalog_agent_id
    """

    sql = f"""
        select ca.*, m.name as merchant_name, m.city as merchant_city,
               m.service_area as merchant_service_area,
               m.tags_json as merchant_tags_json
        from catalog_agents ca
        left join merchants m on m.id = ca.merchant_id
        where {' and '.join(clauses)}
        {order}
        limit ?
    """
    params.append(limit + 1)

    rows = conn.execute(sql, params).fetchall()

    has_more = len(rows) > limit
    result_rows = rows[:limit]
    next_cursor: str | None = None
    if has_more and result_rows:
        next_cursor = str(result_rows[-1]["catalog_agent_id"])

    results = [_row_to_dict(r) for r in result_rows]
    return results, next_cursor


# ── Audit ───────────────────────────────────────────────────────────────────


def append_catalog_audit(
    conn: sqlite3.Connection,
    catalog_agent_id: str,
    actor: str,
    event: str,
    details: dict[str, Any] | None = None,
) -> int:
    """Write a catalog-scoped audit event.  Returns the new event id."""
    from shopping_cli.db.session import encode_json as _encode

    payload = dict(details or {})
    payload.setdefault("schema_version", 1)
    payload.setdefault("event_type", str(event or ""))
    payload.setdefault("catalog_agent_id", catalog_agent_id)

    cursor = conn.execute(
        """
        insert into audit_events(conversation_id, actor, event, details_json, created_at)
        values (?, ?, ?, ?, ?)
        """,
        ("", actor, event, _encode(payload), now_iso()),
    )
    if cursor.lastrowid is None:
        raise RuntimeError("audit event insert did not return an id")
    return cursor.lastrowid

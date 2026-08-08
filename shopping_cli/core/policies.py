"""Merchant policy reference store and retrieval.

Policies are read-only reference clauses a merchant publishes — shipping,
invoice, after-sales, bulk, storage terms — so an assistant can cite a
traceable source (acceptance assertion G3) instead of inventing terms, and
say "merchant has not stated this" when no clause matches (G4).

This is reference data only. Policies carry no price, stock, order, or payment
semantics; they never reserve inventory or complete a transaction. Clauses that
require human judgement (refunds, custom requests, self-invented discounts) are
flagged ``high_risk`` so the caller escalates instead of promising.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from shopping_cli.core.catalog import fts_query, fts_search_document, parse_tags, require_merchant, tokenize
from shopping_cli.core.errors import ConflictError, NotFoundError, ValidationError
from shopping_cli.core.harness import append_audit_event
from shopping_cli.core.limits import MAX_SHORT_TEXT_CHARS, bounded_text, safe_non_negative_int as _safe_non_negative_int
from shopping_cli.db.session import decode_json, encode_json, now_iso

DEFAULT_POLICY_SEARCH_CANDIDATE_LIMIT = 1000
MAX_POLICY_SEARCH_CANDIDATE_LIMIT = 5000
MAX_SQLITE_INTEGER = 2**63 - 1
POLICY_SEARCH_INDEX_TABLE = "policy_search_index"



def require_policy(conn: sqlite3.Connection, merchant_id: str, code: str) -> sqlite3.Row:
    row = conn.execute(
        "select * from policies where merchant_id = ? and code = ?",
        (merchant_id, code),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown policy: {merchant_id}/{code}")
    return row


def create_policy(
    conn: sqlite3.Connection,
    merchant_id: str,
    code: str,
    body: str,
    category: str = "",
    title: str = "",
    tags: str | list[str] | None = None,
    high_risk: bool = False,
) -> dict[str, Any]:
    merchant_id = bounded_text(merchant_id, "merchant id", MAX_SHORT_TEXT_CHARS).strip()
    code = bounded_text(code, "policy code", MAX_SHORT_TEXT_CHARS).strip()
    body = bounded_text(body, "policy body").strip()
    category = bounded_text(category, "policy category", MAX_SHORT_TEXT_CHARS)
    title = bounded_text(title, "policy title", MAX_SHORT_TEXT_CHARS)
    if not merchant_id:
        raise ValidationError("merchant id is required")
    if not code:
        raise ValidationError("policy code is required")
    if not body:
        raise ValidationError("policy body is required")
    require_merchant(conn, merchant_id)
    now = now_iso()
    try:
        conn.execute(
            """
            insert into policies(
                merchant_id, code, category, title, body, tags_json, high_risk,
                active, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                merchant_id,
                code,
                str(category or "").strip(),
                str(title or "").strip(),
                body,
                encode_json(parse_tags(tags)),
                1 if high_risk else 0,
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(f"Policy already exists: {merchant_id}/{code}") from exc
    sync_policy_search_index(conn, merchant_id)
    append_audit_event(conn, "", merchant_id, "policy_created", {"merchant_id": merchant_id, "code": code})
    return policy_summary(conn, merchant_id, code)


def update_policy(
    conn: sqlite3.Connection,
    merchant_id: str,
    code: str,
    body: str | None = None,
    category: str | None = None,
    title: str | None = None,
    tags: str | list[str] | None = None,
    high_risk: bool | None = None,
) -> dict[str, Any]:
    """Update an existing policy and sync the FTS search index.

    Only fields that are not ``None`` are updated.  The search index is
    re-synced so that FTS results reflect the new text immediately.
    """
    merchant_id = bounded_text(merchant_id, "merchant id", MAX_SHORT_TEXT_CHARS).strip()
    code = bounded_text(code, "policy code", MAX_SHORT_TEXT_CHARS).strip()
    if not merchant_id:
        raise ValidationError("merchant id is required")
    if not code:
        raise ValidationError("policy code is required")
    require_policy(conn, merchant_id, code)
    updates: list[str] = []
    values: list[Any] = []
    if body is not None:
        body = bounded_text(body, "policy body").strip()
        if not body:
            raise ValidationError("policy body is required")
        updates.append("body = ?")
        values.append(body)
    if category is not None:
        updates.append("category = ?")
        values.append(bounded_text(category, "policy category", MAX_SHORT_TEXT_CHARS).strip())
    if title is not None:
        title = bounded_text(title, "policy title", MAX_SHORT_TEXT_CHARS).strip()
        if not title:
            raise ValidationError("policy title is required")
        updates.append("title = ?")
        values.append(title)
    if tags is not None:
        updates.append("tags_json = ?")
        values.append(encode_json(parse_tags(tags)))
    if high_risk is not None:
        updates.append("high_risk = ?")
        values.append(1 if high_risk else 0)
    if updates:
        updates.append("updated_at = ?")
        values.append(now_iso())
        values.extend([merchant_id, code])
        conn.execute(
            f"update policies set {', '.join(updates)} where merchant_id = ? and code = ?",
            values,
        )
        sync_policy_search_index(conn, merchant_id)
        append_audit_event(conn, "", merchant_id, "policy_updated", {"merchant_id": merchant_id, "code": code})
    return policy_summary(conn, merchant_id, code)


def policy_summary(conn: sqlite3.Connection, merchant_id: str, code: str) -> dict[str, Any]:
    return _policy_to_summary(require_policy(conn, merchant_id, code))


def _policy_to_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "merchant_id": row["merchant_id"],
        "code": row["code"],
        "category": row["category"],
        "title": row["title"],
        "body": row["body"],
        "tags": decode_json(row["tags_json"], []),
        "high_risk": bool(_safe_non_negative_int(row["high_risk"])),
    }


def list_policies(
    conn: sqlite3.Connection,
    merchant_id: str = "",
    category: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    merchant_id = str(merchant_id or "").strip()
    category = str(category or "").strip()
    window_limit = _safe_non_negative_int(limit)
    window_offset = _safe_non_negative_int(offset)
    values: list[Any] = []
    sql = "select * from policies where active = 1"
    if merchant_id:
        sql += " and merchant_id = ?"
        values.append(merchant_id)
    if category:
        sql += " and lower(category) = lower(?)"
        values.append(category)
    sql += " order by merchant_id, code limit ? offset ?"
    values.extend([window_limit, window_offset])
    rows = conn.execute(sql, values).fetchall()
    return [_policy_to_summary(row) for row in rows]


def _policy_search_text(row: sqlite3.Row) -> str:
    fields = [
        row["code"],
        row["category"],
        row["title"],
        row["body"],
        " ".join(decode_json(row["tags_json"], [])),
    ]
    return " ".join(str(field) for field in fields if field)


def policy_search_index_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            f"""
            create virtual table if not exists {POLICY_SEARCH_INDEX_TABLE}
            using fts5(merchant_id unindexed, text, tokenize='unicode61')
            """
        )
    except sqlite3.OperationalError:
        return False
    return True


def _policy_search_rows(conn: sqlite3.Connection, merchant_id: str = "") -> list[sqlite3.Row]:
    values: list[Any] = []
    sql = "select rowid, * from policies where active = 1"
    if merchant_id:
        sql += " and merchant_id = ?"
        values.append(merchant_id)
    sql += " order by merchant_id, code"
    return conn.execute(sql, values).fetchall()


def rebuild_policy_search_index(conn: sqlite3.Connection) -> bool:
    if not policy_search_index_available(conn):
        return False
    conn.execute(f"delete from {POLICY_SEARCH_INDEX_TABLE}")
    for row in _policy_search_rows(conn):
        conn.execute(
            f"insert into {POLICY_SEARCH_INDEX_TABLE}(rowid, merchant_id, text) values (?, ?, ?)",
            (row["rowid"], row["merchant_id"], fts_search_document(_policy_search_text(row))),
        )
    return True


def sync_policy_search_index(conn: sqlite3.Connection, merchant_id: str = "") -> None:
    if not policy_search_index_available(conn):
        return
    if merchant_id:
        conn.execute(f"delete from {POLICY_SEARCH_INDEX_TABLE} where merchant_id = ?", (merchant_id,))
    else:
        conn.execute(f"delete from {POLICY_SEARCH_INDEX_TABLE}")
    for row in _policy_search_rows(conn, merchant_id=merchant_id):
        conn.execute(
            f"insert into {POLICY_SEARCH_INDEX_TABLE}(rowid, merchant_id, text) values (?, ?, ?)",
            (row["rowid"], row["merchant_id"], fts_search_document(_policy_search_text(row))),
        )


def _ensure_policy_search_index_populated(conn: sqlite3.Connection) -> bool:
    if not policy_search_index_available(conn):
        return False
    try:
        indexed = conn.execute(f"select 1 from {POLICY_SEARCH_INDEX_TABLE} limit 1").fetchone()
        if indexed is not None:
            return True
        policy = conn.execute("select 1 from policies where active = 1 limit 1").fetchone()
    except (AttributeError, TypeError, sqlite3.OperationalError):
        return False
    return True if policy is None else rebuild_policy_search_index(conn)


def _fts_query(query: str) -> str:
    return fts_query(query)


def _match_score(query: str, row: sqlite3.Row) -> float:
    query_lower = query.lower()
    searchable = _policy_search_text(row).lower()
    query_tokens = tokenize(query_lower)
    policy_tokens = tokenize(searchable)
    score = 0.0
    for token in query_tokens:
        if token in searchable:
            score += 10
    for token in policy_tokens:
        if len(token) >= 2 and token in query_lower:
            score += 8
    if query_lower and query_lower in str(row["code"]).lower():
        score += 12
    return round(score, 4)


def search_policies(
    conn: sqlite3.Connection,
    query: str = "",
    merchant_id: str = "",
    category: str = "",
    limit: int = 10,
    offset: int = 0,
    candidate_limit: int | None = None,
) -> list[dict[str, Any]]:
    query = str(query or "").strip()
    merchant_id = str(merchant_id or "").strip()
    category = str(category or "").strip()
    window_start = _safe_non_negative_int(offset)
    window_limit = _safe_non_negative_int(limit)
    fts_match = _fts_query(query) if query else ""
    use_index = bool(fts_match and _ensure_policy_search_index_populated(conn))
    values: list[Any] = []
    if use_index:
        sql = f"""
            select p.*, rank
            from {POLICY_SEARCH_INDEX_TABLE} psi
            join policies p on p.rowid = psi.rowid
            where psi.text match ?
              and p.active = 1
        """
        values.append(fts_match)
        if merchant_id:
            sql += " and p.merchant_id = ?"
            values.append(merchant_id)
        if category:
            sql += " and lower(p.category) = lower(?)"
            values.append(category)
        sql += " order by rank, p.merchant_id, p.code limit ? offset ?"
        values.extend([window_limit, window_start])
        rows = conn.execute(sql, values).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            summary = _policy_to_summary(row)
            summary["match_score"] = _match_score(query, row)
            results.append(summary)
        return results
    requested_window = min(window_start + window_limit, MAX_SQLITE_INTEGER)
    default_candidate_limit = max(DEFAULT_POLICY_SEARCH_CANDIDATE_LIMIT, requested_window)
    if candidate_limit is None:
        candidate_cap = default_candidate_limit
    else:
        candidate_cap = _safe_non_negative_int(candidate_limit)
    candidate_cap = min(candidate_cap, MAX_POLICY_SEARCH_CANDIDATE_LIMIT)
    sql = "select * from policies where active = 1"
    if merchant_id:
        sql += " and merchant_id = ?"
        values.append(merchant_id)
    if category:
        sql += " and lower(category) = lower(?)"
        values.append(category)
    sql += " order by merchant_id, code limit ?"
    values.append(candidate_cap)
    rows = conn.execute(sql, values).fetchall()
    matches: list[tuple[float, str, str, sqlite3.Row]] = []
    for row in rows:
        score = _match_score(query, row)
        if query and score <= 0:
            continue
        matches.append((score, str(row["merchant_id"]), str(row["code"]), row))
    ordered = sorted(matches, key=lambda item: (-item[0], item[1], item[2]))
    results = []
    for score, _merchant_id, _code, row in ordered[window_start : window_start + window_limit]:
        summary = _policy_to_summary(row)
        summary["match_score"] = score
        results.append(summary)
    return results

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

from shopping_cli.core.catalog import parse_tags, require_merchant, tokenize
from shopping_cli.db.session import decode_json, encode_json, now_iso

DEFAULT_POLICY_SEARCH_CANDIDATE_LIMIT = 1000
MAX_POLICY_SEARCH_CANDIDATE_LIMIT = 5000
MAX_SQLITE_INTEGER = 2**63 - 1


def _safe_non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(number, 0)


def require_policy(conn: sqlite3.Connection, merchant_id: str, code: str) -> sqlite3.Row:
    row = conn.execute(
        "select * from policies where merchant_id = ? and code = ?",
        (merchant_id, code),
    ).fetchone()
    if row is None:
        raise SystemExit(f"Unknown policy: {merchant_id}/{code}")
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
    merchant_id = str(merchant_id or "").strip()
    code = str(code or "").strip()
    body = str(body or "").strip()
    if not merchant_id:
        raise SystemExit("merchant id is required")
    if not code:
        raise SystemExit("policy code is required")
    if not body:
        raise SystemExit("policy body is required")
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
        raise SystemExit(f"Policy already exists: {merchant_id}/{code}") from exc
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
    requested_window = min(window_start + window_limit, MAX_SQLITE_INTEGER)
    default_candidate_limit = max(DEFAULT_POLICY_SEARCH_CANDIDATE_LIMIT, requested_window)
    if candidate_limit is None:
        candidate_cap = default_candidate_limit
    else:
        candidate_cap = _safe_non_negative_int(candidate_limit)
    candidate_cap = min(candidate_cap, MAX_POLICY_SEARCH_CANDIDATE_LIMIT)
    values: list[Any] = []
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
    results: list[dict[str, Any]] = []
    for score, _merchant_id, _code, row in ordered[window_start : window_start + window_limit]:
        summary = _policy_to_summary(row)
        summary["match_score"] = score
        results.append(summary)
    return results

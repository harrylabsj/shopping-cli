"""Merchant-private order, refund and cost snapshot read/write handlers.

Kiwi 商家持续经营 v0.2 §11–§13 / §17.2. These handlers are the shopping-cli
data side: Kiwi reads order facts and private cost snapshots with a merchant
token; it never stores the raw external payload itself.

`external_verified` is deliberately rejected here. Only a future trusted
connector credential may assert external verification; a merchant token must not
be able to upgrade its own report to "verified".
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shopping_cli.core.errors import ConflictError, NotFoundError, ValidationError
from shopping_cli.core.harness import append_audit_event
from shopping_cli.db.session import db_session, now_iso

_EVIDENCE_SOURCES = {"manual_report", "csv_import"}
_COMPLETENESS = {"COMPLETE", "ESTIMATED", "INCOMPLETE"}


def _require_text(payload: dict[str, Any], name: str, max_len: int = 256) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > max_len:
        raise ValidationError(f"{name} must be a non-empty string (<= {max_len})")
    return value


def _optional_text(payload: dict[str, Any], name: str, max_len: int = 512) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_len:
        raise ValidationError(f"{name} must be a string (<= {max_len})")
    return value


def _optional_int(payload: dict[str, Any], name: str, minimum: int = 0) -> int | None:
    value = payload.get(name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValidationError(f"{name} must be an integer >= {minimum}")
    return value


def _timestamp(payload: dict[str, Any], name: str) -> str:
    value = _require_text(payload, name, 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    # 统一存 UTC 规范形，避免 +08:00/Z 混用时按文本排序出错。
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _query_int(value: Any, name: str, default: int, minimum: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be an integer") from exc
    return min(max(parsed, minimum), maximum)


def _lines(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("lines")
    if not isinstance(raw, list) or not raw:
        raise ValidationError("lines must be a non-empty array")
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationError(f"lines[{index}] must be an object")
        line_id = _require_text(item, "line_id", 128)
        if line_id in seen:
            raise ValidationError(f"duplicate line_id: {line_id}")
        seen.add(line_id)
        lines.append(
            {
                "line_id": line_id,
                "sku": _require_text(item, "sku", 128),
                "quantity": _optional_int(item, "quantity", 1) or 1,
                "amount_minor": _optional_int(item, "amount_minor", 0) or 0,
                "unit_price_minor": _optional_int(item, "unit_price_minor", 0),
            }
        )
    return lines


def _order_projection(row: Any, lines: list[dict[str, Any]], refunds: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "record_id": row["record_id"],
        "merchant_id": row["merchant_id"],
        "source_namespace": row["source_namespace"],
        "source_account_id": row["source_account_id"],
        "external_order_id": row["external_order_id"],
        "evidence_source": row["evidence_source"],
        "payment_status": row["payment_status"],
        "fulfillment_status": row["fulfillment_status"],
        "refund_status": row["refund_status"],
        "cancellation_status": row["cancellation_status"],
        "currency": row["currency"],
        "total_minor": row["total_minor"],
        "source_version": row["source_version"],
        "observed_at": row["observed_at"],
        "verified_at": row["verified_at"],
        "lines": lines,
        "refunds": refunds,
        "refund_total_minor": sum(
            int(refund["amount_minor"]) for refund in refunds if refund["status"] == "active"
        ),
    }


def _line_rows(conn: Any, record_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "select line_id, sku, quantity, amount_minor, unit_price_minor from order_fact_lines where record_id=? order by line_id",
        (record_id,),
    ).fetchall()
    return [
        {
            "line_id": row["line_id"],
            "sku": row["sku"],
            "quantity": row["quantity"],
            "amount_minor": row["amount_minor"],
            "unit_price_minor": row["unit_price_minor"],
        }
        for row in rows
    ]


def _refund_rows(conn: Any, merchant_id: str, record_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "select external_refund_id, amount_minor, version, status from order_fact_refunds where merchant_id=? and record_id=? order by external_refund_id",
        (merchant_id, record_id),
    ).fetchall()
    return [
        {
            "external_refund_id": row["external_refund_id"],
            "amount_minor": row["amount_minor"],
            "version": row["version"],
            "status": row["status"],
        }
        for row in rows
    ]


def _load_order(conn: Any, merchant_id: str, record_id: str) -> Any:
    row = conn.execute(
        "select * from order_facts where merchant_id=? and record_id=?",
        (merchant_id, record_id),
    ).fetchone()
    if row is None:
        raise NotFoundError("order not found")
    return row


def record_order(
    db_path: str | Path,
    payload: dict[str, Any],
    require_merchant_token: Any,
    *,
    allow_external_verified: bool = False,
) -> dict[str, Any]:
    merchant_id = _require_text(payload, "merchant_id", 128)
    evidence = _require_text(payload, "evidence_source", 32)
    if evidence == "external_verified" and not allow_external_verified:
        raise ValidationError(
            "external verification requires a trusted connector; merchant reports are manual_report/csv_import"
        )
    if evidence not in _EVIDENCE_SOURCES and evidence != "external_verified":
        raise ValidationError("evidence_source is invalid")
    source_version = _optional_text(payload, "source_version", 128)
    verified_at = _optional_text(payload, "verified_at", 64) if evidence == "external_verified" else None
    if evidence == "external_verified":
        if not source_version or not verified_at:
            raise ValidationError("external_verified requires source_version and verified_at")
        _timestamp(payload, "verified_at")
    source_namespace = _require_text(payload, "source_namespace", 64)
    source_account_id = _require_text(payload, "source_account_id", 128)
    external_order_id = _require_text(payload, "external_order_id", 256)
    observed_at = _timestamp(payload, "observed_at")
    lines = _lines(payload)
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        existing = conn.execute(
            """select * from order_facts
               where merchant_id=? and source_namespace=? and source_account_id=? and external_order_id=?""",
            (merchant_id, source_namespace, source_account_id, external_order_id),
        ).fetchone()
        now = now_iso()
        if existing is None:
            record_id = f"ord_{uuid.uuid4()}"
            cursor = conn.execute(
                """insert into order_facts
                   (record_id, merchant_id, source_namespace, source_account_id, external_order_id,
                    evidence_source, payment_status, fulfillment_status, refund_status, cancellation_status,
                    currency, total_minor, source_version, observed_at, verified_at, created_at, updated_at)
                   values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   on conflict do nothing""",
                (
                    record_id,
                    merchant_id,
                    source_namespace,
                    source_account_id,
                    external_order_id,
                    evidence,
                    _optional_text(payload, "payment_status", 64),
                    _optional_text(payload, "fulfillment_status", 64),
                    _optional_text(payload, "refund_status", 64),
                    _optional_text(payload, "cancellation_status", 64),
                    _optional_text(payload, "currency", 8),
                    _optional_int(payload, "total_minor", 0),
                    source_version,
                    observed_at,
                    verified_at,
                    now,
                    now,
                ),
            )
            if cursor.rowcount == 0:
                # 并发同身份申报：另一写者已插入，按幂等返回既有订单。
                existing = conn.execute(
                    """select * from order_facts
                       where merchant_id=? and source_namespace=? and source_account_id=? and external_order_id=?""",
                    (merchant_id, source_namespace, source_account_id, external_order_id),
                ).fetchone()
                if existing is None:
                    raise ConflictError("order identity conflict")
                record_id = existing["record_id"]
        protected = (
            existing is not None
            and existing["evidence_source"] == "external_verified"
            and evidence != "external_verified"
        )
        if existing is not None:
            record_id = existing["record_id"]
        if existing is not None and not protected:
            conn.execute(
                """update order_facts
                   set evidence_source=?, payment_status=?, fulfillment_status=?, refund_status=?,
                       cancellation_status=?, currency=?, total_minor=?, source_version=?, observed_at=?,
                       verified_at=?, updated_at=?
                   where record_id=? and merchant_id=?""",
                (
                    evidence,
                    _optional_text(payload, "payment_status", 64),
                    _optional_text(payload, "fulfillment_status", 64),
                    _optional_text(payload, "refund_status", 64),
                    _optional_text(payload, "cancellation_status", 64),
                    _optional_text(payload, "currency", 8),
                    _optional_int(payload, "total_minor", 0),
                    source_version,
                    observed_at,
                    verified_at,
                    now,
                    record_id,
                    merchant_id,
                ),
            )
        if not protected:
            conn.execute("delete from order_fact_lines where record_id=?", (record_id,))
            for line in lines:
                conn.execute(
                    """insert into order_fact_lines
                       (record_id, line_id, sku, quantity, amount_minor, unit_price_minor)
                       values (?, ?, ?, ?, ?, ?)""",
                    (
                        record_id,
                        line["line_id"],
                        line["sku"],
                        line["quantity"],
                        line["amount_minor"],
                        line["unit_price_minor"],
                    ),
                )
        if evidence != "external_verified":
            append_audit_event(
                conn,
                "",
                "merchant",
                "order.reported",
                {
                    "merchant_id": merchant_id,
                    "source_namespace": source_namespace,
                    "source_account_id": source_account_id,
                    "external_order_id": external_order_id,
                    "evidence_source": evidence,
                    "line_count": len(lines),
                },
            )
        if evidence == "external_verified":
            append_audit_event(
                conn,
                "",
                "order-connector",
                "order.external_verified",
                {
                    "merchant_id": merchant_id,
                    "source_namespace": source_namespace,
                    "source_account_id": source_account_id,
                    "external_order_id": external_order_id,
                    "source_version": source_version,
                },
            )
        row = _load_order(conn, merchant_id, record_id)
        return {
            "ok": True,
            "order": _order_projection(row, _line_rows(conn, record_id), _refund_rows(conn, merchant_id, record_id)),
        }


def record_verified_order(
    db_path: str | Path,
    payload: dict[str, Any],
    require_connector_token: Any,
) -> dict[str, Any]:
    """受信连接器写入 `external_verified`；凭据由 connector token 单独校验。"""
    def require_connector_for_merchant(conn: Any, merchant_id: str, inner: dict[str, Any]) -> None:
        require_connector_token(inner)
        row = conn.execute("select 1 from merchants where id = ?", (merchant_id,)).fetchone()
        if row is None:
            raise NotFoundError("merchant not found")

    return record_order(
        db_path,
        payload,
        require_connector_for_merchant,
        allow_external_verified=True,
    )


def list_orders(
    db_path: str | Path,
    query: dict[str, Any],
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    merchant_id = _require_text(query, "merchant_id", 128)
    limit = _query_int(query.get("limit"), "limit", 50, 1, 100)
    offset = _query_int(query.get("offset"), "offset", 0, 0, 1_000_000)
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        rows = conn.execute(
            "select * from order_facts where merchant_id=? order by observed_at desc, record_id limit ? offset ?",
            (merchant_id, limit + 1, offset),
        ).fetchall()
        items = [
            _order_projection(
                row,
                _line_rows(conn, row["record_id"]),
                _refund_rows(conn, merchant_id, row["record_id"]),
            )
            for row in rows[:limit]
        ]
        total = int(conn.execute("select count(*) count from order_facts where merchant_id=?", (merchant_id,)).fetchone()["count"])
        return {
            "ok": True,
            "merchant_id": merchant_id,
            "items": items,
            "total": total,
            "next_offset": offset + limit if len(rows) > limit else None,
        }


def get_order(
    db_path: str | Path,
    external_order_id: str,
    query: dict[str, Any],
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    merchant_id = _require_text(query, "merchant_id", 128)
    namespace = _optional_text(query, "source_namespace", 64)
    account = _optional_text(query, "source_account_id", 128)
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        sql = "select * from order_facts where merchant_id=? and external_order_id=?"
        params: list[Any] = [merchant_id, external_order_id]
        if namespace:
            sql += " and source_namespace=?"
            params.append(namespace)
        if account:
            sql += " and source_account_id=?"
            params.append(account)
        rows = conn.execute(sql, tuple(params)).fetchall()
        if not rows:
            raise NotFoundError("order not found")
        if len(rows) > 1:
            raise ConflictError(
                "order identity is ambiguous; provide source_namespace and source_account_id"
            )
        row = rows[0]
        return {
            "ok": True,
            "order": _order_projection(
                row,
                _line_rows(conn, row["record_id"]),
                _refund_rows(conn, merchant_id, row["record_id"]),
            ),
        }


def record_refund(
    db_path: str | Path,
    external_order_id: str,
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    merchant_id = _require_text(payload, "merchant_id", 128)
    external_refund_id = _require_text(payload, "external_refund_id", 256)
    amount_minor = _optional_int(payload, "amount_minor", 0)
    if amount_minor is None:
        raise ValidationError("amount_minor is required")
    version = _optional_int(payload, "version", 1) or 1
    status = str(payload.get("status") or "active")
    if status not in {"active", "reversed"}:
        raise ValidationError("status must be active or reversed")
    namespace = _optional_text(payload, "source_namespace", 64)
    account = _optional_text(payload, "source_account_id", 128)
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        sql = "select record_id from order_facts where merchant_id=? and external_order_id=?"
        params: list[Any] = [merchant_id, external_order_id]
        if namespace:
            sql += " and source_namespace=?"
            params.append(namespace)
        if account:
            sql += " and source_account_id=?"
            params.append(account)
        orders_found = conn.execute(sql, tuple(params)).fetchall()
        if not orders_found:
            raise NotFoundError("order not found")
        if len(orders_found) > 1:
            raise ConflictError(
                "order identity is ambiguous; provide source_namespace and source_account_id"
            )
        record_id = orders_found[0]["record_id"]
        existing = conn.execute(
            "select * from order_fact_refunds where merchant_id=? and record_id=? and external_refund_id=?",
            (merchant_id, record_id, external_refund_id),
        ).fetchone()
        if existing is not None:
            if version < existing["version"]:
                return {"ok": True, "deduplicated": True, "refund_total_minor": _refund_total(conn, merchant_id, record_id)}
            if version == existing["version"]:
                if amount_minor == existing["amount_minor"] and status == existing["status"]:
                    return {"ok": True, "deduplicated": True, "refund_total_minor": _refund_total(conn, merchant_id, record_id)}
                raise ConflictError("same refund version with different amount/status")
        conn.execute(
            """insert into order_fact_refunds
               (refund_row_id, merchant_id, record_id, external_refund_id, amount_minor, version, status, updated_at)
               values (?, ?, ?, ?, ?, ?, ?, ?)
               on conflict (merchant_id, record_id, external_refund_id)
               do update set amount_minor=excluded.amount_minor, version=excluded.version,
                             status=excluded.status, updated_at=excluded.updated_at""",
            (
                f"ref_{uuid.uuid4()}",
                merchant_id,
                record_id,
                external_refund_id,
                amount_minor,
                version,
                status,
                now_iso(),
            ),
        )
        return {
            "ok": True,
            "deduplicated": False,
            "refund_total_minor": _refund_total(conn, merchant_id, record_id),
        }


def _refund_total(conn: Any, merchant_id: str, record_id: str) -> int:
    row = conn.execute(
        "select coalesce(sum(amount_minor), 0) total from order_fact_refunds where merchant_id=? and record_id=? and status='active'",
        (merchant_id, record_id),
    ).fetchone()
    return int(row["total"])


def save_cost_snapshot(
    db_path: str | Path,
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    merchant_id = _require_text(payload, "merchant_id", 128)
    ref_kind = _require_text(payload, "ref_kind", 16)
    if ref_kind not in {"quote", "order"}:
        raise ValidationError("ref_kind must be quote or order")
    ref_id = _require_text(payload, "ref_id", 256)
    completeness = _require_text(payload, "completeness", 16)
    if completeness not in _COMPLETENESS:
        raise ValidationError("completeness is invalid")
    costs = payload.get("costs")
    if not isinstance(costs, dict):
        raise ValidationError("costs must be an object")
    missing = payload.get("missing_items") or []
    if not isinstance(missing, list) or any(not isinstance(item, str) or not item.strip() for item in missing):
        raise ValidationError("missing_items must be an array of non-empty strings")
    version = _optional_int(payload, "version", 1) or 1
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        snapshot_id = f"cost_{uuid.uuid4()}"
        conn.execute(
            """insert into merchant_cost_snapshots
               (snapshot_id, merchant_id, ref_kind, ref_id, currency, completeness,
                costs_json, missing_json, version, created_at)
               values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot_id,
                merchant_id,
                ref_kind,
                ref_id,
                str(payload.get("currency") or "CNY"),
                completeness,
                json.dumps(costs, ensure_ascii=False, sort_keys=True),
                json.dumps(missing, ensure_ascii=False),
                version,
                now_iso(),
            ),
        )
        return {"ok": True, "snapshot": _cost_projection(conn, merchant_id, snapshot_id)}


def _cost_projection(conn: Any, merchant_id: str, snapshot_id: str) -> dict[str, Any]:
    row = conn.execute(
        "select * from merchant_cost_snapshots where merchant_id=? and snapshot_id=?",
        (merchant_id, snapshot_id),
    ).fetchone()
    if row is None:
        raise NotFoundError("cost snapshot not found")
    return {
        "snapshot_id": row["snapshot_id"],
        "ref_kind": row["ref_kind"],
        "ref_id": row["ref_id"],
        "currency": row["currency"],
        "completeness": row["completeness"],
        "costs": json.loads(row["costs_json"]),
        "missing_items": json.loads(row["missing_json"]),
        "version": row["version"],
        "created_at": row["created_at"],
    }


def get_cost_snapshot(
    db_path: str | Path,
    snapshot_id: str,
    query: dict[str, Any],
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    merchant_id = _require_text(query, "merchant_id", 128)
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        return {"ok": True, "snapshot": _cost_projection(conn, merchant_id, snapshot_id)}


def register_source_contract(
    db_path: str | Path,
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    merchant_id = _require_text(payload, "merchant_id", 128)
    namespace = _require_text(payload, "source_namespace", 64)
    account = _require_text(payload, "source_account_id", 128)
    ordering = _require_text(payload, "ordering", 16)
    if ordering not in {"monotonic", "unorderable"}:
        raise ValidationError("ordering must be monotonic or unorderable")
    supports_watermark = payload.get("supports_watermark")
    if not isinstance(supports_watermark, bool):
        raise ValidationError("supports_watermark must be boolean")
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        now = now_iso()
        existing = conn.execute(
            "select revision from merchant_source_contracts where merchant_id=? and source_namespace=? and source_account_id=?",
            (merchant_id, namespace, account),
        ).fetchone()
        revision = int(existing["revision"]) + 1 if existing else 1
        conn.execute(
            """insert into merchant_source_contracts
               (merchant_id, source_namespace, source_account_id, ordering, version_scope,
                event_id_scope, supports_watermark, revision, created_at, updated_at)
               values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               on conflict (merchant_id, source_namespace, source_account_id)
               do update set ordering=excluded.ordering, version_scope=excluded.version_scope,
                             event_id_scope=excluded.event_id_scope,
                             supports_watermark=excluded.supports_watermark,
                             revision=excluded.revision, updated_at=excluded.updated_at""",
            (
                merchant_id,
                namespace,
                account,
                ordering,
                _require_text(payload, "version_scope", 64),
                _require_text(payload, "event_id_scope", 64),
                1 if supports_watermark else 0,
                revision,
                now,
                now,
            ),
        )
        return {
            "ok": True,
            "contract": {
                "merchant_id": merchant_id,
                "source_namespace": namespace,
                "source_account_id": account,
                "ordering": ordering,
                "version_scope": payload["version_scope"],
                "event_id_scope": payload["event_id_scope"],
                "supports_watermark": supports_watermark,
                "revision": revision,
            },
        }


def get_source_contract(
    db_path: str | Path,
    source_namespace: str,
    source_account_id: str,
    query: dict[str, Any],
    payload: dict[str, Any],
    require_merchant_token: Any,
) -> dict[str, Any]:
    merchant_id = _require_text(query, "merchant_id", 128)
    with db_session(db_path) as conn:
        require_merchant_token(conn, merchant_id, payload)
        row = conn.execute(
            "select * from merchant_source_contracts where merchant_id=? and source_namespace=? and source_account_id=?",
            (merchant_id, source_namespace, source_account_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("source contract not found")
        return {
            "ok": True,
            "contract": {
                "merchant_id": row["merchant_id"],
                "source_namespace": row["source_namespace"],
                "source_account_id": row["source_account_id"],
                "ordering": row["ordering"],
                "version_scope": row["version_scope"],
                "event_id_scope": row["event_id_scope"],
                "supports_watermark": bool(row["supports_watermark"]),
                "revision": row["revision"],
            },
        }

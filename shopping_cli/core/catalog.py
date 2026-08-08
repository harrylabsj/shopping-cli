"""Catalog search and merchant/product persistence."""

from __future__ import annotations

import math
import re
import sqlite3
from typing import Any, Mapping

from shopping_cli.core.errors import ConflictError, NotFoundError, ValidationError
from shopping_cli.core.harness import append_audit_event
from shopping_cli.core.limits import MAX_SHORT_TEXT_CHARS, bounded_string_list, bounded_text
from shopping_cli.core.limits import safe_non_negative_float as _safe_non_negative_float, safe_non_negative_int as _safe_non_negative_int
from shopping_cli.db.session import decode_json, encode_json, now_iso

MAX_SQLITE_INTEGER = 2**63 - 1
DEFAULT_PRODUCT_SEARCH_CANDIDATE_LIMIT = 1000
MAX_PRODUCT_SEARCH_CANDIDATE_LIMIT = 5000
DEFAULT_MERCHANT_SEARCH_CANDIDATE_LIMIT = 1000
MAX_MERCHANT_SEARCH_CANDIDATE_LIMIT = 5000
PRODUCT_SEARCH_INDEX_TABLE = "product_search_index"
MERCHANT_SEARCH_INDEX_TABLE = "merchant_search_index"


def parse_tags(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return bounded_string_list([str(item).strip() for item in value if str(item).strip()], "tags")
    parts = re.split(r"[,;，；、\n]+", str(value))
    return bounded_string_list([part.strip() for part in parts if part.strip()], "tags")


def tokenize(value: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[\w\u4e00-\u9fff]+", value or "")]


def cjk_bigrams(value: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for sequence in re.findall(r"[\u4e00-\u9fff]+", value or ""):
        for index in range(0, max(len(sequence) - 1, 0)):
            term = sequence[index : index + 2]
            if term not in seen:
                terms.append(term)
                seen.add(term)
    return terms


def fts_search_document(value: str) -> str:
    """Add space-delimited CJK characters and bigrams while preserving the original text.

    unicode61 treats each contiguous CJK block as a single token (e.g.
    "西湖龙井礼盒" is one token). To support single-character queries and
    substring matching, we inject each CJK character as an individual token
    alongside the original text and bigrams.
    """
    original = str(value or "")
    bigrams = cjk_bigrams(original)
    # Extract every individual CJK character so single-char queries match.
    singles: list[str] = []
    seen_singles: set[str] = set()
    for sequence in re.findall(r"[一-鿿]+", original):
        for ch in sequence:
            if ch not in seen_singles:
                singles.append(ch)
                seen_singles.add(ch)
    return " ".join([original, *singles, *bigrams]).strip()


def fts_query(query: str) -> str:
    """Build an FTS5 phrase-query string from a user query.

    Emits phrase queries for whole CJK words, CJK bigrams, and — when the
    query is a single CJK character — the individual character.  The index
    document (fts_search_document) carries the original text, individual
    CJK characters, and bigrams, so all three token classes are searchable.
    """
    terms: list[str] = []
    seen: set[str] = set()
    # Whole CJK words (contiguous \w+ or CJK runs)
    for candidate in tokenize(query):
        if candidate and candidate not in seen:
            terms.append(candidate)
            seen.add(candidate)
    # CJK bigrams
    cj_bigrams = cjk_bigrams(query)
    for candidate in cj_bigrams:
        if candidate and candidate not in seen:
            terms.append(candidate)
            seen.add(candidate)
    # Individual CJK characters — only for single-character queries where
    # no bigrams exist.  For multi-character queries the bigram and
    # full-word phrase tokens are precise enough; individual chars would
    # introduce false positives through the OR semantics.
    if not cj_bigrams:
        for ch in query:
            if "一" <= ch <= "鿿" and ch not in seen:
                terms.append(ch)
                seen.add(ch)
    return " OR ".join(
        f'"{token.replace(chr(34), chr(34) + chr(34))}"'
        for token in terms
    )


def require_merchant(conn: sqlite3.Connection, merchant_id: str) -> sqlite3.Row:
    row = conn.execute("select * from merchants where id = ?", (merchant_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown merchant: {merchant_id}")
    return row


def require_product(conn: sqlite3.Connection, sku: str) -> sqlite3.Row:
    row = conn.execute("select * from products where sku = ?", (sku,)).fetchone()
    if row is None:
        raise NotFoundError(f"Unknown product SKU: {sku}")
    return row


def _finite_float(value: Any, message: str) -> float:
    if isinstance(value, bool):
        raise ValidationError(message)
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValidationError(message) from exc
    if not math.isfinite(number):
        raise ValidationError(message)
    return number


def _whole_int(value: Any, message: str) -> int:
    if isinstance(value, bool):
        raise ValidationError(message)
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValidationError(message)
        number = int(value)
    else:
        text = str(value or "").strip()
        if not text:
            raise ValidationError(message)
        try:
            number = int(text)
        except ValueError as exc:
            raise ValidationError(message) from exc
    if number > MAX_SQLITE_INTEGER:
        raise ValidationError(f"{message}; must be <= {MAX_SQLITE_INTEGER}")
    return number


# safe_non_negative_int / safe_non_negative_float are imported from core.limits


def _audit_catalog(conn: sqlite3.Connection, merchant_id: str, event: str, details: dict) -> None:
    """catalog 写操作审计（conversation_id=''——非会话域事件）。

    actor 用 merchant_id：审计"哪个商家发生了什么数据变更"（此前 catalog
    写操作完全无痕）。
    """
    append_audit_event(conn, "", str(merchant_id or "system"), event, details)


def create_merchant(
    conn: sqlite3.Connection,
    merchant_id: str,
    name: str,
    city: str = "",
    service_area: str = "",
    contact: str = "",
    hours: str = "",
    automation_boundaries: str = "",
    tags: str | list[str] | None = None,
    delivery_fee: float = 0,
    delivery_eta_minutes: int = 0,
    delivery_radius_km: float = 0,
) -> dict[str, Any]:
    merchant_id = bounded_text(merchant_id, "merchant id", MAX_SHORT_TEXT_CHARS).strip()
    name = bounded_text(name, "merchant name", MAX_SHORT_TEXT_CHARS).strip()
    city = bounded_text(city, "city", MAX_SHORT_TEXT_CHARS)
    service_area = bounded_text(service_area, "service area")
    contact = bounded_text(contact, "contact", MAX_SHORT_TEXT_CHARS)
    hours = bounded_text(hours, "hours", MAX_SHORT_TEXT_CHARS)
    automation_boundaries = bounded_text(automation_boundaries, "automation boundaries")
    if not merchant_id:
        raise ValidationError("merchant id is required")
    if not name:
        raise ValidationError("merchant name is required")
    now = now_iso()
    try:
        conn.execute(
            """
        insert into merchants(
            id, name, city, service_area, contact, hours, automation_boundaries,
            tags_json, created_at, updated_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
            merchant_id,
            name,
            city,
            service_area,
            contact,
            hours,
            automation_boundaries,
            encode_json(parse_tags(tags)),
            now,
            now,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(f"Merchant already exists: {merchant_id}") from exc
    upsert_delivery_rule(
        conn,
        merchant_id,
        service_area=service_area,
        fee=delivery_fee,
        eta_minutes=delivery_eta_minutes,
        radius_km=delivery_radius_km,
    )
    sync_merchant_search_index(conn, merchant_id=merchant_id)
    _audit_catalog(conn, merchant_id, "merchant_created", {"merchant_id": merchant_id})
    return merchant_summary(conn, merchant_id)


def update_merchant(
    conn: sqlite3.Connection,
    merchant_id: str,
    name: str | None = None,
    city: str | None = None,
    service_area: str | None = None,
    contact: str | None = None,
    hours: str | None = None,
    automation_boundaries: str | None = None,
    tags: str | list[str] | None = None,
    delivery_fee: float | None = None,
    delivery_eta_minutes: int | None = None,
    delivery_radius_km: float | None = None,
) -> dict[str, Any]:
    merchant = require_merchant(conn, merchant_id)
    if name is not None:
        name = bounded_text(name, "merchant name", MAX_SHORT_TEXT_CHARS).strip()
        if not name:
            raise ValidationError("merchant name is required")
    if city is not None:
        city = bounded_text(city, "city", MAX_SHORT_TEXT_CHARS)
    if service_area is not None:
        service_area = bounded_text(service_area, "service area")
    if contact is not None:
        contact = bounded_text(contact, "contact", MAX_SHORT_TEXT_CHARS)
    if hours is not None:
        hours = bounded_text(hours, "hours", MAX_SHORT_TEXT_CHARS)
    if automation_boundaries is not None:
        automation_boundaries = bounded_text(automation_boundaries, "automation boundaries")
    updates: list[str] = []
    values: list[Any] = []
    field_map = {
        "name": name,
        "city": city,
        "service_area": service_area,
        "contact": contact,
        "hours": hours,
        "automation_boundaries": automation_boundaries,
    }
    for column, value in field_map.items():
        if value is not None:
            updates.append(f"{column} = ?")
            values.append(value)
    if tags is not None:
        updates.append("tags_json = ?")
        values.append(encode_json(parse_tags(tags)))
    if updates:
        updates.append("updated_at = ?")
        values.append(now_iso())
        values.append(merchant_id)
        conn.execute(f"update merchants set {', '.join(updates)} where id = ?", values)

    delivery = delivery_rule(conn, merchant_id)
    if any(value is not None for value in (service_area, delivery_fee, delivery_eta_minutes, delivery_radius_km)):
        upsert_delivery_rule(
            conn,
            merchant_id,
            service_area=service_area if service_area is not None else delivery["service_area"] or merchant["service_area"],
            fee=delivery_fee if delivery_fee is not None else delivery["fee"],
            eta_minutes=delivery_eta_minutes if delivery_eta_minutes is not None else delivery["eta_minutes"],
            radius_km=delivery_radius_km if delivery_radius_km is not None else delivery["radius_km"],
            notes=delivery["notes"],
            currency=delivery["currency"],
        )
    # Only sync product search index when search-relevant merchant fields change.
    merchant_search_fields_changed = any(
        value is not None for value in (name, city, service_area, tags)
    )
    if merchant_search_fields_changed:
        sync_product_search_index(conn, merchant_id=merchant_id)
    sync_merchant_search_index(conn, merchant_id=merchant_id)
    _audit_catalog(conn, merchant_id, "merchant_updated", {"merchant_id": merchant_id})
    return merchant_summary(conn, merchant_id)


def upsert_delivery_rule(
    conn: sqlite3.Connection,
    merchant_id: str,
    service_area: str = "",
    fee: float = 0,
    eta_minutes: int = 0,
    radius_km: float = 0,
    notes: str = "",
    currency: str = "CNY",
) -> dict[str, Any]:
    service_area = bounded_text(service_area, "service area")
    notes = bounded_text(notes, "delivery notes")
    currency = bounded_text(currency, "currency", 16)
    fee = _finite_float(fee, "delivery fee must be finite")
    radius_km = _finite_float(radius_km, "delivery radius must be finite")
    eta_minutes = _whole_int(eta_minutes, "delivery eta minutes must be a whole number")
    if fee < 0:
        raise ValidationError("delivery fee must be non-negative")
    if eta_minutes < 0:
        raise ValidationError("delivery eta minutes must be non-negative")
    if radius_km < 0:
        raise ValidationError("delivery radius must be non-negative")
    require_merchant(conn, merchant_id)
    now = now_iso()
    conn.execute(
        """
        insert into delivery_rules(
            merchant_id, service_area, fee, currency, eta_minutes, radius_km,
            notes, created_at, updated_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(merchant_id) do update set
            service_area = excluded.service_area,
            fee = excluded.fee,
            currency = excluded.currency,
            eta_minutes = excluded.eta_minutes,
            radius_km = excluded.radius_km,
            notes = excluded.notes,
            updated_at = excluded.updated_at
        """,
        (merchant_id, service_area, fee, currency, eta_minutes, radius_km, notes, now, now),
    )
    _audit_catalog(conn, merchant_id, "delivery_rule_updated", {"merchant_id": merchant_id})
    return delivery_rule(conn, merchant_id)


def create_product(
    conn: sqlite3.Connection,
    merchant_id: str,
    sku: str,
    title: str,
    price: float,
    stock: int,
    currency: str = "CNY",
    category: str = "",
    tags: str | list[str] | None = None,
    description: str = "",
    delivery_attributes: str | list[str] | None = None,
) -> dict[str, Any]:
    merchant_id = bounded_text(merchant_id, "merchant id", MAX_SHORT_TEXT_CHARS).strip()
    sku = bounded_text(sku, "product sku", MAX_SHORT_TEXT_CHARS).strip()
    title = bounded_text(title, "product title", MAX_SHORT_TEXT_CHARS).strip()
    description = bounded_text(description, "product description")
    category = bounded_text(category, "product category", MAX_SHORT_TEXT_CHARS)
    currency = bounded_text(currency, "currency", 16)
    if not merchant_id:
        raise ValidationError("merchant id is required")
    if not sku:
        raise ValidationError("product sku is required")
    if not title:
        raise ValidationError("product title is required")
    price = _finite_float(price, "--price must be finite")
    stock = _whole_int(stock, "--stock must be a whole number")
    if price < 0:
        raise ValidationError("--price must be non-negative")
    if stock < 0:
        raise ValidationError("--stock must be non-negative")
    require_merchant(conn, merchant_id)
    now = now_iso()
    try:
        conn.execute(
            """
        insert into products(
            sku, merchant_id, title, description, category, tags_json, price,
            currency, stock, delivery_attributes_json, active, created_at, updated_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
            sku,
            merchant_id,
            title,
            description,
            category,
            encode_json(parse_tags(tags)),
            price,
            currency,
            stock,
            encode_json(parse_tags(delivery_attributes)),
            now,
            now,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(f"Product already exists: {sku}") from exc
    sync_product_search_index(conn, sku=sku)
    _audit_catalog(conn, merchant_id, "product_created", {"sku": sku, "merchant_id": merchant_id})
    return product_summary(conn, sku)


def update_product(
    conn: sqlite3.Connection,
    sku: str,
    merchant_id: str = "",
    title: str | None = None,
    price: float | None = None,
    stock: int | None = None,
    currency: str | None = None,
    category: str | None = None,
    tags: str | list[str] | None = None,
    description: str | None = None,
    delivery_attributes: str | list[str] | None = None,
) -> dict[str, Any]:
    product = require_product(conn, sku)
    if merchant_id and product["merchant_id"] != merchant_id:
        raise ValidationError(f"Product {sku} does not belong to merchant {merchant_id}")
    if title is not None:
        title = bounded_text(title, "product title", MAX_SHORT_TEXT_CHARS).strip()
        if not title:
            raise ValidationError("product title is required")
    if currency is not None:
        currency = bounded_text(currency, "currency", 16)
    if category is not None:
        category = bounded_text(category, "product category", MAX_SHORT_TEXT_CHARS)
    if description is not None:
        description = bounded_text(description, "product description")
    if price is not None:
        price = _finite_float(price, "--price must be finite")
    if price is not None and price < 0:
        raise ValidationError("--price must be non-negative")
    if stock is not None:
        stock = _whole_int(stock, "--stock must be a whole number")
    if stock is not None and stock < 0:
        raise ValidationError("--stock must be non-negative")
    updates: list[str] = []
    values: list[Any] = []
    field_map = {
        "title": title,
        "price": price,
        "stock": stock,
        "currency": currency,
        "category": category,
        "description": description,
    }
    for column, value in field_map.items():
        if value is not None:
            updates.append(f"{column} = ?")
            values.append(value)
    if tags is not None:
        updates.append("tags_json = ?")
        values.append(encode_json(parse_tags(tags)))
    if delivery_attributes is not None:
        updates.append("delivery_attributes_json = ?")
        values.append(encode_json(parse_tags(delivery_attributes)))
    if updates:
        # 本地手改提升权威（§5）：编辑过 source='erp' 的行升级为 'local'，
        # 否则下一次 ERP 同步会静默覆盖本地改动（冲突守卫只保护 local 行）。
        updates.append("source = case when source = 'erp' then 'local' else source end")
        updates.append("updated_at = ?")
        values.append(now_iso())
        values.append(sku)
        conn.execute(f"update products set {', '.join(updates)} where sku = ?", values)
        sync_product_search_index(conn, sku=sku)
        _audit_catalog(conn, merchant_id, "product_updated", {"sku": sku, "merchant_id": merchant_id})
    return product_summary(conn, sku)


def set_stock(conn: sqlite3.Connection, sku: str, stock: int, merchant_id: str = "") -> dict[str, Any]:
    stock = _whole_int(stock, "--stock must be a whole number")
    if stock < 0:
        raise ValidationError("--stock must be non-negative")
    product = require_product(conn, sku)
    if merchant_id and product["merchant_id"] != merchant_id:
        raise ValidationError(f"Product {sku} does not belong to merchant {merchant_id}")
    conn.execute(
        "update products set stock = ?, "
        "source = case when source = 'erp' then 'local' else source end, "
        "updated_at = ? where sku = ?",
        (int(stock), now_iso(), sku),
    )
    sync_product_search_index(conn, sku=sku)
    _audit_catalog(conn, merchant_id, "product_stock_updated", {"sku": sku, "stock": int(stock)})
    return product_summary(conn, sku)


def delivery_rule(conn: sqlite3.Connection, merchant_id: str) -> dict[str, Any]:
    row = conn.execute("select * from delivery_rules where merchant_id = ?", (merchant_id,)).fetchone()
    if row is None:
        return {
            "service_area": "",
            "fee": 0.0,
            "currency": "CNY",
            "eta_minutes": 0,
            "radius_km": 0.0,
            "notes": "",
        }
    return {
        "service_area": row["service_area"],
        "fee": _safe_non_negative_float(row["fee"]),
        "currency": row["currency"],
        "eta_minutes": _safe_non_negative_int(row["eta_minutes"]),
        "radius_km": _safe_non_negative_float(row["radius_km"]),
        "notes": row["notes"],
    }


def public_merchant_summary(merchant: dict[str, Any]) -> dict[str, Any]:
    """公开投影：剥离商家私有字段（contact / automation_boundaries）。

    会话/买家可见的序列化必须走公开投影——automation_boundaries 含议价
    底价（negotiation 路径以 _leaks_private_threshold 守护的隐私）。
    """
    summary = dict(merchant)
    summary.pop("contact", None)
    summary.pop("automation_boundaries", None)
    return summary


def public_product_summary(product: dict[str, Any]) -> dict[str, Any]:
    summary = dict(product)
    merchant = summary.get("merchant")
    if isinstance(merchant, dict):
        summary["merchant"] = public_merchant_summary(merchant)
    return summary


def merchant_summary(conn: sqlite3.Connection, merchant_id: str) -> dict[str, Any]:
    merchant = require_merchant(conn, merchant_id)
    product_count = conn.execute(
        "select count(*) from products where merchant_id = ? and active = 1",
        (merchant_id,),
    ).fetchone()[0]
    return {
        "id": merchant["id"],
        "name": merchant["name"],
        "city": merchant["city"],
        "service_area": merchant["service_area"],
        "contact": merchant["contact"],
        "hours": merchant["hours"],
        "automation_boundaries": merchant["automation_boundaries"],
        "tags": decode_json(merchant["tags_json"], []),
        "delivery": delivery_rule(conn, merchant_id),
        "product_count": product_count,
    }


def _delivery_rule_from_joined_merchant(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "service_area": row["delivery_service_area"] or "",
        "fee": _safe_non_negative_float(row["delivery_fee"]),
        "currency": row["delivery_currency"] or "CNY",
        "eta_minutes": _safe_non_negative_int(row["delivery_eta_minutes"]),
        "radius_km": _safe_non_negative_float(row["delivery_radius_km"]),
        "notes": row["delivery_notes"] or "",
    }


def _merchant_summary_from_search_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "city": row["city"],
        "service_area": row["service_area"],
        "contact": row["contact"],
        "hours": row["hours"],
        "automation_boundaries": row["automation_boundaries"],
        "tags": decode_json(row["tags_json"], []),
        "delivery": _delivery_rule_from_joined_merchant(row),
        "product_count": _safe_non_negative_int(row["active_product_count"]),
    }


def list_merchants(conn: sqlite3.Connection, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    window_limit = _safe_non_negative_int(limit)
    window_offset = _safe_non_negative_int(offset)
    rows = conn.execute(
        """
        select m.*,
               dr.service_area as delivery_service_area,
               dr.fee as delivery_fee,
               dr.currency as delivery_currency,
               dr.eta_minutes as delivery_eta_minutes,
               dr.radius_km as delivery_radius_km,
               dr.notes as delivery_notes,
               count(p.sku) as active_product_count
        from merchants m
        left join delivery_rules dr on dr.merchant_id = m.id
        left join products p on p.merchant_id = m.id and p.active = 1
        group by m.id
        order by m.name, m.id
        limit ? offset ?
        """,
        (window_limit, window_offset),
    ).fetchall()
    return [_merchant_summary_from_search_row(row) for row in rows]


def product_summary(conn: sqlite3.Connection, sku: str) -> dict[str, Any]:
    product = require_product(conn, sku)
    merchant = merchant_summary(conn, product["merchant_id"])
    return {
        "sku": product["sku"],
        "merchant_id": product["merchant_id"],
        "title": product["title"],
        "description": product["description"],
        "category": product["category"],
        "tags": decode_json(product["tags_json"], []),
        "price": _safe_non_negative_float(product["price"]),
        "currency": product["currency"],
        "stock": _safe_non_negative_int(product["stock"]),
        "delivery_attributes": decode_json(product["delivery_attributes_json"], []),
        "merchant": merchant,
        "delivery": merchant["delivery"],
        "warnings": product_warnings(product, merchant),
    }


def _merchant_summary_from_product_search_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["merchant_id"],
        "name": row["merchant_name"],
        "city": row["merchant_city"],
        "service_area": row["merchant_service_area"],
        "contact": row["merchant_contact"],
        "hours": row["merchant_hours"],
        "automation_boundaries": row["merchant_automation_boundaries"],
        "tags": decode_json(row["merchant_tags_json"], []),
        "delivery": _delivery_rule_from_joined_merchant(row),
        "product_count": _safe_non_negative_int(row["active_product_count"]),
    }


def _product_summary_from_search_row(row: sqlite3.Row) -> dict[str, Any]:
    merchant = _merchant_summary_from_product_search_row(row)
    return {
        "sku": row["sku"],
        "merchant_id": row["merchant_id"],
        "title": row["title"],
        "description": row["description"],
        "category": row["category"],
        "tags": decode_json(row["tags_json"], []),
        "price": _safe_non_negative_float(row["price"]),
        "currency": row["currency"],
        "stock": _safe_non_negative_int(row["stock"]),
        "delivery_attributes": decode_json(row["delivery_attributes_json"], []),
        "merchant": merchant,
        "delivery": merchant["delivery"],
        "warnings": product_warnings(row, merchant),
    }


def product_warnings(product: sqlite3.Row, merchant: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    stock = _safe_non_negative_int(product["stock"])
    if stock <= 0:
        warnings.append("out of stock")
    elif stock <= 2:
        warnings.append("low stock")
    if not merchant.get("contact"):
        warnings.append("merchant contact missing")
    if not merchant.get("delivery", {}).get("service_area"):
        warnings.append("delivery rule missing")
    return warnings


def _search_text(product: sqlite3.Row, merchant: Mapping[str, Any]) -> str:
    fields = [
        product["sku"],
        product["title"],
        product["description"],
        product["category"],
        " ".join(decode_json(product["tags_json"], [])),
        merchant["name"],
        merchant["city"],
        merchant["service_area"],
        " ".join(decode_json(merchant["tags_json"], [])),
    ]
    return " ".join(str(field) for field in fields if field)


def _fts_query(query: str) -> str:
    return fts_query(query)


def product_search_index_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            f"""
            create virtual table if not exists {PRODUCT_SEARCH_INDEX_TABLE}
            using fts5(sku unindexed, merchant_id unindexed, text, tokenize='unicode61')
            """
        )
    except sqlite3.OperationalError:
        return False
    return True


def _product_search_document(row: sqlite3.Row) -> str:
    merchant = {
        "name": row["merchant_name"],
        "city": row["merchant_city"],
        "service_area": row["merchant_service_area"],
        "tags_json": row["merchant_tags_json"],
    }
    return fts_search_document(_search_text(row, merchant))


def _joined_product_search_rows(conn: sqlite3.Connection, merchant_id: str = "", sku: str = "") -> list[sqlite3.Row]:
    values: list[Any] = []
    sql = """
        select p.*,
               m.name as merchant_name,
               m.city as merchant_city,
               m.service_area as merchant_service_area,
               m.tags_json as merchant_tags_json
        from products p
        join merchants m on m.id = p.merchant_id
        where p.active = 1
    """
    if merchant_id:
        sql += " and p.merchant_id = ?"
        values.append(merchant_id)
    if sku:
        sql += " and p.sku = ?"
        values.append(sku)
    sql += " order by p.sku"
    return conn.execute(sql, values).fetchall()


def rebuild_product_search_index(conn: sqlite3.Connection) -> bool:
    if not product_search_index_available(conn):
        return False
    conn.execute(f"delete from {PRODUCT_SEARCH_INDEX_TABLE}")
    for row in _joined_product_search_rows(conn):
        conn.execute(
            f"insert into {PRODUCT_SEARCH_INDEX_TABLE}(sku, merchant_id, text) values (?, ?, ?)",
            (row["sku"], row["merchant_id"], _product_search_document(row)),
        )
    return True


def product_search_index_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    if not product_search_index_available(conn):
        return {
            "available": False,
            "healthy": False,
            "active_product_count": 0,
            "indexed_count": 0,
            "missing_count": 0,
            "stale_count": 0,
            "extra_count": 0,
        }
    active_rows = _joined_product_search_rows(conn)
    active_skus = {str(row["sku"]) for row in active_rows}
    indexed_rows = conn.execute(f"select sku, merchant_id, text from {PRODUCT_SEARCH_INDEX_TABLE}").fetchall()
    indexed_by_sku = {str(row["sku"]): row for row in indexed_rows}
    missing_count = 0
    stale_count = 0
    for row in active_rows:
        sku = str(row["sku"])
        indexed = indexed_by_sku.get(sku)
        if indexed is None:
            missing_count += 1
            continue
        if str(indexed["merchant_id"] or "") != str(row["merchant_id"] or ""):
            stale_count += 1
            continue
        if str(indexed["text"] or "") != _product_search_document(row):
            stale_count += 1
    extra_count = sum(1 for row in indexed_rows if str(row["sku"]) not in active_skus)
    indexed_count = len(indexed_rows)
    active_product_count = len(active_rows)
    healthy = (
        indexed_count == active_product_count
        and missing_count == 0
        and stale_count == 0
        and extra_count == 0
    )
    return {
        "available": True,
        "healthy": healthy,
        "active_product_count": active_product_count,
        "indexed_count": indexed_count,
        "missing_count": missing_count,
        "stale_count": stale_count,
        "extra_count": extra_count,
    }


def sync_product_search_index(conn: sqlite3.Connection, sku: str = "", merchant_id: str = "") -> None:
    if not product_search_index_available(conn):
        return
    if sku:
        conn.execute(f"delete from {PRODUCT_SEARCH_INDEX_TABLE} where sku = ?", (sku,))
    elif merchant_id:
        conn.execute(f"delete from {PRODUCT_SEARCH_INDEX_TABLE} where merchant_id = ?", (merchant_id,))
    else:
        conn.execute(f"delete from {PRODUCT_SEARCH_INDEX_TABLE}")
    for row in _joined_product_search_rows(conn, merchant_id=merchant_id, sku=sku):
        conn.execute(
            f"insert into {PRODUCT_SEARCH_INDEX_TABLE}(sku, merchant_id, text) values (?, ?, ?)",
            (row["sku"], row["merchant_id"], _product_search_document(row)),
        )


def _ensure_product_search_index_populated(conn: sqlite3.Connection) -> bool:
    if not product_search_index_available(conn):
        return False
    try:
        indexed = conn.execute(f"select 1 from {PRODUCT_SEARCH_INDEX_TABLE} limit 1").fetchone()
        if indexed is not None:
            return True
        product = conn.execute("select 1 from products where active = 1 limit 1").fetchone()
    except (AttributeError, TypeError, sqlite3.OperationalError):
        return False
    return True if product is None else rebuild_product_search_index(conn)


def merchant_search_index_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute(
            f"""
            create virtual table if not exists {MERCHANT_SEARCH_INDEX_TABLE}
            using fts5(id unindexed, text, tokenize='unicode61')
            """
        )
    except sqlite3.OperationalError:
        return False
    return True


def _merchant_search_document(row: sqlite3.Row) -> str:
    fields = [
        row["id"],
        row["name"],
        row["city"],
        row["service_area"],
        " ".join(decode_json(row["tags_json"], [])),
    ]
    return fts_search_document(" ".join(str(field) for field in fields if field))


def _joined_merchant_search_rows(conn: sqlite3.Connection, merchant_id: str = "") -> list[sqlite3.Row]:
    values: list[Any] = []
    sql = """
        select m.*
        from merchants m
        where 1 = 1
    """
    if merchant_id:
        sql += " and m.id = ?"
        values.append(merchant_id)
    sql += " order by m.id"
    return conn.execute(sql, values).fetchall()


def rebuild_merchant_search_index(conn: sqlite3.Connection) -> bool:
    if not merchant_search_index_available(conn):
        return False
    conn.execute(f"delete from {MERCHANT_SEARCH_INDEX_TABLE}")
    for row in _joined_merchant_search_rows(conn):
        conn.execute(
            f"insert into {MERCHANT_SEARCH_INDEX_TABLE}(id, text) values (?, ?)",
            (row["id"], _merchant_search_document(row)),
        )
    return True


def merchant_search_index_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    if not merchant_search_index_available(conn):
        return {
            "available": False,
            "healthy": False,
            "active_merchant_count": 0,
            "indexed_count": 0,
            "missing_count": 0,
            "stale_count": 0,
            "extra_count": 0,
        }
    active_rows = _joined_merchant_search_rows(conn)
    active_ids = {str(row["id"]) for row in active_rows}
    indexed_rows = conn.execute(f"select id, text from {MERCHANT_SEARCH_INDEX_TABLE}").fetchall()
    indexed_by_id = {str(row["id"]): row for row in indexed_rows}
    missing_count = 0
    stale_count = 0
    for row in active_rows:
        merchant_id = str(row["id"])
        indexed = indexed_by_id.get(merchant_id)
        if indexed is None:
            missing_count += 1
            continue
        if str(indexed["text"] or "") != _merchant_search_document(row):
            stale_count += 1
    extra_count = sum(1 for row in indexed_rows if str(row["id"]) not in active_ids)
    indexed_count = len(indexed_rows)
    active_merchant_count = len(active_rows)
    healthy = (
        indexed_count == active_merchant_count
        and missing_count == 0
        and stale_count == 0
        and extra_count == 0
    )
    return {
        "available": True,
        "healthy": healthy,
        "active_merchant_count": active_merchant_count,
        "indexed_count": indexed_count,
        "missing_count": missing_count,
        "stale_count": stale_count,
        "extra_count": extra_count,
    }


def sync_merchant_search_index(conn: sqlite3.Connection, merchant_id: str = "") -> None:
    if not merchant_search_index_available(conn):
        return
    if merchant_id:
        conn.execute(f"delete from {MERCHANT_SEARCH_INDEX_TABLE} where id = ?", (merchant_id,))
    else:
        conn.execute(f"delete from {MERCHANT_SEARCH_INDEX_TABLE}")
    for row in _joined_merchant_search_rows(conn, merchant_id=merchant_id):
        conn.execute(
            f"insert into {MERCHANT_SEARCH_INDEX_TABLE}(id, text) values (?, ?)",
            (row["id"], _merchant_search_document(row)),
        )


def _ensure_merchant_search_index_populated(conn: sqlite3.Connection) -> bool:
    if not merchant_search_index_available(conn):
        return False
    try:
        indexed = conn.execute(f"select 1 from {MERCHANT_SEARCH_INDEX_TABLE} limit 1").fetchone()
        if indexed is not None:
            return True
        merchant = conn.execute("select 1 from merchants limit 1").fetchone()
    except (AttributeError, TypeError, sqlite3.OperationalError):
        return False
    return True if merchant is None else rebuild_merchant_search_index(conn)


def _match_score(query: str, product: sqlite3.Row, merchant: Mapping[str, Any]) -> float:
    query_lower = query.lower()
    searchable = _search_text(product, merchant).lower()
    query_tokens = tokenize(query_lower)
    product_tokens = tokenize(searchable)
    score = 0.0
    for token in query_tokens:
        if token in searchable:
            score += 10
    for token in product_tokens:
        if len(token) >= 2 and token in query_lower:
            score += 8
    # CJK bigrams catch substring matches when full-word tokens don't overlap
    # (e.g. query "今天想买龙井礼盒" vs product "西湖龙井礼盒").
    for bigram in cjk_bigrams(query_lower):
        if bigram in searchable:
            score += 7
    if _safe_non_negative_int(product["stock"]) > 0:
        score += 5
    score -= _safe_non_negative_float(product["price"]) / 1000
    return round(score, 4)


def _joined_product_merchant(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["merchant_id"],
        "name": row["merchant_name"],
        "city": row["merchant_city"],
        "service_area": row["merchant_service_area"],
        "contact": row["merchant_contact"],
        "hours": row["merchant_hours"],
        "tags_json": row["merchant_tags_json"],
    }


def search_products(
    conn: sqlite3.Connection,
    query: str = "",
    city: str = "",
    area: str = "",
    max_price: float | None = None,
    include_out_of_stock: bool = False,
    limit: int = 10,
    offset: int = 0,
    candidate_limit: int | None = None,
) -> list[dict[str, Any]]:
    query = str(query or "").strip()
    city = str(city or "").strip()
    area = str(area or "").strip()
    window_start = _safe_non_negative_int(offset)
    window_limit = _safe_non_negative_int(limit)
    if max_price is not None:
        max_price = _finite_float(max_price, "--max-price must be finite")
    fts_match = _fts_query(query) if query else ""
    use_index = bool(fts_match and _ensure_product_search_index_populated(conn))
    # Compute candidate cap once so both FTS and non-FTS paths can use it.
    requested_window = min(window_start + window_limit, MAX_SQLITE_INTEGER)
    default_candidate_limit = max(DEFAULT_PRODUCT_SEARCH_CANDIDATE_LIMIT, requested_window)
    if candidate_limit is None:
        candidate_cap = default_candidate_limit
    else:
        candidate_cap = _safe_non_negative_int(candidate_limit)
    candidate_cap = min(candidate_cap, MAX_PRODUCT_SEARCH_CANDIDATE_LIMIT)
    values: list[Any] = []
    select_sql = """
        select p.*,
               m.name as merchant_name,
               m.city as merchant_city,
               m.service_area as merchant_service_area,
               m.contact as merchant_contact,
               m.hours as merchant_hours,
               m.automation_boundaries as merchant_automation_boundaries,
               m.tags_json as merchant_tags_json,
               dr.service_area as delivery_service_area,
               dr.fee as delivery_fee,
               dr.currency as delivery_currency,
               dr.eta_minutes as delivery_eta_minutes,
               dr.radius_km as delivery_radius_km,
               dr.notes as delivery_notes,
               (
                   select count(*)
                   from products pc
                   where pc.merchant_id = m.id and pc.active = 1
               ) as active_product_count
    """
    if use_index:
        sql = (
            select_sql
            + f""",
               rank
        from {PRODUCT_SEARCH_INDEX_TABLE} psi
        join products p on p.sku = psi.sku
        join merchants m on m.id = p.merchant_id
        left join delivery_rules dr on dr.merchant_id = m.id
        where psi.text match ?
          and p.active = 1
    """
        )
        values.append(fts_match)
    else:
        sql = (
            select_sql
            + """
        from products p
        join merchants m on m.id = p.merchant_id
        left join delivery_rules dr on dr.merchant_id = m.id
        where p.active = 1
    """
        )
    if city:
        sql += " and lower(m.city) = lower(?)"
        values.append(city)
    if max_price is not None:
        sql += " and p.price <= ?"
        values.append(max_price)
    if not include_out_of_stock:
        sql += " and p.stock > 0"
    if use_index:
        # Use a candidate cap larger than the display window so that
        # _match_score can re-rank FTS results.  FTS5 BM25 on individual
        # CJK characters (unicode61 tokenizer) produces noisy rankings;
        # Python-side substring scoring is more reliable for CJK.
        index_cap = max(candidate_cap if not query else candidate_cap, window_start + window_limit)
        sql += " order by rank, p.sku limit ?"
        values.append(index_cap)
        rows = conn.execute(sql, values).fetchall()
        scored: list[tuple[float, float, str, sqlite3.Row]] = []
        for row in rows:
            merchant = _joined_product_merchant(row)
            if city and merchant["city"].lower() != city.lower():
                continue
            price = _safe_non_negative_float(row["price"])
            stock = _safe_non_negative_int(row["stock"])
            if max_price is not None and price > max_price:
                continue
            if not include_out_of_stock and stock <= 0:
                continue
            score = _match_score(query, row, merchant)
            if query and score <= (5 if stock > 0 else 0):
                continue
            scored.append((score, price, str(row["sku"]), row))
        ordered = sorted(scored, key=lambda item: (-item[0], item[1], item[2]))
        results = []
        for score, _price, _sku, row in ordered[window_start : window_start + window_limit]:
            summary = _product_summary_from_search_row(row)
            service_area = str(summary["merchant"].get("service_area") or "")
            if area and area.lower() not in service_area.lower():
                summary.setdefault("warnings", []).append("requested area may need merchant confirmation")
            summary["match_score"] = score
            results.append(summary)
        return results
    sql += " order by p.sku limit ?"
    values.append(candidate_cap)
    rows = conn.execute(sql, values).fetchall()
    matches: list[tuple[float, float, str, sqlite3.Row]] = []
    for row in rows:
        merchant = _joined_product_merchant(row)
        if city and merchant["city"].lower() != city.lower():
            continue
        price = _safe_non_negative_float(row["price"])
        stock = _safe_non_negative_int(row["stock"])
        if max_price is not None and price > max_price:
            continue
        if not include_out_of_stock and stock <= 0:
            continue
        score = _match_score(query, row, merchant)
        if query and score <= (5 if stock > 0 else 0):
            continue
        matches.append((score, price, str(row["sku"]), row))

    ordered = sorted(matches, key=lambda item: (-item[0], item[1], item[2]))
    results = []
    for score, _price, _sku, row in ordered[window_start : window_start + window_limit]:
        summary = _product_summary_from_search_row(row)
        service_area = str(summary["merchant"].get("service_area") or "")
        if area and area.lower() not in service_area.lower():
            summary.setdefault("warnings", []).append("requested area may need merchant confirmation")
        summary["match_score"] = score
        results.append(summary)
    return results


def search_merchants(
    conn: sqlite3.Connection,
    query: str = "",
    city: str = "",
    limit: int = 10,
    offset: int = 0,
    candidate_limit: int | None = None,
) -> list[dict[str, Any]]:
    query = str(query or "").strip()
    city = str(city or "").strip()
    query_lower = query.lower()
    query_tokens = tokenize(query_lower)
    window_start = _safe_non_negative_int(offset)
    window_limit = _safe_non_negative_int(limit)
    fts_match = _fts_query(query) if query else ""
    use_index = bool(fts_match and _ensure_merchant_search_index_populated(conn))
    # Compute candidate cap once so both paths can use it.
    requested_window = min(window_start + window_limit, MAX_SQLITE_INTEGER)
    default_candidate_limit = max(DEFAULT_MERCHANT_SEARCH_CANDIDATE_LIMIT, requested_window)
    if candidate_limit is None:
        candidate_cap = default_candidate_limit
    else:
        candidate_cap = _safe_non_negative_int(candidate_limit)
    candidate_cap = min(candidate_cap, MAX_MERCHANT_SEARCH_CANDIDATE_LIMIT)
    values: list[Any] = []
    select_sql = """
        select m.*,
               dr.service_area as delivery_service_area,
               dr.fee as delivery_fee,
               dr.currency as delivery_currency,
               dr.eta_minutes as delivery_eta_minutes,
               dr.radius_km as delivery_radius_km,
               dr.notes as delivery_notes,
               count(p.sku) as active_product_count
    """
    if use_index:
        sql = (
            select_sql
            + f""",
               rank
        from {MERCHANT_SEARCH_INDEX_TABLE} msi
        join merchants m on m.id = msi.id
        left join delivery_rules dr on dr.merchant_id = m.id
        left join products p on p.merchant_id = m.id and p.active = 1
        where msi.text match ?
    """
        )
        values.append(fts_match)
    else:
        sql = (
            select_sql
            + """
        from merchants m
        left join delivery_rules dr on dr.merchant_id = m.id
        left join products p on p.merchant_id = m.id and p.active = 1
    """
        )
        if city:
            sql += " where lower(city) = lower(?)"
            values.append(city)
    if use_index and city:
        sql += " and lower(m.city) = lower(?)"
        values.append(city)
    sql += " group by m.id"
    if use_index:
        sql += " order by rank, m.id limit ?"
        values.append(candidate_cap)
    else:
        sql += " order by m.name, m.id limit ?"
        values.append(candidate_cap)
    rows = conn.execute(sql, values).fetchall()
    if use_index:
        scored: list[tuple[float, str, str, sqlite3.Row]] = []
        for merchant in rows:
            if city and str(merchant["city"] or "").lower() != city.lower():
                continue
            score = _match_merchant_score(query, merchant)
            if query and score <= 0:
                continue
            scored.append((score, str(merchant["id"]), str(merchant["name"]), merchant))
        ordered = sorted(scored, key=lambda item: (-item[0], item[2].lower(), item[1]))
        results = []
        for score, _mid, _mname, merchant in ordered[window_start : window_start + window_limit]:
            summary = _merchant_summary_from_search_row(merchant)
            summary["match_score"] = score
            results.append(summary)
        return results
    matches: list[tuple[float, str, str, sqlite3.Row]] = []
    for merchant in rows:
        if city and merchant["city"].lower() != city.lower():
            continue
        searchable = " ".join(
            [
                merchant["id"],
                merchant["name"],
                merchant["city"],
                merchant["service_area"],
                " ".join(decode_json(merchant["tags_json"], [])),
            ]
        ).lower()
        merchant_tokens = tokenize(searchable)
        score = 0.0
        for token in query_tokens:
            if token in searchable:
                score += 10
        for token in merchant_tokens:
            if len(token) >= 2 and token in query_lower:
                score += 8
        if query and score <= 0:
            continue
        matches.append((round(score, 4), str(merchant["name"]), str(merchant["id"]), merchant))

    ordered = sorted(matches, key=lambda item: (-item[0], item[1], item[2]))
    results = []
    for score, _name, _merchant_id, merchant in ordered[window_start : window_start + window_limit]:
        summary = _merchant_summary_from_search_row(merchant)
        summary["match_score"] = score
        results.append(summary)
    return results


def _match_merchant_score(query: str, merchant: sqlite3.Row) -> float:
    query_lower = query.lower()
    searchable = " ".join(
        [
            merchant["id"],
            merchant["name"],
            merchant["city"],
            merchant["service_area"],
            " ".join(decode_json(merchant["tags_json"], [])),
        ]
    ).lower()
    query_tokens = tokenize(query_lower)
    merchant_tokens = tokenize(searchable)
    score = 0.0
    for token in query_tokens:
        if token in searchable:
            score += 10
    for token in merchant_tokens:
        if len(token) >= 2 and token in query_lower:
            score += 8
    for bigram in cjk_bigrams(query_lower):
        if bigram in searchable:
            score += 7
    return round(score, 4)

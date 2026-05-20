"""Catalog search and merchant/product persistence."""

from __future__ import annotations

import math
import re
import sqlite3
from typing import Any, Mapping

from shopping_cli.db.session import decode_json, encode_json, now_iso

MAX_SQLITE_INTEGER = 2**63 - 1


def parse_tags(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    parts = re.split(r"[,;，；、\n]+", str(value))
    return [part.strip() for part in parts if part.strip()]


def tokenize(value: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[\w\u4e00-\u9fff]+", value or "")]


def require_merchant(conn: sqlite3.Connection, merchant_id: str) -> sqlite3.Row:
    row = conn.execute("select * from merchants where id = ?", (merchant_id,)).fetchone()
    if row is None:
        raise SystemExit(f"Unknown merchant: {merchant_id}")
    return row


def require_product(conn: sqlite3.Connection, sku: str) -> sqlite3.Row:
    row = conn.execute("select * from products where sku = ?", (sku,)).fetchone()
    if row is None:
        raise SystemExit(f"Unknown product SKU: {sku}")
    return row


def _finite_float(value: Any, message: str) -> float:
    if isinstance(value, bool):
        raise SystemExit(message)
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise SystemExit(message) from exc
    if not math.isfinite(number):
        raise SystemExit(message)
    return number


def _whole_int(value: Any, message: str) -> int:
    if isinstance(value, bool):
        raise SystemExit(message)
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise SystemExit(message)
        number = int(value)
    else:
        text = str(value or "").strip()
        if not text:
            raise SystemExit(message)
        try:
            number = int(text)
        except ValueError as exc:
            raise SystemExit(message) from exc
    if number > MAX_SQLITE_INTEGER:
        raise SystemExit(f"{message}; must be <= {MAX_SQLITE_INTEGER}")
    return number


def _safe_non_negative_float(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number < 0:
        return 0.0
    return number


def _safe_non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    try:
        number = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(number, 0)


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
    merchant_id = str(merchant_id or "").strip()
    name = str(name or "").strip()
    if not merchant_id:
        raise SystemExit("merchant id is required")
    if not name:
        raise SystemExit("merchant name is required")
    now = now_iso()
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
    upsert_delivery_rule(
        conn,
        merchant_id,
        service_area=service_area,
        fee=delivery_fee,
        eta_minutes=delivery_eta_minutes,
        radius_km=delivery_radius_km,
    )
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
        name = str(name or "").strip()
        if not name:
            raise SystemExit("merchant name is required")
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
    fee = _finite_float(fee, "delivery fee must be finite")
    radius_km = _finite_float(radius_km, "delivery radius must be finite")
    eta_minutes = _whole_int(eta_minutes, "delivery eta minutes must be a whole number")
    if fee < 0:
        raise SystemExit("delivery fee must be non-negative")
    if eta_minutes < 0:
        raise SystemExit("delivery eta minutes must be non-negative")
    if radius_km < 0:
        raise SystemExit("delivery radius must be non-negative")
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
    merchant_id = str(merchant_id or "").strip()
    sku = str(sku or "").strip()
    title = str(title or "").strip()
    if not merchant_id:
        raise SystemExit("merchant id is required")
    if not sku:
        raise SystemExit("product sku is required")
    if not title:
        raise SystemExit("product title is required")
    price = _finite_float(price, "--price must be finite")
    stock = _whole_int(stock, "--stock must be a whole number")
    if price < 0:
        raise SystemExit("--price must be non-negative")
    if stock < 0:
        raise SystemExit("--stock must be non-negative")
    require_merchant(conn, merchant_id)
    now = now_iso()
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
        raise SystemExit(f"Product {sku} does not belong to merchant {merchant_id}")
    if title is not None:
        title = str(title or "").strip()
        if not title:
            raise SystemExit("product title is required")
    if price is not None:
        price = _finite_float(price, "--price must be finite")
    if price is not None and price < 0:
        raise SystemExit("--price must be non-negative")
    if stock is not None:
        stock = _whole_int(stock, "--stock must be a whole number")
    if stock is not None and stock < 0:
        raise SystemExit("--stock must be non-negative")
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
        updates.append("updated_at = ?")
        values.append(now_iso())
        values.append(sku)
        conn.execute(f"update products set {', '.join(updates)} where sku = ?", values)
    return product_summary(conn, sku)


def set_stock(conn: sqlite3.Connection, sku: str, stock: int, merchant_id: str = "") -> dict[str, Any]:
    stock = _whole_int(stock, "--stock must be a whole number")
    if stock < 0:
        raise SystemExit("--stock must be non-negative")
    product = require_product(conn, sku)
    if merchant_id and product["merchant_id"] != merchant_id:
        raise SystemExit(f"Product {sku} does not belong to merchant {merchant_id}")
    conn.execute(
        "update products set stock = ?, updated_at = ? where sku = ?",
        (int(stock), now_iso(), sku),
    )
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
) -> list[dict[str, Any]]:
    query = str(query or "").strip()
    city = str(city or "").strip()
    area = str(area or "").strip()
    if max_price is not None:
        max_price = _finite_float(max_price, "--max-price must be finite")
    values: list[Any] = []
    sql = """
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
        from products p
        join merchants m on m.id = p.merchant_id
        left join delivery_rules dr on dr.merchant_id = m.id
        where p.active = 1
    """
    if city:
        sql += " and lower(m.city) = lower(?)"
        values.append(city)
    if max_price is not None:
        sql += " and p.price <= ?"
        values.append(max_price)
    if not include_out_of_stock:
        sql += " and p.stock > 0"
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
    window_start = _safe_non_negative_int(offset)
    window_limit = _safe_non_negative_int(limit)
    results: list[dict[str, Any]] = []
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
) -> list[dict[str, Any]]:
    query = str(query or "").strip()
    city = str(city or "").strip()
    query_lower = query.lower()
    query_tokens = tokenize(query_lower)
    values: list[Any] = []
    sql = """
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
    """
    if city:
        sql += " where lower(city) = lower(?)"
        values.append(city)
    sql += " group by m.id order by m.name, m.id"
    rows = conn.execute(sql, values).fetchall()
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
    window_start = _safe_non_negative_int(offset)
    window_limit = _safe_non_negative_int(limit)
    results: list[dict[str, Any]] = []
    for score, _name, _merchant_id, merchant in ordered[window_start : window_start + window_limit]:
        summary = _merchant_summary_from_search_row(merchant)
        summary["match_score"] = score
        results.append(summary)
    return results

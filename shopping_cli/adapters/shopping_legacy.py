"""Import catalog data from the pre-MVP Shopping JSON store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from shopping_cli.core.catalog import create_merchant, create_product


def _legacy_section(data: dict[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise SystemExit(f"Invalid legacy JSON: legacy JSON {name} must be an object")
    return section


def import_json_store(conn: sqlite3.Connection, source: str | Path) -> dict[str, Any]:
    source_path = Path(source)
    try:
        raw = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"Unable to read legacy JSON: {source_path}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid legacy JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}") from exc
    if not isinstance(data, dict):
        raise SystemExit("Invalid legacy JSON: legacy JSON root must be an object")
    imported = {"merchants": 0, "products": 0}
    skipped = {"merchants": 0, "products": 0}
    for merchant_id, merchant in _legacy_section(data, "merchants").items():
        if not isinstance(merchant, dict):
            skipped["merchants"] += 1
            continue
        resolved_merchant_id = str(merchant.get("id") or merchant_id)
        if conn.execute("select 1 from merchants where id = ?", (resolved_merchant_id,)).fetchone():
            skipped["merchants"] += 1
            continue
        create_merchant(
            conn,
            merchant_id=resolved_merchant_id,
            name=str(merchant.get("name") or merchant_id),
            city=str(merchant.get("city") or ""),
            service_area=str(merchant.get("service_area") or merchant.get("serviceArea") or ""),
            contact=str(merchant.get("contact") or ""),
            hours=str(merchant.get("hours") or ""),
            tags=merchant.get("tags") or [],
        )
        imported["merchants"] += 1
    for sku, product in _legacy_section(data, "products").items():
        if not isinstance(product, dict):
            skipped["products"] += 1
            continue
        resolved_sku = str(product.get("sku") or sku)
        if conn.execute("select 1 from products where sku = ?", (resolved_sku,)).fetchone():
            skipped["products"] += 1
            continue
        try:
            create_product(
                conn,
                merchant_id=str(product.get("merchant_id") or product.get("merchant") or ""),
                sku=resolved_sku,
                title=str(product.get("title") or sku),
                description=str(product.get("description") or ""),
                category=str(product.get("category") or ""),
                tags=product.get("tags") or [],
                price=float(product.get("price") or 0),
                currency=str(product.get("currency") or "CNY"),
                stock=int(product.get("stock") or 0),
                delivery_attributes=str(product.get("shipping") or ""),
            )
        except (OverflowError, TypeError, ValueError, SystemExit):
            skipped["products"] += 1
            continue
        imported["products"] += 1
    return {"ok": True, "imported": imported, "skipped": skipped}

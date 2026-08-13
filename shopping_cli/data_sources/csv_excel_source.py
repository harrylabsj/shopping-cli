"""CSV / Excel 数据源适配器（Issue 14 / §6.3：首条接入路径）。

从本地 CSV（stdlib ``csv``）或 Excel ``.xlsx``（内置 zip+XML 最小读取器，
零第三方依赖）读取商品事实，校验必填字段（sku/title/price/stock），upsert 进
本地 ``products`` 表，``source='csv_excel'`` 标注（UPSTREAM_PROXY 缓存语义——
文件是上游，本地手改行仍是权威）。

必填字段（与 ERP 一致）：
* ``sku``——非空字符串；``title``——非空字符串；
* ``price``——非负数值，且不超币种两位小数精度（Decimal 判定，fail-closed）；
* ``stock``——非负整数。
可选：``currency``（缺省 CNY）、``category``、``description``、``merchant_id``。

授权边界：``allowed_merchant_id`` 非空时只允许写入该 merchant 名下（跨租户
防护）；归属冲突（SKU 已被其他 merchant 拥有）与本地权威行冲突 → 跳过并记
conflicts / errors，绝不静默合并冲突权威源。
"""

from __future__ import annotations

import csv
import math
import sqlite3
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from shopping_cli.data_sources.adapter import (
    AdapterError,
    AUTHORITY_UPSTREAM,
    DataSourceAdapter,
    SyncContext,
    SyncReport,
    register,
    resolve_merchant_or_raise,
    upsert_product_row,
)
from shopping_cli.db.provenance import erp_fresh_ttl_seconds

SOURCE_CSV_EXCEL = "csv_excel"

_XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


@dataclass(frozen=True)
class CsvExcelSyncConfig:
    """CSV/Excel 导入配置。"""

    path: str
    default_merchant_id: str = ""
    allowed_merchant_id: str = ""
    source: str = SOURCE_CSV_EXCEL


@dataclass
class CsvExcelReport(SyncReport):
    """CSV/Excel 同步报告（source 恒为 csv_excel）。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 读取器
# ---------------------------------------------------------------------------


def read_rows(path: str) -> list[dict[str, str]]:
    """读取 CSV 或 .xlsx → 有序 dict 列表（首行为表头）。空文件 → []。"""
    p = Path(path)
    if not p.is_file():
        raise AdapterError(f"file not found: {path}")
    if p.suffix.lower() == ".xlsx":
        return _read_xlsx_rows(p)
    if p.suffix.lower() in {".csv", ".tsv", ""}:
        return _read_csv_rows(p)
    raise AdapterError(f"unsupported file type {p.suffix!r} (expected .csv / .xlsx)")


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            rows = [dict(r) for r in reader]
    except (UnicodeDecodeError, csv.Error) as exc:
        raise AdapterError(f"invalid CSV: {exc}") from exc
    return rows


def _xlsx_read_sheet(zf: zipfile.ZipFile, sheet_path: str, shared: list[str]) -> list[list[str]]:
    root = ET.fromstring(zf.read(sheet_path))
    ns = _XLSX_NS
    rows: list[list[str]] = []
    for row in root.iter(f"{{{ns}}}row"):
        cells: list[str] = []
        for cell in row:
            t = cell.get("t", "n")
            v = cell.find(f"{{{ns}}}v")
            if t == "s" and v is not None:
                idx = int(v.text or "0")
                cells.append(shared[idx] if idx < len(shared) else "")
            elif t == "inlineStr":
                is_el = cell.find(f"{{{ns}}}is")
                text = ""
                if is_el is not None:
                    text = "".join(tt.text or "" for tt in is_el.iter(f"{{{ns}}}t"))
                cells.append(text)
            elif v is not None:
                cells.append(str(v.text or ""))
        rows.append(cells)
    return rows


def _read_xlsx_rows(path: Path) -> list[dict[str, str]]:
    try:
        with zipfile.ZipFile(path) as zf:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in root.iter(f"{{{_XLSX_NS}}}si"):
                    shared.append("".join(t.text or "" for t in si.iter(f"{{{_XLSX_NS}}}t")))
            sheet_path = None
            for name in ("xl/worksheets/sheet1.xml", "xl/worksheets/sheet.xml"):
                if name in zf.namelist():
                    sheet_path = name
                    break
            if sheet_path is None:
                raise AdapterError("xlsx has no sheet1")
            grid = _xlsx_read_sheet(zf, sheet_path, shared)
    except (zipfile.BadZipFile, ET.ParseError) as exc:
        raise AdapterError(f"invalid xlsx: {exc}") from exc

    if not grid:
        return []
    header = [str(h or "").strip() for h in grid[0]]
    rows: list[dict[str, str]] = []
    for body in grid[1:]:
        row = {header[i]: (body[i] if i < len(body) else "") for i in range(len(header))}
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 解析 + 同步
# ---------------------------------------------------------------------------


def _parse_product(raw: dict[str, str], index: int) -> dict[str, Any]:
    """CSV/Excel 行 → 本地 products 行（sku/title/price/stock 校验，fail-closed）。"""
    sku = (raw.get("sku") or "").strip()
    title = (raw.get("title") or "").strip()
    price_raw = (raw.get("price") or "").strip()
    stock_raw = (raw.get("stock") or "").strip()
    if not sku:
        raise AdapterError(f"row {index}: missing sku")
    if not title:
        raise AdapterError(f"row {index}: missing title")
    try:
        price = float(price_raw)
    except ValueError as exc:
        raise AdapterError(f"row {index}: invalid price {price_raw!r}") from exc
    if math.isnan(price) or math.isinf(price) or price < 0:
        raise AdapterError(f"row {index}: invalid price {price_raw!r}")
    try:
        scaled = Decimal(price_raw) * 100
    except (InvalidOperation, ValueError) as exc:
        raise AdapterError(f"row {index}: non-decimal price {price_raw!r}") from exc
    if scaled != scaled.to_integral_value():
        raise AdapterError(f"row {index}: price {price_raw!r} exceeds 2-decimal precision")
    try:
        stock = int(stock_raw)
    except ValueError as exc:
        raise AdapterError(f"row {index}: invalid stock {stock_raw!r}") from exc
    if stock < 0:
        raise AdapterError(f"row {index}: invalid stock {stock_raw!r}")
    return {
        "sku": sku,
        "title": title,
        "price": price,
        "stock": stock,
        "currency": (raw.get("currency") or "").strip() or "CNY",
        "category": (raw.get("category") or "").strip(),
        "description": (raw.get("description") or "").strip(),
        "merchant_id": (raw.get("merchant_id") or "").strip(),
    }


def sync_csv_excel(
    conn: sqlite3.Connection,
    config: CsvExcelSyncConfig,
    *,
    now: Callable[[], str] = _now_iso,
    rows: list[dict[str, str]] | None = None,
) -> CsvExcelReport:
    """导入 CSV/Excel 行并 upsert（source='csv_excel'）。``rows`` 注入供测试。"""
    report = CsvExcelReport(source=SOURCE_CSV_EXCEL, authority=AUTHORITY_UPSTREAM)
    if rows is None:
        rows = read_rows(config.path)
    now_ts = now()
    revision = f"csv_excel:{now_ts}"
    fresh_until = (
        datetime.fromisoformat(now_ts) + timedelta(seconds=erp_fresh_ttl_seconds())
    ).isoformat()

    for index, row in enumerate(rows):
        try:
            product = _parse_product(row, index)
        except AdapterError as exc:
            report.errors.append(str(exc))
            continue
        sku = product["sku"]
        merchant_id = product["merchant_id"] or config.default_merchant_id
        if not merchant_id:
            report.errors.append(f"row {index} sku {sku}: no merchant_id (and no default_merchant_id)")
            continue
        if config.allowed_merchant_id and merchant_id != config.allowed_merchant_id:
            report.errors.append(
                f"row {index} sku {sku}: merchant_id {merchant_id!r} does not match actor "
                f"merchant {config.allowed_merchant_id!r}; skipped"
            )
            report.skipped += 1
            continue
        existing = conn.execute(
            "select source, merchant_id from products where sku = ? and merchant_id = ?",
            (sku, merchant_id),
        ).fetchone()
        if existing is None:
            other = conn.execute("select 1 from products where sku = ?", (sku,)).fetchone()
            if other is not None:
                report.errors.append(f"row {index} sku {sku}: already owned by another merchant; refusing to reassign")
                report.skipped += 1
                continue
            resolve_merchant_or_raise(conn, merchant_id)
        elif existing[0] == "local":
            report.conflicts.append({"sku": sku, "reason": "local authoritative row"})
            report.skipped += 1
            continue
        upsert_product_row(
            conn,
            sku=sku,
            merchant_id=merchant_id,
            title=product["title"],
            description=product["description"],
            category=product["category"],
            price=product["price"],
            currency=product["currency"],
            stock=product["stock"],
            source=SOURCE_CSV_EXCEL,
            revision=revision,
            now_ts=now_ts,
            fresh_until=fresh_until,
        )
        report.fetched += 1
        report.upserted += 1

    conn.commit()
    return report


# ---------------------------------------------------------------------------
# Adapter SDK 实现
# ---------------------------------------------------------------------------


class CsvExcelAdapter(DataSourceAdapter):
    """``csv_excel`` 适配器：``sync(ctx)`` 读取 ctx.config['path'] 导入。"""

    name = "csv_excel"
    description = "CSV / Excel（.xlsx）商品导入路径"

    def sync(self, ctx: SyncContext) -> SyncReport:
        path = str(ctx.config.get("path") or "")
        if not path:
            raise AdapterError("csv_excel adapter requires config.path")
        config = CsvExcelSyncConfig(
            path=path,
            default_merchant_id=ctx.default_merchant_id,
            allowed_merchant_id=ctx.allowed_merchant_id,
        )
        return sync_csv_excel(ctx.conn, config, now=ctx.now)


def _register() -> None:
    register(CsvExcelAdapter())


_register()

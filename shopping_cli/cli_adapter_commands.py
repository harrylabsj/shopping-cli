"""Adapter SDK CLI（Issue 14 / §6.3）。

``shopping adapters list``            —— 列出已注册数据源适配器
``shopping import-csv-excel ...``     —— 运行 CSV/Excel 适配器（首条接入路径）
"""

from __future__ import annotations

import argparse
import os

from shopping_cli.cli_common import db_path_from_args, emit
from shopping_cli.data_sources import csv_excel_source  # noqa: F401  # 导入即注册适配器
from shopping_cli.data_sources.adapter import (
    SyncContext,
    registered_adapters,
    run,
)
from shopping_cli.db.session import db_session

# CSV/Excel 导入模板（与 examples/products-template.csv 一致）。必填列 sku/title/
# price/stock；可选 currency（缺省 CNY）/ category / description / merchant_id。
PRODUCTS_TEMPLATE_CSV = (
    "sku,title,price,stock,currency,category,description\n"
    "VQ-001,智能保温杯 500ml,129.00,50,CNY,kitchenware,316 不锈钢内胆真空保温\n"
    "VQ-002,桌面无线充电器 15W,89.00,120,CNY,electronics,兼容 QC3.0\n"
)


def cmd_adapters_list(args: argparse.Namespace) -> None:
    """列出已注册适配器（Adapter SDK 注册表）。"""
    emit(
        [{"name": a.name, "description": a.description} for a in registered_adapters().values()],
        args.format,
    )


def cmd_import_csv_excel(args: argparse.Namespace) -> None:
    """运行 CSV/Excel 适配器导入本地 products 表；--template 只生成模板不导入。"""
    template_path = str(getattr(args, "template", "") or "").strip()
    if template_path:
        with open(template_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(PRODUCTS_TEMPLATE_CSV)
        emit(
            {"ok": True, "message": f"CSV 模板已写入 {template_path}（填好 sku/title/price/stock 后再导入）"},
            getattr(args, "format", "text"),
        )
        return
    file_path = str(args.file or "").strip()
    if not file_path:
        raise SystemExit("--file <path> 必填（或先用 --template <path> 生成模板）")
    db_path = db_path_from_args(args)
    merchant_id = (
        str(args.merchant or "").strip()
        or str(getattr(args, "default_merchant", "")).strip()
        or str(os.environ.get("SHOPPING_MERCHANT_ID") or "").strip()
        or str(os.environ.get("KIWI_MERCHANT_ID") or "").strip()
    )
    if not merchant_id:
        raise SystemExit("--merchant is required (FK 归属防护)")
    with db_session(db_path) as conn:
        ctx = SyncContext(
            conn=conn,
            default_merchant_id=merchant_id,
            allowed_merchant_id=str(args.allowed_merchant or "").strip(),
            config={"path": args.file},
        )
        report = run("csv_excel", ctx)
    emit(report.as_dict(), args.format)
    if report.errors or report.conflicts:
        raise SystemExit(
            f"csv_excel import completed with {len(report.errors)} errors and "
            f"{len(report.conflicts)} conflicts (see report above)"
        )

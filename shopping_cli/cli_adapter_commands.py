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


def cmd_adapters_list(args: argparse.Namespace) -> None:
    """列出已注册适配器（Adapter SDK 注册表）。"""
    emit(
        [{"name": a.name, "description": a.description} for a in registered_adapters().values()],
        args.format,
    )


def cmd_import_csv_excel(args: argparse.Namespace) -> None:
    """运行 CSV/Excel 适配器导入本地 products 表。"""
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

"""ERP 数据源 CLI（shopping-cli v0.3 §3/#3 MVP #3 接线）。

``shopping erp sync`` —— 手动触发 ERP 商品同步（push-first 语义：人为触发，
无 scheduled refresh）。配置优先级：--flag > env（SHOPPING_ERP_BASE_URL /
SHOPPING_ERP_AUTH_TOKEN / SHOPPING_ERP_DEFAULT_MERCHANT）。
"""

from __future__ import annotations

import argparse
import os

from shopping_cli.cli_common import db_path_from_args, emit
from shopping_cli.data_sources.erp_source import ErpSyncConfig, ErpSourceError, sync_erp_products
from shopping_cli.db.session import db_session

_ERP_ENV_BASE_URL = "SHOPPING_ERP_BASE_URL"
_ERP_ENV_AUTH_TOKEN = "SHOPPING_ERP_AUTH_TOKEN"
_ERP_ENV_DEFAULT_MERCHANT = "SHOPPING_ERP_DEFAULT_MERCHANT"


def _erp_sync_config(args: argparse.Namespace) -> ErpSyncConfig:
    base_url = str(args.base_url or "").strip() or os.environ.get(_ERP_ENV_BASE_URL, "").strip()
    if not base_url:
        raise ErpSourceError(
            "ERP base_url is required (--base-url or SHOPPING_ERP_BASE_URL)"
        )
    auth_token = str(args.auth_token or "").strip() or os.environ.get(_ERP_ENV_AUTH_TOKEN, "").strip()
    default_merchant = (
        str(args.default_merchant or "").strip()
        or os.environ.get(_ERP_ENV_DEFAULT_MERCHANT, "").strip()
    )
    return ErpSyncConfig(
        base_url=base_url,
        auth_token=auth_token,
        timeout_seconds=max(1, min(int(args.timeout or 15), 60)),
        page_size=max(1, min(int(args.page_size or 100), 500)),
        default_merchant_id=default_merchant,
    )


def cmd_erp_sync(args: argparse.Namespace) -> None:
    """同步 ERP 商品到本地 products 表（source='erp'，UPSTREAM_PROXY 缓存）。"""
    db_path = db_path_from_args(args)
    try:
        config = _erp_sync_config(args)
    except ErpSourceError as exc:
        raise SystemExit(str(exc)) from exc
    with db_session(db_path) as conn:
        report = sync_erp_products(conn, config)
    emit(report.as_dict(), args.format)
    if report.errors or report.conflicts:
        raise SystemExit(
            f"ERP sync completed with {len(report.errors)} errors and "
            f"{len(report.conflicts)} conflicts (see report above)"
        )

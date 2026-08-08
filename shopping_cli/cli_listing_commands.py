"""Listing 投影 CLI（shopping-cli v0.3 §14；只读预览）。

- ``projections list`` —— 预览可发布投影（public-only；带 provenance 标注）。

v3.0 起发布面（publish-listings / withdraw-listing）随 kiwi-catalog 子系统
剥离——发布职责在独立 kiwi-catalog 服务。
"""

from __future__ import annotations

import argparse
from typing import Any

from shopping_cli.cli_common import db_path_from_args, emit
from shopping_cli.db.session import db_session
from shopping_cli.listings.projection import list_publishable_listings


def cmd_listing_projections_list(args: argparse.Namespace) -> None:
    """预览可发布投影（只读；public-only，provenance 标注可见）。"""
    db_path = db_path_from_args(args)
    with db_session(db_path) as conn:
        projections = list_publishable_listings(conn, merchant_id=str(args.merchant or "").strip())
        if not projections:
            emit("没有可发布的商品投影。", args.format)
            return
        for projection in projections:
            provenance = projection.get("_provenance", {})
            emit(
                f"· {projection['source_product_ref']} — {projection['title']} "
                f"[{projection['listing_type']}] "
                f"authority={provenance.get('authority', '?')} "
                f"revision={provenance.get('source_revision', '?')}",
                args.format,
            )
        emit(f"共 {len(projections)} 条。", args.format)

"""Listing 发布 CLI（shopping-cli v0.3 §14/§15/§16；push-first 手动触发）。

- ``projections list`` —— 预览可发布投影（public-only；带 provenance 标注）；
- ``publish-listings`` —— 全量发布（digest 去重 + active=0 withdraw reconcile）；
- ``withdraw-listing {listing_id}`` —— 手动下架。

发布配置：--kiwi-catalog-url（kiwi-catalog 服务地址）、--owner-token-secret、
--merchant、--owner-agent-id（可选，缺省取 merchant 的 catalog agent）。
"""

from __future__ import annotations

import argparse
import os
from typing import Any

from shopping_cli.cli_common import db_path_from_args, emit
from shopping_cli.core.catalog import require_merchant
from shopping_cli.db.session import db_session
from shopping_cli.kiwi_catalog.publisher import (
    KiwiCatalogPublisher,
    PublishError,
    resolve_merchant_agent_id,
)
from shopping_cli.listings.projection import list_publishable_listings, project_product_listing

_OWNER_TOKEN_SECRET_ENV = "KIWI_CATALOG_OWNER_TOKEN_SECRET"


def _publisher_from_args(
    args: argparse.Namespace, *, resolve_owner: bool = True
) -> KiwiCatalogPublisher:
    base_url = str(args.kiwi_catalog_url or "").strip()
    if not base_url:
        raise PublishError("--kiwi-catalog-url is required")
    secret = (
        str(args.owner_token_secret or "").strip()
        or os.environ.get(_OWNER_TOKEN_SECRET_ENV, "").strip()
    )
    if not secret:
        raise PublishError(
            f"--owner-token-secret is required (or set {_OWNER_TOKEN_SECRET_ENV})"
        )
    merchant_id = str(args.merchant or "").strip()
    if not merchant_id:
        # 单 merchant 构造（owner_token 按 merchant 派生）：无 --merchant 时
        # 一切请求都会被服务端拒绝，直接 fail-closed 报错。
        raise PublishError("--merchant is required (publisher is single-merchant by construction)")
    owner_agent_id = str(args.owner_agent_id or "").strip()
    if not owner_agent_id and resolve_owner:
        # 缺省回退：查 kiwi-catalog 该 merchant 的 catalog agent（与 help
        # 文案一致；与 kiwi merchant publish Step 1 同端点）。
        owner_agent_id = resolve_merchant_agent_id(base_url, merchant_id)
    return KiwiCatalogPublisher(
        base_url=base_url,
        owner_token_secret=secret,
        merchant_id=merchant_id,
        owner_agent_id=owner_agent_id,
    )


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


def cmd_listing_publish_listings(args: argparse.Namespace) -> None:
    """全量发布：projection → kiwi-catalog publish（digest 去重 + withdraw reconcile）。"""
    db_path = db_path_from_args(args)
    publisher = _publisher_from_args(args)
    with db_session(db_path) as conn:
        merchant_id = str(args.merchant or "").strip()
        if merchant_id:
            require_merchant(conn, merchant_id)
        projections = list_publishable_listings(conn, merchant_id=merchant_id)
        active_skus = {p["source_product_ref"] for p in projections}
        report = publisher.reconcile(conn, active_skus)
        for projection in projections:
            source_key = str(projection["source_product_ref"])
            try:
                outcome = publisher.publish_listing(
                    conn, projection, source_key=source_key
                )
                if outcome["skipped"]:
                    report.skipped.append({"source_key": source_key, "reason": "digest unchanged"})
                else:
                    report.published.append(outcome)
            except PublishError as exc:
                report.errors.append(f"{source_key}: {exc}")
        emit(report.as_dict(), args.format)
        if report.errors:
            raise PublishError(f"{len(report.errors)} 条发布失败（详见上）")


def cmd_listing_withdraw(args: argparse.Namespace) -> None:
    """手动下架一条已发布 listing。"""
    db_path = db_path_from_args(args)
    # withdraw 只需 owner_token（由 merchant_id 派生），不需要 owner agent。
    publisher = _publisher_from_args(args, resolve_owner=False)
    listing_id = str(args.listing_id or "").strip()
    if not listing_id:
        raise PublishError("listing_id is required")
    with db_session(db_path) as conn:
        publisher.withdraw_listing(conn, listing_id)
    emit(f"已下架 {listing_id}。", args.format)

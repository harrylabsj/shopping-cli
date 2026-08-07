"""ERP 同步 API handler（shopping-cli v0.3 §3/#3；MVP #3 接线）。

POST /v1/merchant/erp/sync —— admin 或 merchant token 触发的 ERP 商品同步
（写面鉴权与 /products 一致；结果 = ErpSyncReport，含 fetched/upserted/
conflicts/errors；网络/结构错误 fail-closed 返回 200+ok:false 信封）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shopping_cli.api import auth as api_auth
from shopping_cli.core.errors import PermissionDenied, ValidationError
from shopping_cli.data_sources.erp_source import (
    ErpSourceError,
    ErpSyncConfig,
    sync_erp_products,
)
from shopping_cli.db.session import db_session
from shopping_cli.services import tokens as token_service


def sync_erp(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /v1/merchant/erp/sync —— 手动触发 ERP 同步（同步本身可重复执行）。"""
    base_url = str(payload.get("base_url") or "").strip()
    if not base_url:
        raise ValidationError("base_url is required")

    with db_session(db_path) as conn:
        # 鉴权：admin token 或 merchant token（写面，与 /products 一致）
        admin = str(payload.get("admin_token") or "").strip()
        actor: str
        try:
            api_auth.require_admin_token(payload)
            actor = "admin"
        except Exception:
            merchant_id = str(payload.get("merchant_id") or "").strip()
            if not merchant_id:
                raise PermissionDenied("merchant_id (or admin_token) is required for ERP sync") from None
            token_service.require_merchant_token(
                conn, merchant_id, api_auth.payload_token(payload)
            )
            actor = f"merchant:{merchant_id}"

        config = ErpSyncConfig(
            base_url=base_url,
            auth_token=str(payload.get("auth_token") or "").strip(),
            timeout_seconds=max(1, min(int(payload.get("timeout_seconds") or 15), 60)),
            page_size=max(1, min(int(payload.get("page_size") or 100), 500)),
            default_merchant_id=str(payload.get("default_merchant_id") or "").strip(),
        )
        try:
            report = sync_erp_products(conn, config)
        except (ErpSourceError, OSError) as exc:
            # ERP 网络/结构失败统一 fail-closed 信封（200 + ok:false）
            return {"ok": False, "error": str(exc), "actor": actor}
    result = report.as_dict()
    result["ok"] = not result["errors"] and not result["conflicts"]
    result["actor"] = actor
    return result

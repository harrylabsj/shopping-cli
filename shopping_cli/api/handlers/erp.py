"""ERP 同步 API handler（shopping-cli v0.3 §3/#3；MVP #3 接线）。

POST /v1/merchant/erp/sync —— admin 或 merchant token 触发的 ERP 商品同步
（写面鉴权与 /products 一致；结果 = ErpSyncReport，含 fetched/upserted/
conflicts/errors；网络/结构错误 fail-closed 返回 200+ok:false 信封）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from shopping_cli.api import auth as api_auth
from shopping_cli.core.catalog import require_merchant
from shopping_cli.core.errors import PermissionDenied, ValidationError
from shopping_cli.data_sources.erp_source import (
    ErpSourceError,
    ErpSyncConfig,
    sync_erp_products,
)
from shopping_cli.db.session import db_session
from shopping_cli.services import tokens as token_service


def _bounded_int(payload: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    """读整数参数；非数字输入 fail-closed 抛 ValidationError（不落到 500）。"""
    raw = payload.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{key} must be an integer") from exc
    return max(lo, min(value, hi))


def sync_erp(db_path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """POST /v1/merchant/erp/sync —— 手动触发 ERP 同步（同步本身可重复执行）。"""
    base_url = str(payload.get("base_url") or "").strip()
    if not base_url:
        raise ValidationError("base_url is required")

    with db_session(db_path) as conn:
        # 鉴权：admin token 或 merchant token（写面，与 /products 一致）。
        # admin 判定先行（仅探测 token 有效性，无副作用），再进入各自分支，
        # 避免 admin 分支的校验错误被误吞进 merchant 分支。
        try:
            api_auth.require_admin_token(payload)
            is_admin = True
        except Exception:
            is_admin = False

        actor: str
        allowed_merchant_id = ""
        default_merchant_id = str(payload.get("default_merchant_id") or "").strip()
        if is_admin:
            actor = "admin"
            # admin 可任意指定默认归属，但必须真实存在（FK 防护，避免裸
            # IntegrityError 落到 500）。
            if default_merchant_id:
                require_merchant(conn, default_merchant_id)
        else:
            merchant_id = str(payload.get("merchant_id") or "").strip()
            if not merchant_id:
                raise PermissionDenied("merchant_id (or admin_token) is required for ERP sync")
            token_service.require_merchant_token(
                conn, merchant_id, api_auth.payload_token(payload)
            )
            require_merchant(conn, merchant_id)
            actor = f"merchant:{merchant_id}"
            # 跨租户防护：merchant-token 调用者只能写入自己名下的行；调用方
            # 传入其他 default_merchant_id 直接拒绝（fail-closed，不静默覆盖）。
            if default_merchant_id and default_merchant_id != merchant_id:
                raise ValidationError(
                    f"merchant token callers may only sync into their own merchant "
                    f"({merchant_id}); got default_merchant_id={default_merchant_id!r}"
                )
            default_merchant_id = merchant_id
            allowed_merchant_id = merchant_id

        config = ErpSyncConfig(
            base_url=base_url,
            auth_token=str(payload.get("auth_token") or "").strip(),
            timeout_seconds=_bounded_int(payload, "timeout_seconds", 15, 1, 60),
            page_size=_bounded_int(payload, "page_size", 100, 1, 500),
            default_merchant_id=default_merchant_id,
            allowed_merchant_id=allowed_merchant_id,
        )
        try:
            report = sync_erp_products(conn, config)
        except (ErpSourceError, OSError) as exc:
            # ERP 网络/结构失败统一 fail-closed 信封（200 + ok:false）
            return {"ok": False, "error": str(exc), "actor": actor}
        except sqlite3.IntegrityError as exc:
            # 数据完整性失败兜底：不落到 500，转为 4xx。
            raise ValidationError(f"ERP sync failed integrity check: {exc}") from exc
    result = report.as_dict()
    result["ok"] = not result["errors"] and not result["conflicts"]
    result["actor"] = actor
    return result

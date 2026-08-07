"""per-field provenance 配置（shopping-cli v0.3 §5）。

fresh_until TTL 默认值：ERP 同步事实 24h（评审拍板：ProductListing 24h /
CapabilityListing 7d；ERP 商品属 product 类）。env 可覆盖。
"""

from __future__ import annotations

import os

DEFAULT_ERP_FRESH_TTL_SECONDS = 24 * 3600


def erp_fresh_ttl_seconds() -> int:
    raw = str(os.environ.get("SHOPPING_ERP_FRESH_TTL_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_ERP_FRESH_TTL_SECONDS
    try:
        return max(60, min(int(raw), 30 * 24 * 3600))
    except (TypeError, ValueError):
        return DEFAULT_ERP_FRESH_TTL_SECONDS

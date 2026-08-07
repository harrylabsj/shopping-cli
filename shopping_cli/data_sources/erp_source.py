"""ERP 商品数据源（shopping-cli data hub v0.2.1 §3/#7）。

Kiwi merchant 不直连 ERP——shopping-cli 作为 Merchant Commerce Data &
Operations Hub 接入 ERP，把外部商品事实同步进本地 ``products`` 表
（source='erp'，UPSTREAM_PROXY 缓存语义），kiwi 侧只消费 shopping-cli
的 ``/products`` 开放层。

权威模型（§5）：
* source='local'（本地录入）= LOCAL_AUTHORITATIVE——录入即事实；
* source='erp'（本模块同步）= UPSTREAM_PROXY——事实在 ERP，本地是缓存；
* 同 SKU 冲突：ERP 同步只覆盖 source='erp' 的行；本地手改行（source='local'）
  冲突时跳过并记入 ``conflicts``（绝不静默合并冲突权威源，fail-closed）。
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

SOURCE_LOCAL = "local"
SOURCE_ERP = "erp"

# 权威语义（data hub v0.2.1 §5）
AUTHORITY_LOCAL = "LOCAL_AUTHORITATIVE"
AUTHORITY_ERP = "UPSTREAM_PROXY"


class ErpSourceError(Exception):
    """ERP 同步失败（fail-closed：任何网络/结构/校验错误抛本异常，不静默容错）。"""


@dataclass(frozen=True)
class ErpSyncConfig:
    """ERP 端点配置。"""

    base_url: str
    auth_token: str = ""
    timeout_seconds: int = 15
    page_size: int = 100
    # ERP 响应中的商品无 merchant_id 时使用的默认归属商家。
    default_merchant_id: str = ""


@dataclass
class ErpSyncReport:
    """一次同步的结果（审计用）。"""

    fetched: int = 0
    upserted: int = 0
    skipped: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "upserted": self.upserted,
            "skipped": self.skipped,
            "conflicts": self.conflicts,
            "errors": self.errors,
            "source": SOURCE_ERP,
            "authority": AUTHORITY_ERP,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ErpSourceError(f"erp base_url must be http(s): {base_url!r}")
    if parsed.username or parsed.password:
        raise ErpSourceError("erp base_url must not embed credentials (userinfo)")
    return base_url.rstrip("/")


def _default_fetch(url: str, auth_token: str, timeout_seconds: int) -> tuple[int, bytes]:
    """默认 fetch：urllib（零依赖）；返回 (status, body_bytes)。"""
    import urllib.request

    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if auth_token:
        request.add_header("Authorization", f"Bearer {auth_token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return (response.status, response.read())
    except Exception as exc:  # 网络/超时/HTTP —— fail-closed
        raise ErpSourceError(f"erp fetch failed for {url}: {exc}") from exc


def _fetch_json(
    url: str,
    *,
    auth_token: str,
    timeout_seconds: int,
    fetch: Callable[..., tuple[int, bytes]] | None = None,
) -> Any:
    """拉取 + 解析。fetch 可注入（测试）：``fetch(url) -> (status, body_bytes)``。"""
    import json

    if fetch is None:
        status, body = _default_fetch(url, auth_token, timeout_seconds)
    else:
        try:
            status, body = fetch(url)
        except Exception as exc:
            raise ErpSourceError(f"erp fetch failed for {url}: {exc}") from exc
    if status >= 400:
        raise ErpSourceError(f"erp fetch returned HTTP {status} for {url}")
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise ErpSourceError(f"erp response for {url} is not valid JSON") from exc


def _parse_erp_product(raw: Any, index: int) -> dict[str, Any]:
    """ERP 商品 → 本地 products 行（price 元 / stock / title 校验）。"""
    if not isinstance(raw, dict):
        raise ErpSourceError(f"erp product at index {index} is not an object")
    sku = raw.get("sku")
    title = raw.get("title")
    price = raw.get("price")
    stock = raw.get("stock")
    if not isinstance(sku, str) or not sku.strip():
        raise ErpSourceError(f"erp product at index {index} is missing sku")
    if not isinstance(title, str) or not title.strip():
        raise ErpSourceError(f"erp product at index {index} is missing title")
    if not isinstance(price, (int, float)) or not price >= 0:
        raise ErpSourceError(f"erp product at index {index} has invalid price")
    if not isinstance(stock, int) or stock < 0:
        raise ErpSourceError(f"erp product at index {index} has invalid stock")
    row = {
        "sku": sku.strip(),
        "title": title.strip(),
        "price": float(price),
        "stock": stock,
        "currency": str(raw.get("currency") or "CNY"),
        "category": str(raw.get("category") or ""),
        "description": str(raw.get("description") or ""),
        "merchant_id": str(raw.get("merchant_id") or ""),
    }
    return row


def sync_erp_products(
    conn: sqlite3.Connection,
    config: ErpSyncConfig,
    *,
    fetch: Callable[..., Any] | None = None,
    now: Callable[[], str] = _now_iso,
) -> ErpSyncReport:
    """分页拉取 ERP 商品并 upsert 到本地 ``products`` 表（source='erp'）。

    * ERP 行 upsert 为 source='erp'（覆盖此前 ERP 同步的缓存）；
    * 本地手改行（source='local'）同 SKU 冲突 → 跳过并记入 conflicts
      （绝不静默合并冲突权威源）；
    * 任何网络/结构错误 → ErpSourceError（fail-closed），不部分落盘后假装成功。
    """
    base = _validate_url(config.base_url)
    report = ErpSyncReport()
    offset = 0
    now_ts = now()

    while True:
        params = urllib.parse.urlencode(
            {"limit": config.page_size, "offset": offset}
        )
        raw = _fetch_json(
            f"{base}/products?{params}",
            auth_token=config.auth_token,
            timeout_seconds=config.timeout_seconds,
            fetch=fetch,
        )
        if not isinstance(raw, dict) or not isinstance(raw.get("results"), list):
            raise ErpSourceError("erp products response must be an object with a results array")
        results = raw["results"]
        report.fetched += len(results)

        for index, item in enumerate(results):
            product = _parse_erp_product(item, index)
            sku = product["sku"]
            merchant_id = product["merchant_id"] or config.default_merchant_id
            if not merchant_id:
                report.errors.append(f"sku {sku}: no merchant_id (and no default_merchant_id)")
                continue
            # 权威冲突：本地手改行不得被 ERP 静默覆盖。
            existing = conn.execute(
                "select source from products where sku = ?", (sku,)
            ).fetchone()
            if existing is not None and existing[0] == SOURCE_LOCAL:
                report.conflicts.append({"sku": sku, "reason": "local authoritative row"})
                report.skipped += 1
                continue

            # v17 provenance 回填（shopping-cli v0.3 §5）：source_revision =
            # 同步批次时间戳（ERP 无版本号时）；observed_at = 同步时间；
            # fresh_until = now + ERP 同步 TTL（默认 24h，可经 env 覆盖）。
            from shopping_cli.db.provenance import erp_fresh_ttl_seconds

            revision = f"erp-sync:{now_ts}"
            conn.execute(
                """
                insert into products(
                    sku, merchant_id, title, description, category, tags_json,
                    price, currency, stock, delivery_attributes_json, active,
                    source, source_revision, observed_at, fresh_until,
                    created_at, updated_at
                ) values (?, ?, ?, ?, ?, '[]', ?, ?, ?, '[]', 1, ?, ?, ?, ?, ?, ?)
                on conflict(sku) do update set
                    merchant_id=excluded.merchant_id,
                    title=excluded.title,
                    description=excluded.description,
                    category=excluded.category,
                    price=excluded.price,
                    currency=excluded.currency,
                    stock=excluded.stock,
                    source=excluded.source,
                    source_revision=excluded.source_revision,
                    observed_at=excluded.observed_at,
                    fresh_until=excluded.fresh_until,
                    updated_at=excluded.updated_at
                """,
                (
                    sku,
                    merchant_id,
                    product["title"],
                    product["description"],
                    product["category"],
                    product["price"],
                    product["currency"],
                    product["stock"],
                    SOURCE_ERP,
                    revision,
                    now_ts,
                    (datetime.fromisoformat(now_ts) + timedelta(seconds=erp_fresh_ttl_seconds())).isoformat(),
                    now_ts,
                    now_ts,
                ),
            )
            report.upserted += 1

        if len(results) < config.page_size:
            break
        offset += len(results)

    conn.commit()
    return report

"""KiwiCatalogPublisher —— 把 Merchant projection 发布到 kiwi-catalog（v0.4 Phase D）。

* owner_token = HMAC-SHA256(secret, "kiwi-catalog-owner:{merchant_id}")——算法
  与 kiwi-catalog ``api/auth.py`` 逐字节一致（双仓复制；tests/test_kiwi_catalog_publisher
  用固定 secret 测试向量锁定，防漂移）；
* digest 去重（DoD #4）：同内容 projection 不重复发布——本地镜像表
  listing_publications 记录 digest，publish 前比较；
* reconcile（DoD #5）：发布前 diff——已发布但 products.active=0（或 sku 已删）
  → withdraw；push-first 手动触发（v0.3 §15，无 scheduled refresh）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from shopping_cli.listings.projection import strip_provenance

# 与 kiwi-catalog api/auth.py owner_token() 的派生前缀逐字节一致
_OWNER_TOKEN_PREFIX = "kiwi-catalog-owner:"


class PublishError(Exception):
    """发布失败（fail-closed：任何 HTTP/校验错误抛本异常，不静默容错）。"""


@dataclass
class PublishReport:
    """一次发布运行的结果（审计用）。"""

    published: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    withdrawn: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "published": self.published,
            "skipped": self.skipped,
            "withdrawn": self.withdrawn,
            "errors": self.errors,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def owner_token(owner_token_secret: str, merchant_id: str) -> str:
    """kiwi-catalog catalog-owner token（HMAC-SHA256；与 kiwi-catalog 逐字节一致）。"""
    material = f"{_OWNER_TOKEN_PREFIX}{merchant_id}".encode("utf-8")
    return hmac.new(owner_token_secret.encode("utf-8"), material, hashlib.sha256).hexdigest()


def projection_digest(projection: dict[str, Any]) -> str:
    """projection 内容 digest（canonical JSON + sha256；与 kiwi-catalog 同算法）。"""
    content = strip_provenance(projection)
    serialized = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class KiwiCatalogPublisher:
    def __init__(
        self,
        *,
        base_url: str,
        owner_token_secret: str,
        merchant_id: str,
        owner_agent_id: str,
        fetch: Callable[..., Any] | None = None,
        timeout_seconds: int = 15,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise PublishError(f"kiwi-catalog base_url must be http(s): {base_url!r}")
        if not owner_token_secret:
            raise PublishError("owner_token_secret is required")
        self.base_url = base_url.rstrip("/")
        self.owner_token = owner_token(owner_token_secret, merchant_id)
        self.merchant_id = merchant_id
        self.owner_agent_id = owner_agent_id
        self.timeout_seconds = timeout_seconds
        self._fetch = fetch  # 注入式（测试）；None = urllib

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"

        def _default() -> tuple[int, bytes]:
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    return (response.status, response.read())
            except urllib.error.HTTPError as exc:
                return (exc.code, exc.read())
            except Exception as exc:
                raise PublishError(f"kiwi-catalog request failed for {url}: {exc}") from exc

        if self._fetch is not None:
            try:
                status, raw = self._fetch(method, url, body, headers)
            except Exception as exc:
                raise PublishError(f"kiwi-catalog request failed for {url}: {exc}") from exc
        else:
            status, raw = _default()

        if status >= 400:
            try:
                detail = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                detail = {}
            raise PublishError(
                f"kiwi-catalog returned HTTP {status} for {method} {path}: {detail.get('error', raw[:200])}"
            )
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise PublishError(f"kiwi-catalog response for {url} is not valid JSON") from exc

    def publish_listing(
        self,
        conn: sqlite3.Connection,
        projection: dict[str, Any],
        *,
        source_key: str,
    ) -> dict[str, Any]:
        """发布/更新一条 projection（digest 去重：同内容跳过，DoD #4）。

        Returns {listing_id, created, skipped}。
        """
        digest = projection_digest(projection)
        existing = conn.execute(
            "select listing_id, digest from listing_publications"
            " where merchant_id = ? and source_key = ?",
            (self.merchant_id, source_key),
        ).fetchone()
        if existing is not None and existing["digest"] == digest:
            return {"listing_id": existing["listing_id"], "created": False, "skipped": True}

        wire = strip_provenance(projection)
        wire.update(
            {
                "owner_agent_id": self.owner_agent_id,
                "merchant_id": self.merchant_id,
                "owner_token": self.owner_token,
            }
        )
        response = self._request("POST", "/v1/listings/publish", wire)
        listing_id = str(response.get("listing", {}).get("listing_id") or "")
        if not listing_id:
            raise PublishError(f"publish response missing listing.listing_id: {response}")

        now = _now_iso()
        conn.execute(
            """
            insert into listing_publications(
                listing_id, merchant_id, source_key, source_revision, digest,
                publication_state, published_at, updated_at
            ) values (?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
            on conflict(merchant_id, source_key) do update set
                listing_id=excluded.listing_id,
                source_revision=excluded.source_revision,
                digest=excluded.digest,
                publication_state='ACTIVE',
                updated_at=excluded.updated_at
            """,
            (
                listing_id,
                self.merchant_id,
                source_key,
                str(projection.get("source_revision") or ""),
                digest,
                now,
                now,
            ),
        )
        conn.commit()
        return {"listing_id": listing_id, "created": bool(response.get("created")), "skipped": False}

    def withdraw_listing(self, conn: sqlite3.Connection, listing_id: str) -> None:
        """下架一条已发布 listing（DoD #5）。"""
        self._request(
            "POST",
            f"/v1/listings/{urllib.parse.quote(listing_id)}/withdraw",
            {"owner_token": self.owner_token},
        )
        conn.execute(
            "update listing_publications set publication_state = 'WITHDRAWN', updated_at = ?"
            " where listing_id = ?",
            (_now_iso(), listing_id),
        )
        conn.commit()

    def reconcile(self, conn: sqlite3.Connection, active_skus: set[str]) -> PublishReport:
        """发布前 diff：镜像表里已发布但商品已 inactive/删除 → withdraw（DoD #5）。"""
        report = PublishReport()
        rows = conn.execute(
            "select listing_id, source_key, publication_state from listing_publications"
            " where merchant_id = ? and publication_state = 'ACTIVE'",
            (self.merchant_id,),
        ).fetchall()
        for row in rows:
            if row["source_key"] not in active_skus:
                try:
                    self.withdraw_listing(conn, row["listing_id"])
                    report.withdrawn.append(
                        {"listing_id": row["listing_id"], "source_key": row["source_key"]}
                    )
                except PublishError as exc:
                    report.errors.append(f"withdraw {row['listing_id']}: {exc}")
        return report

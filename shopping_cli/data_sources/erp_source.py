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

import http.client
import math
import socket
import sqlite3
import ssl
import urllib.parse
import urllib.request
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
    # 授权边界（跨租户防护）：非空时，本同步只允许写入该 merchant 名下的行；
    # feed 自带的 merchant_id 与 default_merchant_id 都必须等于它，否则跳过并记
    # 入 errors（fail-closed，绝不静默改写其他商户的数据）。merchant-token 调用
    # 者由 API handler 强制设为 actor merchant；admin/CLI 留空 = 不受限。
    allowed_merchant_id: str = ""


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


# SSRF 防护：只允许标准 HTTP(S) 端口；解析期校验全部解析 IP（私有/环回/
# 链路本地/云 metadata/多播/保留全拒）；连接直接用已验证 IP（防 DNS
# rebinding），Host 头与 SNI 保留原主机名；禁重定向；响应体有上限。
_ALLOWED_PORTS = (80, 443)
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
# 分页硬上限：防止恶意/异常 feed 无限返回满页（page_size ≤ 500 → 单次同步
# 最多 5000 行）。
_MAX_PAGES = 10


def _blocked_ip(ip_text: str) -> bool:
    import ipaddress

    try:
        addr = ipaddress.ip_address(ip_text.split("%")[0])
    except ValueError:
        return True
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_reserved:
        return True
    if isinstance(addr, ipaddress.IPv4Address):
        # AWS/GCP/Azure 云 metadata 端点（169.254.169.254 已在 link-local 内，
        # 这里防御性显式列出 GCP metadata 的 169.254.169.252/253）。
        if int(addr) in (0xA9FEA9FC, 0xA9FEA9FD):
            return True
        # 0.0.0.0/8（源地址）与 192.0.0.0/24（协议保留）
        if addr.version == 4 and (int(addr) >> 24) in (0, 192):
            return True
    return False


def _resolve_verified_host(hostname: str) -> str:
    """解析 *hostname* 的全部地址；任一被拦即拒绝；返回首个合法 IP。"""
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ErpSourceError(f"erp base_url hostname does not resolve: {hostname}: {exc}") from exc
    if not infos:
        raise ErpSourceError(f"erp base_url hostname does not resolve: {hostname}")
    ips = {info[4][0] for info in infos}
    for ip in ips:
        if _blocked_ip(ip):
            raise ErpSourceError(f"erp base_url resolves to a blocked address: {ip} ({hostname})")
    return sorted(ips)[0]


class _VerifiedIPHTTPConnection(http.client.HTTPConnection):
    """连接已验证 IP，Host 头保留原主机名（不跟随 DNS 二次解析）。"""

    def __init__(self, verified_ip: str, host: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._verified_ip = verified_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._verified_ip, self.port), self.timeout)


class _VerifiedIPHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, verified_ip: str, host: str, port: int, timeout: float, context: Any):
        super().__init__(host, port, timeout=timeout, context=context)
        self._verified_ip = verified_ip

    def connect(self) -> None:
        sock = socket.create_connection((self._verified_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._host)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class _VerifiedIPHandler(urllib.request.HTTPHandler):
    """连接已验证 IP 的 opener handler（http + https 共用一个连接工厂）。"""

    def __init__(self, verified_ip: str, host: str, port: int, timeout: float):
        super().__init__()
        self._verified_ip = verified_ip
        self._host = host
        self._port = port
        self._timeout = timeout

    def http_open(self, req: Any) -> Any:
        conn = _VerifiedIPHTTPConnection(self._verified_ip, self._host, self._port, self._timeout)
        return self.do_open(conn, req)

    def https_open(self, req: Any) -> Any:
        context = ssl.create_default_context()
        conn = _VerifiedIPHTTPSConnection(
            self._verified_ip, self._host, self._port, self._timeout, context
        )
        return self.do_open(conn, req)


def _read_limited(response: Any, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(maximum - total, 65536))
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise ErpSourceError(f"erp response exceeds {maximum} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _validate_url(base_url: str) -> tuple[str, str]:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ErpSourceError(f"erp base_url must be http(s): {base_url!r}")
    if parsed.username or parsed.password:
        raise ErpSourceError("erp base_url must not embed credentials (userinfo)")
    hostname = parsed.hostname
    if not hostname:
        raise ErpSourceError(f"erp base_url has no hostname: {base_url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in _ALLOWED_PORTS:
        raise ErpSourceError(f"erp base_url port must be one of {sorted(_ALLOWED_PORTS)} (got {port})")
    # 构造期即解析并校验全部解析 IP（fail-closed 最早点；_default_fetch
    # 每页请求仍会重新解析校验一次，防止 DNS 变化）。
    _resolve_verified_host(hostname)
    return base_url.rstrip("/"), hostname


def _default_fetch(url: str, auth_token: str, timeout_seconds: int) -> tuple[int, bytes]:
    """默认 fetch：SSRF 防护的 urllib（零额外依赖）；返回 (status, body_bytes)。

    * DNS 解析 → 校验全部解析 IP（私有/环回/metadata 全拒，fail-closed）；
    * 连接使用已验证 IP（防 DNS rebinding）；Host 头与 SNI 保留原主机名；
    * 重定向被拒绝（不跟随 3xx）；
    * 响应体有 2 MiB 上限。
    """
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ErpSourceError(f"erp fetch url has no hostname: {url}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    verified_ip = _resolve_verified_host(hostname)

    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if auth_token:
        request.add_header("Authorization", f"Bearer {auth_token}")
    opener = urllib.request.build_opener(
        _VerifiedIPHandler(verified_ip, hostname, port, timeout_seconds),
        _NoRedirectHandler(),
    )
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            if 300 <= response.status < 400:
                raise ErpSourceError(f"erp fetch returned redirect HTTP {response.status}; redirects are not allowed")
            body = _read_limited(response, _MAX_RESPONSE_BYTES)
            return (response.status, body)
    except ErpSourceError:
        raise
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
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not price >= 0:
        raise ErpSourceError(f"erp product at index {index} has invalid price")
    if not math.isfinite(float(price)):
        raise ErpSourceError(f"erp product at index {index} has a non-finite price")
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
    base, _hostname = _validate_url(config.base_url)
    report = ErpSyncReport()
    offset = 0
    page = 0
    now_ts = now()

    while True:
        page += 1
        if page > _MAX_PAGES:
            raise ErpSourceError(
                f"erp feed returned more than {_MAX_PAGES} full pages; "
                "aborting to bound fetch/write amplification"
            )
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
            # 跨租户防护：merchant-token 调用者只允许写入自己名下的行。
            if config.allowed_merchant_id and merchant_id != config.allowed_merchant_id:
                report.errors.append(
                    f"sku {sku}: merchant_id {merchant_id!r} does not match actor "
                    f"merchant {config.allowed_merchant_id!r}; skipped"
                )
                report.skipped += 1
                continue
            # 归属冲突：SKU 已属于其他 merchant 的行绝不能被 feed 改划归属
            # （admin/CLI 不受限路径 allowed_merchant_id="" 也拦——跨租户
            # 数据移动是静默覆盖，fail-closed）。探测按 (merchant_id, sku)
            # 作用域——不把其他租户的 SKU 存在性/来源暴露给调用方。
            existing = conn.execute(
                "select source, merchant_id from products where sku = ? and merchant_id = ?",
                (sku, merchant_id),
            ).fetchone()
            if existing is None:
                other = conn.execute(
                    "select 1 from products where sku = ?", (sku,)
                ).fetchone()
                if other is not None:
                    report.errors.append(
                        f"sku {sku}: already owned by another merchant; refusing to reassign"
                    )
                    report.skipped += 1
                    continue
                # 新行归属校验：feed 指定的 merchant 必须真实存在（否则
                # FK 违反会以裸 IntegrityError 中止整个同步）。
                merchant_exists = conn.execute(
                    "select 1 from merchants where id = ?", (merchant_id,)
                ).fetchone()
                if merchant_exists is None:
                    report.errors.append(
                        f"sku {sku}: unknown merchant {merchant_id!r}; skipped"
                    )
                    report.skipped += 1
                    continue
            elif existing[0] == SOURCE_LOCAL:
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

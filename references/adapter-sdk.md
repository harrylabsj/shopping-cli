# Adapter SDK（Issue 14 / §6.3）

shopping-cli 是 Merchant Commerce Data & Operations Hub：外部商品事实经**数据源适配器**
接入本地 `products` 表，Kiwi merchant 只消费 shopping-cli 的开放层。Adapter SDK 定义
**稳定 adapter 接口 + 注册表**，不按品牌堆连接器代码。

## 契约

`shopping_cli/data_sources/adapter.py`：

```python
class DataSourceAdapter(Protocol):
    name: str
    description: str
    def sync(self, ctx: SyncContext) -> SyncReport: ...

@dataclass(frozen=True)
class SyncContext:
    conn: sqlite3.Connection
    default_merchant_id: str = ""
    allowed_merchant_id: str = ""      # 跨租户授权边界
    now: Callable[[], str] = _now_iso
    config: dict[str, Any] = {}

@dataclass
class SyncReport:
    source: str; authority: str
    fetched: int; upserted: int; skipped: int
    conflicts: list[dict]; errors: list[str]
    def as_dict(self) -> dict: ...
```

注册表：`register(adapter)` / `registered_adapters()` / `run(name, ctx)`。未知名字 →
`AdapterError`（fail-closed）。

## 权威语义（data hub v0.2.1 §5）

- `source='local'` = LOCAL_AUTHORITATIVE（本地录入即事实）；
- 外部源（`erp` / `csv_excel`）= UPSTREAM_PROXY（本地是缓存，`fresh_until` TTL）；
- 同 SKU 冲突：外部同步只覆盖同 source 行；本地手改行冲突 → 跳过 + conflicts
  （绝不静默合并冲突权威源）。

## 首条路径：CSV/Excel（`csv_excel` 适配器）

`shopping_cli/data_sources/csv_excel_source.py`：读取 `.csv`（stdlib `csv`）或 `.xlsx`
（内置 zip+XML 最小读取器，零第三方依赖）→ 校验 → upsert（source='csv_excel'）。

```sh
shopping merchant create --id merchant-alpha --name "Alpha 保温杯厂" --city 杭州
shopping import-csv-excel --file products.csv --merchant merchant-alpha
shopping adapters list
```

### 必填字段（与 ERP 一致）

| 字段 | 规则 |
| --- | --- |
| `sku` | 非空字符串 |
| `title` | 非空字符串 |
| `price` | 非负数值，且不超币种两位小数精度（Decimal 判定，fail-closed） |
| `stock` | 非负整数 |
| `currency` | 可选，缺省 CNY |
| `category` / `description` | 可选 |
| `merchant_id` | 可选；缺省用 `--merchant` |

### 同步 / 重试 / 新鲜度

- 手动触发（push-first，同 ERP）；重跑覆盖本 source 行。
- `source_revision = csv_excel:<批次时间戳>`；`observed_at` = 同步时间；
  `fresh_until` = now + TTL（默认 24h，`SHOPPING_ERP_FRESH_TTL_SECONDS` 可覆盖）。

### 读写边界 / 删除 / 回滚

- 只写 `products` 表，不写 conversation / 订单 / 库存预留（no-order 边界保持）。
- 导入不物理删除：如需下架，用 `shopping product update --active 0`（回滚 = 再导
  上一版文件或本地改 active）。
- 单行失败不中止整批：记入 `report.errors` 继续；整批在 `conn.commit()` 前失败
  则整体回滚（SQLite 事务）。

## 接入时间实测

见 `references/csv-excel-onboarding-measurement.md`：**空环境 → 首条有效 listing
≈ 0.27s**（merchant create + import 4 行 + 可搜索）。

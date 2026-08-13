# CSV/Excel 接入时间实测（Issue 14）

> 空环境 → 首条有效 listing 的端到端时间实测（2026-08-13，Apple Silicon / Python 3.14）。

## 测量

```sh
# 1. 空 SQLite（--db <new file>）
shopping merchant create --id merchant-alpha --name "Alpha 保温杯厂" --city 杭州
# 2. CSV 导入（4 行商品）
shopping import-csv-excel --file fixtures/adapters/sample-products.csv --merchant merchant-alpha
# 3. 可搜索（首条有效 listing 可见）
shopping search products --query 保温杯
```

`fixtures/adapters/sample-products.csv`（必填字段样例：sku/title/price/stock）：

```csv
sku,title,price,stock,currency,category,description
VQ-003,iPhone 17 Pro,8999.00,120,CNY,electronics,Apple iPhone 17 Pro 256GB
MUG-001,保温杯 500ml,88.50,300,CNY,kitchen,真空不锈钢保温杯
MUG-002,保温杯 350ml,69.90,250,CNY,kitchen,便携保温杯
GEAR-002,工业轴承 6204,12.40,5000,CNY,industrial,6204 深沟球轴承
```

## 结果

| 步骤 | 耗时 |
| --- | --- |
| merchant create + import 4 行 + search 可见 | **0.27s**（合计 wall-clock） |
| 其中 import（fetch/validate/upsert/commit） | ~0.05s |

**首条有效 listing 时间 ≈ 0.27s**，远低于人工字段映射/逐条录入。

## 必填字段 / 人工映射

- 必填：`sku`、`title`、`price`（≤2 位小数）、`stock`（非负整数）。缺任一 → 该行
  记 `report.errors` 跳过，其余行照常导入。
- 需人工决定的映射：文件 `merchant_id` 与 `--merchant` 的归属；`category` 词表；
  `currency` 缺省 CNY。无额外 schema 审批门（fail-closed 只拦非法值，不拦新分类）。

## 同步 / 重试 / 新鲜度

- 手动触发；重复导入幂等 upsert（`sku` 主键），`source_revision` 指向本批次。
- 失败行不中止整批；整批失败（如文件缺失/格式错）整体回滚。
- `fresh_until` = now + 24h（`SHOPPING_ERP_FRESH_TTL_SECONDS` 可覆盖）。

## 读写边界 / 删除 / 回滚

- 只写 `products`；不触碰 conversation / 订单 / 库存预留（no-order 边界）。
- 无物理删除；下架用 `product update --active 0`；回滚 = 重导上一版文件。

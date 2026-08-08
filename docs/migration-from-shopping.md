# Migration From Shopping

Use:

> v3.0 起 `legacy import` 命令已随宿主适配器子系统移除。旧 Shopping JSON 数据
> 不再支持自动导入；如仍需迁移，请直接写 SQL 插入 merchants/products 表。

The legacy adapter imports:

- merchants
- products
- public tags and catalog fields
- product stock

It intentionally ignores legacy transaction records and payment-like records because the shopping-cli MVP is consultation-only. After import, configure delivery rules with `merchant create` fields or `delivery set`.

The import can be retried safely. Existing merchants are skipped by merchant id, existing products are skipped by sku, and the command reports imported and skipped counts instead of creating duplicates.

# Migration From Shopping

Use:

```bash
python3 scripts/shopping.py --db ./shopping-cli.sqlite legacy import --from-json ./shopping.json --format json
```

The legacy adapter imports:

- merchants
- products
- public tags and catalog fields
- product stock

It intentionally ignores legacy transaction records and payment-like records because the shopping-cli MVP is consultation-only. After import, configure delivery rules with `merchant create` fields or `delivery set`.

The import can be retried safely. Existing merchants are skipped by merchant id, existing products are skipped by sku, and the command reports imported and skipped counts instead of creating duplicates.

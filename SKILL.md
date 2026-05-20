---
name: shopping-cli
description: Standalone local commerce consultation runtime. Use when merchants need to publish shop profiles, products, stock, and delivery rules, run resident merchant agents, or review flagged consultations; use when buyers need to search nearby products, ask merchant agents stock/price/delivery questions, summarize replies, or record purchase intent as conversation context only.
version: 2.0.0
author: Jiang Haidong
license: MIT
metadata:
  author: Jiang Haidong
  hermes:
    tags: [commerce, marketplace, consultation, merchant, buyer, sqlite]
    category: commerce
    related_skills: []
---

# shopping-cli

## Boundary

shopping-cli is a consultation network, not a transaction system. It can answer catalog, stock, price, delivery, and substitution questions from trusted merchant data. It must not create commitments, reserve stock, process payment, record payment state, handle refunds, claim escrow, dispatch couriers, or claim delivery success.

If a buyer wants to proceed, record `quote_request` or `purchase_intent` as a conversation message only.

If a conversation is `waiting_merchant` or `human_required`, treat it as pending and keep the conversation open. Do not report failure merely because the merchant agent or merchant human has not replied yet.

## Quick Start

```bash
python3 scripts/shopping.py --db ./shopping-cli.sqlite merchant create --id seller-a --name "West Lake Tea" --city Hangzhou --service-area "West Lake" --contact "wechat:westlake" --delivery-fee 12 --delivery-eta-minutes 45 --tags "tea,gift,longjing"
python3 scripts/shopping.py --db ./shopping-cli.sqlite product add --merchant seller-a --sku tea-a --title "Longjing Gift Box" --price 88 --stock 5 --category tea --tags "longjing,gift" --delivery-attributes "same-city,courier"
python3 scripts/shopping.py --db ./shopping-cli.sqlite buyer ask --buyer alice --text "longjing gift delivery today" --city Hangzhou --area "West Lake" --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite agent run --merchant seller-a --once --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite buyer summarize --conversation CONV-0001 --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite buyer intent --conversation CONV-0001 --intent purchase_intent --text "Buyer wants merchant confirmation." --format json
python3 scripts/shopping.py agent start --merchant seller-a --db ./shopping-cli.sqlite --interval 3 --format json
python3 scripts/shopping.py agent status --merchant seller-a --db ./shopping-cli.sqlite --format json
python3 scripts/shopping.py agent logs --merchant seller-a --tail 20 --format json
python3 scripts/shopping.py agent stop --merchant seller-a --db ./shopping-cli.sqlite --format json
```

Default database: `~/.local/share/shopping-cli/shopping-cli.sqlite`.

## Merchant Workflow

1. Create a merchant profile with city, service area, contact, hours, delivery fee/ETA, and automation boundaries.
2. Use `merchant update` to edit profile, contact, hours, service area, delivery, and automation boundaries.
3. Add products with SKU, title, price, stock, category, tags, and delivery attributes.
4. Use `product update` or `product stock` to update catalog fields and physical inventory counts.
5. Run `agent run --merchant ID --once` for a single polling pass, or use `agent start/status/logs/stop` for a resident daemon with pid, state, and log files.
6. Use `merchant human-review --merchant ID --format json` for bargaining, unclear, unsupported, or risky conversations.

## Buyer Workflow

1. Use `search merchants`, `search products`, `buyer ask`, or `buyer chat` with buyer text and optional city/area.
2. Wait for the merchant agent to answer from trusted data.
3. Use `buyer summarize` to show the option, missing facts, warnings, and next action.
4. Use `buyer intent --intent quote_request|purchase_intent` or `/intent quote_request|purchase_intent ...` inside `buyer chat` only to record interest in the conversation.

## Conversation CLI

Use `conversation create/show/list/message/close/human-review/resolve-review` when the raw consultation lifecycle is needed without starting the API server. Use `human-review queue` for the global unresolved review queue and `agent list/show` for marketplace heartbeat inspection.

## Human Review Rules

Mark the conversation `human_required` for private discounts, bargaining, unclear product references, binding commitments, stock reservation, order confirmation, payment evidence, refunds, disputes, or unsupported promises.

## Legacy Import

Use `legacy import --from-json PATH` to import merchants and products from the older Shopping JSON store. Legacy transaction records are intentionally ignored.

## Verification

Before claiming the package is ready:

- `python3 scripts/shopping.py --help`
- `python3 -m unittest discover -s tests`
- `node --test tests/shopping_plugin.test.mjs`
- `bash scripts/verify.sh`

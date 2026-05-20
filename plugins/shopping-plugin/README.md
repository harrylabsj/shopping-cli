# shopping-cli Plugin

`shopping-plugin` is the lightweight OpenClaw native bridge for local shopping-cli consultation tools.

It exposes tools for merchant setup, product publishing, product search, buyer consultations, merchant-agent polling, summaries, and recording `quote_request` or `purchase_intent` as conversation messages.

Configure `projectRoot` only if the skill is installed somewhere other than `~/.openclaw/workspace/skills/shopping` or `~/.hermes/skills/commerce/shopping`.

Environment fallbacks:

- `SHOPPING_ROOT`
- `SHOPPING_DB`
- `SHOPPING_DATA` deprecated alias for `SHOPPING_DB`

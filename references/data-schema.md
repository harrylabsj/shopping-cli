# shopping-cli SQLite Schema

Default database: `~/.local/share/shopping-cli/shopping-cli.sqlite`.

Core tables:

- `merchants`: shop profile, city, service area, contact, hours, automation boundaries, public tags.
- `products`: SKU, merchant, title, category, tags, price, currency, stock, delivery attributes.
- `delivery_rules`: merchant service area, fee, ETA, radius, notes.
- `conversations`: buyer, merchant, optional SKU, status, and `next_actor` routing target.
- `messages`: sender, intent, text, structured payload.
- `agents`: merchant-agent heartbeat status and capabilities.
- `moderation_flags`: conversation or product flags requiring human review.
- `agent_message_processes`: per-agent buyer-message idempotency key, attempts, processing status, and last error.
- `audit_events`: append-only conversation routing, message, human-review, and agent action events.

The MVP has no transaction or money-movement tables. `quote_request` and `purchase_intent` are message intents.

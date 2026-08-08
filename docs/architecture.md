# shopping-cli Architecture

shopping-cli is a standalone consultation runtime for local commerce.

```text
Buyer CLI <-> Marketplace API/CLI <-> SQLite trusted state <-> Resident merchant agent
```

Trusted state lives in SQLite tables for merchants, products, delivery rules, conversations, messages, agent heartbeats, expiring/revocable API tokens, moderation flags, agent message processing records, and append-only audit events. Conversations carry a `next_actor` field so the harness can route work between buyer, merchant agent, merchant human, and operator without embedding that logic in OpenClaw or Hermes; suspicious-content reviews route to `operator`, while ordinary merchant review routes to `merchant_human`.

The merchant agent core runs against a typed marketplace tools boundary (`MerchantAgentTools`). The local MVP provides a SQLite-backed implementation and an HTTP API-backed implementation, while the deterministic agent logic itself only calls tool methods for heartbeat, waiting conversations, product summaries, message replies, human-review flags, idempotent message claims, completion records, and retry failures. This keeps the agent worker separate from host-specific OpenClaw/Hermes state, and lets resident agents run against the Marketplace API without owning business state directly.


The MVP intentionally has no transaction tables. Buyer `quote_request` and `purchase_intent` are message intents only.

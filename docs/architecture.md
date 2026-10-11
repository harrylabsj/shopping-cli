# shopping-cli Architecture

shopping-cli is a standalone consultation runtime for local commerce.

```text
Buyer CLI <-> Marketplace API/CLI <-> SQLite trusted state <-> Resident merchant agent
```

Trusted state lives in SQLite tables for merchants, products, delivery rules, conversations, messages, agent heartbeats, expiring/revocable API tokens, moderation flags, agent message processing records, and append-only audit events. Conversations carry a `next_actor` field so the harness can route work between buyer, merchant agent, merchant human, and operator without embedding that logic in OpenClaw or Hermes; suspicious-content reviews route to `operator`, while ordinary merchant review routes to `merchant_human`.

The merchant agent core runs against a typed marketplace tools boundary (`MerchantAgentTools`). The local MVP provides a SQLite-backed implementation and an HTTP API-backed implementation, while the deterministic agent logic itself only calls tool methods for heartbeat, waiting conversations, product summaries, message replies, human-review flags, idempotent message claims, completion records, and retry failures. This keeps the agent worker separate from host-specific OpenClaw/Hermes state, and lets resident agents run against the Marketplace API without owning business state directly.


The MVP intentionally has no transaction tables. Buyer `quote_request` and `purchase_intent` are message intents only.

### Frozen contract provenance and delivery checks

Runtime schemas remain the same four shopping.negotiation/0.1 files. Provenance is pinned to the actual immutable Kiwi e703bc0d67e24a98c417274e46f37d210eff86ac manifest (34 rows), retaining bundle a867a10774c7990f585e57ea186262a46a2eeb62e75d81f0e8606940dc30a4bb. The older lock combined af21 source metadata with this later bundle; this correction does not upgrade wire schemas. The default guard always verifies the trusted raw manifest and all four vendored file bytes without requiring a sibling checkout.

Both npm and wheel payloads include recursive contract JSON resources and the lock. Docker selects the sole locally built project wheel instead of a stale fixed version; the local compose tag is a build alias, not a published version. CI additionally runs the existing end-to-end/benchmark/resource/pack verifier. Release verification uses normal dependency resolution plus pip check and imports, rather than no-deps CLI help alone; its fresh install/Docker execution still requires an actual authorized CI run.

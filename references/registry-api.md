# Marketplace API

The old JSON registry has been replaced by the SQLite-backed marketplace API.

Inspect route metadata:

```bash
python3 scripts/shopping.py --db ./shopping-cli.sqlite api routes --format json
```

Serve locally after installing API dependencies:

```bash
pip install -e '.[api]'
python3 scripts/shopping.py --db ./shopping-cli.sqlite api serve --host 127.0.0.1 --port 8765
```

MVP routes:

- `GET /health`
- `GET /merchants`
- `POST /merchants`
- `GET /merchants/{merchant_id}`
- `PATCH /merchants/{merchant_id}`
- `POST /products`
- `GET /products/{sku}`
- `PATCH /products/{sku}`
- `GET /search/products`
- `GET /search/merchants`
- `POST /channels/messages`
- `POST /buyer/ask`
- `POST /conversations`
- `GET /conversations/{conversation_id}`
- `GET /buyers/{buyer_id}/conversations`
- `GET /merchants/{merchant_id}/conversations`
- `POST /conversations/{conversation_id}/messages`
- `POST /conversations/{conversation_id}/close`
- `POST /agents/heartbeat`
- `GET /agents/tokens`
- `POST /agents/tokens`
- `POST /agents/tokens/revoke`
- `POST /agents/tokens/rotate`
- `POST /agents/messages/claim`
- `POST /agents/messages/complete`
- `POST /agents/messages/fail`
- `POST /agents/messages/abandon`
- `POST /agents/messages/abandon-stale`
- `GET /agents`
- `GET /agents/{agent_id}`
- `GET /merchants/{merchant_id}/agents`
- `POST /audit/tool-calls`
- `GET /audit/events`
- `GET /human-review/queue`
- `GET /human-review/{review_id}`
- `POST /human-review/{review_id}/resolve`
- `GET /merchants/{merchant_id}/human-review`
- `POST /conversations/{conversation_id}/human-review`
- `POST /conversations/{conversation_id}/human-review/resolve`

The API is the trusted state boundary for consultation data. Merchant agents should use API/CLI operations instead of writing SQLite directly.

## API Tokens

`POST /merchants` is a bootstrap operation and requires `SHOPPING_ADMIN_TOKEN` as `admin_token` in the JSON body or as `Authorization: Bearer <token>`. It returns the new merchant's `merchant_token` once. New merchant, buyer, and agent tokens are opaque and do not embed merchant or buyer ids. Raw tokens are stored as SHA-256 digests with non-secret prefix/suffix hints for listing and audit views. Stored token hashes are not accepted as bearer credentials.

Token-protected operations include product writes, merchant profile updates, merchant/merchant-agent conversation replies, agent heartbeats, agent message processing, audit writes, and human-review create/resolve actions. `POST /buyer/ask` and `POST /conversations` can open new buyer consultations without a buyer token, but they create a fresh conversation instead of minting another token for an existing open one. Buyer message appends, buyer closes, buyer conversation reads, and buyer conversation lists require the conversation-scoped `buyer_token` returned at creation. Buyer message appends cannot set conversation status, and generic message appends cannot close conversations; explicit close and human-review endpoints own those state changes. Every close path records a `conversation_closed` audit event, including human-review close resolutions. Merchant or agent message appends that set `human_required` create a matching human-review flag so review queues remain consistent with conversation state.

External channel adapters use `POST /channels/messages` with `channel`, `external_user_id`, `text`, and optional `conversation_id`, `city`, `area`, and `external_message_id`. The API requires a channel token from `SHOPPING_CHANNEL_TOKENS` such as `telegram:secret,whatsapp:secret2`, or a global `SHOPPING_CHANNEL_TOKEN`; pass it as `channel_token` or a Bearer token. Channel buyer ids are always derived from `<normalized-channel>:<external_user_id>`. A stale `processing` ingress claim older than five minutes is released so webhook retry delivery can recover after an interrupted attempt.

List/search, agent heartbeat list, agent token list, audit event, merchant human-review, and human-review queue endpoints accept `limit` and `offset` where applicable. `limit` is capped to prevent unbounded response growth. Closed conversations are terminal for message, close, and human-review mutations; reopen behavior must be implemented through an explicit audited endpoint.

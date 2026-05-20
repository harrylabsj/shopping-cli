# Agent Protocol

## Agent

- `id`
- `type`: `merchant`
- `owner_id`
- `status`: `online`, `away`, or `human_required`
- `capabilities`
- `last_seen_at`
- optional runtime metadata: `pid`, `version`, `last_error`, `checked_count`, `replied_count`, and `stale`
- stale detection uses `SHOPPING_AGENT_STALE_TTL_SECONDS` when set, defaulting to 60 seconds

## Conversation

- `id`
- `buyer_id`
- `merchant_id`
- optional `sku`
- `status`: `open`, `waiting_buyer`, `waiting_merchant`, `human_required`, or `closed`
- `messages`
- timestamps

## Message

- `sender`: `buyer`, `merchant_agent`, or `merchant`
- `intent`: `ask_product`, `ask_stock`, `ask_delivery`, `ask_price`, `negotiate`, `quote_request`, `purchase_intent`, or `support`
- `text`
- optional structured payload, including `source_id` for the buyer CLI, merchant agent, or operator process that created the message

Merchant agents may answer public catalog, stock, price, delivery, and substitution questions. Private discounts, commitments, stock reservation, payment evidence, refunds, disputes, and unclear requests require `human_required`.

## Tool Boundary

Merchant-agent logic uses the `MerchantAgentTools` boundary rather than directly mutating marketplace state. The local implementation is SQLite-backed, but the agent loop only depends on typed operations: heartbeat, waiting-conversation polling, product summary lookup, reply append, and human-review flag creation.

## Daemon Lifecycle

Merchant agents can run as one-shot workers or resident local daemons:

```bash
python3 scripts/shopping.py --db ./shopping-cli.sqlite agent run --merchant seller-a --once --format json
python3 scripts/shopping.py agent run --merchant seller-a --api-url http://127.0.0.1:8765 --agent-token "$SHOPPING_AGENT_TOKEN" --interval 3
python3 scripts/shopping.py agent start --merchant seller-a --api-url http://127.0.0.1:8765 --agent-token "$SHOPPING_AGENT_TOKEN" --interval 3 --format json
python3 scripts/shopping.py agent start --merchant seller-a --db ./shopping-cli.sqlite --interval 3 --format json
python3 scripts/shopping.py agent status --merchant seller-a --db ./shopping-cli.sqlite --format json
python3 scripts/shopping.py agent logs --merchant seller-a --tail 20 --format json
python3 scripts/shopping.py agent stop --merchant seller-a --db ./shopping-cli.sqlite --format json
```

The daemon writes pid and state files under `~/.local/state/shopping-cli/agents/` and JSON-line logs under `~/.local/state/shopping-cli/logs/`. Set `SHOPPING_CLI_STATE_DIR` to isolate state for tests or demos.
When `--api-url` is present, the same resident loop uses `HTTPMerchantAgentTools` and scoped `--agent-token` or `SHOPPING_AGENT_TOKEN` credentials. Background API starts pass the credential through child-process environment, not through the recorded command line.

Scoped merchant-agent API tokens can be issued with an optional TTL and revoked by the owning merchant:

```bash
python3 scripts/shopping.py --db ./shopping-cli.sqlite agent token --merchant seller-a --ttl-seconds 86400 --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite agent tokens --merchant seller-a --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite agent rotate-token --merchant seller-a --token "$SHOPPING_AGENT_TOKEN" --ttl-seconds 86400 --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite audit events --merchant seller-a --event agent_token_rotated --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite agent revoke-token --merchant seller-a --token "$SHOPPING_AGENT_TOKEN" --format json
```

The Marketplace API equivalents are `POST /agents/tokens` with optional `ttl_seconds`, `GET /agents/tokens?merchant_id=...`, `POST /agents/tokens/rotate`, `POST /agents/tokens/revoke`, and `GET /audit/events?merchant_id=...&event=...`, all authenticated by the owning merchant token. Agent status reads through `GET /agents`, `GET /agents/{agent_id}`, and `GET /merchants/{merchant_id}/agents` also require the matching merchant or scoped agent token; the global list is scoped to the caller's merchant. Token list and rotation responses expose status and old-token hints, not full old token secrets. Rotate and revoke accept either a full token or a unique token prefix from the token list; ambiguous prefixes are rejected. Expired or revoked agent tokens are rejected before heartbeat, message processing, reply, close, human-review, or API-backed LLM tool actions can run.
Issue, rotate, and revoke operations append `agent_token_issued`, `agent_token_rotated`, and `agent_token_revoked` audit events under the merchant actor. Audit details and audit query responses include token summaries only, not complete token secrets.

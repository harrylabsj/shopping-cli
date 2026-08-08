# shopping-cli

shopping-cli is a standalone, SQLite-backed AI consultation runtime for local commerce. Merchants publish shop profiles, products, stock, and delivery rules; buyers search nearby supply, open consultations, and receive deterministic merchant-agent replies.

The MVP is not a transaction system. It does not create commitments, reserve stock, process payment, record payment state, custody funds, handle refunds, dispatch couriers, or claim delivery success. Buyer intent is recorded only as `quote_request` or `purchase_intent` messages in a conversation.

Pending merchant states are not failures. When a conversation is `waiting_merchant`, the buyer request has been handed to the merchant agent; when it is `human_required`, the merchant agent has escalated it to the merchant human. Keep the conversation open and wait for the next merchant-side message.

## Install and Verify

```bash
bash scripts/verify.sh
```

For contributor checks, install the API and development extras, then run the
same lint, type, coverage, and Node gates used by CI. Release verification
builds both Python artifacts and installs the wheel into an isolated virtual
environment before checking all three console entry points.

```bash
pip install -e '.[api,dev]'
bash scripts/quality.sh
bash scripts/verify_release.sh
```

Optional API dependencies are declared in `pyproject.toml` for the FastAPI marketplace service:

```bash
pip install -e '.[api]'
```

## Quick Start

```bash
python3 scripts/shopping.py --db ./shopping-cli.sqlite merchant create \
  --id seller-a \
  --name "West Lake Tea" \
  --city Hangzhou \
  --service-area "West Lake" \
  --contact "wechat:westlake" \
  --hours "09:00-21:00" \
  --delivery-fee 12 \
  --delivery-eta-minutes 45 \
  --tags "tea,gift,longjing"

python3 scripts/shopping.py --db ./shopping-cli.sqlite product add \
  --merchant seller-a \
  --sku tea-a \
  --title "Longjing Gift Box" \
  --price 88 \
  --stock 5 \
  --category tea \
  --tags "longjing,gift" \
  --delivery-attributes "same-city,courier"

python3 scripts/shopping.py --db ./shopping-cli.sqlite merchant update --id seller-a --hours "10:00-20:00"
python3 scripts/shopping.py --db ./shopping-cli.sqlite merchant list --limit 50 --offset 0
python3 scripts/shopping.py --db ./shopping-cli.sqlite delivery set --merchant seller-a --service-area "West Lake" --fee 12 --eta-minutes 45
python3 scripts/shopping.py --db ./shopping-cli.sqlite product update --merchant seller-a --sku tea-a --stock 4 --price 92
python3 scripts/shopping.py --db ./shopping-cli.sqlite search products --query "longjing"
python3 scripts/shopping.py --db ./shopping-cli.sqlite search products --query "longjing" --limit 10 --offset 0 --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite search merchants --query "west lake" --city Hangzhou
python3 scripts/shopping.py --db ./shopping-cli.sqlite search merchants --query "west lake" --city Hangzhou --limit 10 --offset 0 --format json

python3 scripts/shopping.py --db ./shopping-cli.sqlite buyer ask \
  --buyer alice \
  --text "longjing gift delivery today" \
  --city Hangzhou \
  --area "West Lake"

python3 scripts/shopping.py --db ./shopping-cli.sqlite buyer ask \
  --buyer alice \
  --text "longjing gift delivery today" \
  --city Hangzhou \
  --area "West Lake" \
  --format json

python3 scripts/shopping.py --db ./shopping-cli.sqlite agent run --merchant seller-a --once --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite buyer summarize --conversation CONV-0001
python3 scripts/shopping.py --db ./shopping-cli.sqlite buyer summarize --conversation CONV-0001 --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite buyer intent --conversation CONV-0001 --intent purchase_intent --text "Buyer wants merchant confirmation."
python3 scripts/shopping.py --db ./shopping-cli.sqlite buyer intent --conversation CONV-0001 --intent purchase_intent --text "Buyer wants merchant confirmation." --format json

printf 'longjing gift delivery today\n/summary\n/quit\n' | \
  python3 scripts/shopping.py --db ./shopping-cli.sqlite buyer chat --buyer alice --city Hangzhou --area "West Lake" --format json
```

Default database path: `~/.local/share/shopping-cli/shopping-cli.sqlite`.

## Channel Ingress

External channel adapters can ingest buyer messages through a stable local entry point before real WhatsApp, Telegram, Slack, or OpenClaw/Hermes gateway bridges are attached:

```bash
python3 scripts/shopping.py --db ./shopping-cli.sqlite channel ingest \
  --channel whatsapp \
  --external-user "+15550001111" \
  --text "longjing gift delivery today" \
  --city Hangzhou \
  --area "West Lake" \
  --format json

python3 scripts/shopping.py --db ./shopping-cli.sqlite channel ingest \
  --channel whatsapp \
  --external-user "+15550001111" \
  --conversation CONV-0001 \
  --text "Any stock left?" \
  --format json
```

The buyer id is always derived as `<channel>:<external-user>` for public channel ingress. Channel names are trimmed and lower-cased before buyer ids, metadata, and idempotency keys are written. Message payloads preserve `source_id`, `channel`, `external_user_id`, and optional `external_message_id`. When `external_message_id` is provided, retries with the same normalized `(channel, external_user_id, external_message_id)` return the original message or original no-match result instead of appending a duplicate or creating a later conversation from an old webhook retry. A stale `processing` ingress row older than five minutes is treated as abandoned so webhook delivery can recover after an interrupted attempt.
The default `channel ingest` text output summarizes the derived buyer, idempotency state, conversation, message, routing status, and selected product; `--format json` preserves the full adapter payload.

## Conversation CLI

The raw conversation lifecycle is available without the API server:

```bash
python3 scripts/shopping.py --db ./shopping-cli.sqlite conversation create --buyer alice --merchant seller-a --sku tea-a --intent ask_stock --text "Is this available?"
python3 scripts/shopping.py --db ./shopping-cli.sqlite conversation create --buyer alice --merchant seller-a --sku tea-a --intent ask_stock --text "Is this available?" --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite conversation show --conversation CONV-0001
python3 scripts/shopping.py --db ./shopping-cli.sqlite conversation show --conversation CONV-0001 --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite conversation message --conversation CONV-0001 --sender merchant_agent --intent ask_stock --text "Stock is 5." --status waiting_buyer
python3 scripts/shopping.py --db ./shopping-cli.sqlite conversation message --conversation CONV-0001 --sender merchant_agent --intent ask_stock --text "Stock is 5." --status waiting_buyer --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite conversation human-review --conversation CONV-0001 --reason low_confidence
python3 scripts/shopping.py --db ./shopping-cli.sqlite conversation human-review --conversation CONV-0001 --reason low_confidence --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite conversation resolve-review --conversation CONV-0001 --action reply --sender merchant --text "Human reviewed."
python3 scripts/shopping.py --db ./shopping-cli.sqlite conversation resolve-review --conversation CONV-0001 --action reply --sender merchant --text "Human reviewed." --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite conversation close --conversation CONV-0001 --sender operator --text "Closed."
python3 scripts/shopping.py --db ./shopping-cli.sqlite conversation close --conversation CONV-0001 --sender operator --text "Closed." --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite conversation list --buyer alice --status waiting_buyer
python3 scripts/shopping.py --db ./shopping-cli.sqlite conversation list --buyer alice --status waiting_buyer --limit 50 --offset 0 --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite merchant human-review --merchant seller-a --limit 50 --offset 0
python3 scripts/shopping.py --db ./shopping-cli.sqlite human-review queue --limit 50 --offset 0
python3 scripts/shopping.py --db ./shopping-cli.sqlite human-review queue --limit 50 --offset 0 --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite human-review show --review 1
python3 scripts/shopping.py --db ./shopping-cli.sqlite human-review show --review 1 --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite human-review resolve --review 1 --action reply --sender merchant --text "Human reviewed."
python3 scripts/shopping.py --db ./shopping-cli.sqlite human-review resolve --review 1 --action reply --sender merchant --text "Human reviewed." --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite agent list --limit 50 --offset 0
python3 scripts/shopping.py --db ./shopping-cli.sqlite agent list --limit 50 --offset 0 --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite agent show --agent shopping-cli-merchant-agent:seller-a
python3 scripts/shopping.py --db ./shopping-cli.sqlite agent show --agent shopping-cli-merchant-agent:seller-a --format json
```

The default `conversation create` text output returns the new conversation id and routing state.
The default `conversation show` text output prints the conversation summary, review count, and messages.
The default `conversation message` text output confirms the appended message id and updated routing state. Buyer messages cannot pass `--status`; routing advances automatically from the sender. `conversation message --status human_required` also creates a human-review flag; use `conversation close` instead of `conversation message --status closed` for audited closure.
The default `conversation human-review` text output confirms the review id and human-routing state.
The default `conversation resolve-review` text output confirms the resolution count and resulting routing state.
The default `conversation close` text output confirms the final status.
The default `conversation list` text output is a compact status table for quick buyer/merchant queue scans; `merchant human-review` uses the same table for one merchant's human-required conversations.
The default `human-review queue` text output is a concise merchant workbench table, `human-review show` prints the review summary plus recent conversation messages, and `human-review resolve` summarizes the resulting conversation status. `--format json` keeps the stable adapter/script output.
The default `agent run --once` text output summarizes checked, replied, failed, and abandoned work. `agent start`, `agent stop`, and `agent status` summarize daemon mode, API URL, host/session metadata, running state, log path, and state path; `agent status` also includes counters and heartbeat state. `agent logs` prints compact recent log lines. Agent run/status/log text redacts token-like values from error strings before printing them. `agent heartbeat` confirms the recorded heartbeat in a readable summary. `agent list` prints a compact heartbeat table for operations checks, while `agent show` prints one heartbeat in detail.

## Resident Agent Daemon

Use `agent run --once` for a single deterministic polling pass. Use the daemon lifecycle commands when a merchant agent should keep polling in the background:

```bash
python3 scripts/shopping.py agent start --merchant seller-a --db ./shopping-cli.sqlite --interval 3 --format json
python3 scripts/shopping.py agent status --merchant seller-a --db ./shopping-cli.sqlite --format json
python3 scripts/shopping.py agent logs --merchant seller-a --tail 20 --format json
python3 scripts/shopping.py agent stop --merchant seller-a --db ./shopping-cli.sqlite --format json
python3 scripts/shopping.py agent start --merchant seller-a --api-url http://127.0.0.1:8765 --agent-token "$SHOPPING_AGENT_TOKEN" --interval 3 --format json
```

Pid, state, and log files are written under `~/.local/state/shopping-cli/` by default. Set `SHOPPING_CLI_STATE_DIR` to use a different state directory for tests or demos.

To run through the Marketplace API boundary instead of direct SQLite access:

```bash
python3 scripts/shopping.py --db ./shopping-cli.sqlite agent token --merchant seller-a --ttl-seconds 86400 --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite agent tokens --merchant seller-a --limit 50 --offset 0 --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite agent rotate-token --merchant seller-a --token "$SHOPPING_AGENT_TOKEN" --ttl-seconds 86400 --format json
python3 scripts/shopping.py --db ./shopping-cli.sqlite audit events --merchant seller-a --event agent_token_rotated --limit 50 --offset 0
python3 scripts/shopping.py --db ./shopping-cli.sqlite audit events --merchant seller-a --event agent_token_rotated --limit 50 --offset 0 --format json
python3 scripts/shopping.py agent run --merchant seller-a --once --api-url http://127.0.0.1:8765 --agent-token "$SHOPPING_AGENT_TOKEN" --format json
python3 scripts/shopping.py agent run --merchant seller-a --api-url http://127.0.0.1:8765 --agent-token "$SHOPPING_AGENT_TOKEN" --interval 3
python3 scripts/shopping.py --db ./shopping-cli.sqlite agent revoke-token --merchant seller-a --token "$SHOPPING_AGENT_TOKEN" --format json
```

Use `agent token` locally, or `POST /agents/tokens` with a merchant token over the API, to issue a narrower token for the default merchant agent. Add `--ttl-seconds` locally, or `ttl_seconds` in the API payload, to issue an expiring scoped token. Use `agent tokens` locally, or `GET /agents/tokens?merchant_id=...&limit=50&offset=0` with a merchant Bearer token, to list scoped token status without exposing full token secrets. The default `agent tokens` text output is a readable status table with token prefixes for routine rotate/revoke operations; `--format json` remains the script-friendly output. Use `agent rotate-token` locally, or `POST /agents/tokens/rotate`, to revoke an old scoped agent token and issue a replacement in one step. Use `agent revoke-token` locally, or `POST /agents/tokens/revoke` with a merchant token over the API, to revoke a scoped agent token. Rotate and revoke accept either the full `token`/`--token` or the unique `token_prefix`/`--token-prefix` from the token list; ambiguous prefixes are rejected. API-backed agent runs accept `--agent-token` or `SHOPPING_AGENT_TOKEN` for that scoped token, while `--merchant-token` and `SHOPPING_MERCHANT_TOKEN` remain available for local demos. Add `--host` and `--session-id`, or `SHOPPING_AGENT_HOST` and `SHOPPING_AGENT_SESSION_ID`, to attach host metadata to API-backed merchant-agent tool-call audit events.
Agent token issue, rotate, and revoke operations append audit events with token status hints, never the full token secret. Use `audit events` locally, or `GET /audit/events?merchant_id=...&event=...&limit=50&offset=0` with a merchant Bearer token, to inspect merchant audit events.
The default `audit events` text output is a compact table with secret-safe detail hints such as token prefixes; `--format json` keeps the full structured, secret-safe audit payload.
Set `SHOPPING_MARKETPLACE_API_URL` or `SHOPPING_API_URL` to omit `--api-url` from repeated agent runs or background starts. `agent start --api-url` passes credentials to the child process through environment variables and keeps tokens out of the recorded pid command.

## Marketplace API

Inspect routes:

```bash
python3 scripts/shopping.py --db ./shopping-cli.sqlite api routes
python3 scripts/shopping.py --db ./shopping-cli.sqlite api routes --format json
```

The default text output lists `METHOD /path` rows for quick inspection. The JSON output keeps a backward-compatible `routes` path list and also includes `route_details` with the HTTP methods available on each path. The local API covers catalog, search, conversations, message append/close, agent token issuance/listing/rotation/revocation, agent heartbeats, agent message claim/complete/fail/abandon, merchant audit-event queries, and human-review queue/detail/resolve operations. In environments without FastAPI installed, `create_app()` still returns a lightweight ASGI app for local tests and demos.

External channel adapters can use `POST /channels/messages` with `channel`, `external_user_id`, `text`, and optional `conversation_id`, `city`, `area`, and `external_message_id`. API channel ingress requires `SHOPPING_CHANNEL_TOKENS` such as `telegram:secret,whatsapp:secret2`, or a global `SHOPPING_CHANNEL_TOKEN`; pass the matching token as `channel_token` or as a Bearer token. The optional `external_message_id` is an idempotency key for webhook retry safety, `channel` is normalized the same way as the CLI ingress path, and stale in-flight ingress claims are recoverable after five minutes.

`POST /merchants` requires `SHOPPING_ADMIN_TOKEN` as `admin_token` in the JSON body or as a Bearer token, then returns a local `merchant_token` once. Use `Idempotency-Key` for safe bootstrap retries; use `POST /merchants/{merchant_id}/token/rotate` with a current merchant token or the admin token if the credential must be replaced, and use the admin-only `/token/revoke` endpoint to revoke all merchant credentials. `GET /merchants/{merchant_id}/private-config` exposes `automation_boundaries` only to that merchant or its scoped agent. `POST /buyer/ask` and `POST /conversations` require `SHOPPING_BUYER_BOOTSTRAP_TOKEN` as `buyer_bootstrap_token` in the JSON body or as a Bearer token before creating durable buyer conversation state; each successful call still opens a fresh conversation and returns a buyer token scoped to that conversation. Merchant and buyer credentials have default TTLs, and closing a conversation revokes its buyer token. Product writes, merchant profile updates, merchant human replies, merchant/operator closes, and human-review resolution require that merchant token in the JSON body as `merchant_token` or as a Bearer token. Agent heartbeats, agent message processing, merchant-agent replies, merchant-agent closes, merchant-agent human-review flags, and tool-call audit writes may use either the merchant token or a scoped agent token. New merchant, buyer, and agent tokens are opaque and do not embed merchant or buyer ids. Raw tokens are stored only as SHA-256 digests with prefix/suffix hints for listing; stored token hashes are not accepted as bearer credentials. Agent status reads also require an owner token: `/agents` is scoped to the caller's merchant, and agent detail or merchant-agent list reads require the matching merchant/agent token. Scoped agent tokens can be revoked and can optionally expire through `ttl_seconds`. Conversation reads, buyer message appends, buyer closes, and human-review queue/detail reads require an owner token: buyer tokens can read or write only their issued conversation, while merchant and agent tokens can read conversations and review queues for their merchant. Buyer message appends cannot override conversation status, and generic message appends cannot close conversations; closes and human-review routing must use their explicit endpoints. Unresolved human review blocks normal close paths until resolution. Every close path records a `conversation_closed` audit event, including human-review close resolutions. Merchant or agent message appends that set `human_required` create a matching human-review flag so the queue and conversation state stay aligned. Closed conversations reject later message, close, and human-review mutations until an explicit audited reopen flow exists. Remote Marketplace API URLs require HTTPS unless `SHOPPING_ALLOW_INSECURE_HTTP=true` is explicitly set for a trusted internal network; long-running Agent credentials must be supplied through environment/secret injection rather than argv.

Serve the FastAPI app after installing API dependencies:

```bash
python3 scripts/shopping.py --db ./shopping-cli.sqlite api serve --host 127.0.0.1 --port 8765
```

## Negotiation Commerce API (shopping.negotiation/0.1)

shopping-cli is the authoritative Commerce Gateway for the frozen `shopping.negotiation/0.1` protocol used by Kiwi negotiation agents. The negotiation endpoints are available in both the FastAPI app and the fallback ASGI router:

- `GET /capabilities` advertises the supported protocol versions and backend capabilities; `orders` is always `false` — negotiation never creates orders, payments, or inventory reservations.
- `GET /negotiation/pending-messages`, `POST /negotiation/claims`, `GET /negotiation/snapshot`, `POST /negotiation/decisions`, and `POST /negotiation/claims/complete|fail|abandon` cover the dual-role (buyer + merchant) claim → snapshot → decision → complete loop.
- Role and owner identity are always derived from the Bearer token (fail closed); client-declared `merchant_id`/`role` fields are ignored. Snapshots are role-trimmed and never expose the merchant's private `automation_boundaries` floor or any buyer-private budget.
- `POST /negotiation/decisions` is the only write intent: the server-side policy gate strictly validates the frozen decision schema, conversation state, `next_actor`, claim ownership, catalog facts, merchant floor/discount boundaries, and privacy leaks before atomically writing the structured message, advancing `next_actor`, and recording audit events. Replays with the same idempotency key never duplicate messages; a conflicting payload under a used key fails closed with 409.
- Frozen schemas ship in `shopping_cli/contracts/shopping.negotiation/0.1/` and cross-language fixtures in `fixtures/negotiation/` (validated by `tests/test_negotiation_contracts.py`), so the runtime never depends on a sibling Kiwi checkout.

See `references/negotiation-api.md` for the full endpoint contract, policy-gate order, Kiwi integration flow, and known limitations.

## License

[Apache License 2.0](LICENSE) — 协议契约与参考实现同许可（与 Kiwi、kiwi-catalog 一致）。

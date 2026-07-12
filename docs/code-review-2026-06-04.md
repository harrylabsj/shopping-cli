# shopping-cli Code Review — 2026-06-04

Scope: whole-project review of `shopping-cli` (~10K LOC Python + the Node plugin), not a branch diff review.

No code changes were made during the review.

## Verification

- `python3 -m unittest discover -s tests` — 341 tests passed

## Summary

The project remains in good shape. The earlier review (`docs/code-review-2026-05-21.md`) was largely addressed by commit `25d0500`; this pass confirms those fixes and focuses on the layer that was not previously in scope — persistence and concurrency.

The highest-priority remaining issues are:

1. SQLite is opened with no `busy_timeout` and no WAL, so the project's own multi-writer design (daemon + API threadpool + CLI) will hard-fail under contention.
2. `init_db` runs the full schema, a token-migration scan, and a whole-table backfill on every connection, including read paths.
3. `search_merchants` still loads the full candidate set before pagination (the `search_products` fix was not mirrored).
4. The production config validation computes a channel-token check it never enforces.
5. Buyer endpoints have auth but still lack rate limiting and idempotency.

## Status of the 2026-05-21 review

| Prior finding | Status |
|---|---|
| P1 Human-review double-resolve | Fixed — `rowcount != 1` guard at `api/app.py:1503` |
| P1 Plugin mutating tools ungated | Fixed — `writesEnabled` gate, read-only by default (`plugins/shopping-plugin/openclaw_compat.js:188,301,341`) |
| P2 Public buyer endpoints unauthenticated | Fixed — buyer-bootstrap + channel tokens, `buyer_id` override rejected (`api/app.py:808,830`) |
| P2 Serializers leak `automation_boundaries`/`contact` | Fixed — `_public_merchant_summary` strips them (`api/app.py:395`) |
| P2 Product search loads all candidates | Partial — `search_products` now capped; `search_merchants` still unbounded (see [P2] below) |
| P2 README/pyproject dependency mismatch | Fixed |

## Findings

### [P1] SQLite opened with no `busy_timeout` and no WAL

**Confidence:** 8/10
**Location:** `shopping_cli/db/session.py:48-55`

```python
conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row
conn.execute("pragma foreign_keys = on")
```

The architecture runs multiple writers against one SQLite file: a resident merchant daemon polling on an interval, the uvicorn API (FastAPI sync endpoints run in a threadpool, so connections are concurrent), and the CLI. With the default `journal_mode=delete` and no `busy_timeout`, a second concurrent writer gets `sqlite3.OperationalError: database is locked` immediately with no retry. This surfaces to API callers as an unhandled 500 — it is not caught by the `except (KeyError, ValueError, SystemExit)` net at `api/app.py:1764`.

**Impact:** lost writes / 500s under normal concurrent operation of the project's own components.

**Recommended fix:** in `open_connection`, set `pragma busy_timeout = 5000` (or higher) and `pragma journal_mode = wal`. WAL also lets readers proceed during a write. Add a test that opens two connections and writes concurrently.

---

### [P2] `init_db` runs full schema + token-migration scan + whole-table backfill on every connection

**Confidence:** 8/10
**Location:** `shopping_cli/db/session.py:58-86` (called from `open_connection`, i.e. every `db_session`)

Every API request and CLI invocation:

1. re-runs every `CREATE TABLE/INDEX IF NOT EXISTS` (`SCHEMA`, `INDEXES`),
2. runs `migrate_api_tokens_to_hashes`, which `select`s over all rows of `api_tokens` (`session.py:93`),
3. runs `update conversations set next_actor = ... where next_actor = ''` — a write over the conversations table (`session.py:66-78`).

So even read-only endpoints (e.g. `/search/products`) perform a table scan of `api_tokens` and a write to `conversations` on every call.

**Impact:** O(rows) work per request, and — combined with [P1] — every read path takes a write lock, amplifying lock contention.

**Recommended fix:** gate migration/backfill behind the `meta.schema_version` value so it runs once per version, not per connection. Keep `CREATE ... IF NOT EXISTS` cheap or also gate it.

---

### [P2] `search_merchants` still loads the full candidate set before pagination

**Confidence:** 8/10
**Location:** `shopping_cli/core/catalog.py:704`

```python
sql += " group by m.id order by m.name, m.id"
rows = conn.execute(sql, values).fetchall()   # no LIMIT
```

`search_products` was fixed with `DEFAULT_/MAX_PRODUCT_SEARCH_CANDIDATE_LIMIT` (`catalog.py:601-646`), but `search_merchants` retains the original unbounded `fetchall()` — it scores and sorts every merchant in Python before applying `offset/limit`.

**Impact:** same DoS-on-large-table profile the prior review flagged for products; result limits do not bound memory/CPU.

**Recommended fix:** apply the same candidate cap used by `search_products`, and add a large-table test that proves the work stays bounded.

---

### [P3] `validate_production_config` computes a channel-token check it never enforces

**Confidence:** 7/10
**Location:** `shopping_cli/config.py:114-141`

`production_config_checks()` returns `channel_tokens_configured`, but `validate_production_config` only enforces `admin_token_configured` and `buyer_bootstrap_token_configured` (line 134). A production deploy that wires merchant/buyer tokens but forgets channel tokens passes validation, then `_require_channel_token` rejects all channel ingress at runtime.

**Impact:** fail-closed (not a vulnerability), but a confusing late failure and a dead check.

**Recommended fix:** either enforce `channel_tokens_configured` in production, or drop the unused check so the intent is unambiguous.

---

### [P3] Buyer endpoints have auth but no rate limit / idempotency

**Confidence:** 7/10
**Location:** `shopping_cli/api/app.py:807-824` (`/buyer/ask`), `shopping_cli/api/app.py:851` (`/conversations`)

The prior review asked for auth + rate limit + idempotency; only the bootstrap-token auth landed. A single shared `SHOPPING_BUYER_BOOTSTRAP_TOKEN` still lets any holder create unbounded conversations/messages and trigger downstream merchant-agent / human-review work.

**Impact:** state-spam vector if the bootstrap token is shared or leaked.

**Recommended fix:** add an idempotency key on `/buyer/ask` and a basic per-token rate limit.

---

### [Low] `dispatch_marketplace_tool` convenience default is the most-privileged scope

**Confidence:** 6/10
**Location:** `shopping_cli/llm/dispatcher.py:549-555`

The helper defaults `token_scope="local_trusted"`, which is in `PRIVILEGED_CONVERSATION_SCOPES` and so bypasses the conversation-ownership check (`dispatcher.py:146`). It is currently only exported, not called internally, so this is theoretical — but as a public API helper it is an easy footgun.

**Recommended fix:** require an explicit scope, or default to a non-privileged one.

**Nit:** `_agent_token_row` (`api/app.py:445-455`) still queries `where token = ? or token = ? or token_hash = ?` with the raw token. Harmless after migration (the `token` column holds the digest), but the raw-token branch is dead and slightly misleading.

## Positive observations

- API tokens stored as SHA-256 digests only (raw never persisted), constant-time compare (`api/app.py:513-550`, `db/session.py:89`).
- Agent message claim is genuinely concurrency-safe: `INSERT ... except IntegrityError` + conditional `UPDATE ... where status in (...)` with `rowcount` checks (`core/harness.py:114-195`). Same rigor on complete/fail/abandon.
- LLM dispatch has a tool allowlist, a per-scope allowlist, and conversation-ownership enforcement (`llm/dispatcher.py:83-162`).
- Subprocess calls use argv arrays, not shell strings (plugin + daemon).
- Plugin project-root override is now validated — world-writable rejected, required markers checked (`plugins/shopping-plugin/openclaw_compat.js:33-48`).

## Suggested fix order

1. `busy_timeout` + WAL in `open_connection` (P1 — correctness under the project's own concurrency model).
2. Gate `init_db` migration/backfill behind `schema_version` (P2 — perf + reduces lock window).
3. Cap candidates in `search_merchants` (P2).
4. Enforce-or-remove the channel-token production check (P3).
5. Add idempotency / rate limit to buyer endpoints (P3).

# shopping-cli Optimization Directions

Date: 2026-06-24

This document summarizes optimization directions identified from a code read of
`shopping-cli`. The roadmap below has been implemented in order. Current
verification is healthy: `bash scripts/verify.sh` passes 369 Python tests and 7
Node plugin tests.

## Implementation Status

The ordered optimization pass has now completed the first operational slice:

- API fallback, handler, and route metadata concerns are split, with route groups
  derived from one registry.
- CLI command handlers are split across catalog, buyer, conversation, agent,
  LLM, and adapter modules.
- API and agent tool internals use domain exceptions instead of `SystemExit`.
- Shared services now cover token helpers, agent lifecycle/token operations,
  human-review normalization, and buyer-bootstrap rate-limit parsing.
- Product search has FTS lifecycle stats plus benchmark smoke automation.
- CLI tests now share a helper and agent token lifecycle tests live in a focused
  test module.
- Audit details include stable `schema_version` and `event_type` metadata.
- `scripts/verify.sh` includes resource-warning and search benchmark hygiene
  checks.

## Current Shape

`shopping-cli` is a standalone SQLite-backed consultation runtime for local
commerce. The core architecture is sound for the current MVP:

- SQLite owns trusted marketplace state.
- CLI, FastAPI/fallback ASGI, host adapters, and the OpenClaw plugin sit outside
  the trusted state boundary.
- The resident merchant agent runs through a typed tool boundary.
- The optional LLM loop uses scoped tool allowlists and audit events.
- The product intentionally does not implement orders, payments, reservations,
  escrow, refunds, or delivery success claims.

The main optimization theme is reducing maintenance risk while preserving those
boundaries.

## Priority 1: Split Large Entry Points

`shopping_cli/cli.py` is over 2300 lines and combines parser construction,
command handlers, text rendering, API token helpers, LLM execution, daemon
commands, and adapter diagnostics. `shopping_cli/api/app.py` similarly combines
fallback ASGI, route metadata, auth, idempotency, rate limiting, request
handlers, and FastAPI route registration.

Suggested direction:

- Move CLI command handlers into domain modules such as `cli/catalog.py`,
  `cli/conversations.py`, `cli/agents.py`, `cli/llm.py`, and `cli/adapters.py`.
- Keep `cli.py` as the small entry point that builds the parser and dispatches.
- Split API concerns into `api/auth.py`, `api/idempotency.py`,
  `api/handlers/*`, and `api/fallback_asgi.py`.
- Make the existing `api/routes_*.py` files either real route registration
  modules or keep them only as explicit route documentation. Avoid the current
  halfway state where route groups exist but all behavior remains in `app.py`.

Expected benefit:

- Smaller diffs for routine feature work.
- Less chance of accidentally changing auth or route behavior while editing an
  unrelated CLI command.
- Easier code review and test targeting.

## Priority 2: Add a Shared Use-Case Layer

The CLI currently imports API private helpers such as token issuance and token
validation helpers. That makes `api/app.py` a de facto service layer and couples
the CLI to API internals.

Suggested direction:

- Introduce a `shopping_cli/services/` package for application use cases:
  catalog writes, conversation mutations, token management, agent operations,
  audit event queries, and human-review resolution.
- Let CLI and API both call the same service functions.
- Keep transport-specific details in the transport layer: argparse parsing in
  CLI, HTTP auth/header handling in API, plugin command spawning in the Node
  plugin.

Expected benefit:

- CLI and API stay behaviorally aligned without importing each other's private
  functions.
- Public API route refactors become safer.
- Token and permission rules have one authoritative implementation.

## Priority 3: Improve Search Scalability

Product, merchant, and policy search currently pull a bounded candidate set from
SQLite, then tokenize, score, and sort in Python. This is good for MVP
simplicity, but it will become a bottleneck as catalogs grow.

Suggested direction:

- Add SQLite FTS5-backed search tables or a maintained search index table.
- Update the index on merchant/product/policy create and update operations.
- Keep the current deterministic ranking as a secondary scoring step if needed.
- Add benchmark fixtures for roughly 1k, 10k, and 50k products.
- Define acceptable latency budgets for CLI and API search paths.

Expected benefit:

- Predictable performance as data grows.
- Better query matching without loading thousands of rows into Python.
- Measurable guardrails for future ranking changes.

## Priority 4: Avoid N+1 Conversation Summaries

`conversation_summary()` eagerly loads messages, moderation flags, audit events,
and product details. List paths then call it once per conversation. This is
simple and correct, but list views can become expensive.

Suggested direction:

- Add a lightweight conversation list summary that excludes full messages and
  audit events by default.
- Add explicit detail loading for conversation detail views.
- Provide batch loaders for messages, flags, audits, and products when a list
  really needs richer context.
- Keep current full summaries for backward-compatible JSON outputs until callers
  are migrated or an explicit `include=` option exists.

Expected benefit:

- Faster queue views for merchants and agents.
- Less repeated SQL work.
- Clearer API contracts between list and detail endpoints.

## Priority 5: Replace Core-Layer `SystemExit`

Many core functions raise `SystemExit` for validation and not-found errors.
That is convenient for argparse, but awkward for library use, API handlers, LLM
dispatchers, and tests.

Suggested direction:

- Define domain exceptions such as `ValidationError`, `NotFoundError`,
  `ConflictError`, and `PermissionDenied`.
- Raise those from core and service layers.
- Convert domain exceptions to `SystemExit` only at CLI boundaries.
- Convert domain exceptions to HTTP status codes only at API boundaries.

Expected benefit:

- Cleaner separation between library behavior and CLI process control.
- More precise API error mapping.
- Easier testing without catching process-exit exceptions.

## Priority 6: Make Database Migrations Explicit

Database initialization currently uses schema creation plus extra-column
backfills, and the package `VERSION` influences whether some migrations run.
Package releases and schema migrations are related, but they should not be the
same source of truth.

Suggested direction:

- Track schema version with explicit migration numbers, for example through
  SQLite `PRAGMA user_version` or a `schema_migrations` table.
- Keep each migration idempotent and narrow.
- Test migrations from representative older database fixtures.
- Reserve package version for distribution and compatibility metadata.

Expected benefit:

- Safer upgrades across releases.
- Easier rollback and forward-fix reasoning.
- Less risk from version bumps that do not actually change schema.

## Priority 7: Consolidate LLM Tool Contracts

The LLM dispatch layer already has a useful scope allowlist and audit behavior.
The local dispatcher and HTTP dispatcher duplicate parts of the same tool
contract and response handling.

Suggested direction:

- Define tool metadata, scope rules, argument normalization, and response shape
  in one declarative module.
- Have local and HTTP dispatchers consume that module.
- Add contract tests that run the same tool cases against both dispatchers.
- Preserve the existing principle that scoped tools cannot bypass marketplace
  authorization.

Expected benefit:

- Lower risk of local/HTTP behavior drift.
- Easier addition of new tools.
- Stronger trust boundary for LLM-driven operations.

## Priority 8: Resource and Runtime Hygiene

The verification suite currently passes, but it emits a SQLite unclosed
connection warning. Daemon lifecycle code is also inherently platform-sensitive
because it manages PID files, process signals, logs, and state files.

Suggested direction:

- Fix the test connection warning and consider making resource warnings fail in
  CI after the fix.
- Add focused daemon lifecycle tests for stale PID replacement, stop timeout,
  log parsing, and API-token environment passing.
- Keep token redaction tests around daemon logs and status output.
- Add a lightweight smoke test for `shopping-cli-api` when optional API
  dependencies are installed.

Expected benefit:

- Cleaner CI signal.
- Fewer hidden process-management regressions.
- Safer operational behavior for resident merchant agents.

## Defer For Now

Do not add order, payment, stock reservation, escrow, refund, or delivery-success
tables unless the product scope changes. The current architecture and README are
explicitly centered on consultation state, not transaction state. Adding
transaction concepts prematurely would increase correctness obligations without
solving the current maintenance bottlenecks.

## Suggested Execution Order

1. Fix the SQLite `ResourceWarning`.
2. Extract API auth, idempotency, and token service helpers behind stable service
   functions.
3. Split the CLI command handlers while keeping command behavior byte-for-byte
   compatible where possible.
4. Add lightweight conversation list summaries and preserve full detail output.
5. Add search benchmarks, then introduce FTS/index-backed search.
6. Convert core `SystemExit` usage to domain exceptions.
7. Consolidate LLM tool contracts and run local/HTTP parity tests.
8. Move database schema evolution to explicit migrations.

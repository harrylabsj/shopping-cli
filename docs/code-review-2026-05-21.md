# shopping-cli Code Review — 2026-05-21

Scope: whole-project review of `shopping-cli`, not a branch diff review.

No code changes were made during the review.

## Verification

- `python3 -m unittest discover -s tests` — 328 tests passed
- `node --test tests/shopping_plugin.test.mjs` — 5 tests passed
- Codex adversarial review did not run because the local `codex` shell wrapper was missing `_codex_cli_bin`

## Summary

The project has a solid local-first shape: SQL is mostly parameterized, API tokens are hashed, subprocess calls use argv arrays instead of shell strings, and the local LLM dispatcher has a tool allowlist plus conversation ownership checks.

The highest-priority issues are:

1. Per-item human-review resolution can double-process concurrent requests.
2. The OpenClaw plugin exposes mutating tools without an authorization or confirmation gate.
3. Public buyer endpoints can create durable state and buyer tokens without authentication.
4. Public catalog responses expose internal merchant automation boundaries.
5. Product search loads the full candidate catalog before pagination.
6. README and package metadata disagree on whether API dependencies are optional.

## Findings

### [P1] Human review single-item resolve can double-process concurrent requests

**Confidence:** 8/10  
**Location:** `shopping_cli/api/app.py:1425`

`_resolve_human_review_item` reads an unresolved moderation flag, then updates it with:

```sql
update moderation_flags
set resolved_at = ?, resolution = ?, resolved_by = ?
where id = ? and resolved_at = ''
```

The code does not check `rowcount` after the conditional update. If two requests resolve the same review concurrently, the second update affects zero rows but the code still continues to write messages, audit events, and route the conversation.

**Impact:** duplicate replies, duplicate audit events, and potentially incorrect conversation routing.

**Recommended fix:** check `resolved.rowcount == 1` immediately after the update. If not, return an already-resolved error. Add a double-resolution test for the same review id.

---

### [P1] Plugin mutating tools lack authorization or confirmation gates

**Confidence:** 8/10  
**Location:** `plugins/shopping-plugin/openclaw_compat.js:134-318`

The plugin registers mutating tools that directly spawn the local CLI:

- `shopping_create_merchant` at `openclaw_compat.js:134-164`
- `shopping_add_product` at `openclaw_compat.js:166-202`
- `shopping_buyer_ask` at `openclaw_compat.js:246-265`
- `shopping_record_intent` at `openclaw_compat.js:284-301`
- `shopping_run_merchant_agent` at `openclaw_compat.js:303-318`

These tools can mutate the local database without token checks, role checks, allowlists, or human confirmation in the plugin layer.

**Impact:** if a host-side LLM/tool invocation is manipulated, it can create merchants, add products, write buyer intent, or trigger merchant-agent processing.

**Recommended fix:** default to registering read-only tools only. Require explicit trusted configuration, host confirmation, or configured admin/merchant tokens before enabling write tools.

---

### [P2] Public buyer endpoints can create durable state without authentication

**Confidence:** 8/10  
**Location:** `shopping_cli/api/app.py:739-755`, `shopping_cli/api/app.py:782-806`, `shopping_cli/api/app.py:1803-1809`

`/buyer/ask` and `/conversations` accept caller-provided buyer and conversation data without authentication, persist conversation/message state, and return buyer tokens.

**Impact:** in public deployments, a remote caller can spam the SQLite database, impersonate buyer identifiers for new conversations, and trigger downstream merchant-agent or human-review work.

**Recommended fix:** add a channel token, buyer/session bootstrap token, rate limit, and idempotency key for state-changing buyer endpoints. Prefer server-generated buyer/session identifiers over trusting caller-provided `buyer_id`.

---

### [P2] Public merchant/product responses expose internal automation fields

**Confidence:** 9/10  
**Location:** `shopping_cli/core/catalog.py:399-416`, `shopping_cli/core/catalog.py:430-442`, `shopping_cli/core/catalog.py:490-521`

Public merchant and product serializers include `contact` and `automation_boundaries`. These are exposed by public routes such as merchant listing, merchant lookup, merchant search, and product search.

**Impact:** `automation_boundaries` can reveal internal agent or merchant operating rules, which may help prompt-injection or social-engineering attempts. `contact` may also be more sensitive than catalog metadata depending on the deployment.

**Recommended fix:** split public and private serializers. Public search and public detail responses should omit `automation_boundaries`. Expose `contact` only if the product decision is explicit. Keep complete merchant summaries behind merchant-authenticated endpoints.

---

### [P2] Product search loads all matching candidates before pagination

**Confidence:** 8/10  
**Location:** `shopping_cli/core/catalog.py:632-653`

`search_products` runs a SQL query and calls `fetchall()` before scoring and sorting all rows in Python. Offset and limit are applied only after the full candidate set is loaded and sorted.

**Impact:** API result limits do not cap memory or CPU work. Large catalogs can make public search degrade linearly with table size.

**Recommended fix:** push more filtering and ranking into SQLite, add a bounded candidate cap, or use SQLite FTS/indexed search. Add a large-catalog test that proves query work stays bounded.

---

### [P2] API dependencies are documented as optional but installed by default

**Confidence:** 9/10  
**Location:** `pyproject.toml:10-14`, `pyproject.toml:21-26`, `README.md:15-18`

The README says FastAPI API dependencies are optional and installed via `pip install -e '.[api]'`, but `fastapi`, `pydantic`, and `uvicorn` are also listed as required project dependencies.

**Impact:** CLI-only installs pull API server dependencies, and installation docs misrepresent the package boundary.

**Recommended fix:** choose one model:

1. Make API dependencies truly optional by removing them from `[project].dependencies` and keeping them under `[project.optional-dependencies].api`.
2. Or keep them installed by default and update README to remove the optional-dependency claim.

---

### [P3] Plugin can execute from a configurable project root

**Confidence:** 6/10  
**Location:** `plugins/shopping-plugin/openclaw_compat.js:26-35`, `plugins/shopping-plugin/openclaw_compat.js:51-62`

`resolveProjectRoot` accepts plugin config or `SHOPPING_ROOT`, and `buildShoppingCommand` executes `${root}/scripts/shopping.py`.

**Impact:** if an attacker can influence plugin config or environment, invoking a shopping tool can execute an arbitrary local Python script under the host user. This depends on host configuration trust, so it is lower confidence/lower priority than the mutating-tool authorization issue.

**Recommended fix:** default to the installed package root. Require an explicit trusted override for custom roots, and reject roots that are missing expected package markers or are world-writable.

## Positive observations

- Most SQL uses parameter binding.
- Token storage uses digests, and comparison uses constant-time matching.
- Local LLM tool dispatch has a scope allowlist and conversation ownership checks.
- Subprocess calls use argument arrays rather than shell command strings.

## Suggested fix order

1. Add `rowcount` handling and tests for human-review double resolution.
2. Gate plugin mutating tools behind explicit confirmation or trusted configuration.
3. Add authentication/rate limiting/idempotency to public state-changing buyer endpoints.
4. Split public/private catalog serializers.
5. Bound product search candidate loading.
6. Align README and package dependencies.

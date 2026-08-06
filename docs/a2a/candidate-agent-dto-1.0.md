---
title: CandidateAgent DTO Contract
version: 1.0
date: 2026-08-06
status: Normative
scope: shopping-cli → Kiwi (Commerce Agent Catalog public read DTO)
owner: shopping-cli (repo-level; see design §21)
---

# CandidateAgent DTO — Contract 1.0

## 0. Status and Ownership

This document is the **normative contract** for the public `CandidateAgent`
DTO that every public read route of the Commerce Agent Catalog returns.  The
DTO is **owned by the shopping-cli repo** (design §21, 仓库归属):

```text
shopping-cli repo:
  Agent Catalog API
  Agent Catalog SDK/client contract
  public CandidateAgent DTO

Kiwi repo:
  ShoppingCliCatalogSource
  AgentDiscovery orchestration
  fresh verification policy
  CounterpartyProfile
  Channel Selection
```

Kiwi's `ShoppingCliCatalogSource` consumes this DTO as-is and MUST NOT redefine
the shape.  The canonical machine-readable definition lives in
`shopping_cli/agent_catalog/candidate_dto.py` (`CANDIDATE_AGENT_SCHEMA`); this
document is the human-readable normative explanation.

Source of truth for the design intent: §8.2 (Search Result Contract), §21
(repository ownership), §22 (Hosted/Direct Status Model) of
`docs/shopping-cli-a2a-upgrade-design-v1.2.1.md`.

## 1. Candidate Semantics (§8.2)

**A `CandidateAgent` is NOT a verified live identity.**

The catalog is an index of *discoverable* commerce agents (§3.1).  A candidate
means:

- the catalog knows this agent exists and holds the metadata below;
- the agent's current online identity, freshness, and live capabilities have
  **not** been re-proven at the moment the DTO was produced.

§8.2 is explicit that the catalog returns candidates, and that Kiwi — before
beginning any real negotiation — SHOULD run its own fresh verification /
cache validation against its `TrustPolicy`, and is responsible for upgrading a
candidate into a usable `CounterpartyProfile`.

```text
Catalog returns candidate
Kiwi  upgrades candidate → CounterpartyProfile (fresh verification / policy)
```

The two responsibilities MUST NOT be merged (§21).

## 2. Public / Private Boundary (§3.4)

The DTO only contains fields from the §3.4 **MAY expose** list:

```text
merchant name, domain, public categories, public products, public tags,
Agent Card URL, UCP Profile URL, protocol versions, public skills,
public capabilities, verification status, last verified timestamp,
hosted/direct mode
```

The DTO MUST NOT contain any field from the §3.4 **MUST NOT expose** list —
including but not limited to:

```text
automation_boundaries, floor price, cost, private discount policy,
agent token, merchant token, private contact, LLM prompt,
internal strategy, private reputation evidence
```

Implementation note: the public serializers
(`shopping_cli/agent_catalog/serializers.py`) strip private fields before any
public response is built, and the schema declares
`additionalProperties: false` so an object containing a private field is a
schema violation.  Consumers SHOULD treat any DTO that contains a private
field as a corruption/failure rather than a contract change.

## 3. Versioning Promise

- Current version: **1.0**.
- Each candidate carries `contract: {"name": "candidate-agent", "version": "1.0"}`.
- **Within `1.x`, the contract is additive-only.**  Existing fields keep their
  type and semantics; new fields may only be added (and MUST be optional or
  have a documented default).  A `1.x` consumer MUST tolerate unknown
  properties.
- **A breaking change — removing/renaming a field, changing a field's type,
  narrowing a required set, or changing an enum by removing values — bumps the
  major version** (e.g. `2.0`), ships a new schema `$id`, and the `contract`
  annotation changes accordingly.  `1.x` responses remain valid until the
  major is retired.

### 3.1 Schema identity

The machine schema is a JSON Schema (draft-07 subset) exposed as the Python
dict `CANDIDATE_AGENT_SCHEMA` in `shopping_cli/agent_catalog/candidate_dto.py`:

```text
$id:  urn:shopping-cli:candidate-agent:1.0
```

Consumers may vendor this dict or translate it to their own validation tooling;
the normative field semantics are in §4.

## 4. Field Semantics

### 4.1 Top level

| Field | Type | Required | Semantics |
|---|---|---|---|
| `catalog_agent_id` | string | yes | Stable catalog identifier for the candidate (`cagt_…`). |
| `merchant` | object | no* | Public merchant reference (see 4.2). *Always present through the read API. |
| `discovery` | object | no | Discovery URLs (see 4.3). Present when any public endpoint exists. |
| `protocols` | object | no | Map of protocol name → advertised versions (see 4.4). |
| `capabilities` | string[] | no | Fully-qualified capability identifiers (see 4.5). |
| `skills` | object[] | no | Public skills (see 4.6). |
| `verification` | object | yes | Verification status snapshot (see 4.7). |
| `hosting` | object | yes | Hosted/direct mode (see 4.8). |
| `contract` | object | yes | `{name, version}` annotation (see §3). |

### 4.2 `merchant`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `id` | string | yes | Merchant id. Empty string when the candidate is not yet bound to a merchant. |
| `name` | string | yes | Public merchant name. |
| `city` | string | no | Public city. |
| `service_area` | string | no | Public service area. |
| `domain` | string | no | Public canonical domain. |
| `tags` | string[] | no | Public tags. |

### 4.3 `discovery`

| Field | Type | Semantics |
|---|---|---|
| `agent_card_url` | string | URL of the A2A Agent Card (`kind=agent_card` endpoint). |
| `ucp_profile_url` | string | URL of the UCP profile (`kind=ucp_profile` endpoint). |
| `a2a_urls` | string[] | Direct A2A transport URLs (`kind=a2a` endpoints). |

At least one sub-field is present whenever the block is present.  Internal
`hosted_gateway` endpoints without a public URL never appear.

### 4.4 `protocols`

A map whose keys are protocol names (e.g. `a2a`, `ucp`) and whose values are
arrays of advertised protocol versions (e.g. `["1.0.0"]`, `["2026-04-08"]`).
Version strings are taken verbatim from the stored endpoint metadata.

### 4.5 `capabilities`

An array of **fully-qualified** capability identifiers.  When the capability
has a non-empty namespace, the element is `namespace:capability_id` (e.g.
`com.shopping.agent.capability:catalog`).  When the namespace is empty, the
element is the bare `capability_id` (e.g. `consultation`).

Short names are internal query aliases only and MUST NOT appear in the
canonical public contract (§8.2).  Consumers matching a capability MUST match
on the fully-qualified form.

### 4.6 `skills`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `skill_id` | string | yes | Public skill identifier. |
| `name` | string | yes | Public skill name. |
| `description` | string | no | Public description. |
| `tags` | string[] | no | Public tags. |

### 4.7 `verification`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `status` | string | yes | Verification status: `discovered`, `profile_valid`, `domain_verified`, `agent_verified`, `commerce_verified`, `stale`, `unreachable`, `suspended`, `rejected` (§6 state machine). |
| `last_verified_at` | string | no | ISO-8601 timestamp of the last successful verification. Absent when never verified. |

### 4.8 `hosting`

| Field | Type | Required | Semantics |
|---|---|---|---|
| `mode` | string | yes | Hosting mode. |

`hosting.mode` aligns with the §22 Hosted/Direct Status Model.  The canonical
enum is:

```text
direct_only    shopping-cli only does discovery; communication goes direct.
hosted_only    communication goes through shopping-cli.
hybrid         prefer direct; policy MAY fall back to hosted.
unknown        the catalog has not classified the mode.
```

**Backward-compatibility note.**  The DB stores the legacy values
`direct`/`hosted`/`hybrid`/`unknown` (no migration in v1.x).  The schema enum
therefore also accepts the legacy aliases `direct` and `hosted`.  New producers
SHOULD emit the canonical form.  The contract module provides an explicit
normalizer for consumers that require canonical values:

```text
to_contract_hosting_mode(stored) → "direct_only" | "hosted_only" | "hybrid" | "unknown"
```

Unrecognised stored values fail closed to `unknown`.  A future `2.0` contract
may drop the legacy aliases and emit canonical values only.

## 5. Where This DTO Is Returned

Every candidate object returned by the four public read routes is a
`CandidateAgent` carrying the `contract` annotation:

| Method | Route | Carrier |
|---|---|---|
| GET | `/v1/agent-catalog/agents/search` | `body["results"][i]` |
| GET | `/v1/agent-catalog/agents` | `body["results"][i]` |
| GET | `/v1/agent-catalog/agents/{catalog_agent_id}` | `body["catalog_agent"]` |
| GET | `/v1/agent-catalog/merchants/{merchant_id}/agents` | `body["results"][i]` |

Write responses (register §10.2 / claim §10.4) embed the same DTO under
`body["catalog_agent"]` extended with the canonical identity fields
(`canonical_domain`, `source_type`) the caller just acted on.  Those extension
fields are NOT part of the `CandidateAgent` DTO and are write-only additions.

### 5.1 Kiwi integration guidance

`ShoppingCliCatalogSource` SHOULD:

1. Validate every received candidate against `CANDIDATE_AGENT_SCHEMA` (or an
   equivalent translation) before use; reject on schema violation.
2. Read the `contract` annotation and refuse to negotiate against a major
   version it does not understand.
3. Treat every candidate as a discovery result, never as a live identity —
   run fresh verification / cache validation (§8.2) before starting any
   negotiation.
4. Match capabilities on the fully-qualified identifier (§4.5).
5. Normalize `hosting.mode` via `to_contract_hosting_mode` when it needs the
   §22 canonical value.

## 6. Conformance

- `tests/test_agent_catalog_candidate_dto.py` validates serializer output and
  all four read routes (fallback ASGI + FastAPI) against the schema, asserts
  the `contract` annotation, enforces fully-qualified capability form, and
  verifies private fields are absent.
- A change that fails any conformance test, or that requires editing the
  `required`/`additionalProperties` tightening, is a contract change and MUST
  follow §3's versioning promise.

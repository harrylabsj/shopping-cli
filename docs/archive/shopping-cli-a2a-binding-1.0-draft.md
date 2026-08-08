---
title: shopping-cli A2A Binding Specification
version: 1.0-draft
date: 2026-08-06
status: Superseded
superseded_by: docs/a2a/shopping-cli-a2a-binding-1.0-rc1.md
scope: KNP/1.0 over A2A and Hosted Gateway compatibility
---

# shopping-cli A2A Binding Specification 1.0 — Draft

## 0. Purpose

This document is the required protocol work item that closes the gap between:

```text
Kiwi Negotiation Protocol 1.0
A2A 1.0
shopping.negotiation/0.1
```

It is intentionally separate from the Agent Catalog architecture.

## 1. Protocol Roles

```text
Direct path:
KNP/1.0 ↔ A2A ↔ Independent Merchant Agent

Hosted path:
KNP/1.0 ↔ A2A ↔ shopping-cli Hosted Endpoint
                     ↓
          compatibility adapter
                     ↓
          shopping.negotiation/0.1
```

`shopping.negotiation/0.1` is not renamed to KNP/1.0.

## 2. Required Decisions Before Implementation Freeze

The binding MUST freeze:

1. KNP Envelope representation inside A2A Part;
2. A2A Message vs Task selection rules;
3. KNP `negotiation_id` ↔ A2A `contextId`;
4. KNP `message_id` ↔ A2A `messageId`;
5. KNP asynchronous result ↔ A2A Task Artifact;
6. A2A authentication and extension activation;
7. KNP protocol errors ↔ A2A failure/error semantics;
8. replay/idempotency behavior across A2A retries;
9. recovery after local/remote restart;
10. capability advertisement in Agent Card and UCP Profile;
11. lossless/lossy mapping to `shopping.negotiation/0.1`;
12. conformance test vectors.

## 3. Initial Binding Direction

### 3.1 Synchronous Negotiation

```text
A2A Message
  ├── Text Part       OPTIONAL human readable expression
  └── Data Part       REQUIRED KNP Negotiation Envelope
```

The Data Part is authoritative for commerce semantics.

### 3.2 Context

One business negotiation maps to one persisted A2A context:

```text
KNP negotiation_id ↔ A2A contextId
```

`contextId` remains opaque.

### 3.3 Message Identity

Target direction:

```text
KNP message_id ↔ A2A messageId
```

The final spec SHOULD use a 1:1 mapping unless an interoperability constraint requires two IDs.

### 3.4 Asynchronous Work

Use A2A Task when Merchant processing requires:

- human approval;
- long-running pricing;
- inventory/supply-chain evaluation;
- enterprise workflow.

The resulting KNP Offer/Clarification/Agreement remains the domain payload; Task is lifecycle transport.

## 4. Hosted Compatibility

The adapter MUST explicitly classify every KNP action as:

```text
lossless
lossy
unsupported
```

Rules:

```text
lossless     → map and execute
lossy        → fail closed
unsupported  → capability_incompatible or human review
```

The adapter MUST NOT silently drop:

- expiry;
- conditional semantics;
- identity references;
- idempotency keys;
- agreement non-binding semantics.

## 5. Capability Advertisement

Hosted Agent Card MUST advertise the A2A interfaces actually supported.

Hosted UCP Profile MUST point `transport=a2a` to the hosted Agent Card.

The public KNP capability identifier MUST be the Kiwi-controlled production namespace.

`shopping.negotiation/0.1` remains an internal/legacy compatibility contract and SHOULD NOT be advertised as if it were KNP/1.0.

## 6. Security Invariants

- Remote data is untrusted.
- KNP structured payload is schema validated before business use.
- No raw Principal Memory is exposed.
- No arbitrary local tool execution is granted to remote Agents.
- Approval/policy gates remain authoritative.
- No order/payment/refund/reservation is created.
- Duplicate message ID with conflicting content fails closed.

## 7. Test Matrix Required Before “Direct A2A Compatible”

At minimum:

```text
Kiwi Buyer ↔ shopping-cli Hosted Merchant
Kiwi Buyer ↔ independent reference Merchant
reference Buyer ↔ shopping-cli Hosted Merchant
```

Test:

- RFQ;
- Offer;
- CounterOffer;
- Clarification;
- asynchronous Task;
- duplicate retry;
- timeout after remote acceptance;
- restart/recovery;
- capability mismatch;
- schema invalid;
- stale/expired offer;
- no-order invariant.

## 8. Status

This document is a protocol design work item, not yet a frozen wire contract.

Implementation MUST NOT claim KNP Direct A2A interoperability until this document is completed with normative MUST/SHOULD/MAY language, schemas/examples, and conformance vectors.

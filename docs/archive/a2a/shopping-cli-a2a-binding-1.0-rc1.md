---
title: shopping-cli A2A Binding Specification
version: 1.0-rc1
date: 2026-08-06
status: Implementation Candidate / Pre-Freeze
scope: KNP/1.0 over A2A and Hosted Gateway compatibility
---

# shopping-cli A2A Binding Specification 1.0 — RC1

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

Pinned external profiles:

```text
A2A Agent Card / Protocol = v1.0.0
UCP Profile                = 2026-04-08
```

A2A Agent Card is the canonical Agent interface/skill/security description. UCP Profile is the canonical commerce service/capability discovery document. Kiwi MUST NOT define a competing Agent Card schema.

## 2. Binding Decision Table

以下决策是 RC1 的 **proposed normative direction**。Owner 使用角色而不是个人姓名；Freeze Milestone 是发布门禁，不凭空假设日历日期。

| # | Decision | Proposed Direction | Rationale | Owner | Freeze Milestone | Status |
|---|---|---|---|---|---|---|
| D1 | KNP Envelope in A2A | KNP Envelope MUST be carried in an A2A structured Data Part; human-readable Text Part is OPTIONAL | 结构化事实与自然语言分离；KNP payload 可独立 schema validate | Kiwi Protocol Owner | before KNP/A2A schema freeze | PROPOSED |
| D2 | Message vs Task | Bounded synchronous negotiation turn → Message; human/enterprise/long-running work → Task | Task 只承载异步生命周期，不重新定义商业对象 | Kiwi Protocol Owner | before first Direct A2A E2E | PROPOSED |
| D3 | `negotiation_id` ↔ `contextId` | One KNP negotiation MUST persist one A2A context mapping; `contextId` remains opaque and is not derived from `negotiation_id` | 避免假设远端 ID 结构，同时支持跨重启恢复 | Kiwi Protocol Owner + shopping-cli A2A Owner | before persistence schema freeze | PROPOSED |
| D4 | KNP `message_id` ↔ A2A `messageId` | MUST preserve a 1:1 logical mapping; exact string equality SHOULD be used when the producer controls both IDs | 减少重复 ID 域，同时避免依赖远端生成规则 | Kiwi Protocol Owner | before SDK contract freeze | PROPOSED |
| D5 | Async result | Task result MUST carry the same KNP domain object in a structured Artifact/Data Part | Offer/Agreement 语义不因 Task transport 改变 | Kiwi Protocol Owner | before Task interop test | PROPOSED |
| D6 | Authentication / extension activation | Use A2A Agent Card security declarations and A2A extension activation; no Kiwi-specific transport auth protocol | 避免重复发明认证层 | shopping-cli A2A Owner | before hosted endpoint release | PROPOSED |
| D7 | Error mapping | Transport/auth failures use A2A binding error semantics; valid KNP domain/protocol rejection remains structured KNP error; Task workflow failures use A2A task terminal state plus structured detail when safe | 区分 transport failure、protocol rejection、business decline | Kiwi Protocol Owner | before error schema freeze | PROPOSED |
| D8 | Replay / idempotency | KNP idempotency is authoritative: `(sender_identity, KNP message_id, digest)`; A2A retries MUST preserve the same logical message mapping; same ID/same digest returns prior result, same ID/different digest fails closed | 与现有 KNP Ledger/idempotency 保持一致 | Kiwi Protocol Owner + shopping-cli Gateway Owner | before any write-capable Direct A2A test | PROPOSED |
| D9 | Recovery | Persist `negotiation_id↔contextId`, `message_id↔messageId`, `taskId`, digest and ack/result; restart MUST reconcile local Ledger with remote A2A task/context state when available | 防止 network ambiguity 造成重复商业承诺 | shopping-cli A2A Owner | before restart/recovery test gate | PROPOSED |
| D10 | Capability advertisement | Agent Card MUST use official A2A v1.0.0 Agent Card semantics; UCP Profile MUST use 2026-04-08; KNP public capability MUST be a Kiwi-controlled fully-qualified production namespace; UCP carries commerce capability, Agent Card carries A2A interfaces/skills/security | 分层清晰，不发明 Kiwi Agent Card | Kiwi Protocol Owner | before public namespace freeze | PROPOSED |
| D11 | Hosted legacy mapping | Maintain an explicit KNP/1.0 ↔ `shopping.negotiation/0.1` mapping matrix; only lossless mappings execute automatically | 现有 Gateway 是事实基线，但不能假装等价于 KNP/1.0 | shopping-cli Gateway Owner | before Hosted A2A release | PROPOSED |
| D12 | Conformance | Direct A2A compatibility MUST require cross-implementation Message/Task/replay/restart/capability/error/no-order tests | 防止“能连上”被误报为协议兼容 | Kiwi Protocol Owner + shopping-cli QA Owner | before removing RC status | PROPOSED |

优先冻结顺序：

```text
D3 context mapping
→ D8 replay/idempotency
→ D10 capability & schema pin
→ D4 message identity
→ D1/D2/D5 payload lifecycle
→ D7 errors
→ D9 recovery
→ D11 hosted mapping
→ D12 conformance
```

RC1 进入 implementation 后，任何改变 D3/D4/D8/D10 的修改都视为 schema/SDK-impacting change，必须重新跑 conformance vectors。

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

The binding MUST preserve a 1:1 logical mapping. Exact string equality SHOULD be used when the producer controls both identifiers; otherwise the mapping MUST be persisted explicitly.

### 3.4 Asynchronous Work

Use A2A Task when Merchant processing requires:

- human approval;
- long-running pricing;
- inventory/supply-chain evaluation;
- enterprise workflow.

The resulting KNP Offer/Clarification/Agreement remains the domain payload; Task is lifecycle transport.


## 3.5 Error Separation

The binding MUST distinguish:

```text
A2A transport/auth error
KNP protocol/domain error
commercial Decline
A2A Task terminal failure
```

A transport failure MUST NOT be converted into a commercial Decline.

A KNP schema/idempotency/state error MUST remain a structured protocol error and MUST NOT advance negotiation state as if a valid commercial action occurred.

## 3.6 Replay and Idempotency

KNP idempotency is authoritative.

Logical key:

```text
sender_identity
+
KNP message_id
+
digest
```

A2A retransmission MUST preserve the same logical KNP message.

```text
same message_id + same digest
→ return previous result / equivalent acknowledgment
→ no duplicate business effect

same message_id + different digest
→ idempotency_conflict
→ fail closed
```

## 3.7 Recovery

A conforming implementation MUST persist:

```text
negotiation_id ↔ contextId
KNP message_id ↔ A2A messageId
taskId
digest
ack/result
```

After restart it MUST reconcile local Ledger state with remote A2A Task/context state when the binding exposes such state.

Ambiguous state that cannot be safely reconciled MUST enter:

```text
reconciliation_required
```

and MUST NOT generate a new commercial commitment automatically.

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

Hosted Agent Card MUST conform to the **official A2A v1.0.0 Agent Card semantics** and MUST advertise only A2A interfaces actually supported.

Hosted UCP Profile MUST conform to the **UCP 2026-04-08 specification family**.

For an A2A service in UCP:

```text
transport = a2a
endpoint  = Agent Card URL
```

The public KNP capability identifier MUST be a Kiwi-controlled fully-qualified production namespace following UCP namespace-authority rules.

Examples using:

```text
EXAMPLE_ONLY.reverse.domain.shopping.negotiation
```

are documentation placeholders and MUST NOT ship.

A2A Agent Card carries:

```text
identity
supportedInterfaces
skills
security
A2A capabilities/extensions
```

UCP Profile carries:

```text
commerce services
commerce capabilities
transport discovery
spec/schema references
```

KNP defines:

```text
pre-transaction negotiation semantics
```

`shopping.negotiation/0.1` remains an internal/legacy Hosted Gateway contract and MUST NOT be advertised as if it were KNP/1.0.

## 6. Security Invariants

- Remote data is untrusted.
- KNP structured payload is schema validated before business use.
- No `principal-private state` is exposed. `principal-private state` means private memory, policy, credential, preference, secret threshold, internal strategy, or reasoning state not explicitly authorized for network disclosure.
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

RC1 is an **implementation candidate**, not yet a final frozen wire contract.

The decisions in §2 are the proposed directions teams SHOULD implement unless an interoperability test demonstrates a concrete incompatibility.

The document may drop `RC1` and become `1.0` only after:

1. public KNP production namespace is frozen;
2. normative Data Part examples are schema-valid;
3. Message/Task mapping is implemented;
4. replay/idempotency vectors pass;
5. restart/reconciliation vectors pass;
6. independent Direct A2A interop passes;
7. Hosted KNP ↔ `shopping.negotiation/0.1` mapping matrix is complete;
8. no-order/no-payment/no-reservation invariants pass.

Implementation MUST NOT claim KNP Direct A2A interoperability before these gates pass.

---

## Appendix A — RC1 Review Closure

This RC1 closes the blocking findings from `design-review-a2a-v1.1-2026-08-06.md`:

- 12 binding decisions now have proposed direction, owner role, freeze milestone and status;
- Agent Card pinned to official A2A v1.0.0 semantics;
- UCP Profile pinned to 2026-04-08;
- capability namespace responsibility made explicit;
- replay/idempotency and recovery promoted from TODOs into implementation direction;
- `principal-private state` replaces an undefined Kiwi-internal memory term.

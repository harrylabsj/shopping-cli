---
title: shopping-cli A2A 升级总体设计
version: v1.2.1
date: 2026-08-06
status: Proposed Architecture
target: shopping-cli 2.x -> Commerce Agent Catalog + Hosted Commerce Gateway
related_external_constraints:
  - Kiwi repo: Kiwi A2A Agent Commerce Network Architecture Baseline
  - Kiwi repo: Kiwi Negotiation Protocol 1.0
current_hosted_contract:
  - shopping.negotiation/0.1
pinned_external_specs:
  - A2A Agent Card / Protocol: v1.0.0
  - UCP Profile: 2026-04-08
---

# shopping-cli A2A 升级总体设计 v1.2.1

## 0. 文档定位

本文定义 shopping-cli 从当前的：

> **Authoritative Negotiation Gateway + Local Commerce Consultation Runtime**

升级为：

> **Commerce Agent Catalog + Discovery & Verification Infrastructure + Hosted Commerce Gateway**

的总体方向。

本次升级不是推倒重写，而是以当前 HEAD 已经存在的能力为事实基线继续演进。

### 0.1 当前已实现底座

现有 shopping-cli 已具备并必须保留：

- SQLite trusted marketplace state；
- merchants / products / delivery / conversations / messages；
- Resident Merchant Agent；
- `MerchantAgentTools` typed boundary；
- local / HTTP 两套工具实现；
- claim / heartbeat / complete / fail / abandon；
- idempotency；
- append-only audit events；
- merchant / buyer / agent token 生命周期；
- human review；
- public/private merchant serialization；
- LLM tool allowlist 与 scope enforcement；
- FTS / bounded search；
- FastAPI + fallback ASGI；
- OpenClaw plugin；
- CI、Ruff、Mypy、coverage、release verification；
- consultation-only / no-order / no-payment / no-reservation 边界；
- **Authoritative Negotiation Gateway**；
- 冻结并随包发布的 `shopping.negotiation/0.1` contract；
- `/capabilities` 与 `/negotiation/*` 权威接口；
- negotiation claim / heartbeat / snapshot / decision / abandon-stale；
- token 推导身份、角色裁剪 snapshot、policy gate 与 authoritative settlement；
- `agents.capabilities_json` 与 heartbeat/stale 能力。

因此本设计不再把“新增 Negotiation Gateway”列为未来工作。

### 0.2 这次真正新增的能力

```text
Commerce Agent Catalog
+
External Agent Discovery
+
UCP Profile Resolver
+
A2A Agent Card Resolver
+
Identity / Trust Verification
+
Kiwi ShoppingCliCatalogSource
+
Hosted A2A Publication
+
Direct A2A Wire Binding
```

新的总体定位：

```text
shopping-cli
=
Commerce Agent Catalog
+
Discovery & Verification Infrastructure
+
Authoritative Hosted Negotiation Gateway
+
Legacy Compatibility Infrastructure
```

其中：

```text
shopping.negotiation/0.1
```

是**当前已实现的 Hosted / Legacy wire contract**。

而：

```text
Kiwi Negotiation Protocol 1.0 (KNP/1.0)
```

是 Kiwi A2A 体系面向开放 Agent Network 的目标 negotiation protocol。

两者不是同一个协议标识，必须通过显式、可测试、fail-closed 的 binding / mapping 工件衔接。

Catalog 是可选 discovery infrastructure，而不是 Kiwi A2A 网络的强制中心。

### 0.3 文档与仓库边界

本文件属于 shopping-cli 仓库的架构设计。

以下文档是**外部约束**，预计由 Kiwi 仓库维护：

- `Kiwi A2A Agent Commerce Network Architecture Baseline`
- `Kiwi Negotiation Protocol 1.0`

shopping-cli 仓库需要维护自己的独立工件：

```text
docs/a2a/
  shopping-cli-a2a-binding-1.0-rc1.md
```

本设计正式 pin：

```text
A2A Agent Card / Protocol = v1.0.0
UCP Profile                = 2026-04-08
```

职责分层固定为：

```text
A2A Agent Card v1.0.0
= Agent identity / A2A interfaces / skills / security

UCP Profile 2026-04-08
= commerce service & capability discovery

KNP/1.0
= pre-transaction negotiation semantics
```

它负责把：

```text
KNP/1.0
↕
A2A Message / Task / Artifact / contextId
↕
shopping.negotiation/0.1 Hosted Gateway
```

之间的 transport 与 compatibility semantics 写清楚。

# 1. 战略定位

## 1.1 为什么 shopping-cli 适合成为 Commerce Agent Catalog

A2A Agent Discovery 本身支持：

- well-known Agent Card；
- curated registries / catalogs；
- direct configuration。

shopping-cli 已经拥有：

- Merchant 数据；
- Product / SKU 数据；
- Category / Delivery / public tags；
- Merchant Agent runtime 信息；
- Agent capability 字段；
- Merchant / Agent auth；
- API；
- search；
- audit；
- hosted conversation runtime。

因此从现有系统演进出 Commerce Agent Catalog，比新建一个完全独立 Registry 更自然。

普通 Agent Registry 只回答：

> “有哪些 Agent？有什么 skill？”

shopping-cli 可以回答更有商业价值的问题：

> “有哪些 Agent 代表能够供应某类商品的 Merchant，并且支持指定地区、指定 Commerce capability、A2A、UCP 和 Kiwi Negotiation？”

这是 shopping-cli 最重要的差异化。

---

# 2. 新产品定义

shopping-cli 未来分成两个逻辑平面：

```text
                   shopping-cli

          ┌─────────────────────────┐
          │   Discovery Plane       │
          │                         │
          │ Commerce Agent Catalog  │
          │ Verification            │
          │ Agent Card Cache        │
          │ UCP Profile Cache       │
          │ Capability Index        │
          │ Trust Metadata          │
          └────────────┬────────────┘
                       │
                       │ candidate / reference
                       ▼
          ┌─────────────────────────┐
          │   Commerce Runtime      │
          │                         │
          │ Hosted Gateway          │
          │ Marketplace Conversation│
          │ Merchant Agent Runtime  │
          │ Human Review            │
          │ claim/idempotency/audit │
          │ Legacy Adapter          │
          └─────────────────────────┘
```

两个平面共享：

- identity；
- audit；
- policy；
- rate limiting；
- public/private serialization；
- service layer；
- persistence abstraction。

但它们的业务职责必须分离。

---

# 3. 核心原则

## 3.1 Catalog Is an Index, Not Identity Authority

shopping-cli Catalog 可以保存：

```text
merchant domain
Agent Card URL
UCP Profile URL
cached Agent Card
cached UCP Profile
capability summary
verification evidence
```

但不能因为 Catalog 中记录了某 Agent，就把 Catalog 自己变成最终身份真相源。

对于独立 Agent：

```text
Catalog
  ↓
找到 merchant.example
  ↓
live/recent verification
  ↓
merchant.example/.well-known/ucp
  ↓
Agent Card
```

Domain / cryptographic identity remains authoritative.

## 3.2 Discovery Must Not Imply Routing

Catalog 找到 Merchant Agent 后：

```text
Kiwi Buyer
   ↓ search
shopping-cli Catalog
   ↓ candidate
Merchant Agent
```

如果 Merchant 支持 Direct A2A：

```text
Kiwi Buyer ───────────► Merchant Agent
          Direct A2A
```

shopping-cli 应退出数据路径。

只有以下情况才走 Hosted Gateway：

- Merchant 不支持 direct A2A；
- Merchant 选择 hosted 模式；
- 企业策略要求托管；
- legacy platform。

```text
Kiwi Buyer
   ↓
ShoppingCliHostedChannel
   ↓
shopping-cli Gateway
   ↓
Merchant
```

## 3.3 Preserve Consultation-Only Boundary

Catalog 升级不等于扩展交易副作用。

第一阶段继续：

```text
NO order creation
NO payment
NO refund
NO inventory reservation
NO escrow
NO delivery-success claim
```

新的 Agent Catalog 只是 discovery infrastructure。

## 3.4 Public Metadata and Private Merchant State Must Stay Separate

Catalog MAY expose：

```text
merchant name
domain
public categories
public products
public tags
Agent Card URL
UCP Profile URL
protocol versions
public skills
public capabilities
verification status
last verified timestamp
hosted/direct mode
```

Catalog MUST NOT expose：

```text
automation_boundaries
floor price
cost
private discount policy
agent token
merchant token
private contact
LLM prompt
internal strategy
private reputation evidence
```

---

# 4. 三种 Agent 来源

## 4.1 Hosted Merchant Agent

现有 shopping-cli Merchant + Resident Agent 可以直接升级为 Hosted Agent。

流程：

```text
merchant create
    ↓
merchant configuration
    ↓
hosted merchant agent enabled
    ↓
generate hosted Agent Card
    ↓
generate hosted UCP Profile
    ↓
auto-register in Commerce Agent Catalog
```

特点：

- shopping-cli 管理 Agent runtime；
- shopping-cli 管理 endpoint；
- shopping-cli 可以保证 Catalog 与 Hosted Agent capability 同步；
- identity = hosted identity。

这是第一阶段最容易跑通的 Agent Catalog 数据源。

## 4.2 Independent Merchant Agent

Merchant 自己运行 A2A Agent。

注册：

```text
merchant / operator
    ↓
submit domain or Agent Card URL
    ↓
shopping-cli verification worker
    ↓
fetch UCP Profile
fetch Agent Card
validate
verify domain / identity
index public metadata
```

Catalog 不托管其 Agent runtime。

## 4.3 Discovered / Unclaimed Agent

shopping-cli MAY 从已知 Merchant domain 主动发现：

```text
merchant.com
  ↓
/.well-known/ucp
/.well-known/agent-card.json
```

发现后状态：

```text
DISCOVERED
```

而不是自动：

```text
VERIFIED
```

Merchant 可以随后 claim / verify ownership。

---

# 5. Agent Catalog Domain Model

建议增加新的领域对象，而不是把所有字段塞进现有 `agents` 表。

## 5.1 `catalog_agents`

```text
catalog_agent_id      PK
merchant_id           nullable FK
hosted_runtime_agent_id nullable FK -> agents.id
display_name
provider_name
canonical_domain
agent_type
source_type
lifecycle_status
verification_status
hosting_mode
first_seen_at
last_seen_at
last_verified_at
created_at
updated_at
```

`source_type`：

```text
hosted
self_registered
discovered
imported
admin_curated
```

`hosting_mode`：

```text
direct
hosted
hybrid
unknown
```


`hosted_runtime_agent_id` 只表示 shopping-cli 托管的具体 runtime instance：

```text
source_type = hosted
→ hosted_runtime_agent_id SHOULD be non-null
→ MUST be non-null when catalog record is published (COMMERCE_VERIFIED)

source_type != hosted
→ hosted_runtime_agent_id SHOULD be null
→ MUST be null when catalog record is published
```

`merchant_id` 与 `hosted_runtime_agent_id` 不是互斥字段：

```text
merchant_id
= 该 Catalog Agent 所属 Merchant

hosted_runtime_agent_id
= 该 Merchant 在 shopping-cli 中被托管的具体 Agent runtime
```

一个 Merchant MAY 拥有多个 runtime Agent，因此实现 MUST NOT 仅通过 `merchant_id` 推断唯一 runtime instance。

## 5.2 `agent_endpoints`

```text
endpoint_id
catalog_agent_id
kind
url
protocol
protocol_version
preference
auth_summary_json
status
last_checked_at
```

`kind`：

```text
a2a
agent_card
ucp_profile
hosted_gateway
```

## 5.3 `agent_capabilities`

结构化保存查询友好的能力。

```text
catalog_agent_id
namespace
capability_id
version
required
source
schema_url
spec_url
last_verified_at
```

例如：

```text
A2A
UCP shopping
Kiwi Negotiation
catalog.search
catalog.lookup
```

## 5.4 `agent_skills`

```text
catalog_agent_id
skill_id
name
description
tags_json
input_modes_json
output_modes_json
```

只存公开 skill。

## 5.5 `agent_profile_snapshots`

不要只保存“当前解析结果”。

保存原始公开 profile snapshot：

```text
snapshot_id
catalog_agent_id
profile_type
source_url
etag
last_modified
content_hash
raw_json
fetched_at
fresh_until
validation_status
```

`profile_type`：

```text
agent_card
ucp
```

这样可以：

- 审计 capability 变化；
- 检测 endpoint 劫持；
- 支持 conditional GET；
- 恢复历史 discovery evidence。

## 5.6 `agent_verifications`

```text
verification_id
catalog_agent_id
verification_type
result
evidence_json
checked_at
expires_at
```

例如：

```text
domain_control
https
agent_card_schema
ucp_schema
namespace_origin
signature
oauth_metadata
endpoint_reachability
```

## 5.7 `agent_trust_observations`（v2.2 / Phase 2）

该表**不属于 v2.1 Agent Catalog Foundation 的必需 schema**，推迟到 v2.2 / Phase 2。

它只保存本地观察，不伪装成全球事实。

```text
observation_id
catalog_agent_id
kind
value
source
evidence_ref
observed_at
expires_at
```

例如：

```text
protocol_compliance
timeout_rate
schema_error_rate
successful_exchange
local_asserted_dispute
```

Commercial Reputation 与 Protocol Trust 必须分开。

v1 中 `local_asserted_dispute`、timeout/failed-exchange 等商业声誉观察默认 **private-only**；Public Catalog 只公开 verification status、capability、freshness、hosting mode 等可验证元数据。

---

# 6. Verification State Machine 与 TrustPolicy

Agent Catalog 不使用单一 `verified: true/false`。

```text
DISCOVERED
   ↓
PROFILE_VALID
   ↓
DOMAIN_VERIFIED
   ↓
AGENT_VERIFIED
   ↓
COMMERCE_VERIFIED
```

同时允许：

```text
STALE
REJECTED
SUSPENDED
UNREACHABLE
```

### DISCOVERED

只有 candidate/domain/reference。

### PROFILE_VALID

Agent Card / UCP Profile schema 与基础 semantic validation 通过。

### DOMAIN_VERIFIED

MVP 的最低身份机制固定为：

> **HTTPS domain-control verification**

第一版不把 DID、复杂 PKI 或私有 CA 作为必选依赖。

对外独立 Agent 至少必须满足：

- HTTPS；
- domain 与 discovery authority 关系通过验证；
- well-known / challenge response 满足 DomainControlPolicy；
- redirect、DNS resolution、certificate 与 SSRF policy 通过。

未来 MAY 增加：

```text
DNS TXT challenge
Agent Card JWS
mTLS
OIDC federation
enterprise CA
```

但它们属于更高 TrustPolicy，而不是 MVP 必选项。

### AGENT_VERIFIED

Agent identity/auth evidence 达到当前 TrustPolicy 门槛。

### COMMERCE_VERIFIED

以下至少通过：

```text
UCP profile validation
A2A Agent Card validation
capability namespace validation
supported protocol/version intersection
Kiwi/KNP capability compatibility when claimed
```

这些状态是 verification，不是 Merchant commercial reputation。

## 6.1 TrustPolicy

新增版本化配置对象：

```text
TrustPolicy
```

至少包含：

```text
policy_version
require_https
allowed_schemes
allowed_ports
domain_control_method
require_live_refresh_before_connect
profile_max_age_seconds
allow_agent_card_jws
require_agent_card_jws
allowed_a2a_versions
allowed_ucp_versions
allowed_knp_versions
redirect_limit
max_profile_bytes
```

MVP 建议实现为：

```text
config schema
+
runtime immutable snapshot
+
audit policy_version
```

Public multi-tenant 版本再考虑 DB-backed policy sets。

Verification event MUST 记录：

```text
trust_policy_version
```

避免未来无法解释“当时为什么判定 verified”。

## 6.2 Claim Proof

按 `source_type` 固定第一版证明路径：

```text
hosted
→ existing merchant/admin identity is proof

self_registered
→ HTTPS domain-control challenge

discovered
→ UNCLAIMED
→ claim
→ same HTTPS domain-control challenge
```

“知道 Agent Card URL”本身不构成 ownership proof。

# 7. Discovery Service

建议新增：

```text
shopping_cli/discovery/
```

主要组件：

```text
DiscoveryService
ProfileFetcher
UcpProfileParser
AgentCardParser
CapabilityResolver
IdentityVerifier
TrustEvaluator
CatalogIndexer
```

---

# 8. Commerce Agent Catalog Search

## 8.1 搜索目标

不要只支持：

```text
search agent by name
```

真正的 Kiwi Commerce Agent Catalog 应支持：

```text
product/category
merchant
region
delivery coverage
skill
A2A version
UCP version
KNP version
hosted/direct
verification state
freshness
```

典型请求：

> 找中国华东地区，支持工业显示器、A2A v1.0、KNP/1.0，且 24 小时内验证过的 Merchant Agents。

## 8.2 Search Result Contract

Catalog 返回的是 Candidate，而不是“当前在线身份已经被永久证明”的远端状态。

公开 capability identifier **MUST 使用全限定标识符**。短名只能作为内部查询 alias，不得出现在 canonical public contract 中。

示例：

```json
{
  "catalog_agent_id": "cagt_01J...",
  "merchant": {
    "id": "mrc_01J...",
    "name": "Example Merchant",
    "domain": "merchant.example"
  },
  "discovery": {
    "agent_card_url": "https://merchant.example/.well-known/agent-card.json",
    "ucp_profile_url": "https://merchant.example/.well-known/ucp"
  },
  "protocols": {
    "a2a": ["1.0.0"],
    "ucp": ["2026-04-08"]
  },
  "capabilities": [
    "EXAMPLE_ONLY.reverse.domain.shopping.negotiation"
  ],
  "verification": {
    "status": "COMMERCE_VERIFIED",
    "last_verified_at": "..."
  },
  "hosting": {
    "mode": "direct"
  }
}
```

`EXAMPLE_ONLY.*` 是文档占位符，**MUST NOT ship**。

生产发布前必须替换为 Kiwi 实际控制域名对应的 reverse-domain capability namespace。

Kiwi AgentDiscovery 在正式开始谈判前仍 SHOULD 根据 TrustPolicy 做 fresh verification / cache validation。

## 8.3 Search Ranking

Ranking 不由 LLM 黑盒决定。

建议：

```text
hard filters
   ↓
commercial relevance
   ↓
capability compatibility
   ↓
verification freshness
   ↓
protocol trust
   ↓
merchant/product relevance
```

不要把 Merchant commercial reputation 和 Protocol verification 混成一个分数。

第一阶段继续利用 SQLite FTS5 + deterministic secondary scoring。

当 Agent Catalog 进入：

```text
10^5 agents
10^6+ products
multi-node public service
```

再考虑 Postgres FTS / OpenSearch。

# 9. Search Ranking

Search Ranking normative direction is defined in §8.3. This section is retained only for numbering compatibility.

# 10. Agent Catalog API 与版本化

现有 shopping-cli API：

```text
/search/*
/agents/*
/negotiation/*
```

保持兼容，不强制整体迁移到 `/v1`。

新的公网 Agent Network surface 从第一天开始版本化，并统一使用：

```text
/v1/agent-catalog/*
```

避免与现有 Merchant/Product `catalog` 概念混淆。

所有新 route MUST 注册到现有 `route_registry.py` / `_ROUTE_TABLE` 元数据源，并同时覆盖：

- FastAPI；
- fallback ASGI；
- auth metadata；
- route contract tests。

## 10.1 Public Read

```text
GET /v1/agent-catalog/agents
GET /v1/agent-catalog/agents/{catalog_agent_id}
GET /v1/agent-catalog/agents/search
GET /v1/agent-catalog/merchants/{merchant_id}/agents
```

搜索参数 MAY 包括：

```text
q
category
region
skill
capability
protocol
hosting_mode
verification_status
verified_after
limit
cursor
```

Public response 只使用 public serializer。

## 10.2 Registration

```text
POST /v1/agent-catalog/agents/register
```

输入：

```text
domain
agent_card_url?
ucp_profile_url?
merchant_id?
```

MUST 支持真正的 idempotency claim/replay。

## 10.3 Verification / Refresh

```text
POST /v1/agent-catalog/agents/{id}/refresh
POST /v1/agent-catalog/agents/{id}/verify
```

权限：

```text
owner merchant
admin
verification worker
```

普通 public client 不允许无限触发 refresh。

## 10.4 Claim

```text
POST /v1/agent-catalog/agents/{id}/claim
```

Claim 使用 §6.2 定义的证明机制。

Registration、claim、refresh 均必须有：

```text
rate limit
idempotency
resource budget
audit
```

# 11. CLI Design 与兼容

现有：

```text
shopping-cli search products
shopping-cli search merchants
shopping-cli search policies
```

保持不变。

Agent Catalog 使用独立命名空间：

```text
shopping-cli agent catalog search
shopping-cli agent catalog get
shopping-cli agent catalog register
shopping-cli agent catalog refresh
shopping-cli agent catalog verify
shopping-cli agent catalog claim
shopping-cli agent catalog suspend

shopping-cli agent catalog stats
shopping-cli agent catalog doctor
```

例如：

```bash
shopping-cli agent catalog search \
  --capability EXAMPLE_ONLY.reverse.domain.shopping.negotiation \
  --protocol a2a \
  --verified
```

以及：

```bash
shopping-cli agent catalog register \
  --domain merchant.example
```

这样避免：

```text
Merchant/Product Catalog
```

与：

```text
Commerce Agent Catalog
```

在 CLI 和代码结构中发生语义碰撞。

# 12. Existing `search_merchants` Compatibility

现有 `search_merchants` 不能突然改变返回契约。

### Phase 1

保持原行为，新增：

```text
search_catalog_agents
get_catalog_agent
```

### Phase 2

`search_merchants` MAY 增加 optional public discovery summary：

```text
agent_discovery:
  available
  best_catalog_agent_id
  direct_a2a
```

但必须 backwards-compatible。

### Phase 3

Kiwi Buyer 的新 AgentDiscovery 优先走：

```text
search_catalog_agents
```

商品/商家搜索仍可作为 candidate source。

---

# 13. Hosted Commerce Gateway：现状与升级

shopping-cli 当前已经拥有 Authoritative Negotiation Gateway，因此本阶段不是“新增 Gateway”。

现有继续负责：

```text
Marketplace Conversation
Resident Merchant Agent
Human Review
claim / heartbeat
message idempotency
audit
merchant auth
buyer auth
shopping.negotiation/0.1
role-trimmed snapshot
policy gate
authoritative settlement
```

新增的是**开放网络外壳**：

```text
Hosted Agent Card
Hosted UCP Profile
A2A Server Endpoint
A2A ↔ Negotiation Binding
```

目标：

> 让现有 Authoritative Negotiation Gateway 既可以继续服务 Hosted/Legacy Kiwi，也可以作为标准 A2A Merchant Agent 被外部 Agent 发现和调用。

# 14. Hosted Agent Publication

每个 Hosted Merchant 可以拥有逻辑 Agent Card。

## 14.1 Shared Host + Agent Path

```text
https://shopping.example/a2a/agents/{agent_id}
```

Catalog record 绑定到 hosted endpoint。

## 14.2 Merchant Custom Domain（Deferred）

MVP 不实现 custom-domain hosted identity。第一版 Hosted Agent 使用 shopping-cli 控制的 Shared Host identity。

以下问题作为独立后续 decision：TLS 由谁终止、merchant domain 如何 delegate、`/a2a` 由谁 serve、custom domain 与 hosted merchant identity 如何绑定。

未来如果 Merchant 配置自有 domain：

```text
merchant.example/.well-known/agent-card.json
```

可以代理/跳转到 hosted runtime，但身份验证必须明确。

MVP 推荐 Shared Host，降低 DNS 和证书复杂度。

---

# 15. Negotiation Contract 与 Compatibility

## 15.1 当前真实 Contract

shopping-cli 当前 authoritative wire contract 是：

```text
shopping.negotiation/0.1
```

它属于当前 Hosted Gateway 的已实现契约，继续冻结并随包发布。

## 15.2 目标开放协议

Kiwi A2A 的目标开放 negotiation protocol 是：

```text
KNP/1.0
```

shopping-cli v1.2.1 设计**不把 `shopping.negotiation/0.1` 重命名成 KNP/1.0**，也不假装两者已等价。

## 15.3 必须新增的独立工件

必须维护：

```text
docs/a2a/shopping-cli-a2a-binding-1.0.md
```

它至少要定义：

```text
KNP Envelope
↔ A2A Message/DataPart

KNP async operation
↔ A2A Task

KNP result
↔ A2A Message / Artifact

negotiation_id
↔ A2A contextId

KNP message_id
↔ A2A messageId

Hosted negotiation operation
↔ shopping.negotiation/0.1
```

（当前工件文件为 `docs/a2a/shopping-cli-a2a-binding-1.0-rc1.md`；通过 rc1 §8 门禁后去掉 `-rc1` 后缀。）

当 Hosted Gateway 继续使用 `shopping.negotiation/0.1` 时：

```text
KNP/1.0
   ↓
A2A Binding
   ↓
HostedNegotiationCompatibilityAdapter
   ↓
shopping.negotiation/0.1
```

规则：

```text
lossless → adapt
lossy → fail closed
unsupported → human review / capability_incompatible
```

不得偷偷丢弃：

```text
condition semantics
expiry
identity
agreement semantics
idempotency
```

现有 `quote_request / negotiate / purchase_intent` 等 legacy message intent 不立即删除。

AcceptedNonbindingAgreement 仍然不得被解释为 transaction。

## 15.5 Direct Path

独立 Merchant Agent 如果原生支持 KNP/1.0：

```text
Kiwi Buyer
   ↓
A2A + KNP/1.0
   ↓
Independent Merchant Agent
```

此路径不经过 `shopping.negotiation/0.1`。

这一区分是 shopping-cli 从 Hosted Gateway 走向开放 Agent Network 的核心。

# 16. Catalog vs Hosted Gateway Boundary

这条边界必须写死：

```text
Catalog
= discovery metadata

Gateway
= hosted authoritative conversation state
```

Catalog 数据变更：

```text
capability changed
endpoint changed
profile refreshed
verification changed
```

不等于 Marketplace Conversation event。

反过来也一样。

不要把 Catalog 的 profile snapshot 和 Hosted Conversation 混在同一状态机。

---

# 17. Security Requirements

开放 Catalog 后攻击面会显著扩大。

## 17.1 SSRF

ProfileFetcher 必须：

- 默认 HTTPS；
- 拒绝 loopback/private/link-local/metadata IP；
- DNS resolve 前后检查；
- 限制 redirect；
- redirect 后重新检查目标；
- 限制 port；
- 限制 response bytes；
- 限制 JSON depth/nodes；
- timeout；
- 禁止 file:// / ftp:// 等 scheme。

## 17.2 Profile Poisoning

Agent Card / UCP Profile 都是 untrusted input。

必须：

```text
fetch
→ size limit
→ JSON parse
→ schema validate
→ semantic validate
→ identity/authority validate
→ public-field projection
→ index
```

不得把 profile 中的自然语言 description 当成系统提示。

## 17.3 Secret Policy

Catalog MUST NOT persist：

```text
Bearer token
API key
password
private key
merchant floor price
private automation policy
```

Agent Card 中如果发现 static secret-like fields：

```text
reject / quarantine
```

## 17.4 Registration Abuse

需要：

```text
rate limiting
idempotency
per-domain limits
verification queue budget
cooldown
abuse flags
```

防止利用 shopping-cli 做大规模 SSRF scanner。

---

# 18. Caching

A2A Agent Card 通常低频变化。

shopping-cli SHOULD 支持：

```text
Cache-Control
ETag
If-None-Match
Last-Modified
If-Modified-Since
```

Catalog snapshot 必须包含：

```text
fetched_at
fresh_until
etag
content_hash
```

状态：

```text
fresh
stale_usable
stale_unusable
```

正式 negotiation 前 Kiwi 可以根据 TrustPolicy 决定是否必须 live refresh。

---

# 19. Persistence Strategy

## 19.1 Local / Single-node

当前 SQLite 继续适用：

```text
development
local demo
small curated catalog
single-node hosted runtime
```

现有工程对 SQLite migration、并发、资源限制和测试已经投入较多，不应无意义重写。

## 19.2 Public Production Catalog

一旦目标变成：

```text
public internet
multi-instance API
large Agent Catalog
continuous verification workers
high write concurrency
```

不建议继续把 SQLite 当长期唯一生产数据库。

因此应先建立 Repository abstraction：

```text
CatalogRepository
ConversationRepository
AuditRepository
```

实现：

```text
SQLiteCatalogRepository
```

未来增加：

```text
PostgresCatalogRepository
```

不要在 MVP 就强制迁移全部现有 Marketplace Conversation 数据到 Postgres。

可以让 Discovery Plane 先获得独立 persistence adapter。

---

# 20. Recommended Module Layout

为了避免与现有 Merchant/Product Catalog 混淆，新增模块统一命名为：

```text
agent_catalog
```

而不是新的顶层 `catalog/`。

```text
shopping_cli/
├── core/
│   ├── catalog.py / existing catalog modules
│   ├── conversations/
│   ├── negotiation/
│   └── agents/
│
├── services/
│   ├── agent_catalog.py
│   ├── agent_registration.py
│   ├── agent_verification.py
│   ├── agent_discovery.py
│   ├── hosted_agents.py
│   └── negotiation.py
│
├── discovery/
│   ├── fetcher.py
│   ├── agent_card.py
│   ├── ucp.py
│   ├── capabilities.py
│   ├── verifier.py
│   ├── trust.py
│   └── cache.py
│
├── agent_catalog/
│   ├── models.py
│   ├── repository.py
│   ├── sqlite_repository.py
│   ├── search.py
│   ├── ranking.py
│   └── serializers.py
│
├── a2a/
│   ├── hosted_server.py
│   ├── agent_card.py
│   ├── ucp_profile.py
│   └── binding.py
│
├── negotiation/
│   ├── legacy_gateway.py
│   └── compatibility_adapter.py
│
├── api/
│   ├── route_registry.py
│   ├── handlers/
│   │   ├── agent_catalog.py
│   │   └── ...
│   └── ...
│
└── cli/
    ├── agent_catalog.py
    └── ...
```

具体目录应结合当前 HEAD 已有文件布局落地；本节表达的是职责分离，而不是要求机械搬迁现有稳定模块。

# 21. AgentDiscovery Integration with Kiwi

Kiwi 侧：

```text
AgentDiscovery
   │
   ├── ShoppingCliCatalogSource
   ├── WellKnownSource
   ├── DirectConfigSource
   └── FutureRegistrySource
```

仓库归属必须明确：

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

端到端流程：

```text
Kiwi user intent
     ↓
Kiwi AgentDiscovery.search()
     ↓
ShoppingCliCatalogSource
     ↓
shopping-cli /v1/agent-catalog/*
     ↓
Candidate Agents
     ↓
Kiwi fresh verification / policy
     ↓
Capability Intersection
     ↓
CounterpartyProfile
     ↓
Channel Selection
     ├── A2ADirectChannel
     └── ShoppingCliHostedChannel
```

Catalog 返回 candidate。

Kiwi 负责把 candidate 升级成当前任务可使用的 `CounterpartyProfile`。

两者职责不能合并。

# 22. Hosted / Direct Status Model

建议 Catalog 显式记录：

```text
DIRECT_ONLY
HOSTED_ONLY
HYBRID
UNKNOWN
```

### DIRECT_ONLY

shopping-cli 只做 discovery。

### HOSTED_ONLY

communication 经过 shopping-cli。

### HYBRID

优先 Direct；根据 policy 可以 fallback hosted。

Fallback 不能自动扩大权限。

---

# 23. Audit

新事件至少包括：

```text
catalog_agent_discovered
catalog_agent_registered
catalog_agent_claimed
catalog_agent_refreshed
catalog_agent_verified
catalog_agent_verification_failed
catalog_agent_suspended
catalog_agent_endpoint_changed
catalog_agent_capability_changed
catalog_agent_stale
```

Audit 记录：

```text
schema_version
event_type
actor
target
result
evidence refs
```

不要把完整 raw secret/profile private data 写入 audit details。

---

# 24. Observability

建议新增指标：

```text
catalog_agent_count
catalog_verified_agent_count
profile_fetch_latency
profile_fetch_error_rate
verification_queue_depth
stale_profile_count
catalog_search_latency
catalog_search_result_count
direct_a2a_ratio
hosted_gateway_ratio
catalog_to_connection_conversion
```

真正重要的网络指标：

```text
discovery
→ verified
→ compatible
→ connected
→ negotiation_started
```

---

# 25. Implementation Phases

**Phase 是依赖顺序，不是发布承诺。**

发布承诺以 §26 Version Proposal 为准。

## Phase 0 — Baseline Alignment

- 把 `shopping.negotiation/0.1` Authoritative Negotiation Gateway 固定为当前事实基线；
- 保持现有 API / CLI 行为；
- 保持 consultation-only；
- 保持 token / audit / idempotency；
- 保持 public/private serializers；
- 不修改 Conversation authority；
- 创建 Direct A2A Binding 独立规范工件。

## Phase 1 — Agent Catalog Foundation

新增：

```text
catalog_agents
agent_endpoints
agent_capabilities
agent_skills
agent_profile_snapshots
agent_verifications
```

`agent_trust_observations` 明确推迟到 v2.2 / Phase 2。

新增：

```text
search_catalog_agents
get_catalog_agent
```

Hosted Merchant 自动创建 Catalog entry。

### Hosted Runtime → Catalog Projection

现有 `agents` 表**不迁移掉，也不与 `catalog_agents` 双向同步**。

职责固定为：

```text
agents
= hosted runtime instance state
  heartbeat / stale / pid / runtime capabilities

catalog_agents
= public discoverable network identity
  Agent Card / UCP / verified capabilities
```

关联：

```text
catalog_agents.hosted_runtime_agent_id
                │
                ▼
             agents.id
```

能力同步方向固定为单向 projection：

```text
agents.capabilities_json
      │ runtime-reported
      ▼
publication policy
      ▼
Hosted Agent Card / UCP Profile
      ▼
schema + identity verification
      ▼
agent_capabilities
```

`agent_capabilities` 是 Catalog 的 public verified/indexed view；不得反向覆盖 runtime 的 `agents.capabilities_json`。

这样避免两个同权 source-of-truth 发生漂移。

## Phase 2 — External Discovery & Verification

新增：

```text
register external agent
fetch /.well-known/ucp
fetch Agent Card
HTTPS domain-control verification
TrustPolicy
refresh/cache
claim
```

加入 SSRF 防护。

单机 SQLite 阶段的 verification 执行模型固定为：

```text
registration / explicit refresh
        ↓
bounded in-process verification queue
        ↓
single-process worker with concurrency budget
```

CLI `agent catalog refresh` MAY 手动触发同一 service。Public multi-node 阶段才升级为 distributed worker queue。

## Phase 3 — Kiwi Cross-Repo Integration

shopping-cli repo 提供：

```text
versioned Agent Catalog API
CandidateAgent DTO
```

Kiwi repo 实现：

```text
ShoppingCliCatalogSource
AgentDiscovery integration
CounterpartyProfile
```

目标：

> Buyer Kiwi 能通过 shopping-cli 找到独立 Merchant Agent，并决定 Direct A2A 或 Hosted Channel。

## Phase 4 — Hosted A2A Publication & Binding

不是新增 Negotiation Gateway，而是在现有 authoritative gateway 外增加：

```text
Hosted Agent Card
Hosted UCP Profile
A2A Server Endpoint
KNP/A2A Binding
KNP ↔ shopping.negotiation/0.1 compatibility adapter
```

## Phase 5 — Public Catalog Productionization

准备公网服务：

- persistence abstraction；
- Postgres option；
- distributed rate limiting；
- verification worker queue；
- observability；
- abuse controls；
- production penetration testing；
- HA；
- moderation / suspension；
- domain claim workflow。

# 26. Version Proposal

**Version 是发布承诺；Phase 是实现依赖顺序。**

不建议大爆炸式直接重写 3.0。

## 2.1 — Agent Catalog Foundation

```text
Agent Catalog tables
Hosted auto-registration
Agent Catalog CLI/API
Search
route_registry integration
```

## 2.2 — Discovery & Verification

```text
UCP resolver
Agent Card resolver
HTTPS domain-control
TrustPolicy
verification/cache
external registration/claim
agent_trust_observations (private-only)
```

## 2.3 — Kiwi Discovery Integration

跨仓 release target：

```text
shopping-cli:
  CandidateAgent API/DTO

Kiwi:
  ShoppingCliCatalogSource
  CounterpartyProfile
  fresh verification
```

## 2.4 — Hosted A2A Publication

```text
Hosted Agent Card
Hosted UCP Profile
Hosted A2A Endpoint
Direct A2A Binding implementation
KNP ↔ shopping.negotiation/0.1 compatibility
```

注意：

> `shopping.negotiation/0.1` Gateway 在此版本之前已经存在；2.4 的增量是开放 A2A publication/binding，而不是重新实现 Gateway。

## 3.0 — Commerce Agent Network Infrastructure

完成：

```text
Commerce Agent Catalog
+
Discovery & Verification
+
Authoritative Hosted Negotiation Gateway
+
Direct A2A discovery path
+
Hosted A2A path
+
public production hardening
```

# 27. MVP Definition

MVP 明确是一个**跨 Phase 1–3、跨 shopping-cli / Kiwi 两个仓库的 Vertical Slice**，而不是单仓 Phase 1 的同义词。

## MVP Slice A — Hosted Agent Catalog

shopping-cli repo：

```text
1. Existing Merchant
2. Existing Resident Merchant Agent
3. Auto-create Agent Catalog entry
4. Publish CandidateAgent metadata
5. Search Agent Catalog
```

Kiwi repo：

```text
6. ShoppingCliCatalogSource
7. Kiwi obtains CandidateAgent
8. Kiwi builds CounterpartyProfile
```

目标：

> Kiwi 第一次能把 shopping-cli 当作 Commerce Agent Catalog 使用。

## MVP Slice B — Independent Agent Discovery

shopping-cli repo：

```text
1. Register merchant.example
2. fetch /.well-known/ucp
3. fetch Agent Card
4. validate
5. HTTPS domain-control verify
6. index CandidateAgent
```

Kiwi repo：

```text
7. search via ShoppingCliCatalogSource
8. fresh verify according to policy
9. select A2ADirectChannel
```

然后：

```text
Kiwi Buyer
     ↓
Direct A2A
     ↓
Independent Merchant Agent
```

shopping-cli 离开消息数据路径。

## MVP Slice C — Hosted A2A

在前两条跑通后：

```text
existing shopping.negotiation/0.1 gateway
     +
Hosted Agent Card
     +
Hosted UCP Profile
     +
A2A Endpoint
     +
A2A/KNP compatibility binding
```

目标：

> 现有 Hosted Merchant 也能被标准 A2A Agent 发现并调用。

当 Slice B 跑通时，shopping-cli 才真正从 Gateway 升级成开放 Commerce Agent Catalog；当 Slice C 跑通时，Hosted Gateway 也正式加入开放 A2A 网络。

# 28. Definition of Done for Agent Catalog v1

必须满足：

1. Existing Merchant 可以无损升级为 Catalog Merchant。
2. Existing Resident Merchant Agent 可以生成 Catalog entry。
3. Independent A2A Agent 可以通过 domain / Agent Card URL 注册。
4. Catalog 可以解析 UCP Profile。
5. Catalog 可以解析 Agent Card。
6. Catalog 可以验证公开 schema。
7. Catalog 可以保存 capability index。
8. Catalog 可以按 commerce capability 搜索。
9. Catalog 支持 ETag / freshness。
10. Public serializer 不暴露 private Merchant fields。
11. ProfileFetcher 有完整 SSRF 防护。
12. Registration / refresh 有 rate limit 与 idempotency。
13. Agent Card / UCP 内容始终视为 untrusted。
14. Catalog 不持有远端 Agent static secret。
15. Search result 明确 verification status / freshness。
16. Catalog candidate 不等于 verified live identity。
17. Kiwi 可以使用 Catalog 作为 AgentDiscovery Source。
18. Kiwi 找到 Direct Agent 后可以绕过 shopping-cli 通信。
19. Hosted Merchant 仍能通过 shopping-cli Gateway 工作。
20. Existing Conversation / claim / audit contract 不回归。
21. no-order / no-payment / no-reservation 不变量继续成立。
22. A2A Agent Card 固定使用官方 A2A v1.0.0 schema/semantics。
23. UCP Profile 固定使用 2026-04-08 specification family。
24. Hosted `agents` → `catalog_agents` 只允许单向 publication projection。
25. Public capability identifier 只使用全限定生产 namespace；文档占位符不得进入 release artifact。

---

# 29. What Not To Do

第一阶段不要：

- 做全球通用 Agent Registry；
- 为 Coding/Research/Medical Agent 建目录；
- 发明另一套 Agent Card；
- 让 Catalog 成为 Direct A2A 必经代理；
- 把 cached Agent Card 当永远可信；
- 用 LLM 自动决定 identity verification；
- 把 merchant reputation 和 protocol trust 混合；
- 把 Catalog 和 Marketplace Conversation 放进同一状态机；
- 在 Agent Card 中保存 credential；
- 为了 Catalog 提前加入订单/支付；
- 把现有 `shopping.negotiation/0.1` 直接重命名成 KNP/1.0；
- 在没有独立 binding spec 和 conformance tests 时宣称 Direct A2A 已兼容；
- 在 MVP 为扩展性直接重写全部 SQLite 核心。

---

# 30. Long-Term Product Position

最终 shopping-cli 可以成为：

> **Kiwi Commerce Agent Network 的发现与托管基础设施。**

它提供两个核心价值：

```text
Commerce Agent Catalog
=
找到合适的商业 Agent

Hosted Commerce Gateway
=
让还没有 Direct A2A 能力的 Merchant 也能加入 Agent Network
```

这样形成：

```text
                   shopping-cli Catalog
                  /                    \
                 /                      \
        Buyer Agents                 Merchant Agents
             │                            │
             └──────── Direct A2A ────────┘
                           │
                           │ when needed
                           ▼
                shopping-cli Hosted Gateway
```

shopping-cli 不必控制所有通信，却仍然可以成为 Kiwi 网络最重要的 discovery、verification 和 hosted onboarding 节点。

这比把它继续定义为单一 Gateway 更有长期价值，也比做一个泛化 Agent Registry 更符合其现有 Commerce 数据、Agent runtime 和安全能力积累。

---

# Appendix A — 现有设计继承依据

本设计基于现有 shopping-cli 文档中的以下事实：

- `architecture.md`：SQLite trusted state、Marketplace API/CLI、Resident Merchant Agent、`MerchantAgentTools` typed boundary、LLM dispatch 与 consultation-only 边界。
- 当前 HEAD Negotiation Gateway：`shopping.negotiation/0.1` frozen contract、`services/negotiation.py` / `core/negotiation.py`、`/negotiation/*` authoritative API、claim/heartbeat/policy/audit/settlement。
- `agent-protocol.md`：Merchant Agent、capabilities、heartbeat/stale、Conversation/Message、Agent token 生命周期。
- `optimization-directions.md`：shared service layer、FTS/search scaling、显式 migration、domain exception、LLM tool contract consolidation。
- `code-review-2026-05-21.md`、`code-review-2026-06-04.md`、`code-review-2026-07-07.md`、`shopping-cli-code-review-2026-07-22.docx`：认证、并发、搜索、插件、公开/私有序列化和生产化风险。
- `p2-remediation-2026-07-24.md`、`p3-remediation-2026-07-24.md`：资源限制、token 生命周期、HTTPS、日志、CI、Ruff/Mypy、发布制品等整改已形成的新工程基线。
- `migration-from-shopping.md`：legacy import 只迁移 Merchant/Product/Public Catalog/Stock，继续忽略 transaction/payment-like records。

---

# Appendix B — 外部协议约束

A2A 当前明确支持的 Agent Discovery 策略包括：

- well-known Agent Card；
- curated registries/catalogs；
- direct configuration。

A2A 当前不规定 curated registry 的统一 API，因此 shopping-cli 可以定义自己的 Commerce-oriented Catalog API，同时返回标准 Agent Card references，并保持 Direct A2A interoperability。

Catalog 的核心原则是：

> **Search centrally if useful; verify at the authority; communicate directly when possible.**


---

# Appendix C — v1.1 修订摘要

本修订吸收 2026-08-06 Design Review 的有效意见：

1. 将 Authoritative Negotiation Gateway 明确写入当前事实基线；
2. 不再把 2.4/Phase 4 描述为“新增 KNP adapter/Gateway”；
3. 把 Direct A2A wire mapping 升级为独立必交付规范；
4. 保留 `shopping.negotiation/0.1` 为现有 Hosted/Legacy contract，同时保留 KNP/1.0 为开放 A2A target protocol；
5. MVP TrustPolicy 选择 HTTPS domain-control；
6. 明确 claim proof；
7. 将 Agent Catalog 代码命名改为 `agent_catalog`；
8. 新公网 API 改为 `/v1/agent-catalog/*`，旧 API 不整体迁移；
9. 明确 route_registry / fallback ASGI 接入；
10. 明确 Kiwi `ShoppingCliCatalogSource` 的跨仓归属；
11. 明确 Version 驱动发布、Phase 驱动依赖；
12. 商业声誉 observation v1 默认 private-only。

---

# Appendix D — v1.2 修订摘要

本修订吸收 `design-review-a2a-v1.1-2026-08-06.md`：

1. Binding 决策从“待办清单”升级为有 Proposed Direction / Owner / Freeze Milestone 的 Decision Table；
2. Pin A2A Agent Card / Protocol v1.0.0；
3. Pin UCP Profile 2026-04-08；
4. `agent_trust_observations` 推迟到 v2.2 / Phase 2；
5. 全部统一为 `agent_profile_snapshots`；
6. 定义旧 `agents` → 新 `catalog_agents` 单向 publication projection；
7. Public capability identifier 强制 fully-qualified，短名仅内部 alias；
8. `EXAMPLE_ONLY.*` 明确禁止进入 release；
9. Hosted custom domain 明确 Deferred；
10. SQLite MVP verification worker 固定为 bounded in-process queue；
11. Binding 中 `Principal Memory` 改为协议中立的 `principal-private state`；
12. 正式路径固定为 `docs/a2a/shopping-cli-a2a-binding-1.0-rc1.md`。

---

# Appendix E — v1.2.1 Implementation-Prep Patch

本补丁不改变架构方向，只修复 `design-review-a2a-v1.2-2026-08-06.md` 指出的实施前接缝问题：

1. 修正 §5.5 表名为 `agent_profile_snapshots`；
2. 在 §5.1 为 `catalog_agents` 增加 `hosted_runtime_agent_id nullable FK -> agents.id`；
3. 明确 `merchant_id` 表示 Merchant ownership，`hosted_runtime_agent_id` 表示具体 hosted runtime instance，两者不互斥；
4. 修正 §15.2 残留的旧版本自引用；
5. 当前 Binding 工件路径固定为 `docs/a2a/shopping-cli-a2a-binding-1.0-rc1.md`；
6. 旧 `shopping-cli-a2a-binding-1.0-draft.md` 标记为 superseded，并从当前发布文档包中移除；
7. §9 numbering-compatibility placeholder 暂不处理，留待下一次结构性重排；
8. §15.3 补充说明：当前工件文件为 `docs/a2a/shopping-cli-a2a-binding-1.0-rc1.md`，冻结后去掉 `-rc1` 后缀；
9. §5.1 `hosted_runtime_agent_id` 收紧为发布态（COMMERCE_VERIFIED）MUST，保留过渡态 SHOULD。

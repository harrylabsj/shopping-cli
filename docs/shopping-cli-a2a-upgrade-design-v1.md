---
title: shopping-cli A2A 升级总体设计
version: v1.0
date: 2026-08-06
status: Proposed Architecture
target: shopping-cli 2.x -> Commerce Agent Catalog + Hosted Commerce Gateway
related:
  - Kiwi A2A Agent Commerce Network Architecture Baseline
  - Kiwi Negotiation Protocol 1.0
---

# shopping-cli A2A 升级总体设计 v1.0

## 0. 文档定位

本文定义 shopping-cli 从当前“本地 Commerce Consultation Runtime / Marketplace Gateway”升级为：

> **Commerce Agent Catalog + Hosted Commerce Gateway**

的总体方向。

本次升级不是推倒重写。

现有 shopping-cli 已具备较成熟的安全与运行底座，应尽量继承：

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
- consultation-only / no-order / no-payment / no-reservation 边界。

升级的核心是：

```text
原来：
shopping-cli
=
Local Commerce Runtime
+
Marketplace Gateway

未来：
shopping-cli
=
Commerce Agent Catalog
+
Discovery & Verification Infrastructure
+
Hosted Commerce Gateway
+
Legacy Commerce Adapter
```

其中 Catalog 是可选 discovery infrastructure，而不是 Kiwi A2A 网络的强制中心。

---

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

## 5.7 `agent_trust_observations`

只保存本地观察，不伪装成全球事实。

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

---

# 6. Verification State Machine

建议不要只使用 `verified: true/false`。

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

Agent Card / UCP Profile schema 合法。

### DOMAIN_VERIFIED

endpoint 与 domain authority 关系满足策略。

### AGENT_VERIFIED

Agent identity/auth evidence 达到当前 TrustPolicy 门槛。

### COMMERCE_VERIFIED

Commerce capability、UCP namespace、Kiwi Negotiation 等验证完成。

这些状态是 verification，不是 Merchant reputation。

---

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

# 8. Catalog Search

## 8.1 搜索目标

不要只支持：

```text
search agent by name
```

真正的 Kiwi Commerce Catalog 应支持：

```text
product/category
merchant
region
delivery coverage
skill
A2A version
UCP version
Kiwi Negotiation version
hosted/direct
verification state
trust level
freshness
```

典型请求：

> 找中国华东地区，支持工业显示器，支持 A2A + Kiwi Negotiation，且 24 小时内验证过的 Merchant Agents。

## 8.2 Search Result

Catalog 返回的是 Candidate，不是“永远可信的远端状态”。

```json
{
  "catalog_agent_id": "cagt_...",
  "merchant": {
    "id": "seller-a",
    "name": "Seller A",
    "domain": "seller.example"
  },
  "discovery": {
    "agent_card_url": "https://seller.example/.well-known/agent-card.json",
    "ucp_profile_url": "https://seller.example/.well-known/ucp"
  },
  "capabilities": {
    "a2a": ["1.0"],
    "kiwi_negotiation": ["1.0"]
  },
  "verification": {
    "status": "COMMERCE_VERIFIED",
    "last_verified_at": "..."
  },
  "hosting": {
    "mode": "direct"
  }
}
```

Kiwi AgentDiscovery 在正式开始谈判前仍 SHOULD 做 fresh verification / cache validation。

---

# 9. Search Ranking

Ranking 不应该由 LLM 黑盒决定。

建议基础排序：

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

---

# 10. Catalog API

建议新增版本化 API。

## 10.1 Public Read

```text
GET /v1/catalog/agents
GET /v1/catalog/agents/{catalog_agent_id}
GET /v1/catalog/agents/search
GET /v1/catalog/merchants/{merchant_id}/agents
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
POST /v1/catalog/agents/register
```

输入：

```text
domain
agent_card_url?
ucp_profile_url?
merchant_id?
```

必须幂等。

同一 registration retry 应返回相同 logical result。

## 10.3 Verification / Refresh

```text
POST /v1/catalog/agents/{id}/refresh
POST /v1/catalog/agents/{id}/verify
```

权限：

```text
owner merchant
admin
verification worker
```

普通 public client 不允许强制无限 refresh，防止 SSRF / resource abuse。

## 10.4 Claim

对于 discovered/unclaimed Agent：

```text
POST /v1/catalog/agents/{id}/claim
```

Claim 本身不能只靠“知道 URL”。

必须有 domain / account / cryptographic proof。

---

# 11. CLI Design

建议新增：

```text
shopping-cli catalog agent search
shopping-cli catalog agent get
shopping-cli catalog agent register
shopping-cli catalog agent refresh
shopping-cli catalog agent verify
shopping-cli catalog agent claim
shopping-cli catalog agent suspend

shopping-cli catalog stats
shopping-cli catalog doctor
```

例如：

```bash
shopping-cli catalog agent search \
  --capability example.kiwi.shopping.negotiation \
  --protocol a2a \
  --verified
```

以及：

```bash
shopping-cli catalog agent register \
  --domain merchant.example
```

---

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

# 13. Hosted Commerce Gateway Upgrade

Catalog 新增后，现有 Gateway 不应该删除。

Hosted Gateway 继续负责：

```text
Marketplace Conversation
Resident Merchant Agent
Human Review
claim/heartbeat
message idempotency
audit
merchant auth
buyer auth
legacy consultation
```

新增：

```text
Hosted A2A Endpoint
Hosted Agent Card
Hosted UCP Profile
Kiwi Negotiation Adapter
```

---

# 14. Hosted Agent Publication

每个 Hosted Merchant 可以拥有逻辑 Agent Card。

## 14.1 Shared Host + Agent Path

```text
https://shopping.example/a2a/agents/{agent_id}
```

Catalog record 绑定到 hosted endpoint。

## 14.2 Merchant Custom Domain

如果 Merchant 配置自有 domain：

```text
merchant.example/.well-known/agent-card.json
```

可以代理/跳转到 hosted runtime，但身份验证必须明确。

MVP 推荐 Shared Host，降低 DNS 和证书复杂度。

---

# 15. KNP / Legacy Conversation Adapter

现有 shopping-cli 会话模型：

```text
Conversation
Message
intent
next_actor
claim
human review
```

继续保留。

新增加 Adapter：

```text
KNP Envelope
     ↕
HostedNegotiationAdapter
     ↕
shopping-cli Conversation
```

现有 `quote_request / negotiate / purchase_intent` 等 message intent 不立即删除。

迁移规则：

```text
lossless → adapt
lossy → fail closed
unsupported → human review
```

不得偷偷把 AcceptedNonbindingAgreement 解释成 transaction。

---

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

```text
shopping_cli/
├── core/
│   ├── catalog/
│   ├── conversations/
│   └── agents/
│
├── services/
│   ├── catalog_agents.py
│   ├── agent_registration.py
│   ├── agent_verification.py
│   ├── agent_discovery.py
│   ├── hosted_agents.py
│   └── ...
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
├── catalog/
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
│   └── extension.py
│
├── negotiation/
│   └── knp_adapter.py
│
├── api/
│   ├── handlers/
│   │   ├── catalog_agents.py
│   │   └── ...
│   └── ...
│
└── cli/
    ├── catalog_agents.py
    └── ...
```

---

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

shopping-cli 提供：

```text
ShoppingCliCatalogSource
```

流程：

```text
Kiwi user intent
     ↓
AgentDiscovery.search()
     ↓
shopping-cli Catalog
     ↓
Candidate Agents
     ↓
Kiwi fresh verification
     ↓
Capability Intersection
     ↓
CounterpartyProfile
     ↓
Channel Selection
     ├── A2ADirectChannel
     └── ShoppingCliHostedChannel
```

Catalog 返回 candidate，Kiwi 返回 CounterpartyProfile。

两者职责不能合并。

---

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

# 25. Migration Strategy

## Phase 0 — Preserve and Freeze

在新 Catalog 功能前：

- 保持现有 API / CLI 行为；
- 保持 consultation-only；
- 保持 existing token/audit/idempotency；
- 保持 public/private serializers；
- 不修改现有 Conversation authority。

## Phase 1 — Catalog Foundation

新增：

```text
catalog_agents
agent_endpoints
agent_capabilities
agent_skills
profile_snapshots
agent_verifications
```

新增：

```text
search_catalog_agents
get_catalog_agent
```

Hosted Merchant 自动创建 Catalog entry。

目标：

> shopping-cli 第一次同时拥有 Merchant Catalog 和 Agent Catalog。

## Phase 2 — External Discovery

新增：

```text
register external agent
fetch /.well-known/ucp
fetch Agent Card
verify
refresh/cache
```

加入 SSRF 防护。

目标：

> shopping-cli 可以索引一个自己不托管的 Merchant Agent。

## Phase 3 — Kiwi Integration

实现：

```text
ShoppingCliCatalogSource
```

让 Kiwi AgentDiscovery 可以：

```text
search → candidate → live verify → CounterpartyProfile
```

目标：

> Buyer Kiwi 能通过 shopping-cli 找到独立 Merchant Agent，然后 Direct A2A。

## Phase 4 — Hosted A2A

现有 Resident Merchant Agent 增加：

```text
Hosted Agent Card
Hosted UCP Profile
A2A server endpoint
KNP adapter
```

目标：

> 不具备自建 A2A 能力的 Merchant，也可以由 shopping-cli 托管成为 A2A Merchant Agent。

## Phase 5 — Public Catalog

在准备公网服务前：

- persistence abstraction；
- Postgres option；
- distributed rate limiting；
- verification worker queue；
- observability；
- abuse controls；
- production penetration testing；
- HA；
- catalog moderation / suspension；
- domain claim workflow。

---

# 26. Version Proposal

不建议直接把当前 2.x 跳成“大爆炸 3.0”。

## 2.1 — Agent Catalog Foundation

```text
Catalog tables
Hosted auto-registration
Catalog CLI/API
Search
```

## 2.2 — Discovery & Verification

```text
UCP resolver
Agent Card resolver
verification
cache
external registration
```

## 2.3 — Kiwi Discovery Integration

```text
ShoppingCliCatalogSource
Counterparty candidate contract
fresh verification
```

## 2.4 — Hosted A2A

```text
Hosted Agent Card
Hosted A2A endpoint
KNP adapter
```

## 3.0 — Commerce Agent Network Infrastructure

完成：

```text
Commerce Agent Catalog
+
Hosted Commerce Gateway
+
Direct A2A discovery path
+
public production hardening
```

---

# 27. MVP Definition

第一版最值得做的 vertical slice：

```text
1. Existing Merchant
2. Enable hosted_agent
3. Auto-create Catalog Agent
4. Search Catalog Agent
5. Return Agent Card / capability metadata
6. Kiwi discovers it through ShoppingCliCatalogSource
7. Kiwi obtains CounterpartyProfile
```

然后第二个 slice：

```text
1. Register merchant.example
2. shopping-cli fetches /.well-known/ucp
3. fetches Agent Card
4. validates and indexes
5. Kiwi searches it
6. Kiwi leaves shopping-cli
7. Buyer ↔ Merchant Direct A2A
```

当第二个 slice 跑通时，shopping-cli 才真正从 Gateway 升级成开放 Agent Catalog。

---

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

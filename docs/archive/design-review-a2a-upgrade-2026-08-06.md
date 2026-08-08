# shopping-cli A2A 升级设计评审报告

**评审日期**：2026-08-06
**评审对象**：`docs/shopping-cli-a2a-upgrade-design-v1.md`（v1.0, Proposed Architecture）
**评审方式**：设计文档通读 + 对照当前 HEAD 代码核实事实基线（`db/models.py`、`db/migrations.py`、`api/route_registry.py`、`api/app.py`、`services/negotiation.py`、`core/negotiation.py`、`core/catalog.py`、`core/channels.py`、`cli.py`、`cli_catalog_commands.py`、`references/negotiation-api.md`）

---

## 1. 总体评价

设计的方向与原则是扎实的：**Catalog Is an Index, Not Identity Authority**、**Discovery Must Not Imply Routing**、consultation-only 边界延续、public/private 分离、SSRF/Profile Poisoning 防护、验证状态机、审计与可观测性清单，都与仓库既有的加固路线（`core/limits.py`、显式迁移、幂等、append-only audit）一致，且 §29 What Not To Do 展示了良好的范围自律。

但存在一个影响全局的问题：**文档的现有底座清单写的是升级前的快照，漏掉了已经上线的 Negotiation Gateway**。这导致：

1. Phase 计划高估了"KNP adapter"的增量（实际已大部分完成）；
2. 真正未定的技术核心——Direct A2A 的线协议映射——被留白，而它恰恰是文档自己定义的"从 Gateway 升级成开放 Catalog"的判据；
3. API 版本化、catalog 命名消歧、MVP 与 Phase 的映射关系未处理。

按现状基线修订后，Phase 计划与 MVP 定义需要重排。

---

## 2. 高优先级问题

### 2.1 事实基线漂移：文档没算上已上线的 Authoritative Negotiation Gateway

- **文件**：`shopping_cli/services/negotiation.py`、`shopping_cli/core/negotiation.py`、`shopping_cli/contracts/shopping.negotiation/0.1/*.schema.json`、`api/route_registry.py`（`/negotiation/*` 9 条路由）
- **问题**：HEAD（`94ade8d` "feat: add authoritative negotiation gateway"）已使 shopping-cli 成为 `shopping.negotiation/0.1` 协议的权威 Commerce Gateway：
  - 冻结契约随包发布，`core/negotiation.py:17-19` 明确"运行时绝不依赖 sibling 仓库"；
  - `/capabilities` 广告 `orders: false`；`/negotiation/{pending-messages,claims,snapshot,decisions,heartbeat,abandon-stale}` 全套；token 推导身份、policy gate、角色裁剪快照（`references/negotiation-api.md`）；
  - `agents` 表已含 `capabilities_json` 字段（`db/models.py`），heartbeat/stale 在位。
- **影响**：设计 §15 "新增加 Adapter: KNP Envelope ↔ HostedNegotiationAdapter ↔ Conversation" 和 Phase 4 / 版本 2.4 的 "KNP adapter" 增量项，实际已大部分完成。缺的不是协议适配层，而是 **A2A 传输层（endpoint + Agent Card/UCP 发布）和 Discovery Plane**。
- **建议**：
  - 修订 §0 底座清单，把 negotiation gateway 写入；
  - Phase 4 / 2.4 重定义为"给现有 gateway 包一层 A2A endpoint + 发布 Agent Card / UCP Profile"，而非"新增 adapter"；
  - 对外能力字符串使用代码内真实协议标识 `shopping.negotiation/0.1`。frontmatter 与 §8.1 中的 "Kiwi Negotiation Protocol 1.0" 与代码命名不一致，capability intersection 会对不上。

### 2.2 Direct A2A 线协议映射是最大未定项

- **问题**：§27 第二个 slice（Kiwi 离开 shopping-cli、Buyer↔Merchant Direct A2A）是文档自己定义的"真正升级"判据，但全文未定义：
  - `shopping.negotiation/0.1` 如何在 A2A JSON-RPC 任务里传输？task schema 由谁定？payload 原样嵌套还是映射？
  - §29 说"不要发明另一套 Agent Card"，但未 pin 住采用哪套 schema、哪个版本（A2A 规范 agent-card？Kiwi 自己的 UCP？）。
- **影响**：slice 2 不可交付；Direct A2A（设计的核心差异化路径）依赖此决定。
- **建议**：新增一节"Direct A2A 协议映射"，作为独立协议设计工件（与 `shopping.negotiation/0.1` 冻结契约同级的 schema 随包发布），给出决策人与期限。v1 设计可以留白，但必须显式声明它是独立工件，而不是隐式默认。

### 2.3 Verification 状态机缺证据语义与 TrustPolicy 定义

- **问题**：§6 的 `DOMAIN_VERIFIED / AGENT_VERIFIED` 引用"当前 TrustPolicy 门槛"，但全文未定义 TrustPolicy 是什么、放哪、谁配置、是否按 catalog 版本化。§3.1 "Domain / cryptographic identity remains authoritative" 未选身份机制（DNS TXT？证书？DID？）。
- **建议**：MVP 二选一——对独立 agent，domain-control（DNS/HTTPS 验证）是最低成本路径；文档至少列出候选机制、决策人、TrustPolicy 的配置载体（config 文件 / DB 表 / 版本化 schema）。

### 2.4 API 版本化与 "catalog" 命名冲突未处理

- **问题**：
  - 现有 API 全部无版本前缀（`/search/merchants`、`/agents`、`/negotiation/*`，见 `api/route_registry.py`），设计直接抛 `/v1/catalog/...`，未说明新旧 surface 如何共存，也未说明 fallback ASGI 与 `route_registry.py`（测试/认证依赖的路由元数据源）如何接新路由。
  - "catalog" 一词已被占用：`core/catalog.py` = 商家/商品目录 + FTS/CJK 搜索，`api/handlers/catalog.py` = `/search/*`。§20 模块布局把 Agent Catalog 放在 `shopping_cli/catalog/`，模块树内两个 "catalog" 语义互相干扰。
  - CLI 侧：现有 `search products|merchants|policies` 组（`cli.py:606`），设计只给了 API 层兼容方案（§12），未讨论 `catalog agent search` 与 `search merchants` 并存问题。
- **建议**：包名用 `agent_catalog/`（或等价消歧）；明确 `/v1` 挂载方案与 route_registry 更新路径；补 CLI 兼容说明。

### 2.5 MVP 与 Phase 计划映射不明确

- **问题**：§27 slice 1 第 6 步 "Kiwi discovers through ShoppingCliCatalogSource" 是 Kiwi 侧组件（§21），而 Phase 3 才排 "Kiwi Integration"。MVP 实际横跨 Phase 1–3，但 §25 列为串行阶段。
- **建议**：明说 "MVP = Phase 1 + 2 + 3 的 Kiwi 集成"，并回答 Kiwi 侧 `ShoppingCliCatalogSource` 属于哪个仓库、由谁在何时建——否则 slice 1 最后一步是跨仓库空头支票。

---

## 3. 中低优先级问题

### 3.1 Claim 证明机制未定

§10.4 "domain / account / cryptographic proof" 未定机制。建议按 `source_type` 各列一个候选（hosted → 托管身份即证明；self_registered → domain-control；discovered → claim 后走同一验证）。

### 3.2 商业声誉观察默认可见性未定

§5.7 `local_asserted_dispute` 等商业声誉观察，文档只说与 protocol trust 分开。建议 v1 默认 private-only（世界可见性仅保留验证状态与 freshness）。

### 3.3 外部引用不可寻址

frontmatter `related` 引用的 "Kiwi A2A Agent Commerce Network Architecture Baseline" 与 "Kiwi Negotiation Protocol 1.0" 在本仓库不存在。应注明存放位置或显式标为外部约束。

### 3.4 双规划轴未明确主从

§25（Phase 0–5）与 §26（版本 2.1–3.0）是两条边界略有不同的规划轴。应说明哪条轴驱动路线图承诺（建议：版本轴驱动发布，Phase 轴驱动依赖顺序）。

---

## 4. 确认扎实的部分（无需修改）

- §3 核心原则（catalog=index、discovery≠routing、consultation-only 延续、public/private 分离）；
- §5 领域模型拆分（新增表而非塞进 `agents` 表）、§5.5 snapshot 保留 raw evidence；
- §17 SSRF / Profile Poisoning 防护，与 `core/limits.py` 既有加固文化一致；
- §19.2 Repository 抽象、"MVP 不重写 SQLite 核心"的自律；
- §23 审计事件清单、§24 指标（`catalog_to_connection_conversion` 作为北极星）；
- §28 DoD 21 条、§29 What Not To Do。

---

## 5. 建议的修订动作

1. 基线锚到 HEAD：把 negotiation gateway 写入 §0 清单，重写 §15 与 Phase 4 / 2.4；
2. 新增"Direct A2A 协议映射"独立工件，给决策期限；
3. 明确 TrustPolicy 载体与身份机制的 MVP 选择；
4. 消歧 catalog 命名（`agent_catalog/`），给出 `/v1` 挂载与 route_registry 更新方案；
5. 显式声明 MVP 跨 Phase 1–3 及 Kiwi 侧 source 的仓库归属。

# shopping-cli A2A 升级设计 v1.1 评审报告

**评审日期**：2026-08-06
**评审对象**：
- `docs/shopping-cli-a2a-upgrade-design-v1.1.md`（v1.1, Proposed Architecture）
- `docs/shopping-cli-a2a-binding-1.0-draft.md`（1.0-draft, Protocol Design Work Item）
**评审方式**：文档通读 + 对照当前 HEAD 核实事实基线（`shopping_cli/api/route_registry.py`、`shopping_cli/db/models.py`、`shopping_cli/contracts/shopping.negotiation/0.1/`、`shopping_cli/cli.py`）+ 与 `design-review-a2a-upgrade-2026-08-06.md`（v1.0 评审）逐条对账

---

## 1. 总体评价

v1.1 是高质量的修订：设计评审的 12 条意见**逐条落实**，Appendix C 可核对，没有推诿或假动作。评审 2.1（基线漂移）、2.4（命名消歧 + `/v1` 挂载 + route_registry）、2.5（MVP 跨 Phase + Kiwi 仓库归属）、3.1–3.4 全部到位；`shopping.negotiation/0.1` 与 KNP/1.0 不再混淆，Phase 4/2.4 已如实重定义。binding draft 作为评审 2.2 要求的独立工件**结构性成立**，且 §8 明确声明 "MUST NOT claim interop until normative language + conformance vectors"，没有提前宣称兼容。

两份文档对账一致：§15.3/§15.4 的映射清单与 lossless/lossy/unsupported 规则，在 binding draft §1/§2/§4 原样成立，无冲突。

**剩余问题集中在两处**：binding draft 仍是"决策清单"而非"决策"（12 项无一有倾向性结论或期限）；以及两文档接缝处的几个命名/路径不一致。没有影响方向的问题。

---

## 2. 高优先级问题

### 2.1 binding draft 12 项决策全部悬空，无 owner、无期限、无 tentative 方向

- **文件**：`shopping-cli-a2a-binding-1.0-draft.md` §2
- **问题**：v1.0 评审 2.2 要求"给出决策人与期限"。draft 列出了 12 项必冻结决策，但每一项都只有标题。而文档自身 §3 其实已经隐含了方向（§3.3 倾向 1:1 messageId、§3.1 Data Part 承载 KNP envelope、§3.4 Task 仅作生命周期 transport）——这些倾向没有回填到 §2 决策表。当前 draft 是"待办清单"，下一轮迭代应把 §3 的方向提升为 tentative decisions（带一句 rationale），并为 12 项补 owner + deadline。
- **后果**：无期限的决策清单会无限期停留在 draft 状态，而 v1.1 §27 把 Slice B（Direct A2A）定义为"真正升级"判据——它被这份 draft 阻塞。
- **建议**：§2 每项加 `proposed direction / owner / deadline` 三列；`contextId` 映射、replay 幂等（决策 8）和 capability 标识符（决策 10）优先级最高，因为影响 schema 冻结与跨仓 SDK。

### 2.2 Agent Card schema/版本仍未 pin 住（v1.0 评审 2.2 的剩余一半）

- **问题**：评审明确问"采用哪套 Agent Card schema、哪个版本（A2A 规范 agent-card？Kiwi 自己的 UCP？）"。v1.1 §0.2/§4.3/§5.5 引用 `/.well-known/agent-card.json` 与 `profile_type: agent_card`，binding draft §5 要求 "Hosted Agent Card MUST advertise the A2A interfaces actually supported"——但**采用哪份 schema 规范仍是隐式默认**。§29 "不发明另一套 Agent Card"与"未 pin 住用哪套"之间存在空洞。
- **建议**：在 binding draft §2 决策 10 下明示两个候选（A2A spec 的 agent card / Kiwi 自有 UCP 字段），并给默认值（建议：UCP Profile 承载 commerce 语义、Agent Card 承载 A2A 接口声明，两者都采用 schema version 化），decision 前 Kiwi repo 是唯一决策方。

---

## 3. 中优先级问题

### 3.1 `agent_trust_observations` 表不在 Phase 1 表清单中

v1.1 §5.7 定义了该表且写明 v1 默认 private-only，但 §25 Phase 1 的表清单（`catalog_agents / agent_endpoints / agent_capabilities / agent_skills / profile_snapshots / agent_verifications`）没有它。如果 v1 就要保存私有声誉观察，表应在 Phase 1 出现；否则应显式声明推迟到 2.2。现在两头都说得通，但没有明说。

### 3.2 Phase 1 表名不一致：`profile_snapshots` vs `agent_profile_snapshots`

§25 Phase 1 写 `profile_snapshots`，§5.5 的正式表名是 `agent_profile_snapshots`。其余表全部 `agent_` 前缀，统一掉即可（取 `agent_profile_snapshots`）。

### 3.3 既有 `agents` 行与 `catalog_agents` 的映射机制未定义

DoD #1 要求"Existing Merchant 无损升级为 Catalog Merchant"，§4.1 流程只写了 auto-register。但旧 `agents` 表（含 `capabilities_json`）与新 `catalog_agents` + `agent_capabilities` 是**迁移、外键关联、还是双写同步**？`capabilities_json` 与 `agent_capabilities` 表如何避免双向漂移？Phase 1 应给出 hosted 场景的数据流图（一图即可）。

### 3.4 公开 capability 标识符命名不统一

三处三个样子：§8.2 示例 `"a2a": ["1.0"], "kiwi_negotiation": ["1.0"]`（短名）；§5.3 namespace + capability_id 结构；§11 CLI 示例 `example.kiwi.shopping.negotiation`（`example.` 前缀像占位，但 binding draft §5 说 capability 标识 MUST 是 Kiwi 生产 namespace）。建议统一：公开面一律全限定标识符（短名只作内部查询别名），并把 `example.` 明确标注为占位。

---

## 4. 低优先级问题

- **4.1** §14.2 自定义 domain："身份验证必须明确"一句带过。谁托管该 domain 的 TLS、`/a2a` 路径由谁 serve、hosted identity 如何映射到 merchant domain（否则 DOMAIN_VERIFIED 验证的是哪个 domain）？MVP 已推荐 Shared Host 不阻塞，但应把此项标为 open decision 而非模糊约束。
- **4.2** §10.3/§4.2 出现 "verification worker"，单机 SQLite 阶段 worker 是 in-process 后台任务还是 CLI 触发、refresh 并发与 budget（§17.4）如何落地，未写明。Phase 2 应补一句单机执行模型。
- **4.3** binding draft §6 "No raw Principal Memory is exposed" 是 Kiwi 术语但无定义；独立规范应加一句定义或链接。
- **4.4** §8.2 示例 `"merchant": {"id": "seller-a"}` 是 mai 时代 id 风格；用真实 merchant id 形态更严谨。
- **4.5** 文件路径：v1.1 §0.3/§15.3 规定工件位于 `docs/a2a/shopping-cli-a2a-binding-1.0.md`，实际文件在 `docs/` 根且带 `-draft` 后缀（无 `docs/a2a/` 目录）。建议立即移到 `docs/a2a/`，冻结时去 `-draft`——路径本身也是契约的一部分。

---

## 5. 确认扎实的部分（无需修改）

- Appendix C 12 条逐项落实，v1.0 评审 2.1/2.4/2.5/3.1–3.4 全部闭环；基线描述与 HEAD 完全一致（抽查 4 处事实：`contracts/shopping.negotiation/0.1/` 4 个 schema、`/negotiation/*` 9 条路由 + `_ROUTE_TABLE`、`agents.capabilities_json`、CLI `search products|merchants|policies`）；
- §15.3 映射清单与 binding draft §1–§4 对账无冲突；§15.4 三分类规则两文档一致；
- §6.2 claim proof 按 source_type 固定路径（hosted → 既有身份即证明）；§6 状态机含 STALE/REJECTED/SUSPENDED/UNREACHABLE 收尾态；
- §3.2 "discovery 不隐含 routing + shopping-cli 退出数据路径"、§16 Catalog/Gateway 状态机分离——两条红线写死了；
- §17 SSRF/Profile Poisoning/Secret Policy 与既有 `core/limits.py` 加固文化一致；§19.2 "MVP 不重写 SQLite 核心"；
- §25/§26/§27 三轴关系（Phase=依赖顺序、Version=发布承诺、MVP=跨 Phase 1–3 vertical slice）表述清晰；
- §28 DoD 21 条可测；§29 What Not To Do 保留且新增 binding/conformance 红线。

---

## 6. 建议动作（按序）

1. **binding draft 下一轮**：§2 12 项决策补 `proposed direction / owner / deadline`，把 §3 的倾向回填；决策 8（replay 幂等）与 10（capability 标识）优先。
2. 文件移到 `docs/a2a/shopping-cli-a2a-binding-1.0-draft.md`，与 v1.1 §0.3/§15.3 对齐。
3. Phase 1 表清单补 `agent_trust_observations`（或显式推迟），统一为 `agent_profile_snapshots`。
4. Phase 1 补 hosted 数据流：旧 `agents` ↔ `catalog_agents` 关系 + `capabilities_json` ↔ `agent_capabilities` 同步方向。
5. 统一公开 capability 标识符全限定格式，`example.` 标注占位。

---

**整体结论**：两份文档可以进入实施准备；阻塞项只有 2.1（决策期限）与 2.2（Agent Card schema pin 定），其余是接缝修补。

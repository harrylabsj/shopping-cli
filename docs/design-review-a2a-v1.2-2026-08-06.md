# shopping-cli A2A 升级设计 v1.2 评审报告

**评审日期**：2026-08-06
**评审对象**：
- `docs/shopping-cli-a2a-upgrade-design-v1.2.md`（v1.2, Proposed Architecture）
- `docs/shopping-cli-a2a-binding-1.0-rc1.md`（1.0-rc1, Implementation Candidate / Pre-Freeze）
**评审方式**：与 `design-review-a2a-v1.1-2026-08-06.md` 12 项发现逐条对账 + 两文档交叉一致性检查（事实基线未变，无需重验代码）

---

## 1. 总体评价

上一轮的 **2 个阻塞项全部关闭**，10 个接缝问题中 8 个彻底闭环，2 个半闭环。rc1 是合格的 implementation candidate：决策表有方向、有 rationale、有 owner 角色、有 freeze milestone，门禁 8 条与冻结顺序自洽；v1.2 的修订摘要（Appendix D）诚实可核。**文档层面已无阻塞项，可以进入实施**。

剩余问题全是小的接缝/执行问题——但有一个是修订过程中新引入的笔误，建议在代码开工前修掉，避免扩散进 migration。

---

## 2. 上一轮发现逐条闭环核对

| 上轮发现 | 状态 | 落点 |
|---|---|---|
| 2.1 决策表无方向/owner/期限 | ✅ 闭环 | rc1 §2 Decision Table（12 项全有 Proposed Direction / Rationale / Owner 角色 / Freeze Milestone）+ 优先冻结顺序 D3→D8→D10→D4→… |
| 2.2 Agent Card schema 未 pin | ✅ 闭环 | v1.2 frontmatter `pinned_external_specs` + §0.3 职责分层；rc1 §1/D10；DoD #22/#23；"Kiwi MUST NOT define a competing Agent Card schema" |
| 3.1 trust_observations 不在 Phase 1 | ✅ 闭环 | §5.7 明确标注 v2.2/Phase 2；Phase 1 显式推迟；§26 2.2 收录 |
| 3.2 表名不一致 | ⚠️ 半闭环 | Phase 1 已统一为 `agent_profile_snapshots`，但 §5.5 标题**新引入笔误** `agent_agent_profile_snapshots`（见 3.1） |
| 3.3 agents↔catalog_agents 映射未定义 | ⚠️ 半闭环 | "Hosted Runtime → Catalog Projection"一节定义单向 projection，方向正确；但投影用的 `hosted_runtime_agent_id` 未进 §5.1 字段表（见 3.2） |
| 3.4 capability 标识命名不统一 | ✅ 闭环 | §8.2 全限定标识符数组 + 短名仅内部 alias + `EXAMPLE_ONLY.*` MUST NOT ship + DoD #25 |
| 4.1 custom domain 模糊 | ✅ 闭环 | §14.2 明确 Deferred + 4 个待决问题列出 + MVP 固定 Shared Host identity |
| 4.2 worker 执行模型未定 | ✅ 闭环 | Phase 2 固定 bounded in-process queue + 并发 budget；CLI 手动触发同 service |
| 4.3 Principal Memory 未定义 | ✅ 闭环 | rc1 §6 改为 `principal-private state` 并给出完整定义 |
| 4.4 seller-a 示例 | ✅ 闭环 | 改为 `mrc_01J...` / `cagt_01J...` |
| 4.5 文件路径 | ⚠️ 半闭环 | v1.2 Appendix D#12 声明了 `docs/a2a/` 正式路径，但**磁盘上文件仍在 docs/ 根**（见 3.4） |

---

## 3. 新发现（按优先级）

### 3.1 §5.5 标题笔误：`agent_agent_profile_snapshots`

- **位置**：`shopping-cli-a2a-upgrade-design-v1.2.md` §5.5 标题
- **问题**：本轮修订的主题之一就是统一表名（Appendix D#5），Phase 1 清单已写 `agent_profile_snapshots`，但 §5.5 标题写成了双前缀 `agent_agent_profile_snapshots`。两处又对不上了，只是方向反了。这是**会直接扩散到 migration/schema 的笔误**，开工前必须改回 `agent_profile_snapshots`。

### 3.2 `catalog_agents.hosted_runtime_agent_id` 不在 §5.1 字段清单中

- **位置**：v1.2 §25 Phase 1 投影小节（`catalog_agents.hosted_runtime_agent_id` ← `agents.id`）vs §5.1 字段表
- **问题**：投影机制定义了新字段（指向 `agents.id`），但 §5.1 的 `catalog_agents` 字段表没有它（只有 nullable `merchant_id`）。字段表和投影机制应当同源；否则建表时会漏。
- **建议**：§5.1 补 `hosted_runtime_agent_id nullable FK → agents.id`（仅 hosted 来源非空），并在 §5.1 注明与其他 FK 的互斥关系（hosted 时 merchant_id 与 hosted_runtime_agent_id 的关系也值得一句话）。

### 3.3 §15.2 残留 v1.1 自引用

- **位置**：`shopping-cli-a2a-upgrade-design-v1.2.md` §15.2："shopping-cli **v1.1** 设计不把 `shopping.negotiation/0.1` 重命名成 KNP/1.0"——这是 v1.2 文档，应写 v1.2。文本复制时的残留，修一处即可。

### 3.4 磁盘布局与文档声明不一致

- **问题**：v1.2 §0.3/§15.3/Appendix D#12 说正式工件在 `docs/a2a/shopping-cli-a2a-binding-1.0.md`，但磁盘上是 `docs/shopping-cli-a2a-binding-1.0-rc1.md`（根目录），且已被取代的 `shopping-cli-a2a-binding-1.0-draft.md` 仍并存。读者无法从文件系统判断哪个是现行版本。
- **建议**：立即把 rc1 移到 `docs/a2a/`（保留 `-rc1` 后缀），删除或标注 superseded 的 draft；冻结时按 rc1 §8 门禁去掉 `-rc1`。设计文档里同时更新 v1.1/v1.2 旧版文件是否保留由你们定，但现行工件路径必须与文档一致。

### 3.5（接受即可）§9 空壳节

§9 保留为 "retained only for numbering compatibility" 的占位。功能上无害、意图明确，但属于重排的残余——下次大改时直接 renumber 掉即可，不必现在处理。

---

## 4. 确认扎实的部分

- **rc1 §2 决策表质量高**：D3（contextId 不派生自 negotiation_id）、D8（`(sender_identity, message_id, digest)` 幂等键 + 同 ID 异 digest fail closed）、D9（`reconciliation_required` 状态、不自动生成新承诺）都是正确且可实施的规范级决策；冻结顺序 D3→D8→D10 与 §8 门禁 1–3 对应关系清晰。
- **rc1 §3.5 错误四分类**（transport / protocol / commercial Decline / Task terminal）堵住了"传输失败被误报为商业拒绝"这类真实错误；v1.2 §15.4 的 lossless/lossy/unsupported 与之一致。
- **单向 projection 方向正确**：`agents.capabilities_json` → publication policy → Agent Card/UCP → 验证 → `agent_capabilities`，"不得反向覆盖"写死，从根上避免双源漂移。
- **v1.2 §8.2 契约升级到位**：`protocols: {"a2a": ["1.0.0"], "ucp": ["2026-04-08"]}` 与 pin 一致，`EXAMPLE_ONLY.*` 双处标注 MUST NOT ship。
- rc1 门禁 8 条全部可测；"Implementation MUST NOT claim interop before gates pass" 依然站得住。

---

## 5. 建议动作（按序）

1. 修 §5.5 标题 `agent_agent_profile_snapshots` → `agent_profile_snapshots`（开工前）。
2. §5.1 补 `hosted_runtime_agent_id` 字段及与 `merchant_id` 的关系说明。
3. 删/标 superseded `shopping-cli-a2a-binding-1.0-draft.md`，rc1 移入 `docs/a2a/`。
4. §15.2 的 "v1.1" → "v1.2"。
5. （可选）§9 空壳留给下次 renumber。

---

**整体结论**：两份文档达到实施门槛。阻塞项 0；必改项 3.1（笔误）与 3.2（字段表补齐）应在本轮修订内完成，3.3/3.4 是执行动作。

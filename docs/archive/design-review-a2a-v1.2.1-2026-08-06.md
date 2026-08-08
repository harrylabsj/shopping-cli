# shopping-cli A2A 升级设计 v1.2.1 评审报告

**评审日期**：2026-08-06
**评审对象**：`docs/shopping-cli-a2a-upgrade-design-v1.2.1.md`（v1.2.1, Implementation-Prep Patch）
**评审方式**：与 `design-review-a2a-v1.2-2026-08-06.md` 5 项发现逐条对账 + 与 rc1（未变更）交叉一致性检查

---

## 1. 总体评价

这是一次干净的实施前补丁：不改架构方向，只修接缝。上一轮 5 项发现**4 项在文档层面全部闭环，1 项（磁盘布局）只落了文档声明、未落文件系统**。文档内容本身已无阻塞项、无必改项——**可以进入 Phase 1 实施**。

---

## 2. 上一轮发现逐条核验

| v1.2 发现 | 状态 | 核实 |
|---|---|---|
| 3.1 §5.5 笔误 `agent_agent_profile_snapshots` | ✅ 闭环 | 标题已改回 `agent_profile_snapshots`（§5.5），与 Phase 1 表清单一致，两处对账无差 |
| 3.2 `hosted_runtime_agent_id` 缺字段 | ✅ 闭环（超预期） | §5.1 已补 `nullable FK -> agents.id`，且给出完整语义：source_type=hosted → 应为非空、非 hosted → 应为空；**并明确 merchant_id（ownership）与 hosted_runtime_agent_id（runtime instance）不互斥、一 Merchant 可多 runtime、禁止仅凭 merchant_id 推断唯一 runtime**。这个"不互斥"的处理比评审建议的"互斥关系"更正确——它直接堵住了真实建模坑（一个商家多个 runtime 的情形），方向值得肯定 |
| 3.3 §15.2 残留 v1.1 自引用 | ✅ 闭环 | 已改为 "shopping-cli v1.2.1 设计…" |
| 3.4 磁盘布局与声明不一致 | ⚠️ 部分闭环 | 文档已声明正确路径（§0.3、Appendix D#12、E#5）并声明 draft 已 superseded（E#6），**但文件系统未执行**：rc1 仍在 `docs/` 根、`docs/a2a/` 不存在、draft 文件原样保留且无 superseded 标注（见下 §3.1） |
| 3.5 §9 空壳节 | ✅ 闭环 | Appendix E#7 显式记录"留待下一次结构性重排"——处理决策已文档化 |

---

## 3. 新发现

### 3.1（唯一未闭环项，执行动作）磁盘与文档声明脱节

- **位置**：`docs/` 目录 vs v1.2.1 §0.3 / Appendix D#12 / Appendix E#5-6
- **问题**：文档三处声明"正式路径为 `docs/a2a/shopping-cli-a2a-binding-1.0-rc1.md`"、"draft 已标记 superseded 并从发布文档包移除"，但磁盘上：rc1 文件仍在根目录、`docs/a2a/` 不存在、draft 文件既未删除也未在 frontmatter 加 superseded 标注。
- **性质**：这不是文档内容问题，是执行项——但它是当前唯一挡住"文档与工作区事实一致"的事项。一次 `mkdir docs/a2a && mv` + draft 标注即可完成。

### 3.2（低）§15.3 与 §0.3 对同一工件给出两个文件名，关系未明说

- **位置**：§15.3 写 `docs/a2a/shopping-cli-a2a-binding-1.0.md`（冻结名），§0.3 / Appendix E#5 写 `-rc1`（当前文件名）。
- **性质**：可以共存（当前 rc1，冻结时按 rc1 §8 门禁去后缀），但两处没有一句话点明"rc1 是当前文件、1.0 是冻结后名"。读者会疑惑。加一行即可，例如 §15.3 末尾："（当前版本为 `shopping-cli-a2a-binding-1.0-rc1.md`，通过 rc1 §8 门禁后去掉 `-rc1` 后缀。）"

### 3.3（可选，设计细节）`hosted_runtime_agent_id` 的 SHOULD 可考虑收紧为条件 MUST

- **位置**：§5.1 语义段 "source_type = hosted → SHOULD be non-null"
- **性质**：SHOULD 留下了"hosted 但无 runtime 指针"的合法空洞——Hosted Agent 的 verification/refresh/Agent Card 生成都依赖该指针。如果担心迁移过渡态（entry 先于 runtime 存在），可以写成 "MUST be non-null at time of publication / COMMERCE_VERIFIED"，既保留过渡态又收紧发布态。不改也不阻塞，属于实现时的约束细化。

---

## 4. 确认扎实的部分

- **§5.1 语义段质量高**：三行话把 ownership / runtime instance / 多 runtime 三种情况讲完，且附 MUST NOT 反例（不靠 merchant_id 推断唯一 runtime）——这是本轮补丁最有价值的增量。
- **修订摘要链完整**：Appendix C（v1.1）/ D（v1.2）/ E（v1.2.1）三层，每层都标明了吸收的是哪份评审，可审计。
- 补丁严格限定在接缝问题内，未借机改动架构内容——范围自律保持住了。
- 与 rc1（未变更）交叉检查：无冲突，pin 的 A2A v1.0.0 / UCP 2026-04-08 两处一致。

---

## 5. 建议动作（按序）

1. **执行 3.1**：建 `docs/a2a/`，把 rc1 移入（保留 `-rc1` 后缀）；draft 删除或 frontmatter 标 superseded。
2. §15.3 加一行"当前 rc1 / 冻结去后缀"的说明（3.2）。
3. （可选）§5.1 的 SHOULD 收紧为发布态 MUST（3.3），实现时定即可。

---

**整体结论**：文档达到实施门槛，阻塞项 0，文档内必改项 0；剩一个文件系统执行动作（3.1）和一句说明（3.2）。

---

## 6. 执行记录（2026-08-06 当日）

以下建议动作已在评审当日执行完毕：

1. ✅ `docs/a2a/` 已创建，rc1 移入 `docs/a2a/shopping-cli-a2a-binding-1.0-rc1.md`；
2. ✅ `shopping-cli-a2a-binding-1.0-draft.md` frontmatter 标记 `status: Superseded` + `superseded_by: docs/a2a/shopping-cli-a2a-binding-1.0-rc1.md`；
3. ✅ v1.2.1 §15.3 补充当前 rc1 / 冻结去后缀说明（v1.2.1.md:1273）；
4. ✅ v1.2.1 §5.1 `hosted_runtime_agent_id` 收紧为发布态（COMMERCE_VERIFIED）MUST（v1.2.1.md:504-508）；
5. ✅ v1.2.1 Appendix E 追加第 8、9 条修订记录（v1.2.1.md:2221-2222）。

至此 v1.2.1 评审的全部建议已闭环，docs 文档链与文件系统一致。

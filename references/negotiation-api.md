# shopping.negotiation/0.1 Commerce API

shopping-cli 2.x 是 `shopping.negotiation/0.1` 协议的权威 Commerce Gateway（LocalMarketplace 后端）。
Kiwi（或任何双边磋商 Agent runtime）只通过本 HTTP API 访问电商事实与写入口；不允许直连 SQLite，
不允许绕过策略门写消息。本文档是该协议在 shopping-cli 侧的权威对接说明。

## 硬边界（no-order boundary）

- 磋商只形成**非约束性共识**：不创建订单、不支付、不锁定或扣减库存。
- `capabilities.capabilities.orders` 恒为 `false`；decision schema 中 `stock.reserved` 恒为 `false`。
- 不新增 proposal/order 业务表；结构化 proposal 保存在 message 的 `structured_payload`，生命周期写入 audit events。
- 即使双方都提交 `accept_nonbinding`，也只是咨询共识，不是合同。

## 冻结契约

- schema 的权威副本：`shopping_cli/contracts/shopping.negotiation/0.1/*.schema.json`（随包发布，运行时不依赖任何 sibling 仓库）。
- 跨语言 fixtures：`fixtures/negotiation/*.json`，由 `tests/test_negotiation_contracts.py`（Python 内置校验器）与 Kiwi 侧 Ajv 测试共同校验。
- `additionalProperties: false` 严格执行；未知字段、错误枚举、`reserved: true`、错误 `protocol_version` 都是 400。

## 端点

所有响应使用仓库统一信封：成功 `{"ok": true, ...}`（HTTP 200），失败 `{"ok": false, "error": "..."}`
（400/403/404/405/409/413/429/500）。认证：`Authorization: Bearer <token>`。角色与 owner 永远由
token 推导，**客户端声明的 `merchant_id`/`role`/`owner_id` 一律被忽略**。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/capabilities` | 协议与 capability 广告。响应 `{"ok": true, "capabilities": {...}}`，内层对象严格符合 `capabilities.schema.json`（`orders: false`）。无需认证。 |
| GET | `/negotiation/pending-messages` | 当前 token 角色的待处理消息：`next_actor` 属于本方且最后一条对方消息尚未被本 agent 处理（processing/processed 之外）。buyer token 绑定单会话，merchant 看到自己全部 `waiting_merchant` 会话。响应 `{"ok": true, "role", "owner_id", "pending": [{conversation_id, message_id, conversation_status, sender_role, preview, created_at}]}`。 |
| POST | `/negotiation/claims` | 领取消息。body `{conversation_id, message_id, idempotency_key}`。校验会话归属、轮次（next_actor）、对方消息且为最新一条。复用 `agent_message_processes`，幂等：重复 claim 返回 `claimed: false`，`failed/abandoned` 可重领。 |
| GET | `/negotiation/snapshot?conversation_id=&message_id=` | 角色裁剪的权威快照，严格符合 `snapshot.schema.json`。要求该消息处于本 agent 的 `processing` claim 中。不含 merchant 私有底价（`automation_boundaries`）或任何私有阈值；库存带 `observed_at`/`source`/`reserved: false`，不制造预留。 |
| POST | `/negotiation/decisions` | 唯一写入口。body `{idempotency_key, decision}`，`decision` 严格符合 `decision.schema.json`。响应 `{"ok": true, "policy_result": {...}}`，内层严格符合 `policy-result.schema.json`。 |
| POST | `/negotiation/claims/complete` | body `{message_id}`。claim → `processed`。 |
| POST | `/negotiation/claims/fail` | body `{message_id, error?}`。claim → `failed`（可重领）。 |
| POST | `/negotiation/claims/abandon` | body `{message_id, error?}`。claim → `abandoned`（可重领）。 |
| POST | `/negotiation/claims/heartbeat` | body `{message_id?}`。刷新本 actor 自己处于 `processing` 的 claim 的 `updated_at`（提供 message_id 时仅该条，且须属于 token 绑定的会话）；不触碰已结算或他人 claim，不复活 stale 工作。响应 `{"ok": true, "heartbeat": {"status": "ok", "refreshed": <count>, "at": <iso>}}`，并写 `agent_message_heartbeat` 审计。 |
| POST | `/negotiation/claims/abandon-stale` | body `{ttl_seconds?}`（默认 300，上限 86400；只接受正整数/整数值，bool、小数、非正值、超限、非数字字符串一律 400，不做静默纠正）。只 abandon 本 actor 自己的 stale `processing` claim（buyer 仍受会话绑定约束），用于崩溃恢复；每条写 `agent_message_abandoned`（reason `stale_processing_claim`）审计，可重领。响应 `{"ok": true, "stale": {"abandoned": <count>, "message_ids": [...], "ttl_seconds": <n>, "at": <iso>}}`。 |

旧的 `POST /agents/messages/claim|complete|fail|abandon|abandon-stale` 保持向后兼容；
stale claim 恢复复用现有 `abandon-stale` 机制（merchant agent daemon 按 TTL 自动执行）。
运行时（如 Kiwi）在新进程启动/每轮开始时先调 `abandon-stale` 恢复本身份 stale claim，再在
claim 存活期间周期性 `heartbeat`；顺序必须是先恢复后心跳，绝不能用心跳复活 stale 工作。

## token 与角色映射（fail closed）

| token role | 协议角色 | claim 身份（服务端推导） |
| --- | --- | --- |
| `merchant`（绑定 merchant_id） | merchant | `shopping-cli-merchant-agent:{merchant_id}` |
| `agent`（绑定 merchant_id + 默认 agent_id） | merchant | 同上 |
| `buyer`（绑定 buyer_id + conversation_id） | buyer | `shopping-cli-buyer-agent:{buyer_id}` |

未知、吊销、过期、越租户的 token 一律 403。buyer token 只能看到/操作其绑定的会话。

## 决策策略门（固定顺序）

1. 冻结 schema 校验（decision）。
2. 会话存在 + token 身份/租户归属。
3. **幂等重放检查**（在轮次/claim 检查之前）：同 key 同 payload → 返回首次 accepted 结果（含原 `message_id`），不重复写消息；同 key 不同 payload → 409 fail closed。重放只命中本 actor（`agent_id`）+ 本会话已写入且 payload 逐字节相同的 decision，绝不产生新写入；不同 token/owner 无法借重放读到他人结果。
4. `next_actor` 属于当前角色，否则 409。
5. `in_reply_to_message_id` 是对方消息且处于本 agent 的 `processing` claim 中，否则 409。
6. 角色策略门（用最新 catalog 数据重查）：
   - 双方：`propose`/`counter` 必须带 proposal；SKU 与会话一致且属于该 merchant；币种一致；数量 ≤ 当前库存；proposal 携带的库存观察（`stock.quantity`/`stock.status`）必须与服务端最新库存及其映射状态一致（不一致 → `stale_inventory`，不写消息，防止伪造库存进入公开结构化消息）；`observed_at`/`valid_until`/配送 ETA 必须是带时区的合法 RFC 3339（schema 阶段即拒绝 naive 时间）；`valid_until` 未过期；售后引用必须存在于该 merchant 的公开政策（`policy:{code}`）；`public_message` 非空。
   - merchant 额外：库存观察时间新鲜（默认 900 秒，过期 → `stale_inventory`）；单价不得低于 `automation_boundaries` 中授权的最低成交价（低于 → `below_floor` 转人工）；无授权规则时不得报低于目录价（→ `unauthorized_discount` 转人工）；`public_message` 不得泄露私有底价（阈值词 + 底价数值 → `private_threshold_leak` 转人工；等于底价的正常报价且无阈值语义时可自动接受）。
   - buyer 额外：proposal 是非绑定意愿，仅结构/事实/时效校验；但 accepted proposal 同样写入公开结构化消息，因此库存观察一致性对 buyer 同样强制；私有预算保存在 Kiwi profile，服务端不可见也不接收。
7. `action: escalate` 或 `request_human_review: true` → `human_required`：写 moderation flag + `negotiation_human_required` 审计，**不写消息**。
8. `rejected_retryable`：只写 `negotiation_policy_denied` 审计，业务状态不变，`retries_remaining` 按 claim 次数递减（每次 claim 最多 3 次尝试）。
9. `accepted`：单事务原子写入结构化 message（`structured_payload` 携带 `protocol_version`/`idempotency_key`/`agent_id`/`role`/`decision`）、推进 `status`/`next_actor`、写 `negotiation_decision_submitted` + `negotiation_policy_accepted` 审计。`decline` 关闭会话并吊销 buyer token。

动作 → 状态迁移：`ask`/`propose`/`counter`/`accept_nonbinding` → 等待对方；`decline` → `closed`；
`escalate`/策略人工 → `human_required`。被拒绝的决策不产生任何部分写入。

## Kiwi 对接要点

- 启动时调用 `GET /capabilities`；缺少 `shopping.negotiation/0.1` 或所需 capability 时 fail closed。
- 单轮流程：`pending-messages` → `claims` → `snapshot` →（模型推理）→ `decisions` → accepted 后 `claims/complete`；retryable 用相同 claim 有限重试；human/fatal 走 `claims/fail` 或 `claims/abandon`。
- 幂等键约定：`{agent_id}:{message_id}:shopping.negotiation/0.1`。写入响应丢失时用相同 key 重交 `decisions`，服务端返回首个结果而不重复发言。
- `policy_result.public_reason` 可安全展示给模型；绝不包含私有阈值。

## 已知限制（0.1）

- buyer token 绑定单会话：buyer 的 `pending-messages` 最多覆盖该绑定会话；跨会话聚合需要每个会话一个 token。
- merchant 私有底价仍保存在自由文本 `automation_boundaries` 中（复用现有正则解析）；没有结构化底价/折扣字段。
- `open` 状态（next_actor=buyer）下 buyer 首条消息仍走现有 `POST /conversations/{id}/messages`；本协议端点覆盖领取之后的磋商轮次。
- 快照配送 ETA 由 merchant delivery rule 的 `eta_minutes` 推导观察窗口，不是平台实时承诺。
- stale claim 恢复现已有协议端点（`claims/abandon-stale`，actor 自身份范围）供运行时主动调用；merchant agent daemon 的 TTL/abandon-stale 机制仍作为兜底。
- `accept_nonbinding` 不改变会话路由状态语义（仅等待对方），共识达成后的关闭由任一方 `decline` 或人工 close 完成。

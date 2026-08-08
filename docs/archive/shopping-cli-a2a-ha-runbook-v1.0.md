# HA Runbook v1.0

- 设计出处：`docs/shopping-cli-a2a-upgrade-design-v1.2.1.md` §25 Phase 5（HA）、§19（Persistence Strategy）
- 范围：v3.0 / Phase 5 收尾（部署与运营工件）。工作区文档，不入库。
- 更新：2026-08-06

## 1. 目标

本文件回答：shopping-cli 公网化时「高可用」意味着什么、当前单节点能做什么、
多实例需要什么前提。**当前代码是单节点设计**（SQLite 单写者 + 进程内队列 +
进程内限流计数）——HA 是多实例路径的目标，不是今天就能切过去的开关。

## 2. 单节点现状（诚实基线）

| 组件 | 现状 | 单写者/单进程约束 |
| --- | --- | --- |
| 存储 | SQLite（`db/session.py`，busy timeout 串行写） | **单写者**：多进程共享同一 SQLite 文件不安全（尤其网络文件系统），多实例必须 PG（见 P3 接缝文档阶段 1） |
| verification queue | 进程内调度 + **SQLite ledger 写穿**（P4，schema v15） | **单进程消费**：ledger 只保证重启不丢任务（crash recovery），不提供跨实例互斥；多实例共享 ledger 时同一任务可能被两个实例执行——task kind 全部幂等（verify/refresh/mark_stale/suspend 重跑无重复副作用），浪费但不损坏；multi-node 换 distributed queue（P4 决策） |
| 限流 | 固定窗口计数器，SQLite backend（P5） | **单写者安全**：SQLite 串行化保证原子自增；多实例需 Redis backend（P5 接缝） |
| 幂等（catalog write / a2a ledger） | DB 层 claim/replay（ON CONFLICT） | **多实例共享 DB 时安全**：唯一约束在 DB 侧，两个实例并发 claim 只有一个成功 |
| hosted gateway | JSON-RPC 端点（v2.4），a2a ledger 幂等 | 多实例共享 DB 时安全（同上） |
| 验证 worker | 每任务独立连接（`make_verification_worker`） | 多实例并发 verify 同一 agent：状态机转换 + §23 audit 幂等，不会损坏状态 |

**结论**：多实例 HA 的前置是 §19.2 阶段 1（PG adapter + Discovery Plane 独立持久化）。
在此之前，HA 指的是**单节点可靠性**——见 §3。

## 3. 单节点可靠性实践（现在就能做）

### 3.1 进程守护与崩溃恢复

- 用 systemd（Linux）/ launchd（macOS）守护 API 进程与 worker；
- **崩溃后重启即自动恢复**：P4 的 ledger 会把 pending/running 任务重新入队，
  无需人工干预（验证过：新实例 `drain()` 完整重跑遗留任务）；
- 恢复语义：任务可能被重跑（幂等兜底），不会丢失。

### 3.2 备份

- SQLite 文件整体备份 + 业务表导出。增量考虑：`verification_queue_tasks` /
  `a2a_inbound_idempotency` / `agent_catalog_write_idempotency` 是幂等账本，
  备份恢复后重放安全（claim/replay 语义在 DB 侧）；
- 建议启用 WAL 模式降低读写互斥（`PRAGMA journal_mode=WAL`，备份用
  `.backup` API 而非文件拷贝）。

### 3.3 健康检查与信号

- `/health` 端点（已存在）——进程存活探测；
- §24 runtime metrics（P1）：`verification_queue_depth` 持续满 → worker 故障；
  `profile_fetch_error_rate` 飙升 → 出站路径异常；
- audit 事件（§23）作为审计轨迹。

### 3.4 容量边界（单节点硬上限）

- 队列 `max_pending`（默认 100）——超限 fail-closed 返回 429 类错误；
- 限流 60/分钟（写路由）与 20/小时（domain）——防 abuse 也是防过载；
- 出站 fetch 是唯一外部依赖——其超时（默认 10s）与错误率是单节点
  可靠性的主信号。

## 4. 多实例 HA 路径（阶段 1+，前置条件）

依赖 P3 接缝文档的迁移阶段：

| 阶段 | 动作 | HA 收益 |
| --- | --- | --- |
| 1 | PG adapter（Discovery Plane 独立持久化） | 多实例共享存储；SQLite 单写者约束解除 |
| 2 | Redis 限流 backend（P5 `RateLimitBackend` 接缝） | 跨实例原子限流 |
| 3 | distributed queue（替换 P4 进程内调度） | 跨实例任务分派与互斥 |
| — | hosted gateway 多实例 | 共享 a2a ledger 幂等已就绪 |

部署形态（阶段 1+）：

```text
LB → API x N（无状态；幂等/限流/ledger 都在 DB/Redis）
         │
    PG 主（+只读副本可选）
         │
    worker x N（distributed queue 消费；每任务独立连接）
```

failover 原则：所有「写」路径依赖 DB 原子性（ON CONFLICT / 唯一约束），
不依赖进程内状态——因此任意实例崩溃不影响一致性；客户端通过幂等键重试。

## 5. 非目标

- 本阶段不做 active-active 写（SQLite 时代无意义）；
- 不做跨区域部署；
- 不做 PG 主备自动切换的编排（属部署平台职责，非本仓）。

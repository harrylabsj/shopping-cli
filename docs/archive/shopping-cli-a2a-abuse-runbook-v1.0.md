# Abuse Controls Runbook v1.0

- 设计出处：`docs/shopping-cli-a2a-upgrade-design-v1.2.1.md` §17.4（Registration Abuse）、§10.2–§10.4
- 范围：v3.0 / Phase 5 的 abuse controls（P5）。工作区文档，不入库（与既有设计文档一致）。
- 更新：2026-08-06

## 1. 威胁模型

**主威胁**：利用公开面做大规模 SSRF scanner——攻击者通过 `POST /v1/agent-catalog/agents/register`
提交任意 `domain`，服务端会解析 DNS 并 fetch `.well-known/agent-card.json` / `ucp`
（SSRF 保护在 `discovery/fetcher.py`，但 fetch 本身消耗出站流量）。

**其他威胁**：
- 高频刷新（已认证 merchant 对自己的 agent 反复 refresh → 出站 fetch 放大）；
- 用同一 domain 反复注册探测（绕过 per-actor 限流时以 domain 维度防住）；
- 批量注册垃圾 catalog entry（catalog 污染）。

## 2. 滥用维度矩阵（P5 盘点结论）

| 路由 | 认证面 | 滥用维度 | 限流 | 幂等 |
| --- | --- | --- | --- | --- |
| `register` | 公开（无 token） | **actor 分钟 + domain 小时（双维度）** | 60/分钟 + 20/小时 | ✅ request hash |
| `refresh` | owner merchant / admin / worker | actor 分钟 | 60/分钟 | ✅ |
| `verify` | 同 refresh | actor 分钟 | 60/分钟 | ✅ |
| `claim` | merchant / admin（+domain challenge） | actor 分钟 | 60/分钟 | ✅ |
| `suspend` / `reinstate` | **admin-only**（P2） | actor 分钟 | 60/分钟 | ✅ |
| buyer bootstrap | buyer token | 独立预算（`buyer_bootstrap_rate_limits`） | 60/分钟 | ✅ |

**盘点结论**：注册双维度、其余写路由 actor 维度——**无缺口**。refresh/verify 的
fetch 放大面被「认证门槛」（merchant owner 只能刷新自己的 agent）挡住，公开
SSRF scanner 只能走 register，而 register 已有 domain 小时维度封顶；因此
refresh 不需要 domain 维度（认证面已等价覆盖，加维度是过度防御）。

阈值 env 配置：`SHOPPING_AGENT_CATALOG_WRITE_RATE_LIMIT_PER_MINUTE`（默认 60）、
`SHOPPING_AGENT_CATALOG_REGISTER_DOMAIN_LIMIT_PER_HOUR`（默认 20）。

## 3. §17.4 六项防御现状

| §17.4 需求 | 状态 | 位置 |
| --- | --- | --- |
| rate limiting | ✅ 已落地（P5 收敛为 `services/rate_limit.py` 单一核心 + 可插拔 backend） | `enforce_rate_limit` / `SQLiteRateLimitBackend` |
| idempotency | ✅ | `api/idempotency.py`（catalog write idempotency 表，claim/replay） |
| per-domain limits | ✅ | `agent_catalog_register_limits`（20/小时） |
| verification queue budget | ⚠️ 部分 | in-process bounded queue（`max_pending` 软约束 + 并发预算）；无跨实例全局预算（multi-node 时随 distributed queue 一起做） |
| cooldown | ❌ 未实现 | 未来：被拒绝（rejected）的 domain 冷却期后允许重试 |
| abuse flags | ❌ 未实现 | 未来：catalog 域 abuse flag（现有 `moderation_flags` 属 conversation 域） |

## 4. 分布式限流接入点（Redis 等）

`services/rate_limit.py` 的 `RateLimitBackend` Protocol 是唯一接缝：

```python
class RateLimitBackend(Protocol):
    def consume(self, *, key: str, window_start: str, limit: int) -> bool: ...
```

Redis 实现（如 `INCR + EXPIRE` 或固定窗口 Lua 脚本）实现同一 Protocol 即可被
`enforce_rate_limit` 使用，无需改动任何调用方。注意三点：
1. 原子性：多实例并发必须原子（Lua / WATCH）；
2. 窗口边界：与 `fixed_window_start` 的 epoch 取模一致（多实例天然对齐）；
3. 故障语义：Redis 不可用时 fail-open（记录日志并放行）还是 fail-closed（拒绝）
   须显式决策——默认建议 fail-open + 告警（限流是防御不是正确性依赖）。

## 5. 监控与响应

监控信号（§24 runtime metrics + 现有 audit）：
- `verification_queue_depth` 持续满 → 攻击或 worker 故障；
- `profile_fetch_error_rate` 飙升 → SSRF 探测或目标域故障；
- 429 频率（write rate limits 表 request_count 峰值）→ 单 actor/domain 攻击；
- audit 事件 `catalog_agent_registered` 密集且 `source_type=self_registered` →
  批量注册尝试。

响应动作（人工）：
1. 用 `SHOPPING_AGENT_CATALOG_WRITE_RATE_LIMIT_PER_MINUTE` 调低 actor 预算；
2. 用 `SHOPPING_AGENT_CATALOG_REGISTER_DOMAIN_LIMIT_PER_HOUR` 调低 domain 预算；
3. 确认恶意 agent 用 `shopping-cli agent catalog suspend <id> --reason abuse` 挂起
   （P2 moderation，admin-only）；
4. 需要永久拉黑时联系运营走 rejected 流程（重新注册不可自动恢复）。

## 6. 非目标（本 Phase）

- cooldown / catalog abuse flags（§17.4 剩余两项）；
- 跨实例全局队列预算（随 distributed queue）；
- Redis 实现本身（只留 Protocol 接缝）。

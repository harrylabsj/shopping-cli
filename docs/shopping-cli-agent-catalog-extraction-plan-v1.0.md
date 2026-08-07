# Agent Catalog 抽离方案 A — 独立部署清单 v1.0

- 范围：把 Agent Catalog 的「发布 → 验证 → 发现/搜索」域抽成独立产品**独立部署**。
- **切割分水岭：不含托管协商（hosted negotiation gateway）**——`/a2a/agents/{id}`（KNP message/send）、conversations/negotiation 域留在 shopping-cli。
- 状态：**阶段 0–4 已全部完成**（2026-08-06）。独立产品落在 `<WORKSPACE>/kiwi-catalog/`（新 git 仓）。阶段 1 的锁修复（enqueue 移出事务）同时回馈了 shopping-cli 本身。

## 1. 目标与边界

```
独立产品：Agent Catalog Service
  ├── 注册/发布（register / hosted 发布面 agent-card.json + ucp）
  ├── 验证（HTTPS domain-control + agent identity + commerce，验证队列）
  ├── 发现/搜索（search / get / candidate DTO）
  ├── 治理（suspend/reinstate、限流、审计、runtime metrics）
  └── 不包含：negotiation 会话、消息、托管协商端点
```

代码事实支撑（2026-08-06 核查）：

- `load_hosted_agent` 只查 catalog 表（`get_catalog_agent_with_merchant`），**不读 `agents` 表**——`hosted_runtime_agent_id` 外键可去（发布逻辑零影响）；
- catalog 服务对 marketplace 的 import 只有两处：`services/tokens`（merchant token 认证）、`core.conversations` + `core.harness`（hosted 协商路径）；
- 11 张 catalog 表内部自引用完整；
- P3 的 `CatalogRepository` 契约、P5 的 `RateLimitBackend`、P4 的持久队列（`verification_queue_tasks`）全部原样带走。

## 2. 新包结构（建议名 `shopping_catalog`，独立 repo）

```text
shopping_catalog/
├── pyproject.toml            # 独立包（FastAPI + 可选 CLI）
├── shopping_catalog/
│   ├── db/
│   │   ├── models.py         # catalog 域 DDL（从 shopping-cli db/models.py 子集复制）
│   │   ├── migrations.py     # catalog 链（v10–v15，见 §5）
│   │   └── session.py        # open_connection / db_session（复制，删 marketplace 依赖）
│   ├── agent_catalog/        # 原样搬走
│   │   ├── repository.py     # P3 契约（含 Conversation/Audit——见 §4 决策点）
│   │   ├── sqlite_repository.py
│   │   ├── search.py / serializers.py / candidate_dto.py
│   ├── discovery/            # 原样搬走（SSRF fetcher / agent_card / ucp / verifier / trust / cache）
│   ├── services/
│   │   ├── agent_catalog.py / agent_catalog_writes.py / agent_verification.py
│   │   ├── agent_trust_observations.py / agent_catalog_metrics.py
│   │   ├── catalog_runtime_metrics.py / rate_limit.py      # P1/P5 原样
│   ├── a2a/
│   │   ├── agent_card.py / ucp_profile.py                  # hosted 发布面（无 hosted_server/knp/binding）
│   ├── api/
│   │   ├── app.py（独立 app，仅 catalog 路由组）
│   │   ├── handlers/（agent_catalog.py / hosted_publication.py）
│   │   ├── idempotency.py（catalog 写幂等 + 限流，删 buyer 侧）
│   │   └── auth.py（独立，见 §6）
│   └── cli_agent_catalog_commands.py（CLI 全组：register/verify/search/stats/doctor/suspend/reinstate/claim）
└── tests/                    # 迁移对应测试（见 §7）
```

## 3. 文件清单（三分类）

### 3.1 原样搬走（零改动）
- `shopping_cli/agent_catalog/` 全部（repository/sqlite_repository/search/serializers/candidate_dto）
- `shopping_cli/discovery/` 全部（fetcher/agent_card/ucp/verifier/trust/cache/_validation）
- `shopping_cli/services/agent_verification.py`（含 VerificationQueue P4 持久队列）
- `shopping_cli/services/agent_trust_observations.py`、`agent_catalog_metrics.py`、`catalog_runtime_metrics.py`、`rate_limit.py`
- `shopping_cli/a2a/agent_card.py`、`ucp_profile.py`
- `shopping_cli/services/agent_catalog.py`、`agent_catalog_writes.py`（认证注入点除外，见 §6）

### 3.2 改造搬走
| 文件 | 改造点 |
| --- | --- |
| `db/models.py`（子集） | 只保留 11 张 catalog 表 + audit 表；`catalog_agents` 去掉两个外键（merchant_id、hosted_runtime_agent_id → 弱引用） |
| `db/migrations.py`（子集） | 只保留 v10–v15 链（§5）；CURRENT_SCHEMA_VERSION 从 15 改为 6（catalog 链自己的版本号） |
| `api/handlers/agent_catalog.py` | auth 从 `_require_catalog_write_auth`（merchant token）改为独立 auth（§6） |
| `api/auth.py` | 保留 admin token；删 merchant/buyer token 设施（或保留 admin + catalog-owner 两种） |
| `api/idempotency.py` | 删 buyer 侧（buyer_bootstrap/merchant_bootstrap）；保留 catalog 写幂等 + 限流 |
| `api/handlers/hosted_publication.py` | 保留（agent-card.json/ucp 发布是 catalog 域能力）；`merchant_public_ref` 的 JOIN 改为影子表 |
| `cli_agent_catalog_commands.py` | 保留全部 catalog 命令；`_cli_actor` 的 merchant token 分支改为 catalog-owner token |

### 3.3 留在 shopping-cli
- `a2a/hosted_server.py`、`knp.py`、`binding.py`（托管协商 + KNP 绑定）
- `core/negotiation.py`、`core/conversations.py`、`core/harness.py`、`core/catalog.py`（marketplace 域）
- `services/negotiation.py`、`conversations.py`、`agents.py`、`buyer_bootstrap.py`、`human_review.py`、`audit.py`、`tokens.py`
- `api/handlers/`：negotiation.py、conversations.py、agents.py、buyer.py、catalog.py、human_review.py、hosted_a2a.py
- marketplace 表（merchants/products/conversations/messages/agents/api_tokens 等）

## 4. 数据层解耦（3 个点）

| 解耦点 | 现状 | 动作 |
| --- | --- | --- |
| **merchant 影子表** | `catalog_agents.merchant_id → merchants`（FK + JOIN 取 public ref） | 新库建 `catalog_merchants`（id/name 等 public 字段子集）；`get_catalog_agent_with_merchant` 改 JOIN 影子表；`claim` 的 merchant 概念用影子表 owner 表达 |
| **hosted_runtime 弱引用** | `catalog_agents.hosted_runtime_agent_id → agents`（FK） | 去 FK，保留 id 字符串（发布逻辑只依赖 `source_type=hosted`，已验证不读 agents 表） |
| **audit 独立** | `audit_events` 与 marketplace 共用 | 新库自带 `catalog_audit_events`（catalog 写路径的 audit 从 `core.harness.append_audit_event` 改为独立表 + 独立写入函数） |

## 5. Migration 拆分

- 建表：`db/models.py` 中 catalog 域 DDL 复制为新库 init 表（11 张 catalog 表 + `catalog_merchants` + `catalog_audit_events`）；
- 增量链：shopping-cli migrations v10–v15 原样搬运，版本号重排为 1–6：
  - v1 = agent_catalog（6 表）、v2 = register_limits、v3 = write_idempotency + write_rate_limits、v4 = trust_observations、v5 = a2a_inbound_idempotency、v6 = verification_queue_tasks；
- 现有 shopping-cli 的 v15 保持不变（两库从此各自演化，schema 版本独立）。

## 6. 认证改造

独立库无法复用 merchant token（依赖 merchants + api_tokens 表）。替代方案（决策点）：

- **方案 i（推荐）**：admin token（保留）+ `catalog-owner token`（新：影子表 owner 维度的 HMAC token，替代 merchant token 的 owner 语义）——register/claim/refresh 的 owner 认证改用此；
- 方案 ii：无 owner token，仅 admin——失去 merchant owner 自助写能力（claim/refresh 全部 admin 代操作），功能收缩。

## 7. 测试迁移

| 测试 | 动作 |
| --- | --- |
| `test_agent_catalog_*.py`、`test_discovery_*.py`、`test_verification*`、`test_catalog_runtime_metrics`、`test_rate_limit`、`test_repository_abstraction`、`test_knp_data_part_examples`、`test_interop_kiwi_buyer` | 搬走（interop 测试依赖 kiwi 仓 fixture，保持 `../kiwi/contracts/interop/` 相对路径约定） |
| `test_a2a_hosted_server.py`、`test_hosted_binding.py`、`test_negotiation*`、`test_api_agent_catalog_writes.py` 中的 negotiation 相关 | 留 shopping-cli；catalog 写路由测试拆出 catalog 部分搬走 |
| 新增：`test_catalog_merchants.py`（影子表 JOIN）、`test_catalog_audit.py`（独立审计表） | 新写 |

## 8. 分阶段实施顺序

| 阶段 | 内容 | 完成定义 |
| --- | --- | --- |
| 0 | 本清单评审 + 决策点拍板（§6 认证、§9 命名） | ✅ 拍板：kiwi-catalog / 方案 i / 发布面随走 |
| 1 | **部署级裁剪原型**（同一代码库）：路由组只挂 catalog（含 hosted 发布面）+ 独立 DB 文件（models 拆分 + migration 子链） | ✅ shopping-cli commit 511036a：create_catalog_app + catalog_route_info + 锁修复 + 冒烟 4 测试；**调整**：DB 用全量 schema 超集（最小风险验证），正式拆分在阶段 2 |
| 2 | 数据解耦落地（阶段 1 原型的正式化）：影子表、弱引用、独立 audit | ✅ kiwi-catalog commit 30c7d17：独立 schema（10 表去 FK + merchants/audit 影子表）+ migration 子链 v1–v6 + 14 测试全绿 |
| 3 | 新仓库搭建：包结构、认证改造（§6）、测试迁移（§7） | ✅ 同 commit 30c7d17：认证方案 i（admin + catalog-owner HMAC token）+ 测试迁移（marketplace 耦合测试留 shopping-cli） |
| 4 | 部署验证：单 VM（形态 A）+ Dockerfile + systemd | ✅ commit 2be0760：Dockerfile + systemd unit + README（含 serverless 部署警告：SSRF socket 防护要求真实网络栈） |

每阶段可独立交付/回退；阶段 1 成本最低，先行验证产品假设。

## 9. 决策点（待拍板）

1. 新包/repo 命名（`shopping_catalog`？`agent-catalog`？）；
2. 认证方案 i（catalog-owner token）vs ii（仅 admin）；
3. hosted 发布面（agent-card.json/ucp）是否随 catalog 走——本清单默认**随走**（它是 catalog 的发布能力）；若留 shopping-cli 则发布面缩为「托管商家的内部发布」，独立产品只剩注册/验证/搜索。

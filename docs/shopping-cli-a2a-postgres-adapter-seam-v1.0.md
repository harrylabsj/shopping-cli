# Postgres 适配接缝文档 v1.0

- 设计出处：`docs/shopping-cli-a2a-upgrade-design-v1.2.1.md` §19（Persistence Strategy）、§20（Module Layout）
- 范围：v3.0 / Phase 5 的持久化抽象（P3）。**只定义接缝与契约，不实现 PG 适配器。**
- 更新：2026-08-06

## 1. 目标与原则

§19.1：SQLite 继续适用于 local / demo / 小型 curated catalog / 单节点 hosted runtime，
不对现有 SQLite migration、并发、资源限制与测试做无意义重写。

§19.2：一旦成为公网多实例服务（multi-instance API / large catalog / continuous
verification workers / high write concurrency），SQLite 不是长期唯一生产数据库。
**先建 Repository abstraction，未来增加 Postgres adapter；不要让 MVP 强制迁移
全部现有 Marketplace Conversation 数据；Discovery Plane 可以先获得独立
persistence adapter。**

本文件固化：

1. 三层 Repository 契约（`shopping_cli/agent_catalog/repository.py`）与 SQLite 现状实现的对应；
2. 四类接缝领域（idempotency / rate-limit / trust-observation / a2a-ledger）的盘点结论；
3. 表级 SQLite→PG 差异清单；
4. 分阶段迁移策略。

契约↔实现的映射表在 `tests/test_repository_abstraction.py` 有可执行版本（防漂移）。

## 2. 三层 Repository 契约现状矩阵

| 契约（repository.py） | 域 | 覆盖状态 | SQLite 现状实现 |
| --- | --- | --- | --- |
| `CatalogRepository` | catalog_agents / agent_endpoints / agent_capabilities / agent_skills / agent_profile_snapshots / agent_verifications / agent_trust_observations / agent_catalog_register_limits / §23 catalog audit | ✅ 完整（P3 补齐，含 trust observations） | `agent_catalog/sqlite_repository.py`（模块级函数，conn 注入） |
| `ConversationRepository` | conversations / messages / moderation_flags | ✅ 契约已定义 | `core/conversations.py`（ensure/append/flag/messages）+ `services/conversations.py`（close 的 use-case 层） |
| `AuditRepository` | audit_events | ✅ 契约已定义 | `core/harness.py`（`append_audit_event` / `conversation_audit_events`） |

现状结构说明：`CatalogRepository` 是 Protocol（契约），`sqlite_repository.py` 是模块级
函数（conn 首参）——两者目前通过调用方直接 import 绑定，尚无实现类。PG 适配的
第一步（阶段 1）是给 `sqlite_repository` 包一个 `SQLiteCatalogRepository` 类，
与 `PostgresCatalogRepository` 同形态。

## 3. 四类接缝领域盘点结论（P3）

PM 要求盘点 idempotency / rate-limit / trust-observation / a2a-ledger 是否都在
Repository 抽象后：

| 领域 | 表 | 现状 | 是否在 CatalogRepository 后 | 决策 |
| --- | --- | --- | --- | --- |
| trust-observation | `agent_trust_observations` | `sqlite_repository` 有 insert/list/count/counts_by_kind | ❌ 此前不在 Protocol → **P3 已补** | catalog 域，PG 适配随 Discovery Plane |
| idempotency（catalog 写） | `agent_catalog_write_idempotency` | `api/idempotency.py` 直接 SQL（claim/replay 依赖 `IntegrityError`） | ❌ 不在（传输层） | 独立接缝：API 基础设施 adapter（阶段 2）。**PG 上 IntegrityError 语义要改为 UniqueViolation / ON CONFLICT**（见 §4.4） |
| rate-limit（catalog 写） | `agent_catalog_write_rate_limits` + `agent_catalog_register_limits`（后者已在 sqlite_repository） | 前者在 `api/idempotency.py` 直接 SQL；后者在 sqlite_repository 且已在 Protocol | 部分 | register 限流随 catalog 域；write rate limits 随 API 基础设施 adapter |
| a2a-ledger | `a2a_inbound_idempotency` | `a2a/hosted_server.py` 直接 SQL（幂等 claim 依赖 `IntegrityError`） | ❌ 不在（hosted 域） | 独立接缝：hosted gateway adapter（阶段 3），同样处理完整性冲突语义 |
| （顺带）buyer 侧 | `buyer_request_idempotency` / `buyer_bootstrap_rate_limits` / `merchant_bootstrap_idempotency` | `api/idempotency.py` 直接 SQL | ❌ | 同 API 基础设施 adapter（阶段 2），非本 Phase 优先级 |

## 4. 表级 SQLite→PG 差异清单

### 4.1 主键与自增

| 模式 | SQLite | Postgres | 涉及表 |
| --- | --- | --- | --- |
| TEXT 主键（业务前缀，如 `cagt_`） | `text primary key` | 同，无差异 | catalog_agents（catalog_agent_id）、agent_capabilities / agent_endpoints / agent_skills（复合或业务 id） |
| 自增主键 | `integer primary key autoincrement`，插入后 `cursor.lastrowid` | `bigint generated always as identity`，插入后 `returning id`；代码需从 `lastrowid` 改为 INSERT ... RETURNING | agent_profile_snapshots / agent_verifications / agent_trust_observations / audit_events / messages |
| 幂等键 | `text primary key`（digest / request_hash） | 同，但唯一冲突处理不同（见 4.4） | agent_catalog_write_idempotency（request_hash+actor+endpoint 复合）、a2a_inbound_idempotency（digest） |

### 4.2 JSON 存储

现有实现全部用 TEXT 列存 JSON 字符串（`tags_json` / `details_json` / `evidence_json` /
`structured_payload` 等），`encode_json` / `decode_json`（db/session.py）封装编解码。
PG 可继续用 TEXT（零差异），或迁移到 `jsonb`（阶段 1 不建议——行为变更面大，
TEXT 可移植性最稳）。

### 4.3 约束与类型

| 项 | SQLite | Postgres | 迁移动作 |
| --- | --- | --- | --- |
| CHECK 约束 | `check(hosting_mode in (...))` 等（models.py） | 语法兼容 | 直接移植 |
| 时间戳 | ISO-8601 TEXT（`now_iso()`） | 可继续 TEXT（避免 tz/时区语义差异）；不引 `timestamptz` | 无差异，保持 TEXT |
| 参数占位符 | `?` | `%s`（psycopg） | **所有手写 SQL 都要改**——repository 抽象正好隔离此面（阶段 1 只在 adapter 内） |
| row 访问 | `sqlite3.Row` 列名访问 | psycopg `dict_row` | adapter 内部处理 |
| LIKE 模糊搜索 | `display_name like ?`（search 三处） | 同语法；大规模时换 `pg_trgm`（非阻塞优化） | 语义一致，直接移植 |

### 4.4 完整性冲突语义（幂等 claim 的关键差异）

SQLite 实现依赖 `sqlite3.IntegrityError` 捕获来做幂等 claim（`api/idempotency.py`
252-260 行、`a2a/hosted_server.py` 304-314 行）。PG 等价物：

- psycopg 抛 `psycopg.errors.UniqueViolation`（包裹在 `IntegrityError` 下）；
- 或改写为 `INSERT ... ON CONFLICT (...) DO NOTHING` + 后续 SELECT（推荐：无异常路径，
  与 SQLite 的「try insert → 冲突则 select」语义一致）。

**接缝要求**：adapter 必须把幂等写入收敛成单一接口（如 `claim_idempotency(key) ->
claimed | replayed`），让上层不感知底层冲突机制。当前直接 SQL 的调用方（api/idempotency、
hosted_server）在阶段 2/3 迁入 adapter 时做此收敛。

### 4.5 并发与连接管理

| 项 | SQLite | Postgres |
| --- | --- | --- |
| 写入并发 | 单写者 + busy timeout（`SQLITE_BUSY_TIMEOUT_MS`，db/session.py:56） | MVCC 多写者；verification worker 的并发预算（queue concurrency）直接受益 |
| 连接 | `db_session(db_path)` 上下文管理器（每次打开/关闭） | 需要连接池（psycopg_pool / SQLAlchemy）；`db_session` 工厂按环境切换 |
| 事务隔离 | 默认 deferred | 默认 read committed（行为兼容）；幂等 claim 需要 `READ COMMITTED` 或显式事务边界 |

## 5. 接缝点清单（契约方法 → 实现 → PG 注意点）

CatalogRepository 的 26 个契约方法 + ConversationRepository 5 个 + AuditRepository 2 个，
完整映射表见 `tests/test_repository_abstraction.py`。PG 适配按 §4 差异分类即可：

- 8 个只读查询（get/list/latest/count）→ 占位符 + row 工厂改造；
- 9 个写入（upsert/insert/set）→ 占位符 + 自增 RETURNING；
- 幂等/限流 3 个（enforce_catalog_register_domain_limit 等）→ 完整性冲突语义（§4.4）；
- search → LIKE 直接移植，cursor 分页语义保留（SQLite 实现为 keyset，PG 同模式）。

## 6. 迁移策略（分阶段，依赖顺序）

| 阶段 | 内容 | 完成定义 |
| --- | --- | --- |
| 0（本次 P3） | 契约冻结：三层 Protocol + 映射表测试 + 本文件 | 已交付 |
| 1 | `SQLiteCatalogRepository` 实现类（包装现有函数）+ `PostgresCatalogRepository`（Discovery Plane 独立 adapter）+ 环境切换（`SHOPPING_CATALOG_DSN`） | Discovery Plane 全部读写走 adapter；SQLite 路径行为零变化 |
| 2 | API 基础设施 adapter：write idempotency / rate limits（收敛 claim 语义，§4.4） | 幂等写入无 `IntegrityError` 依赖 |
| 3 | hosted gateway ledger adapter（a2a_inbound_idempotency） | 同上 |
| 明确不做 | Marketplace Conversation 数据迁移到 PG（§19.2）；Conversation/AuditRepository 的 PG 实现 | — |

## 7. 非目标

- 不实现 PG 适配器（P3 只到契约与接缝文档）；
- 不迁移现有 Marketplace Conversation 数据；
- 不引入 ORM；adapter 继续手写 SQL（占位符差异被隔离在 adapter 内）。

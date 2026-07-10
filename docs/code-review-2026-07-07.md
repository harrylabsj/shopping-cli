# shopping-cli 代码审查报告

**审查日期**：2026-07-07
**审查范围**：`shopping-cli` 全仓库（Python 核心、API、CLI、Agent、LLM、插件、测试、构建配置）
**审查方式**：静态代码阅读 + 测试运行 + 结构分析

---

## 1. 总体评价

`shopping-cli` 是一个边界清晰、测试较充分、分层设计合理的本地商业（local commerce）AI 咨询运行时项目。核心亮点包括：

- **范围自律**：明确声明为“咨询系统，非交易系统”，并通过测试断言不存在订单/支付表。
- **共享服务层**：`services/` 被 CLI 与 API 共用，行为对齐。
- **Token 安全**：API Token 仅保存 SHA-256 digest + prefix/suffix，原始 token 只返回一次。
- **显式数据库迁移**：使用 `PRAGMA user_version`，迁移幂等且编号化。
- **幂等控制**：买家引导、渠道 ingress、Agent 消息 claim 均有幂等机制。
- **FastAPI 降级 ASGI**：无 FastAPI 时仍可运行基础 API。
- **测试覆盖**：369 个 Python unittest + 7 个 Node 测试，`scripts/verify.sh` 通过。

当前最突出的风险集中在：**API 路由与错误处理层未完全解耦**、**LLM Dispatcher 错误模型不当**、**列表/搜索性能随数据量增长会恶化**、**Agent 进程管理跨平台健壮性不足**、**工程工具链缺失**。

---

## 2. 高优先级问题

### 2.1 `api/app.py` 仍是“半拉子”路由层

- **文件**：`shopping_cli/api/app.py`（1162 行）
- **问题**：
  - `handle_request()` 手工维护 40+ 条 `if path == ... and method == ...` 路由，与 `api/route_registry.py` 中的元数据重复。
  - FastAPI 装饰器路由又重复注册一遍（约 820–1160 行）。
  - `_payload_token`、`_payload_admin_token`、`_idempotency_key_header_default()` 等薄包装，实际逻辑已在 `api/auth.py`、`api/idempotency.py` 中实现。
- **风险**：修改路由或认证逻辑时需要在 `route_registry.py`、`handle_request()`、FastAPI 装饰器三处保持一致，极易漏改。
- **建议**：
  - 让 `handle_request()` 只做内部 dispatch，统一从 `route_registry.py` 生成路由表。
  - 或让 FastAPI 路由与手动路由共用同一份 handler 函数表，删除重复薄包装。
  - 目标：把 `api/app.py` 缩减为入口装配与启动逻辑。

### 2.2 异常映射过宽，可能掩盖内部错误

- **文件**：`shopping_cli/api/app.py:740–758`
- **问题**：
  ```python
  except (KeyError, ValueError) as exc:
      return 400, {"ok": False, "error": str(exc)}
  ```
  `KeyError`/`ValueError` 在 Python 标准库与业务代码中大量使用，直接映射为 400 会把真正的编程错误（字典键拼写错误、类型转换失败）暴露为“客户端错误”，既可能泄露实现细节，也使调试困难。
- **建议**：
  - 仅映射自定义领域异常（如 `ValidationError`、`NotFoundError` 等）。
  - 将通用 `KeyError`/`ValueError` 视为 500，或在边界处显式转换为 `ValidationError`。
  - 同步移除 FastAPI 侧未使用的 `KeyError` 全局异常处理器（约 807–813 行）。

### 2.3 LLM Dispatcher 在库代码中抛出 `SystemExit`

- **文件**：`shopping_cli/llm/dispatcher.py:83`、`:90`、`:292`、`shopping_cli/llm/dispatcher.py:354`、`:356`、`:358`、`:386`
- **问题**：
  - `MarketplaceToolDispatcher.dispatch` 与 `HTTPMarketplaceToolDispatcher._request` / `_validate_response` 中抛出 `SystemExit`。
  - 作为库调用时无法通过正常异常恢复。
  - HTTP 鉴权/权限/网络错误全部变成进程退出，再被 `runner.py` 吞掉，调用方无法区分错误类型。
  - 与项目自己倡导的“用领域异常替代 SystemExit”方向相矛盾。
- **建议**：
  - Dispatcher 中改为抛 `ToolAccessDenied`、`HTTPMarketplaceError`、`ValidationError` 等自定义领域异常。
  - 仅在 `cli_llm_commands.py` 顶层 catch 并转换为 `SystemExit`。
  - 同步更新 `runner.py`，不再需要捕获 `SystemExit`。

---

## 3. 中优先级问题

### 3.1 会话列表默认加载完整详情，存在 N+1 风险

- **文件**：`shopping_cli/core/conversations.py:285–307`、`:309–333`、`:340–361`
- **问题**：
  - `conversation_summary()` 会一次性加载全部 messages、flags、audit_events。
  - `merchant_conversations()` 默认 `summary_only=False`，调用方列表视图会逐条调用 `conversation_summary()`。
  - `conversation_list_summary_from_row()` 虽然已存在，但每条记录仍触发 3 条子查询统计 messages/flags/audits。
- **建议**：
  - 列表接口默认返回 lightweight summary，新增 `include=details` 显式加载完整数据。
  - 统计子查询改为 JOIN 聚合或缓存字段。
  - CLI/API 将默认列表切换到 summary 模式。

### 3.2 搜索仍把大量候选行载入 Python 排序

- **文件**：`shopping_cli/core/catalog.py:745–853`、`855–928`
- **问题**：
  - 产品搜索执行 `match` 后仍把 `candidate_cap`（默认 1000，最大 5000）行全部取回 Python，再逐行 tokenize、打分、过滤、排序。
  - 商家搜索未用 FTS，直接全表拉取后 Python 打分。
  - `candidate_cap` 与窗口 `limit/offset` 语义不一致：分页靠 Python 切片，数据库只能按 SKU 排序。
- **建议**：
  - 在 FTS 查询中直接通过 `rank`/`bm25` 排序，减少候选集。
  - 对商家/政策搜索同样建立 FTS 索引。
  - 为分页增加稳定的排序键与数据库层 `LIMIT/OFFSET`，而非全量拉回后切片。

### 3.3 Agent 进程管理跨平台脆弱

- **文件**：`shopping_cli/agents/merchant_daemon.py:112–134`、`308–376`
- **问题**：
  - `is_process_running()` 依赖 `ps -o stat= -p <pid>`，这是类 Unix 命令，Windows 上直接失败。
  - PID 文件 read → check process → start subprocess 之间存在竞态窗口；多进程同时启动可能重复启动 Agent。
  - `stop_agent()` 通过 `os.kill(pid, SIGTERM)` 后轮询状态文件，若状态文件损坏会误判。
- **建议**：
  - 使用跨平台库（如 `psutil`）或至少在 Windows 路径回退。
  - 对 PID 文件加文件锁。
  - 状态与 PID 信息写入同一原子记录。

### 3.4 `scripts/shopping_agent.py` 与 `agents/agent_cli.py` 反向依赖 `cli.py`

- **文件**：`scripts/shopping_agent.py:14`、`shopping_cli/agents/agent_cli.py:9`
- **问题**：
  - 两者从 `shopping_cli.cli` import `DEFAULT_DB_PATH` 和 `emit`。
  - `cli.py` 是入口模块，反向引用它会让 CLI 的导入图变得复杂。虽然目前未产生循环导入，但 `cli.py` 已导入 `cli_agent_commands → merchant_daemon`，未来若 `merchant_daemon` 需要导入 agent 入口就易产生循环。
  - `scripts/shopping_agent.py` 还有一个无意义的 `while True: ... break` 循环。
- **建议**：
  - `emit` 移入 `cli_common.py`（事实上 `cli_common.py` 已有 `emit`）。
  - `DEFAULT_DB_PATH` 从 `shopping_cli.config` 导入。
  - 移除 `scripts/shopping_agent.py` 中的无意义循环。

### 3.5 LLM `merchant` 角色在 API 模式下缺少 `automation_boundaries`

- **文件**：`shopping_cli/cli_llm_commands.py:81–89`
- **问题**：当通过 `--api-url` 运行时，商家 LLM 的系统提示拿不到 `automation_boundaries`，导致议价规则等自动化边界失效。这与 API-backed Agent 工具链的设计目标不一致。
- **建议**：通过 API `GET /merchants/{merchant_id}` 拉取商家配置，或在本地缓存。

### 3.6 认证/请求头解析在 FastAPI 与 Fallback ASGI 中重复

- **文件**：`shopping_cli/api/fallback_asgi.py:77–86`、`shopping_cli/api/auth.py`
- **问题**：Fallback ASGI 自己解析 Bearer token 与 `Idempotency-Key`，没有复用 `api_auth.payload_with_auth()`。虽然当前逻辑相同，但未来修改认证方式时容易漏改。
- **建议**：把 header → payload 合并逻辑抽到 `api/auth.py` 一个函数，两个入口都调用。

### 3.7 缺少 lint/type-check 工具链

- **文件**：`pyproject.toml`
- **问题**：没有 dev 依赖，也没有配置 ruff、mypy、pytest。项目已写大量类型注解，但没有强制检查，长此以往类型会腐烂。
- **建议**：
  - 在 optional/dev 依赖中加入 `ruff`、`mypy`、`pytest`。
  - 配置 `pyproject.toml` 的 `[tool.ruff]` / `[tool.mypy]`。
  - 建议增加 CI workflow 做 lint + type-check + test。

### 3.8 动态 SQL 使用 f-string 拼接列名（模式脆弱）

- **文件**：`shopping_cli/core/catalog.py:199`、`368`
- **问题**：
  ```python
  conn.execute(f"update merchants set {', '.join(updates)} where id = ?", values)
  ```
  列名来自代码内部硬编码字典，目前没有 SQL 注入风险，但属于脆弱的动态 SQL 模式。若未来把列名来源改为外部输入，风险会立即上升。
- **建议**：使用白名单映射表（`dict[str, str]`）把输入字段映射到安全列名，再拼接。

### 3.9 大量重复的 `_safe_*` 辅助函数

- **文件**：`core/catalog.py`、`core/channels.py`、`core/harness.py`、`core/conversations.py`、`agents/tools.py`、`agents/merchant_agent.py`、`agents/merchant_daemon.py`、`llm/dispatcher.py`、`llm/runner.py`、`llm/providers.py`、`services/agents.py`、`api/handlers/common.py` 等
- **问题**：几乎每个模块都重新定义了 `_safe_non_negative_int`、`_safe_positive_float` 等。行为可能轻微不一致（例如对 `bool` 的处理、默认值），重复代码增加维护成本。
- **建议**：在 `shopping_cli/utils.py` 或 `core/common.py` 中集中定义这些数值安全函数，各模块统一导入。

---

## 4. 低优先级问题

### 4.1 `api/app.py` 注册了未使用的全局错误处理器

- **文件**：`shopping_cli/api/app.py:807–813`
- **问题**：FastAPI 自身的请求体验证与自定义异常已覆盖主要路径，`KeyError`/`ValueError` 作为全局 handler 容易捕获框架内部的非业务错误。
- **建议**：移除或收紧为仅处理自定义异常。

### 4.2 `db/session.py` 每次开连接都执行全部迁移与索引

- **文件**：`shopping_cli/db/session.py:67–81`
- **问题**：每个请求/CLI 命令都会执行 `init_db()`，包含所有 `CREATE TABLE IF NOT EXISTS`、迁移、索引。在高并发或长连接场景下是额外开销。
- **建议**：迁移只在数据库文件新建或版本落后时执行；常规连接只做 `PRAGMA` 设置。

### 4.3 `cli.py` 仍保留部分命令处理逻辑

- **文件**：`shopping_cli/cli.py:102–494`
- **问题**：`cmd_merchant_human_review`、`cmd_conversation_human_review`、`cmd_human_review_queue/show/resolve`、`cmd_audit_events`、`cmd_legacy_import`、`cmd_api_routes`、`cmd_api_serve` 仍定义在 `cli.py`。与项目自己的优化方向“Keep `cli.py` as the small entry point”仍有差距。
- **建议**：新增 `cli_human_review_commands.py`、`cli_api_commands.py`，让 `cli.py` 只负责 `build_parser()` 与 `main()`。

### 4.4 `cli_conversation_commands.py` 直接拼接 SQL 列表条件

- **文件**：`shopping_cli/cli_conversation_commands.py:62–89`
- **问题**：
  ```python
  for column, value in (("status", args.status), ...):
      if value:
          clauses.append(f"{column} = ?")
          values.append(value)
  ```
  当前 `column` 来自硬编码元组，安全；但 `updated_since` 以字符串形式拼入。
- **建议**：统一使用参数化查询，并把列名白名单化，避免未来误改。

### 4.5 仓库 SQLite 文件与构建目录

- `shopping-cli.sqlite` 文件出现在仓库根目录，已被 `*.sqlite` 规则忽略，但建议确认是否曾被误跟踪。
- `build/` 目录包含旧版本源码，应确保不会被提交或打包；`.gitignore` 已忽略 `build/`。

---

## 5. 改进建议优先级表

| 优先级 | 建议 | 目标文件 |
|---|---|---|
| **P0** | 将 `api/app.py` 的 `handle_request()` 与 FastAPI 路由统一：要么用 FastAPI 注册所有路由并让 `handle_request` 只做内部 dispatch，要么生成装饰器路由。删除重复薄包装。 | `api/app.py`、`api/route_registry.py` |
| **P0** | LLM Dispatcher 改抛领域异常，顶层 CLI 再转 `SystemExit` | `llm/dispatcher.py`、`cli_llm_commands.py`、`runner.py` |
| **P1** | 列表接口默认返回 lightweight summary；对话详情按需加载；合并统计子查询 | `core/conversations.py`、`api/handlers/conversations.py`、`cli_conversation_commands.py` |
| **P1** | 搜索使用 FTS rank 预排序，建立商家/政策 FTS 索引，数据库层分页 | `core/catalog.py`、`core/policies.py` |
| **P1** | Agent daemon 引入跨平台进程检查与 PID 文件锁 | `agents/merchant_daemon.py` |
| **P2** | 解除 `scripts/shopping_agent.py` / `agents/agent_cli.py` 对 `cli.py` 的依赖 | `scripts/shopping_agent.py`、`agents/agent_cli.py`、`cli_common.py` |
| **P2** | 统一 header/认证解析，Fallback ASGI 复用 `api_auth` | `api/fallback_asgi.py`、`api/auth.py` |
| **P2** | API 模式下 LLM merchant 提示也加载 `automation_boundaries` | `cli_llm_commands.py` |
| **P2** | 集中数值安全函数到公共模块 | 各 `core/`、`agents/`、`llm/`、`services/` |
| **P2** | 添加 ruff、mypy、pytest dev 依赖与 CI 配置 | `pyproject.toml`、CI workflow |
| **P3** | 把 `cli.py` 中剩余 handler 拆分到 `cli_human_review_commands.py`、`cli_api_commands.py` | `cli.py` |
| **P3** | 动态 SQL 列名白名单化 | `core/catalog.py` |
| **P3** | 数据库初始化按需执行迁移 | `db/session.py` |

---

## 6. 未来优化方向

1. **API 契约硬化**：引入 Pydantic 模型（可选依赖已声明）对请求/响应做校验与文档化，减少手写 `str(payload.get(...))` 的类型不确定问题。
2. **连接池与事务**：SQLite 在当前“每请求一连接”模式下够用，但未来可考虑 `sqlite3` 线程模式与连接池，减少 `init_db()` 开销。
3. **审计与日志保留**：`audit_events` 目前只追加不清理，长期运行会无限增长。应增加保留策略或归档机制。
4. **事件/消息队列**：当前 Agent 是轮询 SQLite；当咨询量增大时，可引入轻量队列或 SQLite `RETURNING`/`notify` 机制减少空轮询。
5. **插件安全加固**：Node 插件目前通过命令行拼接调用 Python，应对参数做严格校验（虽然当前仅支持预定义参数）；考虑通过 Unix socket/stdio 或 API 与 Python 通信，避免 shell 注入风险。
6. **可观测性**：增加 Prometheus/OpenMetrics 风格的指标端点，暴露搜索延迟、Agent claim/complete/fail 计数、人工审核队列深度等。
7. **多租户与数据隔离**：当前以 `merchant_id` 做行级隔离，未来若引入平台运营方角色，需完善 RBAC 与审计归属。

---

## 7. 审查结论

`shopping-cli` 在 MVP 范围内是健康且可维护的项目。核心数据模型、Token 安全、审计与幂等设计是亮点。建议优先处理 **P0/P1** 项（API 路由解耦、LLM 异常模型、列表/搜索性能、Agent 进程健壮性），再逐步补齐工程工具链与细节打磨。后续优化方向明确，可按优先级持续推进。

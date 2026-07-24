# P2 整改记录（2026-07-24）

对应《shopping-cli 全仓库代码审查报告》中的 P2-01 至 P2-10。本轮已完成以下整改：

1. 缺失字段和重复资源稳定返回 400/409，不再作为未知 500。
2. FastAPI 与 fallback 统一 404、405、413、500 JSON 错误结构；fallback 的同步工作通过线程执行。
3. Human-review 的持久化 actor/resolved_by 从已认证 token 派生，忽略 payload 中伪造的主体。
4. 请求体默认限制为 1 MiB，并限制 JSON 深度、节点、字符串和数组规模；核心持久化文本也有上限。
5. Merchant/buyer token 增加默认 TTL；会话关闭撤销 buyer token；merchant 支持自助轮换和管理员撤销；远端 API 默认要求 HTTPS；长驻 Agent 不接受 argv token。
6. OpenClaw 子进程调用改为异步 `execFile`，带 15 秒超时和 1 MiB 输出上限。
7. FTS 查询热路径不再执行全表健康扫描；完整一致性检查保留在显式 stats/诊断路径。
8. 会话摘要列表改为单条分页查询，Agent backlog 上限为 100。
9. 当前 schema 的 SQLite 连接跳过重复建表、迁移与 meta 写入；初始化失败立即关闭连接。
10. Daemon 日志增加 5 MiB 轮转、尾部读取、错误指数退避和永久认证错误终止；Compose 增加必填变量和健康依赖。

## 验证

- Python 全量：393 tests passed，1 skipped（系统 Python 未安装 FastAPI）。另从临时环境安装构建出的 `.[api]` wheel，真实 FastAPI/Starlette ASGI 契约测试 1/1 passed。
- Node/OpenClaw：由 `scripts/verify.sh`、Node test 和 OpenClaw validate/build-check 覆盖。
- 新增 `tests/test_p2_regressions.py`，覆盖错误映射、资源上限、异步隔离、HTTPS、token 生命周期、O(1) 列表、FTS 热路径、连接初始化和日志边界。

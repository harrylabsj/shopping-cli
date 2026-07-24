# P3 整改记录（2026-07-24）

对应《shopping-cli 全仓库代码审查报告》中的 P3-01 至 P3-03。本轮已完成以下整改：

1. 新增 GitHub Actions CI，覆盖 Python 3.11/3.13、Node 22、Ruff、Mypy、分支覆盖率和发布制品验证；覆盖率下限设为 80%，当前全量结果为 84%。
2. 新增 `scripts/quality.sh` 和 `scripts/verify_release.sh`。发布验证会构建 sdist/wheel、在隔离 venv 中安装 wheel、检查三个 console entry point，并检查根包和 OpenClaw 插件的 npm pack 内容。
3. FastAPI/fallback 的公开路由元数据改为从可执行 `_ROUTE_TABLE` 聚合生成，消除路径和 HTTP 方法的重复清单；保留独立的分组元数据，并在两者漂移时立即失败。
4. Merchant Agent 与 LLM Dispatcher 统一使用 `shopping_cli/http_client.py`，集中 HTTPS 策略、超时归一化、JSON/错误解码和响应结构验证。
5. CLI 的会话列表、人审队列/解决流程和 LLM 商户配置查询已通过 core/service 接口访问，不再在 CLI command handler 中直接执行 SQL。
6. Dockerfile 改为固定 patch 版本的多阶段 wheel 构建，运行镜像离线安装 builder 产出的 wheels，并使用 UID/GID 10001 的非 root 用户；Docker context 额外排除环境文件、密钥、凭据、审查文档和本地状态库。
7. OpenClaw skill 默认路径统一为 `~/.openclaw/skills/shopping-cli`，安装脚本、运行时兼容层和插件 README 保持一致。
8. 覆盖率插桩暴露了 macOS Python launcher 到 `Python.app` 的进程身份切换竞态；daemon 现在等待身份稳定后再写 PID 记录，同时继续校验 PID、create time、可执行文件和关键命令参数。

## 验证

- Ruff：通过。
- Mypy：71 个源码文件通过。
- Python：400 个测试通过（包含真实 FastAPI 运行时测试和新增 P3 回归测试）。
- Coverage：84%，超过 80% 门禁。
- Node/OpenClaw：7 个 Node 测试通过；根包和插件 npm pack 验证通过。
- Python 制品：sdist/wheel 构建通过；隔离 venv 安装 wheel 后三个 console entry point smoke test 通过。

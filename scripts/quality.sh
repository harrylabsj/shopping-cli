#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python3 scripts/verify_contract_lock.py
python3 -m ruff check shopping_cli tests
python3 -m mypy shopping_cli
python3 -m coverage erase
# pytest 收集 unittest 风格 + pytest 风格全部测试；unittest discover 会漏掉
# 顶层 def test_*（2026-08-10 审查 P1-1：约 240 个测试在 CI 静默不执行）。
python3 -m coverage run -m pytest tests/ -q
python3 -m coverage report
node --test tests/shopping_plugin.test.mjs

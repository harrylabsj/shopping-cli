#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python3 -m ruff check shopping_cli tests
python3 -m mypy shopping_cli
python3 -m coverage erase
python3 -m coverage run -m unittest discover -s tests
python3 -m coverage report
node --test tests/shopping_plugin.test.mjs

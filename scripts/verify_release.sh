#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

cd "$ROOT_DIR"
python3 -m build --outdir "$TMP_DIR/dist"
python3 -m venv "$TMP_DIR/venv"
"$TMP_DIR/venv/bin/python" -m pip install --no-deps --force-reinstall "$TMP_DIR"/dist/*.whl
"$TMP_DIR/venv/bin/shopping-cli" --help >/dev/null
"$TMP_DIR/venv/bin/shopping-cli-api" --help >/dev/null
"$TMP_DIR/venv/bin/shopping-cli-agent" --help >/dev/null
npm pack --dry-run --json --pack-destination "$TMP_DIR" >/dev/null
npm pack --dry-run --json --pack-destination "$TMP_DIR" ./plugins/shopping-plugin >/dev/null

echo "release artifacts verified"

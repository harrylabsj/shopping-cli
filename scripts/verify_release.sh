#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

cd "$ROOT_DIR"

# Ensure the build frontend is available. A local build/ directory (setuptools
# artifact) shadows the PyPI "build" package, so test for the __main__ module.
HOST_PYTHON="${SHOPPING_CLI_PYTHON:-python3}"
if ! "$HOST_PYTHON" -c "import build.__main__" 2>/dev/null; then
  "$HOST_PYTHON" -m venv "$TMP_DIR/build-venv"
  BUILD_PYTHON="$TMP_DIR/build-venv/bin/python"
  "$BUILD_PYTHON" -m pip install --quiet "build>=1.2"
else
  BUILD_PYTHON="$HOST_PYTHON"
fi

"$BUILD_PYTHON" -m build --outdir "$TMP_DIR/dist"
python3 -m venv "$TMP_DIR/venv"
set -- "$TMP_DIR"/dist/*.whl
if [[ "$#" -ne 1 || ! -f "$1" || -L "$1" ]]; then
  echo "expected exactly one regular project wheel" >&2
  exit 1
fi
"$TMP_DIR/venv/bin/python" -m pip install --force-reinstall "$1[api]"
# Check the installed wheel away from the checkout; environment shadowing must fail.
(
  cd "$TMP_DIR"
  "$TMP_DIR/venv/bin/python" -m pip check
  "$TMP_DIR/venv/bin/python" - <<'PYTHON'
import sys
from pathlib import Path
import psutil, fastapi, uvicorn
import shopping_cli
from shopping_cli.core.negotiation_contracts import load_contract_schema

package_file = Path(shopping_cli.__file__).resolve()
venv_prefix = Path(sys.prefix).resolve()
if not package_file.is_relative_to(venv_prefix):
    raise SystemExit("shopping_cli import escaped the isolated venv")
[load_contract_schema(name) for name in ('decision', 'snapshot', 'policy-result', 'capabilities')]
PYTHON
  "$TMP_DIR/venv/bin/shopping-cli" --help >/dev/null
  "$TMP_DIR/venv/bin/shopping-cli-api" --help >/dev/null
  "$TMP_DIR/venv/bin/shopping-cli-agent" --help >/dev/null
)
npm pack --dry-run --json --pack-destination "$TMP_DIR" >/dev/null
npm pack --dry-run --json --pack-destination "$TMP_DIR" ./plugins/shopping-plugin >/dev/null

echo "release artifacts verified"

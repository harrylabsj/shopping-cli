#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
DB_FILE="$TMP_DIR/shopping-cli.sqlite"

python3 "$ROOT_DIR/scripts/shopping.py" --help >/dev/null
python3 "$ROOT_DIR/scripts/shopping_registry.py" --help >/dev/null
bash "$ROOT_DIR/scripts/install.sh" --both --dry-run >/dev/null
python3 -m unittest discover -s "$ROOT_DIR/tests"
node --test "$ROOT_DIR/tests/shopping_plugin.test.mjs"

python3 "$ROOT_DIR/scripts/shopping.py" --db "$DB_FILE" merchant create \
  --id seller-a \
  --name "West Lake Tea" \
  --city Hangzhou \
  --service-area "West Lake" \
  --contact "wechat:westlake" \
  --hours "09:00-21:00" \
  --delivery-fee 12 \
  --delivery-eta-minutes 45 \
  --tags "tea,gift,longjing" \
  --format json >"$TMP_DIR/merchant.json"

python3 "$ROOT_DIR/scripts/shopping.py" --db "$DB_FILE" product add \
  --merchant seller-a \
  --sku tea-a \
  --title "Longjing Gift Box" \
  --price 88 \
  --stock 5 \
  --category tea \
  --tags "longjing,gift" \
  --delivery-attributes "same-city,courier" \
  --format json >"$TMP_DIR/product.json"

python3 "$ROOT_DIR/scripts/shopping.py" --db "$DB_FILE" buyer ask \
  --buyer alice \
  --text "longjing gift delivery today" \
  --city Hangzhou \
  --area "West Lake" \
  --format json >"$TMP_DIR/ask.json"

python3 "$ROOT_DIR/scripts/shopping.py" --db "$DB_FILE" agent run \
  --merchant seller-a \
  --once \
  --format json >"$TMP_DIR/agent.json"

python3 "$ROOT_DIR/scripts/shopping.py" --db "$DB_FILE" buyer summarize \
  --conversation CONV-0001 \
  --format json >"$TMP_DIR/summary.json"

python3 "$ROOT_DIR/scripts/shopping.py" --db "$DB_FILE" buyer intent \
  --conversation CONV-0001 \
  --intent purchase_intent \
  --text "I want to continue after merchant confirmation." \
  --format json >"$TMP_DIR/intent.json"

python3 - "$ROOT_DIR" "$DB_FILE" "$TMP_DIR/ask.json" "$TMP_DIR/agent.json" "$TMP_DIR/summary.json" "$TMP_DIR/intent.json" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1])
db_file = Path(sys.argv[2])
ask = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
agent = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))
summary = json.loads(Path(sys.argv[5]).read_text(encoding="utf-8"))
intent = json.loads(Path(sys.argv[6]).read_text(encoding="utf-8"))

assert ask["conversation"]["id"] == "CONV-0001"
assert ask["candidates"][0]["sku"] == "tea-a"
assert agent["replied"][0]["conversation_id"] == "CONV-0001"
assert summary["option"]["stock"] == 5
assert summary["no_order_created"] is True
assert summary["no_stock_reserved"] is True
assert intent["message"]["intent"] == "purchase_intent"

conn = sqlite3.connect(db_file)
try:
    tables = {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}
finally:
    conn.close()
assert {"merchants", "products", "delivery_rules", "conversations", "messages", "agents", "moderation_flags"} <= tables
assert "orders" not in tables
assert "payments" not in tables

skill = (root / "SKILL.md").read_text(encoding="utf-8")
assert "name: shopping-cli" in skill
assert "TODO" not in skill

package = json.loads((root / "package.json").read_text(encoding="utf-8"))
clawhub = json.loads((root / "clawhub.json").read_text(encoding="utf-8"))
assert package["name"] == clawhub["name"] == "shopping-cli"
assert package["version"] == clawhub["version"]
plugin_package = json.loads((root / "plugins" / "shopping-plugin" / "package.json").read_text(encoding="utf-8"))
plugin = json.loads((root / "plugins" / "shopping-plugin" / "openclaw.plugin.json").read_text(encoding="utf-8"))
assert plugin_package["name"] == plugin["id"] == "shopping-plugin"
assert plugin["version"] == plugin_package["version"] == package["version"]
assert plugin_package["openclaw"]["extensions"] == ["./index.js"]

openai_yaml = (root / "agents" / "openai.yaml").read_text(encoding="utf-8")
assert 'display_name: "shopping-cli"' in openai_yaml
assert "$shopping-cli" in openai_yaml

print("verification ok")
PY

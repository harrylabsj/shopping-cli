#!/usr/bin/env bash
# Optional dev-time cross-language acceptance for shopping.negotiation/0.1.
#
# Generates real negotiation snapshots with shopping-cli (merchant + buyer
# roles, from an actual sqlite DB), then validates the JSON with the sibling
# Kiwi repo's Ajv + ajv-formats validator (strict format: date-time). It also
# asserts that Kiwi's Ajv rejects a naive (offset-less) created_at, proving
# the two sides agree on the frozen contract.
#
# shopping-cli has no runtime dependency on the kiwi checkout; this script is
# the only bridge and is intentionally NOT part of the unit-test suite.
# Override the kiwi location with KIWI_DIR=/path/to/kiwi.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KIWI_DIR="${KIWI_DIR:-$(cd "$ROOT_DIR/../kiwi" && pwd)}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

if [[ ! -f "$KIWI_DIR/dist/contracts/schemas.js" ]]; then
  echo "kiwi dist not found at $KIWI_DIR/dist/contracts/schemas.js (run npm run build in kiwi)" >&2
  exit 1
fi

python3 - "$ROOT_DIR" "$TMP_DIR" <<'PY'
"""Generate real merchant/buyer negotiation snapshots into JSON files."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, sys.argv[1])

from shopping_cli.core import negotiation as protocol
from shopping_cli.core.catalog import create_merchant, create_product
from shopping_cli.core.conversations import append_message, ensure_conversation
from shopping_cli.core.policies import create_policy
from shopping_cli.db.session import db_session
from shopping_cli.services import negotiation as negotiation_service
from shopping_cli.services import tokens as token_service

tmp_dir = Path(sys.argv[2])
db_file = tmp_dir / "marketplace.sqlite"


def rfc3339(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


with db_session(db_file) as conn:
    create_merchant(
        conn,
        merchant_id="seller-a",
        name="West Lake Tea",
        automation_boundaries="手写陶瓷杯最低可成交价 80 元",
        delivery_eta_minutes=60,
    )
    create_product(
        conn,
        merchant_id="seller-a",
        sku="cup-1",
        title="手写陶瓷杯",
        price=99.0,
        stock=12,
        description="景德镇手工陶瓷杯 350ml",
    )
    create_policy(
        conn,
        merchant_id="seller-a",
        code="return-7d",
        title="签收后 7 天内支持无理由退货。",
        body="签收后 7 天内支持无理由退货。",
    )
    merchant_token = token_service.issue_merchant_token(conn, "seller-a")
    conversation = ensure_conversation(conn, buyer_id="buyer-001", merchant_id="seller-a", sku="cup-1")
    conversation_id = conversation["id"]
    buyer_token = token_service.issue_buyer_token(conn, "buyer-001", conversation_id)
    buyer_message = append_message(conn, conversation_id, "buyer", "ask_price", "买 2 件可以便宜一点吗？")
    buyer_message_id = int(buyer_message["id"])

    merchant = negotiation_service.require_negotiation_actor(conn, merchant_token)
    negotiation_service.claim_message(
        conn, merchant, conversation_id, buyer_message_id, "verify:1:shopping.negotiation/0.1"
    )
    merchant_snapshot = negotiation_service.build_snapshot(conn, merchant, conversation_id, buyer_message_id)

    now = datetime.now(timezone.utc)
    decision = {
        "protocol_version": protocol.PROTOCOL_VERSION,
        "conversation_id": conversation_id,
        "in_reply_to_message_id": buyer_message_id,
        "action": "counter",
        "proposal": {
            "sku": "cup-1",
            "quantity": 2,
            "unit_price": 89.0,
            "currency": "CNY",
            "stock": {
                "status": "available",
                "quantity": merchant_snapshot["stock"]["quantity"],
                "observed_at": rfc3339(now),
                "reserved": False,
            },
            "delivery": {
                "eta_start": rfc3339(now + timedelta(hours=20)),
                "eta_end": rfc3339(now + timedelta(hours=24)),
                "fee": 0,
            },
            "after_sales_policy_refs": ["policy:return-7d"],
            "valid_until": rfc3339(now + timedelta(minutes=5)),
        },
        "open_issues": [],
        "public_message": "如果购买 2 件，单价可调整为 89 元，明天下午送达。",
        "reason_codes": ["within_policy"],
        "request_human_review": False,
    }
    result = negotiation_service.submit_decision(conn, merchant, decision, "verify:1:shopping.negotiation/0.1")
    assert result["result"] == "accepted", result
    merchant_message_id = int(result["message_id"])

    buyer = negotiation_service.require_negotiation_actor(conn, buyer_token)
    negotiation_service.claim_message(
        conn, buyer, conversation_id, merchant_message_id, "verify:2:shopping.negotiation/0.1"
    )
    buyer_snapshot = negotiation_service.build_snapshot(conn, buyer, conversation_id, merchant_message_id)

for name, snapshot in (("snapshot.merchant.json", merchant_snapshot), ("snapshot.buyer.json", buyer_snapshot)):
    (tmp_dir / name).write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"generated {name} (messages: {len(snapshot['messages'])})")
PY

cat > "$TMP_DIR/validate.mjs" <<'JS'
// Validate the generated snapshots with Kiwi's Ajv + ajv-formats validator.
import { readFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

const [tmpDir, kiwiDir] = process.argv.slice(2);
const schemasUrl = pathToFileURL(path.join(kiwiDir, "dist", "contracts", "schemas.js")).href;
const { validateAgainst } = await import(schemasUrl);

let failed = false;
for (const name of ["snapshot.merchant.json", "snapshot.buyer.json"]) {
  const data = JSON.parse(readFileSync(path.join(tmpDir, name), "utf-8"));
  const errors = validateAgainst("snapshot", data);
  if (errors.length > 0) {
    console.error(`${name} FAILED Kiwi Ajv validation:\n  ${errors.join("\n  ")}`);
    failed = true;
  } else {
    console.log(`${name}: passes Kiwi Ajv validation`);
    for (const message of data.messages) {
      if (!/(Z|[+-]\d\d:\d\d)$/.test(message.created_at)) {
        console.error(`${name}: message ${message.id} created_at lacks offset: ${message.created_at}`);
        failed = true;
      }
    }
  }
}

// Negative control: a naive created_at must be rejected by Kiwi's Ajv.
const mutated = JSON.parse(readFileSync(path.join(tmpDir, "snapshot.merchant.json"), "utf-8"));
mutated.messages[0].created_at = "2026-08-04T00:37:20";
if (validateAgainst("snapshot", mutated).length === 0) {
  console.error("naive created_at was NOT rejected by Kiwi Ajv (expected rejection)");
  failed = true;
} else {
  console.log("naive created_at: rejected by Kiwi Ajv as expected");
}

process.exit(failed ? 1 : 0);
JS

node "$TMP_DIR/validate.mjs" "$TMP_DIR" "$KIWI_DIR"

echo "kiwi cross-language snapshot verification ok"

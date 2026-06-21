import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from shopping_cli import cli  # noqa: E402
from shopping_cli.core.catalog import create_merchant  # noqa: E402
from shopping_cli.core.policies import (  # noqa: E402
    create_policy,
    list_policies,
    policy_summary,
    search_policies,
)
from shopping_cli.db.session import db_session, open_connection  # noqa: E402


class PolicyCoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "shopping.sqlite"
        self.conn = open_connection(self.db_path)
        self.addCleanup(self.conn.close)
        create_merchant(self.conn, "yunqi-tea", "云栖茶礼坊", city="杭州")
        create_merchant(self.conn, "other-shop", "别家店", city="上海")

    def _seed_tea_policies(self):
        create_policy(
            self.conn,
            "yunqi-tea",
            code="POL-SHIP-1",
            category="配送政策 [POL-SHIP]",
            title="当日发货",
            body="工作日 16:00 前完成付款当天发出，不承诺保证今天送达。",
            tags="配送,发货",
        )
        create_policy(
            self.conn,
            "yunqi-tea",
            code="POL-INV-1",
            category="发票政策 [POL-INV]",
            title="类型",
            body="可开增值税普通发票（电子）与增值税专用发票。",
            tags="发票,开票",
        )
        create_policy(
            self.conn,
            "yunqi-tea",
            code="POL-BULK-2",
            category="团购批量政策 [POL-BULK]",
            title="折扣",
            body="具体折扣率需商家逐单确认，AI 不得自创折扣。",
            tags="团购,批量",
            high_risk=True,
        )

    def test_create_and_summary_round_trip(self):
        policy = create_policy(
            self.conn,
            "yunqi-tea",
            code="POL-AS-1",
            category="售后政策 [POL-AS]",
            title="七天无理由",
            body="食品类未拆封可在签收 7 天内申请退货。",
            tags="售后,退货",
        )
        self.assertEqual(policy["merchant_id"], "yunqi-tea")
        self.assertEqual(policy["code"], "POL-AS-1")
        self.assertEqual(policy["tags"], ["售后", "退货"])
        self.assertFalse(policy["high_risk"])
        fetched = policy_summary(self.conn, "yunqi-tea", "POL-AS-1")
        self.assertEqual(fetched, policy)

    def test_high_risk_flag_persists(self):
        policy = create_policy(
            self.conn,
            "yunqi-tea",
            code="POL-AS-2",
            body="赔偿金额由人工核定，AI 不得直接承诺。",
            high_risk=True,
        )
        self.assertTrue(policy["high_risk"])

    def test_blank_body_is_rejected(self):
        with self.assertRaises(SystemExit):
            create_policy(self.conn, "yunqi-tea", code="POL-X", body="   ")

    def test_unknown_merchant_is_rejected(self):
        with self.assertRaises(SystemExit):
            create_policy(self.conn, "ghost", code="POL-X", body="something")

    def test_duplicate_code_for_same_merchant_is_rejected(self):
        create_policy(self.conn, "yunqi-tea", code="POL-SHIP-1", body="原条目")
        with self.assertRaises(SystemExit):
            create_policy(self.conn, "yunqi-tea", code="POL-SHIP-1", body="重复条目")

    def test_same_code_allowed_across_merchants(self):
        create_policy(self.conn, "yunqi-tea", code="POL-SHIP-1", body="云栖发货")
        # composite primary key (merchant_id, code) — no collision across merchants
        other = create_policy(self.conn, "other-shop", code="POL-SHIP-1", body="别家发货")
        self.assertEqual(other["merchant_id"], "other-shop")
        self.assertEqual(other["body"], "别家发货")

    def test_search_chinese_terms_hit_traceable_clause(self):
        self._seed_tea_policies()
        for query, expected_code in (("发货", "POL-SHIP-1"), ("发票", "POL-INV-1"), ("团购", "POL-BULK-2")):
            with self.subTest(query=query):
                results = search_policies(self.conn, query=query, merchant_id="yunqi-tea")
                self.assertTrue(results, f"no match for {query}")
                self.assertEqual(results[0]["code"], expected_code)
                self.assertGreater(results[0]["match_score"], 0)

    def test_search_surfaces_high_risk(self):
        self._seed_tea_policies()
        results = search_policies(self.conn, query="折扣", merchant_id="yunqi-tea")
        self.assertEqual(results[0]["code"], "POL-BULK-2")
        self.assertTrue(results[0]["high_risk"])

    def test_search_is_scoped_to_merchant(self):
        self._seed_tea_policies()
        create_policy(self.conn, "other-shop", code="POL-INV-1", body="别家发票政策", tags="发票")
        scoped = search_policies(self.conn, query="发票", merchant_id="yunqi-tea")
        self.assertEqual({r["merchant_id"] for r in scoped}, {"yunqi-tea"})

    def test_list_filters_by_merchant_and_category(self):
        self._seed_tea_policies()
        create_policy(self.conn, "other-shop", code="POL-INV-9", category="发票政策 [POL-INV]", body="别家发票")
        by_merchant = list_policies(self.conn, merchant_id="yunqi-tea")
        self.assertEqual({p["merchant_id"] for p in by_merchant}, {"yunqi-tea"})
        by_category = list_policies(self.conn, merchant_id="yunqi-tea", category="发票政策 [POL-INV]")
        self.assertEqual([p["code"] for p in by_category], ["POL-INV-1"])

    def test_empty_query_returns_all_for_merchant(self):
        self._seed_tea_policies()
        results = search_policies(self.conn, query="", merchant_id="yunqi-tea")
        self.assertEqual(len(results), 3)


class PolicyCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "shopping.sqlite"
        with db_session(self.db_path) as conn:
            create_merchant(conn, "yunqi-tea", "云栖茶礼坊", city="杭州")

    def run_cli(self, *args):
        output = StringIO()
        with redirect_stdout(output):
            cli.main(["--db", str(self.db_path), *args])
        return output.getvalue()

    def parse_single_json_value(self, output):
        decoder = json.JSONDecoder()
        value, end = decoder.raw_decode(output)
        self.assertEqual(output[end:].strip(), "")
        return value

    def test_policy_add_and_search_via_cli(self):
        added = self.parse_single_json_value(
            self.run_cli(
                "policy", "add",
                "--merchant", "yunqi-tea",
                "--code", "POL-INV-1",
                "--category", "发票政策 [POL-INV]",
                "--title", "类型",
                "--body", "可开增值税普通发票与专用发票。",
                "--tags", "发票,开票",
                "--format", "json",
            )
        )
        self.assertTrue(added["ok"])
        self.assertEqual(added["policy"]["code"], "POL-INV-1")

        found = self.parse_single_json_value(
            self.run_cli("search", "policies", "--query", "发票", "--merchant", "yunqi-tea", "--format", "json")
        )
        self.assertTrue(found["ok"])
        self.assertEqual(found["results"][0]["code"], "POL-INV-1")

    def test_policy_high_risk_flag_via_cli(self):
        added = self.parse_single_json_value(
            self.run_cli(
                "policy", "add",
                "--merchant", "yunqi-tea",
                "--code", "POL-AS-2",
                "--body", "赔偿由人工核定，AI 不得承诺。",
                "--high-risk",
                "--format", "json",
            )
        )
        self.assertTrue(added["policy"]["high_risk"])

    def test_policy_show_via_cli(self):
        self.run_cli(
            "policy", "add",
            "--merchant", "yunqi-tea",
            "--code", "POL-SHIP-1",
            "--body", "工作日 16:00 前付款当天发出。",
            "--format", "json",
        )
        shown = self.parse_single_json_value(
            self.run_cli("policy", "show", "--merchant", "yunqi-tea", "--code", "POL-SHIP-1", "--format", "json")
        )
        self.assertTrue(shown["ok"])
        self.assertEqual(shown["policy"]["code"], "POL-SHIP-1")


if __name__ == "__main__":
    unittest.main()

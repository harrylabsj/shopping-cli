import tempfile
import unittest
from pathlib import Path

from shopping_cli.core import catalog
from shopping_cli.db.session import db_session


class CatalogSearchIndexTest(unittest.TestCase):
    def test_product_search_index_tracks_updates_and_backfills(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                if not catalog.product_search_index_available(conn):
                    self.skipTest("SQLite FTS5 is unavailable")

                catalog.create_merchant(conn, merchant_id="seller-a", name="West Lake Tea", tags="tea")
                catalog.create_product(
                    conn,
                    merchant_id="seller-a",
                    sku="tea-a",
                    title="Longjing Gift Box",
                    price=88,
                    stock=5,
                    tags="longjing,gift,龙井,礼盒",
                )

                indexed_count = conn.execute(f"select count(*) from {catalog.PRODUCT_SEARCH_INDEX_TABLE}").fetchone()[0]
                self.assertEqual(indexed_count, 1)
                self.assertEqual([item["sku"] for item in catalog.search_products(conn, query="longjing")], ["tea-a"])
                self.assertEqual([item["sku"] for item in catalog.search_products(conn, query="今天想买龙井礼盒")], ["tea-a"])

                catalog.update_product(conn, "tea-a", title="Dragon Well Gift Box", tags="dragonwell,gift")
                self.assertEqual([item["sku"] for item in catalog.search_products(conn, query="dragonwell")], ["tea-a"])
                self.assertEqual(catalog.search_products(conn, query="longjing"), [])

                catalog.update_merchant(conn, "seller-a", tags="longjing")
                self.assertEqual([item["sku"] for item in catalog.search_products(conn, query="longjing")], ["tea-a"])

                conn.execute(f"delete from {catalog.PRODUCT_SEARCH_INDEX_TABLE}")
                stats = catalog.product_search_index_stats(conn)
                self.assertFalse(stats["healthy"])
                self.assertEqual(stats["missing_count"], 1)
                self.assertEqual([item["sku"] for item in catalog.search_products(conn, query="dragonwell")], ["tea-a"])

    def test_product_search_index_stats_detects_stale_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                if not catalog.product_search_index_available(conn):
                    self.skipTest("SQLite FTS5 is unavailable")

                catalog.create_merchant(conn, merchant_id="seller-a", name="West Lake Tea", tags="tea")
                catalog.create_product(
                    conn,
                    merchant_id="seller-a",
                    sku="tea-a",
                    title="Longjing Gift Box",
                    price=88,
                    stock=5,
                    tags="longjing,gift",
                )
                self.assertTrue(catalog.product_search_index_stats(conn)["healthy"])

                conn.execute("update products set title = 'Out Of Band Matcha Box' where sku = 'tea-a'")
                stale_stats = catalog.product_search_index_stats(conn)
                self.assertFalse(stale_stats["healthy"])
                self.assertEqual(stale_stats["stale_count"], 1)

                self.assertTrue(catalog.rebuild_product_search_index(conn))
                rebuilt_stats = catalog.product_search_index_stats(conn)
                self.assertTrue(rebuilt_stats["healthy"])
                self.assertEqual([item["sku"] for item in catalog.search_products(conn, query="matcha")], ["tea-a"])

    def test_cjk_fts_pagination_is_stable_and_non_overlapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_file = Path(tmp) / "shopping.sqlite"
            with db_session(db_file) as conn:
                if not catalog.product_search_index_available(conn):
                    self.skipTest("SQLite FTS5 is unavailable")

                catalog.create_merchant(conn, merchant_id="seller-a", name="西湖茶庄", city="杭州", tags="tea")
                titles = [
                    "西湖龙井礼盒装",
                    "明前龙井嫩芽罐装",
                    "龙井茶礼家庭装",
                    "狮峰龙井精品礼盒",
                    "手工龙井伴手礼",
                    "龙井桂花组合装",
                ]
                for index, title in enumerate(titles):
                    catalog.create_product(
                        conn,
                        merchant_id="seller-a",
                        sku=f"tea-{index:02d}",
                        title=title,
                        price=88 + index,
                        stock=5,
                        tags="龙井,茶叶",
                    )

                for query in ("龙井", "今天想买西湖龙井礼盒装送人"):
                    with self.subTest(query=query):
                        full = [item["sku"] for item in catalog.search_products(conn, query=query, limit=50)]
                        self.assertEqual(len(full), len(titles))

                        def page(offset: int) -> list[str]:
                            return [
                                item["sku"]
                                for item in catalog.search_products(conn, query=query, limit=2, offset=offset)
                            ]

                        pages = [page(offset) for offset in (0, 2, 4, 6)]
                        self.assertEqual(pages[3], [])
                        merged = pages[0] + pages[1] + pages[2]
                        self.assertEqual(len(merged), len(set(merged)))
                        self.assertEqual(merged, full)
                        repeated = [page(offset) for offset in (0, 2, 4)]
                        self.assertEqual(repeated, pages[:3])


if __name__ == "__main__":
    unittest.main()

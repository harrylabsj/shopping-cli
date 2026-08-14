"""CSV/Excel 数据源适配器测试（Issue 14 / §6.3）。"""

from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from shopping_cli.data_sources.adapter import AdapterError, SyncContext, run
from shopping_cli.data_sources.csv_excel_source import (
    MAX_IMPORT_CELL_CHARS,
    MAX_IMPORT_FILE_BYTES,
    SOURCE_CSV_EXCEL,
    CsvExcelSyncConfig,
    read_rows,
    sync_csv_excel,
)
from shopping_cli.db.session import open_connection

NOW = "2026-08-13T12:00:00Z"

CSV_GOOD = "sku,title,price,stock,currency,description\nSKU-001,Coffee,99.0,12,CNY,\"Hot coffee\"\nSKU-002,Tea,42,5,CNY,\n"
CSV_BAD_PRICE = "sku,title,price,stock\nSKU-001,Coffee,abc,12\n"
CSV_LOSSY_PRICE = "sku,title,price,stock\nSKU-001,Coffee,19.995,12\n"


def _setup_db(tmp: str, merchant_id: str = "merchant-1") -> object:
    conn = open_connection(str(Path(tmp) / "shop.sqlite"))
    conn.execute(
        "insert into merchants(id, name, created_at, updated_at) values (?, 'Acme', 't', 't')",
        (merchant_id,),
    )
    conn.commit()
    return conn


def _write(tmp: str, name: str, content: str) -> str:
    p = Path(tmp) / name
    p.write_text(content, encoding="utf-8")
    return str(p)


class CsvImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.conn = _setup_db(self.tmp)

    def tearDown(self) -> None:
        self.conn.close()

    def test_csv_import_upserts_with_source(self) -> None:
        path = _write(self.tmp, "p.csv", CSV_GOOD)
        report = sync_csv_excel(self.conn, CsvExcelSyncConfig(path=path, default_merchant_id="merchant-1"), now=lambda: NOW)
        self.assertEqual(report.fetched, 2)
        self.assertEqual(report.upserted, 2)
        self.assertEqual(report.errors, [])
        row = self.conn.execute("select title, price, stock, source from products where sku='SKU-001'").fetchone()
        self.assertEqual(row[0], "Coffee")
        self.assertEqual(row[1], 99.0)
        self.assertEqual(row[2], 12)
        self.assertEqual(row[3], SOURCE_CSV_EXCEL)

    def test_csv_bad_price_fails_closed(self) -> None:
        path = _write(self.tmp, "bad.csv", CSV_BAD_PRICE)
        report = sync_csv_excel(self.conn, CsvExcelSyncConfig(path=path, default_merchant_id="merchant-1"), now=lambda: NOW)
        self.assertEqual(report.upserted, 0)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("invalid price", report.errors[0])

    def test_csv_lossy_price_rejected(self) -> None:
        path = _write(self.tmp, "lossy.csv", CSV_LOSSY_PRICE)
        report = sync_csv_excel(self.conn, CsvExcelSyncConfig(path=path, default_merchant_id="merchant-1"), now=lambda: NOW)
        self.assertEqual(report.upserted, 0)
        self.assertIn("2-decimal", report.errors[0])

    def test_local_authoritative_row_conflict_skipped(self) -> None:
        path = _write(self.tmp, "p.csv", CSV_GOOD)
        sync_csv_excel(self.conn, CsvExcelSyncConfig(path=path, default_merchant_id="merchant-1"), now=lambda: NOW)
        # 本地手改（source=local）后再次导入 → 冲突跳过，不覆盖。
        self.conn.execute("update products set source='local' where sku='SKU-001'")
        self.conn.commit()
        report = sync_csv_excel(self.conn, CsvExcelSyncConfig(path=path, default_merchant_id="merchant-1"), now=lambda: NOW)
        self.assertEqual(report.upserted, 1)  # SKU-002 仍可导入
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(report.conflicts[0]["sku"], "SKU-001")
        row = self.conn.execute("select source from products where sku='SKU-001'").fetchone()
        self.assertEqual(row[0], "local")

    def test_cross_tenant_allowed_boundary(self) -> None:
        path = _write(self.tmp, "p.csv", "sku,title,price,stock,merchant_id\nSKU-001,X,1,1,merchant-1\nSKU-002,Y,2,2,merchant-2\n")
        # allowed_merchant=merchant-1：merchant-2 的行被跳过（跨租户防护）。
        report = sync_csv_excel(
            self.conn,
            CsvExcelSyncConfig(path=path, default_merchant_id="", allowed_merchant_id="merchant-1"),
            now=lambda: NOW,
        )
        self.assertEqual(report.upserted, 1)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(len(report.errors), 1)

    def test_unknown_merchant_fails_closed(self) -> None:
        path = _write(self.tmp, "p.csv", CSV_GOOD)
        with self.assertRaises(AdapterError):
            sync_csv_excel(self.conn, CsvExcelSyncConfig(path=path, default_merchant_id="nope"), now=lambda: NOW)

    def test_file_size_limit_is_enforced_before_parse(self) -> None:
        path = Path(self.tmp) / "oversized.csv"
        path.write_bytes(b"x" * (MAX_IMPORT_FILE_BYTES + 1))
        with self.assertRaisesRegex(AdapterError, "exceeds"):
            read_rows(str(path))

    def test_csv_cell_limit_is_fail_closed(self) -> None:
        path = _write(
            self.tmp,
            "large-cell.csv",
            f"sku,title,price,stock\nSKU-001,{'x' * (MAX_IMPORT_CELL_CHARS + 1)},1,1\n",
        )
        # 超长 cell 由 csv.field_size_limit(MAX_IMPORT_CELL_CHARS) 在读取时拒绝
        # （csv.Error → AdapterError，"field larger than field limit"）；per-cell
        # "characters" 检查是冗余防线。断言 fail-closed（AdapterError）即可。
        with self.assertRaises(AdapterError):
            read_rows(path)


class XlsxImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.conn = _setup_db(self.tmp)

    def tearDown(self) -> None:
        self.conn.close()

    def _make_xlsx(self) -> str:
        # 最小 .xlsx：sharedStrings + sheet1（inline string 头 + 数值）。
        p = Path(self.tmp) / "p.xlsx"
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
            )
            z.writestr(
                "xl/workbook.xml",
                f'<workbook xmlns="{ns}"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></sheets></workbook>',
            )
            z.writestr(
                "xl/worksheets/sheet1.xml",
                f'<worksheet xmlns="{ns}"><sheetData>'
                f'<row r="1"><c r="A1" t="inlineStr"><is><t>sku</t></is></c><c r="B1" t="inlineStr"><is><t>title</t></is></c><c r="C1" t="inlineStr"><is><t>price</t></is></c><c r="D1" t="inlineStr"><is><t>stock</t></is></c></row>'
                f'<row r="2"><c r="A2" t="inlineStr"><is><t>SKU-X1</t></is></c><c r="B2" t="inlineStr"><is><t>Mug</t></is></c><c r="C2"><v>55</v></c><c r="D2"><v>8</v></c></row>'
                f'</sheetData></worksheet>',
            )
        return str(p)

    def test_xlsx_import(self) -> None:
        path = self._make_xlsx()
        report = sync_csv_excel(self.conn, CsvExcelSyncConfig(path=path, default_merchant_id="merchant-1"), now=lambda: NOW)
        self.assertEqual(report.upserted, 1)
        row = self.conn.execute("select title, price, stock, source from products where sku='SKU-X1'").fetchone()
        self.assertEqual(row[0], "Mug")
        self.assertEqual(row[1], 55.0)
        self.assertEqual(row[2], 8)
        self.assertEqual(row[3], SOURCE_CSV_EXCEL)

    def test_xlsx_read_rows_via_adapter(self) -> None:
        path = self._make_xlsx()
        rows = read_rows(path)
        self.assertEqual(rows[0]["sku"], "SKU-X1")
        self.assertEqual(rows[0]["title"], "Mug")


class AdapterSdkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.conn = _setup_db(self.tmp)

    def tearDown(self) -> None:
        self.conn.close()

    def test_registry_lists_csv_excel(self) -> None:
        from shopping_cli.data_sources.adapter import registered_adapters

        adapters = registered_adapters()
        self.assertIn("csv_excel", adapters)
        self.assertIn("csv_excel", adapters["csv_excel"].name)

    def test_run_unknown_adapter_fails_closed(self) -> None:
        from shopping_cli.data_sources.adapter import run

        with self.assertRaises(AdapterError):
            run("not-a-real-adapter", SyncContext(conn=self.conn))

    def test_run_csv_excel_adapter_via_sdk(self) -> None:
        path = _write(self.tmp, "p.csv", CSV_GOOD)
        report = run(
            "csv_excel",
            SyncContext(conn=self.conn, default_merchant_id="merchant-1", config={"path": path}, now=lambda: NOW),
        )
        self.assertEqual(report.upserted, 2)


if __name__ == "__main__":
    unittest.main()

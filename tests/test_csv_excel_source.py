"""CSV/Excel 数据源适配器测试（Issue 14 / §6.3）。"""

from __future__ import annotations

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

    def _make_xlsx_sheet(self, sheet_xml: str, shared_xml: str | None = None) -> str:
        """按给定 sheet1/sharedStrings XML 构造 .xlsx（审查 S-M5 用例用）。"""
        p = Path(self.tmp) / "s.xlsx"
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
            if shared_xml is not None:
                z.writestr("xl/sharedStrings.xml", shared_xml)
            z.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        return str(p)

    def test_xlsx_empty_cell_does_not_shift_columns(self) -> None:
        # 审查 S-M5：中间可选列（category）为空单元格（真实 Excel 会省略或只带
        # 样式）——按文档顺序对齐会把 price 左移到 category 位、stock 左移到
        # price 位，静默写坏数据（本用例 6 列下旧代码会静默写入 price=8/stock=10）。
        # 修复后按 r 属性定位，空列补 ""，值落在正确列。
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        sheet = (
            f'<worksheet xmlns="{ns}"><sheetData>'
            f'<row r="1"><c r="A1" t="inlineStr"><is><t>sku</t></is></c><c r="B1" t="inlineStr"><is><t>title</t></is></c><c r="C1" t="inlineStr"><is><t>category</t></is></c><c r="D1" t="inlineStr"><is><t>price</t></is></c><c r="E1" t="inlineStr"><is><t>stock</t></is></c><c r="F1" t="inlineStr"><is><t>note</t></is></c></row>'
            f'<row r="2"><c r="A2" t="inlineStr"><is><t>SKU-E1</t></is></c><c r="B2" t="inlineStr"><is><t>Mug</t></is></c><c r="C2"/><c r="D2"><v>55</v></c><c r="E2"><v>8</v></c><c r="F2" t="inlineStr"><is><t>10</t></is></c></row>'
            f"</sheetData></worksheet>"
        )
        path = self._make_xlsx_sheet(sheet)
        rows = read_rows(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "SKU-E1")
        self.assertEqual(rows[0]["title"], "Mug")
        self.assertEqual(rows[0]["category"], "")
        self.assertEqual(rows[0]["price"], "55")
        self.assertEqual(rows[0]["stock"], "8")
        self.assertEqual(rows[0]["note"], "10")
        # 端到端：price/stock 必须落在正确列（旧代码此场景静默写 8/10）。
        report = sync_csv_excel(
            self.conn, CsvExcelSyncConfig(path=path, default_merchant_id="merchant-1"), now=lambda: NOW
        )
        self.assertEqual(report.upserted, 1)
        row = self.conn.execute("select title, category, price, stock from products where sku='SKU-E1'").fetchone()
        self.assertEqual(row[0], "Mug")
        self.assertEqual(row[1], "")
        self.assertEqual(row[2], 55.0)
        self.assertEqual(row[3], 8)

    def test_xlsx_sparse_cells_align_by_r_attribute(self) -> None:
        # 稀疏单元格（A/C 有值、B 省略 + 末尾多列）：r 属性定位必须与表头对齐。
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        sheet = (
            f'<worksheet xmlns="{ns}"><sheetData>'
            f'<row r="1"><c r="A1" t="inlineStr"><is><t>sku</t></is></c><c r="B1" t="inlineStr"><is><t>title</t></is></c><c r="C1" t="inlineStr"><is><t>price</t></is></c><c r="D1" t="inlineStr"><is><t>stock</t></is></c><c r="E1" t="inlineStr"><is><t>note</t></is></c></row>'
            f'<row r="2"><c r="A2" t="inlineStr"><is><t>SKU-E2</t></is></c><c r="C2"><v>9</v></c></row>'
            f"</sheetData></worksheet>"
        )
        path = self._make_xlsx_sheet(sheet)
        rows = read_rows(path)
        self.assertEqual(rows[0]["sku"], "SKU-E2")
        self.assertEqual(rows[0]["title"], "")
        self.assertEqual(rows[0]["price"], "9")
        self.assertEqual(rows[0]["stock"], "")
        self.assertEqual(rows[0]["note"], "")

    def test_xlsx_shared_string_bad_index_fails_closed(self) -> None:
        # 审查 L-1：非整数共享字符串索引不再抛裸 ValueError（→ AdapterError）。
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        sheet = (
            f'<worksheet xmlns="{ns}"><sheetData>'
            f'<row r="1"><c r="A1" t="inlineStr"><is><t>sku</t></is></c><c r="B1" t="inlineStr"><is><t>title</t></is></c></row>'
            f'<row r="2"><c r="A2" t="s"><v>abc</v></c></row>'
            f"</sheetData></worksheet>"
        )
        shared = f'<sst xmlns="{ns}"><si><t>SKU-BAD</t></si></sst>'
        path = self._make_xlsx_sheet(sheet, shared)
        with self.assertRaisesRegex(AdapterError, "shared string index"):
            read_rows(path)

    def test_xlsx_shared_string_out_of_range_fails_closed(self) -> None:
        # 越界共享字符串索引不再静默给 ""（fail-closed）。
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        sheet = (
            f'<worksheet xmlns="{ns}"><sheetData>'
            f'<row r="1"><c r="A1" t="inlineStr"><is><t>sku</t></is></c><c r="B1" t="inlineStr"><is><t>title</t></is></c></row>'
            f'<row r="2"><c r="A2" t="s"><v>99</v></c></row>'
            f"</sheetData></worksheet>"
        )
        shared = f'<sst xmlns="{ns}"><si><t>SKU-OK</t></si></sst>'
        path = self._make_xlsx_sheet(sheet, shared)
        with self.assertRaisesRegex(AdapterError, "out of range"):
            read_rows(path)


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

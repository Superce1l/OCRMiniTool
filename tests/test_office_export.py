# -*- coding: utf-8 -*-
"""tests/test_office_export.py — E9/E10：stdlib 手写 OOXML 的 docx/xlsx 写入器

分两层（与任务约定一致）：

  • **结构类（始终跑）**：只用标准库（zipfile / ElementTree），断言包成员齐全、
    所有 XML 可解析、标题样式/分页符/gridSpan/mergeCells/无表格报错等结构事实。
  • **真解析库校验类（skipUnless）**：用 python-docx / openpyxl 打开同一个字节流，
    断言段落样式/表格行列/单元格值/sheet 数/merged_cells —— 证明"不是自说自话"。
    本机校验库在 `D:/Project/_work/validators`（用 PYTHONPATH 指过去即可跑）。

输入全部来自 **真实渲染管线**：`ocr_postprocess.build_document(raw)` 的输出
（含真实 HTML 表格、colspan/rowspan、图片块、Markdown 行内标记），不造理想输入。

不跑推理、不联网、不写仓库文件（落盘测试写临时目录）。
"""
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ocr_postprocess as post  # noqa: E402
import office_export as oe  # noqa: E402

try:                                   # 校验库（有则跑，无则整类跳过）
    import docx as _python_docx
    HAVE_DOCX = True
except Exception:                      # pragma: no cover - 取决于环境
    _python_docx = None
    HAVE_DOCX = False

try:
    import openpyxl as _openpyxl
    HAVE_OPENPYXL = True
except Exception:                      # pragma: no cover - 取决于环境
    _openpyxl = None
    HAVE_OPENPYXL = False

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# 真实模型 raw 输出形态：两页、标题/正文/表格（含 th、colspan、rowspan）/图片块
RAW_REAL = (
    "<PAGE><BLOCK>title<LEVEL1>[40,40,960,120]<FORK>第一章 概述"
    "<BLOCK>text[40,130,960,300]<FORK>这是 **加粗** 与 `code` 以及 "
    "[链接](http://example.com) 的正文 &amp; 实体。"
    "<BLOCK>table[40,310,960,600]<FORK>"
    "<table><tr><th>项目</th><th>值</th><th>备注</th></tr>"
    "<tr><td colspan=\"2\">合并两列</td><td>a1</td></tr>"
    "<tr><td rowspan=\"2\">跨两行</td><td>1</td><td>2.5</td></tr>"
    "<tr><td>3</td><td>4</td></tr></table>"
    "<BLOCK>image<LEVEL1>[40,610,400,800]<FORK>![](page1_40_610_400_800.jpg)"
    "<PAGE><BLOCK>title<LEVEL2>[40,40,900,100]<FORK>1.1 小节"
    "<BLOCK>text[40,110,900,200]<FORK>第二页正文。"
    "<BLOCK>table[40,210,900,400]<FORK>"
    "<table><tr><td>t2a</td><td>t2b</td></tr></table>"
)

# 另一类真实形态：四级标题（要压到 Heading 3）+ 无井号标题（用 level 兜底）
RAW_LEVELS = (
    "<PAGE><BLOCK>title<LEVEL4>[40,40,900,90]<FORK>四级标题"
    "<BLOCK>title<LEVEL2>[40,100,900,150]<FORK>无井号二级"
    "<BLOCK>text[40,160,900,240]<FORK>正文。"
)


def _doc(raw: str) -> dict:
    """真实渲染管线：raw → build_document 的页信封（dict 形态）。"""
    return post.build_document(raw)


def _zip(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


def _part(data: bytes, name: str) -> bytes:
    return _zip(data).read(name)


def _members(data: bytes) -> set[str]:
    return set(_zip(data).namelist())


def _xml(data: bytes, name: str) -> ET.Element:
    return ET.fromstring(_part(data, name))


def _docx_text(data: bytes) -> str:
    """document.xml 里所有 w:t 拼起来（用于"标记是否被剥掉"这类断言）。"""
    root = _xml(data, "word/document.xml")
    return "".join(el.text or "" for el in root.iter(W + "t"))


def _pstyles(data: bytes) -> list[str]:
    root = _xml(data, "word/document.xml")
    return [el.get(W + "val") for el in root.iter(W + "pStyle")]


class DocxStructureTest(unittest.TestCase):
    """docx 包结构（始终跑；不依赖 python-docx）。"""

    @classmethod
    def setUpClass(cls):
        cls.pages = _doc(RAW_REAL)
        cls.data = oe.docx_bytes(cls.pages, title="OCR 导出")

    def test_required_members(self):
        need = {"[Content_Types].xml", "_rels/.rels", "word/document.xml",
                "word/_rels/document.xml.rels", "word/styles.xml"}
        self.assertTrue(need <= _members(self.data), _members(self.data))

    def test_all_xml_parts_parse(self):
        zf = _zip(self.data)
        for name in zf.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                with self.subTest(part=name):
                    ET.fromstring(zf.read(name))

    def test_heading_styles_present(self):
        styles = _pstyles(self.data)
        self.assertIn("Heading1", styles)
        self.assertIn("Heading2", styles)
        self.assertIn("第一章 概述", _docx_text(self.data))

    def test_body_markdown_stripped(self):
        text = _docx_text(self.data)
        for marker in ("**", "`", "](", "!["):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, text)
        self.assertIn("加粗", text)
        self.assertIn("链接", text)

    def test_image_block_skipped(self):
        text = _docx_text(self.data)
        self.assertNotIn("page1_", text)
        self.assertNotIn("图片", text)

    def test_page_break_between_pages(self):
        root = _xml(self.data, "word/document.xml")
        breaks = [b for b in root.iter(W + "br")
                  if b.get(W + "type") == "page"]
        self.assertEqual(len(breaks), len(self.pages["pages"]) - 1)

    def test_table_grid_span(self):
        root = _xml(self.data, "word/document.xml")
        spans = [el.get(W + "val") for el in root.iter(W + "gridSpan")]
        self.assertIn("2", spans)
        grids = [el for el in root.iter(W + "tblGrid")]
        self.assertEqual(len(grids), 2)
        self.assertEqual(len(list(grids[0].iter(W + "gridCol"))), 3)

    def test_table_rowspan_is_conservative(self):
        """rowspan 近似：文字只在起始格出现一次，后续行用空单元格占位。"""
        root = _xml(self.data, "word/document.xml")
        texts = [el.text or "" for el in root.iter(W + "t")]
        self.assertEqual(texts.count("跨两行"), 1)
        self.assertNotIn("vMerge", _part(self.data, "word/document.xml").decode())

    def test_entities_escaped_then_decoded(self):
        text = _docx_text(self.data)
        self.assertIn("&", text)                      # 实体被解码回字符
        self.assertIn("实体", text)

    def test_core_title_written_or_omitted(self):
        core = _part(self.data, "docProps/core.xml").decode("utf-8")
        self.assertIn("OCR 导出", core)
        empty = _part(oe.docx_bytes([]), "docProps/core.xml").decode("utf-8")
        self.assertNotIn("<dc:title>", empty)

    def test_empty_pages_still_valid(self):
        data = oe.docx_bytes([])
        self.assertIn("word/document.xml", _members(data))
        self.assertEqual(_docx_text(data), "")
        root = _xml(data, "word/document.xml")
        self.assertEqual(len(list(root.iter(W + "p"))), 0)

    def test_empty_text_block_skipped(self):
        pages = [{"page": 1, "blocks": [
            {"type": "text", "text": "", "label": "text"},
            {"type": "text", "text": "   ", "label": "text"},
            {"type": "text", "text": "有内容", "label": "text"}]}]
        data = oe.docx_bytes(pages)
        self.assertEqual(_docx_text(data), "有内容")

    def test_heading_level_clamped_to_three(self):
        """渲染器把 LEVEL4 变成 `####`：导出压到 Heading 3（Word 只有 1..3 级用得上）。"""
        data = oe.docx_bytes(_doc(RAW_LEVELS))
        styles = _pstyles(data)
        self.assertIn("Heading3", styles)
        self.assertIn("Heading2", styles)
        self.assertIn("四级标题", _docx_text(data))

    def test_heading_without_hash_uses_block_level(self):
        """手工构造的块可能没有 `#`：退回 block.level（这里 2 → Heading2）。"""
        pages = [{"page": 1, "blocks": [
            {"type": "title", "level": 2, "text": "无井号标题",
             "label": "title"}]}]
        data = oe.docx_bytes(pages)
        self.assertIn("Heading2", _pstyles(data))
        self.assertEqual(_docx_text(data), "无井号标题")

    def test_accepts_envelope_and_page_list(self):
        pages = _doc(RAW_REAL)
        env = oe.docx_bytes(pages, title="T")
        lst = oe.docx_bytes(pages["pages"], title="T")
        self.assertEqual(_part(env, "word/document.xml"),
                         _part(lst, "word/document.xml"))

    def test_write_docx_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.docx"
            got = oe.write_docx(target, _doc(RAW_REAL), title="写盘")
            self.assertEqual(Path(got), target)
            self.assertEqual(target.read_bytes(),
                             oe.docx_bytes(_doc(RAW_REAL), title="写盘"))

    def test_br_inside_cell_becomes_paragraphs(self):
        pages = [{"page": 1, "blocks": [
            {"type": "table", "label": "table",
             "text": "<table><tr><td>A<br>B</td></tr></table>"}]}]
        data = oe.docx_bytes(pages)
        root = _xml(data, "word/document.xml")
        texts = [el.text or "" for el in root.iter(W + "t")]
        self.assertIn("A", texts)
        self.assertIn("B", texts)


class XlsxStructureTest(unittest.TestCase):
    """xlsx 包结构（始终跑；不依赖 openpyxl）。"""

    @classmethod
    def setUpClass(cls):
        cls.pages = _doc(RAW_REAL)
        cls.data = oe.xlsx_bytes(cls.pages, name="清单")

    def test_required_members(self):
        need = {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                "xl/_rels/workbook.xml.rels", "xl/worksheets/sheet1.xml",
                "xl/worksheets/sheet2.xml", "xl/styles.xml"}
        self.assertTrue(need <= _members(self.data), _members(self.data))

    def test_all_xml_parts_parse(self):
        zf = _zip(self.data)
        for name in zf.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                with self.subTest(part=name):
                    ET.fromstring(zf.read(name))

    def test_one_sheet_per_table(self):
        root = _xml(self.data, "xl/workbook.xml")
        names = [el.get("name") for el in root.iter(S + "sheet")]
        self.assertEqual(names, ["表格1", "表格2"])

    def test_merge_cells_from_colspan_and_rowspan(self):
        sheet = _part(self.data, "xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn('<mergeCells count="2">', sheet)
        self.assertIn('ref="A2:B2"', sheet)             # colspan=2
        self.assertIn('ref="A3:A4"', sheet)             # rowspan=2

    def test_only_origin_cell_has_value(self):
        sheet = _part(self.data, "xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("合并两列", sheet)
        self.assertNotIn('r="B2"', sheet)              # 合并区域非左上角不写值
        self.assertNotIn('r="A4"', sheet)

    def test_numeric_cells_are_numbers(self):
        sheet = _part(self.data, "xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn('<c r="B3"><v>1</v></c>', sheet)
        self.assertIn('<c r="C3"><v>2.5</v></c>', sheet)

    def test_column_widths_present(self):
        sheet = _part(self.data, "xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("<cols>", sheet)
        self.assertIn('customWidth="1"', sheet)

    def test_inline_strings_preserved(self):
        sheet = _part(self.data, "xl/worksheets/sheet2.xml").decode("utf-8")
        self.assertIn("t2a", sheet)
        self.assertIn('t="inlineStr"', sheet)

    def test_cell_br_becomes_newline(self):
        pages = [{"page": 1, "blocks": [
            {"type": "table", "label": "table",
             "text": "<table><tr><td>A<br>B</td></tr></table>"}]}]
        data = oe.xlsx_bytes(pages)
        sheet = _part(data, "xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("A\nB", sheet)

    def test_no_tables_raises(self):
        pages = [{"page": 1, "blocks": [
            {"type": "text", "text": "只有正文", "label": "text"}]}]
        with self.assertRaises(ValueError) as ctx:
            oe.xlsx_bytes(pages)
        self.assertEqual(str(ctx.exception), "no tables")

    def test_empty_pages_raises(self):
        for empty in ([], None, {"pages": []}):
            with self.subTest(pages=empty):
                with self.assertRaises(ValueError):
                    oe.xlsx_bytes(empty)

    def test_write_xlsx_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.xlsx"
            got = oe.write_xlsx(target, _doc(RAW_REAL), name="写盘")
            self.assertEqual(Path(got), target)
            self.assertEqual(target.read_bytes(),
                             oe.xlsx_bytes(_doc(RAW_REAL), name="写盘"))

    def test_core_name_written(self):
        core = _part(self.data, "docProps/core.xml").decode("utf-8")
        self.assertIn("清单", core)


@unittest.skipUnless(HAVE_DOCX, "缺少 python-docx（用 PYTHONPATH 指向 D:/Project/_work/validators）")
class DocxValidatorTest(unittest.TestCase):
    """真解析库校验：python-docx 打开字节流，断言段落/样式/表格。"""

    @classmethod
    def setUpClass(cls):
        cls.data = oe.docx_bytes(_doc(RAW_REAL), title="校验")

    def test_paragraphs_and_styles(self):
        doc = _python_docx.Document(io.BytesIO(self.data))
        styles = [p.style.name for p in doc.paragraphs if p.text]
        self.assertEqual(styles[:2], ["Heading 1", "Normal"])
        self.assertIn("Heading 2", styles)
        texts = [p.text for p in doc.paragraphs if p.text]
        self.assertIn("第一章 概述", texts)
        self.assertIn("第二页正文。", texts)

    def test_table_shape_and_cells(self):
        doc = _python_docx.Document(io.BytesIO(self.data))
        self.assertEqual(len(doc.tables), 2)
        table = doc.tables[0]
        self.assertEqual(len(table.rows), 4)
        self.assertEqual(len(table.columns), 3)
        self.assertEqual([c.text for c in table.rows[0].cells],
                         ["项目", "值", "备注"])
        # colspan=2：python-docx 按网格展开，所以同一文本占两格
        self.assertEqual([c.text for c in table.rows[1].cells],
                         ["合并两列", "合并两列", "a1"])

    def test_real_document_end_to_end(self):
        """真实 build_document 输出（含图片块/行内标记/两页）→ docx 可被打开。"""
        doc = _python_docx.Document(io.BytesIO(self.data))
        self.assertEqual(len(doc.tables), 2)
        self.assertEqual(doc.tables[1].rows[0].cells[0].text, "t2a")
        self.assertEqual(doc.tables[0].rows[0].cells[0].text, "项目")


@unittest.skipUnless(HAVE_OPENPYXL, "缺少 openpyxl（用 PYTHONPATH 指向 D:/Project/_work/validators）")
class XlsxValidatorTest(unittest.TestCase):
    """真解析库校验：openpyxl 打开字节流，断言 sheet/值/合并/类型。"""

    @classmethod
    def setUpClass(cls):
        cls.data = oe.xlsx_bytes(_doc(RAW_REAL), name="校验")

    def test_sheets_and_values(self):
        wb = _openpyxl.load_workbook(io.BytesIO(self.data))
        self.assertEqual(wb.sheetnames, ["表格1", "表格2"])
        ws = wb["表格1"]
        self.assertEqual(ws["A1"].value, "项目")
        self.assertEqual(ws["C1"].value, "备注")
        self.assertEqual(ws["C3"].value, 2.5)
        self.assertEqual(ws["A2"].value, "合并两列")

    def test_merged_cells(self):
        wb = _openpyxl.load_workbook(io.BytesIO(self.data))
        merged = {str(r) for r in wb["表格1"].merged_cells.ranges}
        self.assertEqual(merged, {"A2:B2", "A3:A4"})
        self.assertEqual(len(wb["表格2"].merged_cells.ranges), 0)

    def test_numeric_cells_are_numbers(self):
        wb = _openpyxl.load_workbook(io.BytesIO(self.data))
        ws = wb["表格1"]
        self.assertIsInstance(ws["B3"].value, (int, float))
        self.assertEqual(ws["B4"].value, 3)
        self.assertEqual(ws["C3"].value, 2.5)

    def test_real_document_end_to_end(self):
        wb = _openpyxl.load_workbook(io.BytesIO(self.data))
        self.assertEqual(len(wb.sheetnames), 2)
        self.assertEqual(wb["表格2"]["A1"].value, "t2a")


if __name__ == "__main__":
    unittest.main()

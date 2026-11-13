# -*- coding: utf-8 -*-
"""tests/test_searchable_pdf.py — E8：可搜索 PDF（原 PDF + 不可见文本层）

契约（task-29）：
  • `build_searchable_pdf(src_pdf, blocks, out_path, *, dpi=200, page_range="")`
    —— 保留原页与原内容，按 E2 blocks 的 bbox（像素×72/dpi）叠**不可见**文本；
    页范围把「相对页序」映射回原 PDF 页；无 bbox / 越界块跳过；原子落盘。
  • `WebBackend.export_searchable_pdf()` —— 只在 merged + 单个 PDF + 有 blocks
    时可用；输出到 output_dir()、同名递增；fitz 缺失 → err.noPymupdf。
  • 识别 payload 的 pdfAvailable / pdfSource 出现条件。
  • CLI `--format pdf`：单个 PDF + blocks 才有输出；非 PDF → 1，缺 blocks /
    缺 pymupdf → 2。

依赖 PyMuPDF：用 `PYTHONPATH=".venv/Lib/site-packages"` 跑本文件（否则跳过
需要 fitz 的用例，其余用例仍然执行）。不联网、不碰项目目录。
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import applog  # noqa: E402
import cli  # noqa: E402
import webview_backend as wb  # noqa: E402

try:                                   # PyMuPDF 新名优先，老名兼容
    import pymupdf as fitz             # type: ignore
    FITZ_OK = True
except ImportError:                    # pragma: no cover
    try:
        import fitz                    # type: ignore
        FITZ_OK = True
    except ImportError:                # pragma: no cover
        fitz = None
        FITZ_OK = False

_NEED_FITZ = unittest.skipUnless(
    FITZ_OK, "需要 PyMuPDF（PYTHONPATH=.venv/Lib/site-packages）")


def _rmtree(p: Path):
    for f in sorted(p.rglob("*"), reverse=True):
        try:
            f.unlink() if f.is_file() else f.rmdir()
        except OSError:
            pass
    try:
        p.rmdir()
    except OSError:
        pass


# ══════════════════════════════════════════════════════════
# 纯函数（不依赖 fitz）
# ══════════════════════════════════════════════════════════
class CleanPdfTextTest(unittest.TestCase):
    def test_markdown_and_html_stripped(self):
        self.assertEqual(wb._clean_pdf_text("# 标题"), "标题")
        self.assertEqual(wb._clean_pdf_text("**粗** 与 *斜*"), "粗 与 斜")
        self.assertEqual(wb._clean_pdf_text("<td>单元格</td>"), "单元格")
        self.assertEqual(wb._clean_pdf_text("[文字](http://x)"), "文字")

    def test_whitespace_collapsed(self):
        self.assertEqual(wb._clean_pdf_text("a\n\n  b\tc "), "a b c")

    def test_empty_and_none(self):
        self.assertEqual(wb._clean_pdf_text(""), "")
        self.assertEqual(wb._clean_pdf_text(None), "")


# ══════════════════════════════════════════════════════════
# 构建器（真 fitz）
# ══════════════════════════════════════════════════════════
@_NEED_FITZ
class BuildSearchablePdfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_spdf_"))
        self.src = self.tmp / "src.pdf"
        doc = fitz.open()
        for i in (1, 2):
            page = doc.new_page(width=595, height=842)
            page.insert_text((72, 100), f"ORIGINAL PAGE {i}", fontsize=14)
        doc.save(str(self.src))
        doc.close()
        self.out = self.tmp / "out.pdf"

    def tearDown(self):
        _rmtree(self.tmp)

    def _texts(self, pdf: Path) -> list[str]:
        doc = fitz.open(str(pdf))
        try:
            return [p.get_text() for p in doc]
        finally:
            doc.close()

    def test_page_count_preserved(self):
        wb.build_searchable_pdf(
            self.src, [{"page": 1, "blocks": [
                {"bbox": [100, 150, 500, 200], "text": "文本"}]}], self.out)
        doc = fitz.open(str(self.out))
        try:
            self.assertEqual(doc.page_count, 2)
        finally:
            doc.close()

    def test_inserted_text_extractable(self):
        wb.build_searchable_pdf(
            self.src, [{"page": 1, "blocks": [
                {"bbox": [100, 150, 500, 200], "text": "中文标题"}]}], self.out)
        self.assertIn("中文标题", self._texts(self.out)[0])

    def test_original_content_kept(self):
        wb.build_searchable_pdf(
            self.src, [{"page": 1, "blocks": [
                {"bbox": [100, 150, 500, 200], "text": "叠加"}]}], self.out)
        texts = self._texts(self.out)
        self.assertIn("ORIGINAL PAGE 1", texts[0])
        self.assertIn("ORIGINAL PAGE 2", texts[1])

    def test_page_range_maps_relative_to_original(self):
        """blocks 的相对页 1 + page_range="2" → 文本应落在原 PDF 第 2 页。"""
        wb.build_searchable_pdf(
            self.src, [{"page": 1, "blocks": [
                {"bbox": [100, 150, 500, 200], "text": "第二页文本"}]}],
            self.out, page_range="2")
        texts = self._texts(self.out)
        self.assertNotIn("第二页文本", texts[0])
        self.assertIn("第二页文本", texts[1])

    def test_block_without_bbox_skipped(self):
        wb.build_searchable_pdf(
            self.src, [{"page": 1, "blocks": [
                {"text": "无 bbox 的块"},
                {"bbox": [100, 150, 500, 200], "text": "有 bbox"}]}], self.out)
        text = self._texts(self.out)[0]
        self.assertNotIn("无 bbox 的块", text)
        self.assertIn("有 bbox", text)

    def test_out_of_range_page_skipped(self):
        wb.build_searchable_pdf(
            self.src, [{"page": 9, "blocks": [
                {"bbox": [100, 150, 500, 200], "text": "越界"}]}], self.out)
        for text in self._texts(self.out):
            self.assertNotIn("越界", text)

    def test_bbox_clamped_to_page(self):
        """bbox 超出页面也要 clamp 后写入，不抛异常。"""
        wb.build_searchable_pdf(
            self.src, [{"page": 1, "blocks": [
                {"bbox": [-500, -500, 5000, 5000], "text": "整页"}]}], self.out)
        self.assertIn("整页", self._texts(self.out)[0])

    def test_empty_blocks_raises(self):
        with self.assertRaises(ValueError):
            wb.build_searchable_pdf(self.src, [], self.out)
        self.assertFalse(self.out.exists(), "失败不应留下输出文件")

    def test_no_writable_block_raises(self):
        with self.assertRaises(ValueError):
            wb.build_searchable_pdf(
                self.src, [{"page": 1, "blocks": [{"text": "只有字没有框"}]}],
                self.out)

    def test_atomic_no_tmp_residue(self):
        wb.build_searchable_pdf(
            self.src, [{"page": 1, "blocks": [
                {"bbox": [100, 150, 500, 200], "text": "原子"}]}], self.out)
        self.assertTrue(self.out.is_file())
        self.assertEqual(list(self.tmp.glob("*.tmp.pdf")), [])

    def test_refuses_to_overwrite_source(self):
        """out == src 时必须拒绝（否则原扫描件被毁）。"""
        before = self.src.read_bytes()
        with self.assertRaises(ValueError):
            wb.build_searchable_pdf(
                self.src, [{"page": 1, "blocks": [
                    {"bbox": [100, 150, 500, 200], "text": "x"}]}], self.src)
        self.assertEqual(self.src.read_bytes(), before)

    def test_missing_fitz_raises_import_error(self):
        with mock.patch.object(wb, "_import_fitz", side_effect=ImportError("x")):
            with self.assertRaises(ImportError):
                wb.build_searchable_pdf(
                    self.src, [{"page": 1, "blocks": [
                        {"bbox": [100, 150, 500, 200], "text": "x"}]}],
                    self.out)


# ══════════════════════════════════════════════════════════
# 识别 payload 的 pdfAvailable / pdfSource（不需要 fitz）
# ══════════════════════════════════════════════════════════
class PdfAvailableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_pdfav_"))
        self.out = self.tmp / "outputs"
        self.out.mkdir()
        self.settings_file = self.tmp / "settings.json"
        self.settings_file.write_text(json.dumps({"lang": "zh"}),
                                      encoding="utf-8")
        self._patches = [
            mock.patch.object(wb, "SETTINGS_FILE", self.settings_file),
            mock.patch.object(wb, "OUTPUT_DIR", self.out),
            mock.patch.object(applog, "_DIR", self.tmp / "logs"),
        ]
        for p in self._patches:
            p.start()
        applog._FH = None
        applog._CURRENT_MONTH = ""
        self.backend = wb.WebBackend()
        self.backend.engine.ready = True
        self.pdf = self.tmp / "doc.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake")
        self.png = self.tmp / "pic.png"
        self.png.write_bytes(b"PNG")

    def tearDown(self):
        applog._FH = None
        for p in self._patches:
            p.stop()
        _rmtree(self.tmp)

    def _recognize(self, path: Path, *, mode="merged", blocks=None,
                   name="doc"):
        payload = {"text": "t", "pages": ["t"], "elapsed": 0.0,
                   "truncated": False, "pagesMismatch": False}
        if blocks is not None:
            payload["blocks"] = blocks
        with mock.patch.object(self.backend.engine, "recognize",
                               side_effect=lambda *a, **k: dict(payload)):
            return self.backend.recognize({"paths": [str(path)],
                                           "name": name, "mode": mode})

    def test_merged_single_pdf_with_blocks(self):
        blocks = [{"page": 1, "blocks": [
            {"bbox": [1, 2, 3, 4], "text": "x"}]}]
        res = self._recognize(self.pdf, blocks=blocks)
        self.assertIs(res.get("pdfAvailable"), True)
        self.assertEqual(res.get("pdfSource"), "doc.pdf")
        self.assertIsNotNone(self.backend._last_pdf)
        self.assertEqual(self.backend._last_pdf["dpi"], 200)
        self.assertEqual(self.backend._last_pdf["path"], str(self.pdf))

    def test_png_has_no_pdf_available(self):
        blocks = [{"page": 1, "blocks": [{"bbox": [1, 2, 3, 4], "text": "x"}]}]
        res = self._recognize(self.png, blocks=blocks)
        self.assertNotIn("pdfAvailable", res)
        self.assertIsNone(self.backend._last_pdf)

    def test_pdf_without_blocks_has_no_pdf_available(self):
        res = self._recognize(self.pdf, blocks=None)
        self.assertNotIn("pdfAvailable", res)
        self.assertIsNone(self.backend._last_pdf)

    def test_batch_mode_clears_cache(self):
        blocks = [{"page": 1, "blocks": [{"bbox": [1, 2, 3, 4], "text": "x"}]}]
        self._recognize(self.pdf, blocks=blocks)          # 先建立缓存
        self.assertIsNotNone(self.backend._last_pdf)
        res = self._recognize(self.pdf, mode="batch", blocks=blocks)
        self.assertNotIn("pdfAvailable", res)
        self.assertIsNone(self.backend._last_pdf)

    def test_export_without_cache(self):
        out = self.backend.export_searchable_pdf()
        self.assertIs(out["ok"], False)
        self.assertEqual(out["error"], wb.tr("err.pdfUnavailable"))

    def test_export_without_fitz(self):
        blocks = [{"page": 1, "blocks": [{"bbox": [1, 2, 3, 4], "text": "x"}]}]
        self._recognize(self.pdf, blocks=blocks)
        with mock.patch.object(wb, "_import_fitz",
                               side_effect=ImportError("no fitz")):
            out = self.backend.export_searchable_pdf()
        self.assertIs(out["ok"], False)
        self.assertEqual(out["error"], wb.tr("err.noPymupdf"))

    def test_export_build_failure_reports_error(self):
        self.backend._last_pdf = {"path": str(self.tmp / "gone.pdf"),
                                  "dpi": 200,
                                  "blocks": [{"page": 1, "blocks": [
                                      {"bbox": [1, 2, 3, 4], "text": "x"}]}],
                                  "page_range": "", "name": "gone"}
        out = self.backend.export_searchable_pdf()
        self.assertIs(out["ok"], False)
        self.assertTrue(out["error"])


@_NEED_FITZ
class ExportSearchablePdfTest(PdfAvailableTest):
    """真 fitz：缓存 → 导出 → 同名递增（复用上面的 backend 夹具）。"""

    def setUp(self):
        super().setUp()
        # 父类只放了一个假 PDF 字节；这里换成真 PDF（导出要 fitz 能打开）
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "ORIGINAL", fontsize=14)
        doc.save(str(self.pdf))
        doc.close()

    def _cache_pdf(self):
        blocks = [{"page": 1, "blocks": [
            {"bbox": [100, 150, 500, 200], "text": "可搜索中文"}]}]
        res = self._recognize(self.pdf, blocks=blocks)
        self.assertIs(res.get("pdfAvailable"), True)

    def test_export_success_and_text_layer(self):
        self._cache_pdf()
        out = self.backend.export_searchable_pdf()
        self.assertIs(out["ok"], True, out)
        target = Path(out["path"])
        self.assertEqual(target.parent, self.out)
        self.assertEqual(target.suffix, ".pdf")
        doc = fitz.open(str(target))
        try:
            self.assertEqual(doc.page_count, 1)
            self.assertIn("可搜索中文", doc[0].get_text())
        finally:
            doc.close()

    def test_export_same_name_increments(self):
        self._cache_pdf()
        first = self.backend.export_searchable_pdf()
        second = self.backend.export_searchable_pdf()
        self.assertIs(first["ok"], True)
        self.assertIs(second["ok"], True)
        self.assertNotEqual(first["path"], second["path"])
        self.assertTrue(Path(second["path"]).name.startswith("doc (1)"),
                        second["path"])

    def test_export_through_server_route(self):
        import http.client
        import webview_server as ws
        self._cache_pdf()
        srv = ws.WebViewServer(port=0)
        # 路由走的是 server 自己的 backend 实例；把刚建立的缓存挪过去
        srv.backend._last_pdf = self.backend._last_pdf
        srv.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=20)
            try:
                conn.request("POST", "/api/export-pdf", body=b"{}",
                             headers={"Host": f"127.0.0.1:{srv.port}",
                                      "Content-Type": "application/json"})
                resp = conn.getresponse()
                data = json.loads(resp.read())
            finally:
                conn.close()
        finally:
            srv.stop()
        self.assertEqual(resp.status, 200)
        self.assertIs(data["ok"], True, data)
        self.assertTrue(Path(data["path"]).is_file())


# ══════════════════════════════════════════════════════════
# CLI --format pdf
# ══════════════════════════════════════════════════════════
class _FakeCliEngine:
    """最小假引擎：可选返回 blocks（避免加载模型）。"""

    def __init__(self, blocks=None, pages=("正文",)):
        self.ready = True
        self.cancel_event = None
        self._blocks = blocks
        self._pages = list(pages)

    def load(self, device_pref="auto", cb=None):
        return None

    def recognize(self, image_paths, prompt="", **kwargs):
        res = {"text": "\n\n".join(self._pages), "pages": list(self._pages),
               "elapsed": 0.1, "truncated": False, "pagesMismatch": False,
               "groups": 1}
        if self._blocks is not None:
            res["blocks"] = self._blocks
        return res


class CliPdfFormatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_clipdf_"))
        self.png = self.tmp / "a.png"
        self.png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        self.pdf = self.tmp / "doc.pdf"
        if FITZ_OK:
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            page.insert_text((72, 100), "ORIGINAL", fontsize=14)
            doc.save(str(self.pdf))
            doc.close()
        else:
            # 无 fitz 时也放一个「扩展名是 .pdf 的文件」，好让参数/环境校验
            # 走到「缺 pymupdf」分支（本类不涉真实解析）
            self.pdf.write_bytes(b"%PDF-1.4 dummy")
        self.out_dir = self.tmp / "out"
        self._patch = mock.patch.object(applog, "_DIR", self.tmp / "logs")
        self._patch.start()
        applog._FH = None
        applog._CURRENT_MONTH = ""

    def tearDown(self):
        applog._FH = None
        self._patch.stop()
        _rmtree(self.tmp)

    def _run(self, argv, engine):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv, engine_factory=lambda: engine)
        return rc, out.getvalue(), err.getvalue()

    def test_non_pdf_input_exits_1(self):
        rc, _out, err = self._run(
            ["--input", str(self.png), "--format", "pdf",
             "--out", str(self.out_dir)], _FakeCliEngine())
        self.assertEqual(rc, cli.EXIT_PARAM)
        self.assertIn("PDF", err)

    def test_missing_pymupdf_exits_2(self):
        with mock.patch.object(cli, "_find_spec", return_value=None), \
                mock.patch.object(cli, "_pkg_version", return_value=None):
            rc, _out, err = self._run(
                ["--input", str(self.pdf), "--format", "pdf",
                 "--out", str(self.out_dir)], _FakeCliEngine())
        self.assertEqual(rc, cli.EXIT_ENV)
        self.assertIn("pymupdf", err)

    def test_engine_without_blocks_exits_2(self):
        # 装作本机有 pymupdf（只验证「引擎没给 blocks」→ 2 这条分支）
        with mock.patch.object(cli, "_find_spec", return_value=object()):
            rc, _out, err = self._run(
                ["--input", str(self.pdf), "--format", "pdf",
                 "--out", str(self.out_dir)], _FakeCliEngine(blocks=None))
        self.assertEqual(rc, cli.EXIT_ENV)
        self.assertIn("blocks", err)

    @_NEED_FITZ
    def test_end_to_end_writes_searchable_pdf(self):
        blocks = [{"page": 1, "blocks": [
            {"bbox": [100, 150, 500, 200], "text": "命令行中文"}]}]
        rc, _out, _err = self._run(
            ["--input", str(self.pdf), "--format", "pdf",
             "--out", str(self.out_dir)], _FakeCliEngine(blocks=blocks))
        self.assertEqual(rc, cli.EXIT_OK, _err)
        target = self.out_dir / "doc.pdf"
        self.assertTrue(target.is_file(), list(self.out_dir.iterdir()))
        doc = fitz.open(str(target))
        try:
            self.assertEqual(doc.page_count, 1)
            self.assertIn("命令行中文", doc[0].get_text())
            self.assertIn("ORIGINAL", doc[0].get_text())
        finally:
            doc.close()
        self.assertEqual(list(self.out_dir.glob("*.tmp.pdf")), [])


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""tests/test_engine_batch2.py — E7 页范围 + E12/E14 后处理接线的引擎侧回归

覆盖：
  • page_range.parse_pages / is_valid_spec（语法、夹取、开区间、去重、全角逗号）
    与 cli.parse_pages 薄包装的一致性与错误类型（ValueError → CliError）；
  • pdf_to_images(page_range=...) 只导出选中页、文件名保留原页号、非法规格 →
    EngineError(err.badPageRange)（用假 fitz，不依赖 pymupdf）；
  • recognize 把 page_range 透传给 pdf_to_images（非 PDF 忽略）；
  • recognize 把 join_lines / cjk_spacing / drop_headers 透传给后处理，并把
    stats["tableWarnings"] 汇总进结果（后处理尚未支持时静默降级为 0）。

不加载 torch/transformers，不写项目目录（applog 重定向到临时目录）。
"""
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import applog  # noqa: E402
import cli  # noqa: E402
import ocr_engine  # noqa: E402
import page_range  # noqa: E402
from ocr_engine import EngineError, OcrEngine  # noqa: E402


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
# A) page_range 纯函数
# ══════════════════════════════════════════════════════════
class ParsePagesUnitTest(unittest.TestCase):
    def test_basic_forms(self):
        self.assertEqual(page_range.parse_pages("5"), [5])
        self.assertEqual(page_range.parse_pages("1-3"), [1, 2, 3])
        self.assertEqual(page_range.parse_pages("1-3,5,10-", 12),
                         [1, 2, 3, 5, 10, 11, 12])
        self.assertEqual(page_range.parse_pages("2,2,1-2"), [1, 2])
        self.assertEqual(page_range.parse_pages("1，2"), [1, 2])

    def test_empty_means_no_filter(self):
        self.assertEqual(page_range.parse_pages(""), [])
        self.assertEqual(page_range.parse_pages(None), [])
        self.assertEqual(page_range.parse_pages("   "), [])

    def test_open_range_requires_total(self):
        with self.assertRaises(ValueError):
            page_range.parse_pages("10-")

    def test_clamping(self):
        self.assertEqual(page_range.parse_pages("1-3,5", 3), [1, 2, 3])
        self.assertEqual(page_range.parse_pages("10-", 5), [5])
        self.assertEqual(page_range.parse_pages("1-3", 0), [])

    def test_invalid_forms(self):
        for bad in ("abc", "3-1", "0", "-3", "1--2", "1-abc"):
            with self.subTest(spec=bad):
                with self.assertRaises(ValueError):
                    page_range.parse_pages(bad)

    def test_error_messages_kept_for_cli(self):
        """clii 的历史文案不能变（tests/test_cli.py 与用户脚本都依赖）。"""
        cases = {
            "abc": "not an integer: 'abc'",
            "-3": "missing start in '-3'",
            "3-1": "bad range '3-1'",
            "10-": "open range '10-' needs total page count",
            "0": "page must be >= 1: '0'",
        }
        for spec, msg in cases.items():
            with self.assertRaises(ValueError) as cm:
                page_range.parse_pages(spec)
            self.assertEqual(str(cm.exception), msg)

    def test_is_valid_spec(self):
        for good in ("", None, "5", "1-3", "10-", "1-3,5,10-", "1，2"):
            self.assertTrue(page_range.is_valid_spec(good), repr(good))
        for bad in ("abc", "3-1", "0", "-3", "1--2", "1-abc"):
            self.assertFalse(page_range.is_valid_spec(bad), repr(bad))

    def test_cli_wrapper_matches(self):
        for spec, total in (("5", None), ("1-3", None), ("1-3,5,10-", 12),
                            ("10-", 5), ("1-3,5", 3), ("", None)):
            self.assertEqual(cli.parse_pages(spec, total),
                             page_range.parse_pages(spec, total), spec)

    def test_cli_wrapper_raises_cli_error_not_valueerror(self):
        with self.assertRaises(cli.CliError):
            cli.parse_pages("abc")
        self.assertFalse(issubclass(cli.CliError, ValueError),
                         "CliError 不是 ValueError（wrapper 必须转换）")


# ══════════════════════════════════════════════════════════
# B) pdf_to_images 页范围（假 fitz）
# ══════════════════════════════════════════════════════════
class _FakePixmap:
    def __init__(self, page_no: int):
        self.page_no = page_no

    def save(self, path: str):
        Path(path).write_bytes(f"page{self.page_no}".encode())


class _FakePage:
    def __init__(self, page_no: int):
        self.page_no = page_no

    def get_pixmap(self, matrix=None):
        return _FakePixmap(self.page_no)


class _FakeDoc:
    def __init__(self, path: str, pages: int):
        self.path = path
        self.page_count = pages

    def __getitem__(self, idx: int):
        return _FakePage(idx + 1)

    def close(self):
        pass


def _fake_fitz(pages: int):
    mod = types.SimpleNamespace()
    mod.Matrix = lambda a, b: (a, b)
    mod.opened = []

    def _open(path):
        mod.opened.append(str(path))
        return _FakeDoc(str(path), pages)

    mod.open = _open
    return mod


class PdfToImagesRangeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_pr_"))
        self.pdf = self.tmp / "doc.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake")
        self.out = self.tmp / "out"
        self.out.mkdir()
        self._patches = [mock.patch.object(applog, "_DIR", self.tmp / "logs")]
        for p in self._patches:
            p.start()
        applog._FH = None
        applog._CURRENT_MONTH = ""

    def tearDown(self):
        applog._FH = None
        for p in self._patches:
            p.stop()
        _rmtree(self.tmp)

    def _render(self, page_range: str, pages: int = 10) -> list[Path]:
        fitz = _fake_fitz(pages)
        with mock.patch.dict(sys.modules, {"fitz": fitz}):
            return OcrEngine.pdf_to_images(self.pdf, dpi=100,
                                           out_dir=self.out,
                                           page_range=page_range)

    def test_selected_pages_only_and_original_numbers(self):
        got = self._render("2-3")
        self.assertEqual([p.name for p in got],
                         ["_ocr_page_doc_0002.png", "_ocr_page_doc_0003.png"],
                         "文件名必须保留原页号")
        self.assertTrue(all(p.is_file() for p in got))
        self.assertEqual((self.out / "_ocr_page_doc_0002.png").read_bytes(),
                         b"page2")
        self.assertFalse((self.out / "_ocr_page_doc_0001.png").exists())

    def test_empty_range_exports_all_pages(self):
        got = self._render("", pages=4)
        self.assertEqual([p.name for p in got],
                         [f"_ocr_page_doc_{n:04d}.png" for n in (1, 2, 3, 4)])

    def test_out_of_range_clamped(self):
        got = self._render("9-", pages=3)
        self.assertEqual([p.name for p in got], ["_ocr_page_doc_0003.png"])

    def test_invalid_range_raises_engine_error(self):
        with self.assertRaises(EngineError) as cm:
            self._render("abc")
        self.assertIn("abc", str(cm.exception))
        self.assertFalse(list(self.out.glob("*.png")), "非法规格不应导出任何页")

    def test_no_pymupdf_raises_engine_error(self):
        with mock.patch.dict(sys.modules, {"fitz": None}):
            with self.assertRaises(EngineError):
                OcrEngine.pdf_to_images(self.pdf, out_dir=self.out)


# ══════════════════════════════════════════════════════════
# C) recognize 接线（页范围 + 后处理开关 + tableWarnings）
# ══════════════════════════════════════════════════════════
def _bare_engine(pdf_to_images=None):
    eng = object.__new__(OcrEngine)
    eng.ready = True
    eng._lock = threading.Lock()
    eng.cancel_event = threading.Event()
    eng.model = None
    eng.tokenizer = None
    eng.stream_state = {"chars": 0}
    eng._error = None
    eng._last_infer = None
    eng.generated_chars = lambda: 0
    if pdf_to_images is not None:
        eng.pdf_to_images = pdf_to_images
    return eng


class RecognizeWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_b2e_"))
        self._patches = [mock.patch.object(applog, "_DIR", self.tmp / "logs")]
        for p in self._patches:
            p.start()
        applog._FH = None
        applog._CURRENT_MONTH = ""
        ocr_engine._STREAM_HOOK["cancel_event"] = None
        ocr_engine._STREAM_HOOK["state"] = None

    def tearDown(self):
        applog._FH = None
        for p in self._patches:
            p.stop()
        _rmtree(self.tmp)

    def _infer_patch(self, text="正文", stats=None):
        return mock.patch.object(
            OcrEngine, "_infer",
            new=lambda self_eng, images, prompt, crop_mode=None,
            max_new_tokens=None: (text, dict(stats or {})))

    def test_page_range_passed_to_pdf_to_images(self):
        seen = {}

        def fake_pdf(self_eng, pdf_path, dpi=200, out_dir=None,
                     page_range=""):
            seen.update(dpi=dpi, page_range=page_range)
            return [Path(out_dir) / "p1.png"]

        eng = _bare_engine()
        with mock.patch.object(OcrEngine, "pdf_to_images", new=fake_pdf), \
                self._infer_patch():
            eng.recognize([Path("doc.pdf")], page_range="2-4")
        self.assertEqual(seen["page_range"], "2-4")
        self.assertEqual(seen["dpi"], 200)

    def test_page_range_ignored_for_non_pdf(self):
        called = []

        def fake_pdf(*a, **k):
            called.append(1)
            return []

        eng = _bare_engine()
        with mock.patch.object(OcrEngine, "pdf_to_images", new=fake_pdf), \
                self._infer_patch():
            res = eng.recognize([Path("p1.png")], page_range="2-4")
        self.assertEqual(called, [], "非 PDF 不应调用 pdf_to_images")
        self.assertEqual(res["text"], "正文")

    def test_join_cjk_kwargs_reach_postprocess_and_warnings_summed(self):
        seen = {}

        def fake_post(raw, drop_headers=False, join_lines=False,
                      cjk_spacing=False):
            seen.update(drop_headers=drop_headers, join_lines=join_lines,
                        cjk_spacing=cjk_spacing)
            return "MD", {"droppedBlocks": 2, "tableWarnings": 3}

        eng = _bare_engine()
        with mock.patch.object(ocr_engine, "to_gui_markdown_with_stats",
                               fake_post), self._infer_patch():
            res = eng.recognize([Path("p1.png")], drop_headers=True,
                                join_lines=True, cjk_spacing=True)
        self.assertEqual(seen, {"drop_headers": True, "join_lines": True,
                                "cjk_spacing": True})
        self.assertEqual(res["tableWarnings"], 3)
        self.assertEqual(res["droppedBlocks"], 2)
        self.assertEqual(res["pages"], ["MD"])

    def test_old_postprocess_without_new_kwargs_is_tolerated(self):
        """后处理还没加 join_lines/cjk_spacing 时：不炸、tableWarnings=0。"""
        def old_post(raw, drop_headers=False):
            return "MD", {"droppedBlocks": 0}

        eng = _bare_engine()
        with mock.patch.object(ocr_engine, "to_gui_markdown_with_stats",
                               old_post), self._infer_patch():
            res = eng.recognize([Path("p1.png")], join_lines=True,
                                cjk_spacing=True)
        self.assertEqual(res["tableWarnings"], 0, "缺字段按 0 返回")
        self.assertEqual(res["pages"], ["MD"])

    def test_table_warnings_default_zero_without_field(self):
        def post(raw, drop_headers=False):
            return "MD", {"droppedBlocks": 0}

        eng = _bare_engine()
        with mock.patch.object(ocr_engine, "to_gui_markdown_with_stats",
                               post), self._infer_patch():
            res = eng.recognize([Path("p1.png")])
        self.assertEqual(res["tableWarnings"], 0)

    def test_multi_page_warnings_are_summed(self):
        def post(raw, drop_headers=False, join_lines=False, cjk_spacing=False):
            return f"MD:{raw}", {"droppedBlocks": 1, "tableWarnings": 2}

        eng = _bare_engine()
        with mock.patch.object(ocr_engine, "to_gui_markdown_with_stats",
                               post), \
                self._infer_patch(text="第一页<PAGE>第二页"):
            res = eng.recognize([Path("a.png"), Path("b.png")])
        self.assertEqual(res["tableWarnings"], 4, "两页各 2 处应求和")
        self.assertEqual(len(res["pages"]), 2)

    def test_real_postprocess_table_warnings_flow(self):
        """真后处理（ocr_postprocess）验证：列数不符的表格 → tableWarnings ≥ 1。

        期望值不写死：先用真后处理算一遍，再断言 recognize 结果与之一致——
        这样后处理侧调整计数口径时本用例不会误报。
        """
        raw = "| a | b | c |\n|---|---|---|\n| 1 | 2 |\n"
        _, _, expected = ocr_engine._render_with_stats(
            raw, drop_headers=False, join_lines=True, cjk_spacing=True)
        eng = _bare_engine()
        with self._infer_patch(text=raw):
            res = eng.recognize([Path("p1.png")], join_lines=True,
                                cjk_spacing=True)
        self.assertEqual(res["tableWarnings"], expected)
        self.assertGreaterEqual(res["tableWarnings"], 1,
                                "列数不符的表格应至少计一次告警")


if __name__ == "__main__":
    unittest.main()

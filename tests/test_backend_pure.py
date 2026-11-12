# -*- coding: utf-8 -*-
"""tests/test_backend_pure.py — 后端纯逻辑（不启动服务、不碰真实用户数据）

这些函数是"把手滑变成数据事故"的地方：文件名净化、输出目录穿越防护、
解释器路径归一、Markdown 转纯文本。全部用临时目录，零副作用。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webview_backend as wb  # noqa: E402
from ocr_engine import EngineError  # noqa: E402


class SanitizeNameTest(unittest.TestCase):
    def test_banned_chars_removed(self):
        self.assertEqual(wb.WebBackend._sanitize_name('a<b>c:d"e/f\\g|h?i*j'),
                         "abcdefghij")

    def test_trailing_dots_and_spaces(self):
        self.assertEqual(wb.WebBackend._sanitize_name("  name..  "), "name")

    def test_empty_falls_back(self):
        self.assertEqual(wb.WebBackend._sanitize_name(""), "result")
        self.assertEqual(wb.WebBackend._sanitize_name("..."), "result")

    def test_length_capped(self):
        self.assertEqual(len(wb.WebBackend._sanitize_name("x" * 500)), 80)

    def test_unicode_kept(self):
        self.assertEqual(wb.WebBackend._sanitize_name("扫描件 2024"), "扫描件 2024")


class MdToPlainTest(unittest.TestCase):
    def test_heading_stripped(self):
        self.assertEqual(wb.WebBackend._md_to_plain("## 标题"), "标题")

    def test_bold_italic_stripped(self):
        self.assertEqual(wb.WebBackend._md_to_plain("**粗** 与 *斜*"), "粗 与 斜")

    def test_link_kept_as_text(self):
        self.assertEqual(wb.WebBackend._md_to_plain("[文字](http://x)"), "文字")

    def test_code_fence_removed(self):
        self.assertEqual(wb.WebBackend._md_to_plain("```py\nx=1\n```"), "x=1")

    def test_table_cells_become_tabs(self):
        out = wb.WebBackend._md_to_plain(
            "<table><tr><td>a</td><td>b</td></tr></table>")
        self.assertIn("a\tb", out)
        self.assertNotIn("<td>", out)

    def test_empty_input(self):
        self.assertEqual(wb.WebBackend._md_to_plain(""), "")


class HistoryPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_out_"))
        self._patch = mock.patch.object(wb, "OUTPUT_DIR", self.tmp)
        self._patch.start()
        self._settings = mock.patch.object(
            wb, "SETTINGS_FILE", self.tmp / "settings.json")
        self._settings.start()
        self.backend = wb.WebBackend()

    def tearDown(self):
        self._settings.stop()
        self._patch.stop()
        for p in sorted(self.tmp.rglob("*"), reverse=True):
            try:
                p.unlink() if p.is_file() else p.rmdir()
            except OSError:
                pass
        try:
            self.tmp.rmdir()
        except OSError:
            pass

    def test_normal_name(self):
        self.assertEqual(self.backend._history_path("结果.md"),
                         self.tmp / "结果.md")

    def test_missing_suffix_appended(self):
        self.assertEqual(self.backend._history_path("结果").name, "结果.md")

    def test_path_traversal_neutralized(self):
        for evil in ("../../../etc/passwd", "..\\..\\windows\\win.ini",
                     "/absolute/path.md", "a/../../b.md"):
            p = self.backend._history_path(evil)
            self.assertEqual(p.parent, self.tmp,
                             f"{evil!r} 逃出了 outputs/：{p}")
            self.assertNotIn("..", p.name)

    def test_history_read_rejects_traversal(self):
        out = self.backend.history_read("../../settings.json")
        self.assertFalse(out["ok"])

    def test_history_read_ok(self):
        (self.tmp / "a.md").write_text("正文", encoding="utf-8")
        out = self.backend.history_read("a.md")
        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "正文")

    def test_list_history_only_md_txt(self):
        (self.tmp / "a.md").write_text("x", encoding="utf-8")
        (self.tmp / "b.bin").write_bytes(b"x")
        names = [i["name"] for i in self.backend.list_history()["items"]]
        self.assertIn("a.md", names)
        self.assertNotIn("b.bin", names)


class NormalizePythonPathTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(wb.WebBackend.normalize_python_path(""), "")
        self.assertEqual(wb.WebBackend.normalize_python_path(None), "")

    def test_quotes_stripped(self):
        self.assertEqual(
            wb.WebBackend.normalize_python_path(f'"{sys.executable}"'),
            str(Path(sys.executable).resolve()))

    def test_missing_path_raises(self):
        with self.assertRaises(EngineError):
            wb.WebBackend.normalize_python_path(
                "C:/definitely/not/here/python.exe")

    def test_dir_without_interpreter_raises(self):
        tmp = Path(tempfile.mkdtemp(prefix="ocr_py_"))
        try:
            with self.assertRaises(EngineError):
                wb.WebBackend.normalize_python_path(str(tmp))
        finally:
            tmp.rmdir()


if __name__ == "__main__":
    unittest.main()

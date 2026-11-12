# -*- coding: utf-8 -*-
r"""tests/test_postprocess_batch2.py — 后处理第二批（E12/E13/E14）

覆盖：
  E13 normalize_latex：\(..\)→$..$、\[..\]→$$..$$（含跨行）、代码区跳过、幂等
  E12 join_wrapped_lines：中/英合并、句末标点/空行/标题/列表/表格/引用/HTML/
      代码围栏边界、幂等
  E12 space_cjk_latin：汉字↔拉丁/数字边界插空格、URL/code/表格/HTML/链接语法
      跳过、已有空格不动、幂等
  E14 check_tables：列数不一致的表格块计数（忽略分隔行、只计数）
  公共入口：convert/convert_with_stats/to_gui_markdown[_with_stats] 新 kwargs
      向后兼容 + stats 含 droppedBlocks/tableWarnings + 管线顺序
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_postprocess import (  # noqa: E402
    check_tables, convert, convert_with_stats, join_wrapped_lines,
    normalize_latex, space_cjk_latin, to_gui_markdown,
    to_gui_markdown_with_stats,
)


def blk(label: str, content: str, level: str = "",
        bbox: str = "[0,0,100,100]") -> str:
    return f"<BLOCK>{label}{level}{bbox}<FORK>{content}"


class NormalizeLatexTest(unittest.TestCase):
    def test_inline_delimiters(self):
        self.assertEqual(normalize_latex(r"公式 \(x^2+1\) 结束"), "公式 $x^2+1$ 结束")

    def test_display_delimiters(self):
        self.assertEqual(normalize_latex(r"\[E=mc^2\]"), "$$E=mc^2$$")

    def test_multiline_display(self):
        src = "\\[\na = b + c\n\\]"
        self.assertEqual(normalize_latex(src), "$$a = b + c$$")

    def test_skip_fenced_code(self):
        src = "```\n\\(x\\) and \\[y\\]\n```"
        self.assertEqual(normalize_latex(src), src)

    def test_skip_inline_code(self):
        self.assertEqual(normalize_latex(r"看 `\(x\)` 但 \(y\)"),
                         "看 `\\(x\\)` 但 $y$")

    def test_idempotent(self):
        once = normalize_latex(r"\(a\) 与 \[b\]")
        self.assertEqual(normalize_latex(once), once)
        self.assertNotIn(r"\(", once)
        self.assertNotIn(r"\[", once)

    def test_mixed_inline_and_display(self):
        self.assertEqual(normalize_latex(r"\(a\) \[b\] \(c\)"), "$a$ $$b$$ $c$")

    def test_distant_or_cross_line_delimiters_untouched(self):
        # 跨空行的 \[ ... \] 不当作块公式；跨行的 \( ... \) 不当作行内公式
        block = "\\[\n公式一\n\n正文二\n\\]"
        self.assertEqual(normalize_latex(block), block)
        inline = "\\(开头\n结尾\\)"
        self.assertEqual(normalize_latex(inline), inline)


class JoinWrappedLinesTest(unittest.TestCase):
    def test_chinese_lines_join_directly(self):
        self.assertEqual(join_wrapped_lines("中文第一行\n第二行继续"),
                         "中文第一行第二行继续")

    def test_english_lines_join_with_space(self):
        self.assertEqual(join_wrapped_lines("hello world\nthis is a test"),
                         "hello world this is a test")

    def test_cjk_latin_join_has_no_forced_space(self):
        # 中英之间的空格留给 space_cjk_latin（管线里它是后一步）
        self.assertEqual(join_wrapped_lines("中文abc\n继续"), "中文abc继续")

    def test_sentence_end_blocks_join(self):
        for end in ("。", "！", "？", "；", "：", ".", "!", "?", ";", ":", "、",
                    "」", "』", "）", "】"):
            src = f"第一句{end}\n第二句"
            self.assertEqual(join_wrapped_lines(src), src, f"end={end!r}")

    def test_blank_line_separates_paragraphs(self):
        self.assertEqual(join_wrapped_lines("段落一\n段落二\n\n段落三\n段落四"),
                         "段落一段落二\n\n段落三段落四")

    def test_structural_lines_not_merged(self):
        src = ("# 标题\n正文\n- 列表项\n继续\n> 引用\n继续\n"
               "1. 编号\n继续\n| a | b |\n继续")
        out = join_wrapped_lines(src)
        for keep in ("# 标题", "- 列表项", "> 引用", "1. 编号", "| a | b |"):
            self.assertIn(keep + "\n", out)

    def test_code_fence_not_merged(self):
        src = "```\nline1\nline2\n```"
        self.assertEqual(join_wrapped_lines(src), src)

    def test_html_block_not_merged(self):
        src = "<div>\n中文\n</div>\n正文\n下一行"
        self.assertEqual(join_wrapped_lines(src),
                         "<div>\n中文\n</div>\n正文下一行")

    def test_html_comment_boundary(self):
        src = "<!-- 注释\n继续注释\n-->\n正文\n下一行"
        self.assertEqual(join_wrapped_lines(src),
                         "<!-- 注释\n继续注释\n-->\n正文下一行")

    def test_idempotent(self):
        src = "中文第一行\n第二行\n\nEnglish line one\nline two"
        once = join_wrapped_lines(src)
        self.assertEqual(join_wrapped_lines(once), once)

    def test_switch_on_public_entry(self):
        raw = blk("text", "第一行\n第二行")
        self.assertIn("第一行\n第二行", convert(raw))
        self.assertEqual(convert(raw, join_lines=True), "第一行第二行")


class SpaceCjkLatinTest(unittest.TestCase):
    def test_cjk_latin_boundaries(self):
        self.assertEqual(space_cjk_latin("中文English混排"), "中文 English 混排")

    def test_cjk_digit_boundaries(self):
        self.assertEqual(space_cjk_latin("共123页OCR2"), "共 123 页 OCR2")

    def test_existing_spaces_untouched(self):
        src = "中文 English 混排 123 abc"
        self.assertEqual(space_cjk_latin(src), src)

    def test_url_untouched(self):
        self.assertEqual(space_cjk_latin("见https://a.com/中文x"),
                         "见https://a.com/中文x")
        self.assertEqual(space_cjk_latin("www.例子.com中文"), "www.例子.com中文")

    def test_inline_code_untouched(self):
        self.assertEqual(space_cjk_latin("用 `中文abc` 示例"),
                         "用 `中文abc` 示例")

    def test_table_row_skipped(self):
        src = "| 中文abc | x |"
        self.assertEqual(space_cjk_latin(src), src)

    def test_html_untouched(self):
        for src in ("<td>中文abc</td>", "<div class=\"中文abc\">",
                    "</div>中文abc"):
            self.assertEqual(space_cjk_latin(src), src)

    def test_link_and_image_syntax_untouched(self):
        src = "图![](page1_1_2_3.jpg)说明abc"
        out = space_cjk_latin(src)
        self.assertIn("![](page1_1_2_3.jpg)", out)
        self.assertTrue(out.endswith("说明 abc"))

    def test_idempotent(self):
        once = space_cjk_latin("中文English混排123abc")
        self.assertEqual(space_cjk_latin(once), once)

    def test_switch_on_public_entry(self):
        raw = blk("text", "中文English混排")
        self.assertEqual(convert(raw), "中文English混排")
        self.assertEqual(convert(raw, cjk_spacing=True), "中文 English 混排")


class CheckTablesTest(unittest.TestCase):
    def test_no_table(self):
        self.assertEqual(check_tables("纯文本\n没有表格"), 0)

    def test_consistent_table(self):
        self.assertEqual(check_tables(
            "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"), 0)

    def test_bad_data_row(self):
        self.assertEqual(check_tables(
            "| a | b |\n|---|---|\n| 1 | 2 | 3 |"), 1)

    def test_separator_row_ignored(self):
        # 分隔行 `|:--|--:|` 只有两格且不算数据行
        self.assertEqual(check_tables("| a | b |\n|:--|--:|\n| 1 | 2 |"), 0)

    def test_multiple_tables_count_each_bad_block_once(self):
        text = ("| a |\n|---|\n|1|\n\n"
                "| x | y |\n|---|---|\n| 1 | 2 | 3 |\n| 4 | 5 |\n\n"
                "| p | q |\n|---|---|\n| 1 |\n| 2 |")
        self.assertEqual(check_tables(text), 2)

    def test_table_without_separator(self):
        self.assertEqual(check_tables("| a | b |\n| 1 | 2 |\n| 3 |"), 1)

    def test_stats_wiring(self):
        text = blk("text", "正文") + blk(
            "table", "| a | b |\n|---|---|\n| 1 | 2 | 3 |")
        md, stats = convert_with_stats(text)
        self.assertEqual(stats["tableWarnings"], 1)
        self.assertIn("droppedBlocks", stats)
        self.assertEqual(convert_with_stats("无表格")[1]["tableWarnings"], 0)


class PublicApiTest(unittest.TestCase):
    def test_backward_compatible_positional(self):
        self.assertEqual(convert("正文", True), "正文")
        self.assertEqual(convert_with_stats("正文", True)[1]["droppedBlocks"], 0)

    def test_stats_keys(self):
        md, stats = convert_with_stats("正文")
        self.assertEqual(md, "正文")
        self.assertEqual(set(stats), {"droppedBlocks", "tableWarnings"})
        md2, stats2 = to_gui_markdown_with_stats("正文")
        self.assertEqual(md2, "正文")
        self.assertEqual(set(stats2), {"droppedBlocks", "tableWarnings"})

    def test_pipeline_order_and_equivalence(self):
        raw = (blk("text", "公式 \\(x\\) 与断行\n下一行")
               + blk("header", "页眉")
               + blk("table", "| a | b |\n|---|---|\n| 1 | 2 | 3 |"))
        md, stats = convert_with_stats(raw, drop_headers=True,
                                       join_lines=True, cjk_spacing=True)
        # drop_headers 先于文本规整：页眉消失
        self.assertNotIn("页眉", md)
        # normalize_latex 始终；join 合并块内硬换行；cjk 补空格
        self.assertIn("$x$", md)
        self.assertIn("公式 $x$ 与断行下一行", md)
        self.assertEqual(stats["droppedBlocks"], 1)
        self.assertEqual(stats["tableWarnings"], 1)

    def test_two_entries_agree(self):
        raw = (r"\(a\) 中文English" + "\n" + "续行"
               + blk("table", "| a | b |\n|---|---|\n| 1 |"))
        md, stats = convert_with_stats(raw, join_lines=True, cjk_spacing=True)
        gmd, gstats = to_gui_markdown_with_stats(raw, join_lines=True,
                                                 cjk_spacing=True)
        self.assertEqual(md, gmd)
        self.assertEqual(stats, gstats)

    def test_gui_entry_still_replaces_images(self):
        raw = blk("image", "![](page1_0_0_10_10.jpg)")
        out, stats = to_gui_markdown_with_stats(
            raw, join_lines=True, cjk_spacing=True)
        self.assertIn("图片区域 第1页", out)
        self.assertNotIn("page1_0_0_10_10.jpg", out)
        self.assertEqual(stats["tableWarnings"], 0)

    def test_to_gui_markdown_signature_compat(self):
        raw = blk("text", "中文English")
        self.assertEqual(to_gui_markdown(raw, False, True, True),
                         "中文 English")

    def test_join_lines_off_by_default(self):
        raw = blk("text", "第一行\n第二行")
        self.assertEqual(convert(raw), "第一行\n第二行")
        self.assertEqual(convert(raw, join_lines=True), "第一行第二行")


if __name__ == "__main__":
    unittest.main()

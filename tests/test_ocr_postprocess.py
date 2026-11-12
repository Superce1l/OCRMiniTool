# -*- coding: utf-8 -*-
"""tests/test_ocr_postprocess.py — 布局块 → Markdown 的纯函数回归测试

ocr_postprocess 是整条链路里唯一"零依赖、零 IO、纯函数"的模块，也是
模型输出格式变化时最先被打脸的地方，因此作为回归基线重点覆盖。

期望值来自 v1.2.0 的实际行为（已逐条人工核对），改动本模块时应同步更新。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_postprocess import (  # noqa: E402
    PAGE_SPLIT_RE, convert, to_gui_markdown,
)


def blk(label: str, content: str, bbox: str = "[0,0,100,100]",
        level: str = "") -> str:
    return f"<BLOCK>{label}{level}{bbox}<FORK>{content}"


class TestPageSplit(unittest.TestCase):
    def test_page_split_regex(self):
        parts = PAGE_SPLIT_RE.split("甲<PAGE>乙<PAGE 2>丙</PAGE>")
        self.assertEqual([p for p in parts if p], ["甲", "乙", "丙"])

    def test_pages_joined_with_blank_line(self):
        md = convert(blk("text", "第一页") + "<PAGE>" + blk("text", "第二页"))
        self.assertEqual(md, "第一页\n\n第二页")


class TestTitles(unittest.TestCase):
    def test_level1(self):
        self.assertEqual(convert(blk("title", "标题", level="<LEVEL1>")), "# 标题")

    def test_level3(self):
        self.assertEqual(convert(blk("title", "子标题", level="<LEVEL3>")), "### 子标题")

    def test_missing_level_defaults_to_h1(self):
        self.assertEqual(convert(blk("title", "无层级")), "# 无层级")

    def test_adjacent_same_level_titles_merge(self):
        # 同页、同 LEVEL 的相邻标题合并成一个标题（上游行为）
        md = convert(blk("title", "甲", level="<LEVEL1>")
                     + blk("title", "乙", level="<LEVEL1>"))
        self.assertEqual(md, "# 甲 乙")

    def test_different_level_not_merged(self):
        md = convert(blk("title", "甲", level="<LEVEL1>")
                     + blk("title", "乙", level="<LEVEL2>"))
        self.assertEqual(md, "# 甲\n\n## 乙")

    def test_single_root_demotes_second_top_title(self):
        md = convert(blk("title", "甲", level="<LEVEL1>") + "<PAGE>"
                     + blk("title", "乙", level="<LEVEL1>"))
        self.assertEqual(md, "# 甲\n\n## 乙")

    def test_empty_heading_dropped(self):
        self.assertEqual(convert(blk("title", "   ", level="<LEVEL1>")), "")


class TestTables(unittest.TestCase):
    def test_simple_table_kept_as_html(self):
        raw = blk("table", "<table><tr><th>项</th><th>值</th></tr>"
                           "<tr><td>a</td><td>1</td></tr></table>")
        self.assertEqual(convert(raw),
                         "<table><tr><th>项</th><th>值</th></tr>"
                         "<tr><td>a</td><td>1</td></tr></table>")

    def test_invalid_colspan_normalized(self):
        # colspan="x" 这类坏值必须归一成数字，否则浏览器渲染塌陷
        md = convert(blk("table", '<table><tr><td colspan="x">宽</td></tr></table>'))
        self.assertIn('colspan="1"', md)

    def test_valid_colspan_preserved(self):
        md = convert(blk("table", '<table><tr><td colspan="3">宽</td></tr></table>'))
        self.assertIn('colspan="3"', md)

    def test_cross_page_table_merges_and_dedups_header(self):
        raw = (blk("table", "<table><tr><th>H1</th></tr><tr><td>r1</td></tr></table>")
               + "<PAGE>"
               + blk("append_table", "<table><tr><th>H1</th></tr>"
                                     "<tr><td>r2</td></tr></table>"))
        md = convert(raw)
        self.assertEqual(md.count("<th>H1</th>"), 1, "跨页表头必须去重")
        self.assertIn("r1", md)
        self.assertIn("r2", md)


class TestTextAndNoise(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(convert(blk("text", "正文内容")), "正文内容")

    def test_merge_columns_concatenated(self):
        raw = (blk("text", "左栏") + "<MERGE>" + blk("text", "右栏"))
        self.assertEqual(convert(raw), "左栏右栏")

    def test_noise_content_dropped(self):
        self.assertEqual(convert(blk("image", "[non-text]")), "")

    def test_bare_block_prefix_fixed(self):
        # 模型偶尔漏 <BLOCK>，已知标签要能自己补回来
        self.assertEqual(convert("text[0,0,1,1]裸块正文"), "裸块正文")

    def test_markdown_fence_stripped(self):
        md = convert("```markdown\n" + blk("text", "围栏内正文") + "\n```")
        self.assertEqual(md, "围栏内正文")

    def test_stray_tokens_removed(self):
        md = convert(blk("text", "正文<|det|><image>尾巴"))
        self.assertEqual(md, "正文尾巴")

    def test_coloneqq_replacement(self):
        self.assertEqual(convert(blk("text", "a \\coloneqq b")), "a := b")

    def test_empty_input(self):
        self.assertEqual(convert(""), "")
        self.assertEqual(convert(None), "")


class TestGuiMarkdown(unittest.TestCase):
    def test_image_placeholder_rendered_as_note(self):
        # 正确格式：page{页码}_{x1}_{y1}_{x2}_{y2}.jpg（5 个数字）
        raw = blk("image", "![](page1_0_0_10_10.jpg)")
        self.assertEqual(to_gui_markdown(raw), "*［图片区域 第1页 (0,0)-(10,10)］*")

    def test_unknown_image_link_untouched(self):
        raw = blk("image", "![](page1_0_0_10_10_1.jpg)")
        # 上游格式之外的占位符按原样保留（不做猜测性改写）
        self.assertIn("![](page1_0_0_10_10_1.jpg)", to_gui_markdown(raw))


if __name__ == "__main__":
    unittest.main()

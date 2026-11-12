# -*- coding: utf-8 -*-
"""tests/test_token_budget.py — maxPageTokens 校准的守卫测试（不跑推理）

守三件事（防止"只改一处"的漂移）：
  1. 默认值 `maxPageTokens` 落在合理区间且是 1024 的倍数；
  2. `docs/token-budget.md` 的机读建议值与 `webview_backend.DEFAULT_SETTINGS` 一致；
  3. 校准文档生成脚本的纯函数（建议值公式、字数/行数折算、meta 结构）正确。

另有两类"真跑一次"的测试（都写临时目录、不加载模型）：
  • 字形自检：`gen_calib_docs.audit_glyphs()` 应无缺字形（字体缺失时跳过）；
  • 渲染集成：D2 真实画一张图 → PNG 尺寸/meta.json 与规格一致。

不 import torch、不联网、不跑引擎推理。
"""
import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import gen_calib_docs as gen  # noqa: E402
import webview_backend as wb  # noqa: E402

DOC_PATH = ROOT / "docs" / "token-budget.md"
TRY_PIL = importlib.util.find_spec("PIL") is not None
FONTS_OK = all(Path(p).is_file() for p in
               (gen.FONT_CJK, gen.FONT_CJK_BOLD, gen.FONT_MONO))
SYMBOL_OK = any(Path(p).is_file() for p in gen.FONT_SYMBOL_CANDIDATES)


def _doc_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def _doc_int(label: str) -> int:
    m = re.search(label + r"\s*[:：]\s*(\d+)", _doc_text())
    if not m:
        raise AssertionError(f"docs/token-budget.md 缺少机读行：{label}")
    return int(m.group(1))


# ══════════════════════════════════════════════════════════════════
class DefaultSettingTest(unittest.TestCase):
    """① 默认值本身是否合法（1024 步进、有上下限）。"""

    def setUp(self):
        self.value = wb.DEFAULT_SETTINGS["maxPageTokens"]

    def test_is_int(self):
        self.assertIsInstance(self.value, int)

    def test_within_hard_bounds(self):
        self.assertGreaterEqual(self.value, gen.TOKEN_FLOOR)
        self.assertLessEqual(self.value, gen.TOKEN_CAP)

    def test_multiple_of_1024(self):
        self.assertEqual(self.value % gen.TOKEN_STEP, 0,
                         f"maxPageTokens={self.value} 不是 {gen.TOKEN_STEP} 的倍数"
                         "（设置页是 1024 步进档位）")

    def test_not_unlimited_by_default(self):
        # 0 = 不限，会让最坏单页时长失控；默认值不允许是无限制
        self.assertNotEqual(self.value, 0)

    def test_get_settings_fallback_matches(self):
        # DEFAULT_SETTINGS 与 get_settings() 的兜底值必须一致（历史上是两处硬编码）
        src = (ROOT / "webview_backend.py").read_text(encoding="utf-8")
        defaults = re.findall(r'"maxPageTokens"\s*:\s*(\d+)', src)
        fallbacks = re.findall(
            r'get\(\s*"maxPageTokens"\s*,\s*(\d+)\s*\)', src)
        self.assertTrue(defaults, "DEFAULT_SETTINGS 里找不到 maxPageTokens 默认值")
        self.assertTrue(fallbacks, "get_settings() 里找不到 maxPageTokens 兜底值")
        self.assertEqual({int(x) for x in defaults + fallbacks}, {self.value})


class DocsConsistencyTest(unittest.TestCase):
    """② 文档里的建议值与设置默认值一致（防止改一处忘另一处）。"""

    def setUp(self):
        self.doc = _doc_text()

    def test_doc_exists(self):
        self.assertTrue(DOC_PATH.is_file())

    def test_recommended_matches_settings(self):
        self.assertEqual(_doc_int("建议默认值"),
                         wb.DEFAULT_SETTINGS["maxPageTokens"])

    def test_current_default_matches_settings(self):
        self.assertEqual(_doc_int("当前默认值"),
                         wb.DEFAULT_SETTINGS["maxPageTokens"])

    def test_formula_value_is_legal(self):
        """公式值是 p95 口径的机械结果：可以不同于默认值，但必须合法。"""
        formula = _doc_int("公式值")
        self.assertGreaterEqual(formula, gen.TOKEN_FLOOR)
        self.assertLessEqual(formula, gen.TOKEN_CAP)
        self.assertEqual(formula % gen.TOKEN_STEP, 0)

    def test_doc_floor_and_cap_match_generator(self):
        self.assertEqual(_doc_int("下限"), gen.TOKEN_FLOOR)
        self.assertEqual(_doc_int("上限"), gen.TOKEN_CAP)

    def test_doc_states_criteria_not_just_numbers(self):
        # 判据必须是"finish_reason=length 才算不足"，不能只写一个数字
        self.assertIn("finishReason", self.doc)
        self.assertIn("length", self.doc)
        self.assertIn("判据", self.doc)

    def test_doc_contains_phase_two_measurements(self):
        """第二阶段已跑完：实测表与 D6 runaway 证据必须留在文档里。"""
        for token in ("1184", "2333", "2311.6", "3072", "runaway", "12285"):
            with self.subTest(token=token):
                self.assertIn(token, self.doc)
        self.assertIn("D6", self.doc)
        self.assertIn("p95 仅用 D1–D5", self.doc)

    def test_doc_mentions_p95_and_factor(self):
        self.assertIn("p95", self.doc)
        self.assertIn("1.3", self.doc)


class RecommendFormulaTest(unittest.TestCase):
    """③ 建议值公式（纯函数）。"""

    def test_zero_or_invalid_falls_back_to_floor(self):
        for v in (0, None, -5, "x"):
            with self.subTest(value=v):
                self.assertEqual(gen.recommend_max_page_tokens(v),
                                 gen.TOKEN_FLOOR)

    def test_rounds_up_to_step(self):
        # 973（实机长截图）× 1.3 = 1264.9 → 2048
        self.assertEqual(gen.recommend_max_page_tokens(973), 2048)

    def test_exact_step_boundary(self):
        # 3000 × 1.3 = 3900 → 向上取整到 4096
        self.assertEqual(gen.recommend_max_page_tokens(3000), 4096)

    def test_capped_at_upper_bound(self):
        self.assertEqual(gen.recommend_max_page_tokens(7000), gen.TOKEN_CAP)
        self.assertEqual(gen.recommend_max_page_tokens(100000), gen.TOKEN_CAP)

    def test_never_below_floor(self):
        self.assertEqual(gen.recommend_max_page_tokens(10), gen.TOKEN_FLOOR)

    def test_custom_parameters(self):
        self.assertEqual(
            gen.recommend_max_page_tokens(1000, factor=2.0, step=512,
                                          floor=512, cap=4096),
            2048)


class PureHelpersTest(unittest.TestCase):
    """③ 生成脚本的纯函数（不依赖 Pillow）。"""

    def test_visual_width_cjk_vs_ascii(self):
        self.assertAlmostEqual(gen.visual_width("中文"), 2.0)
        self.assertAlmostEqual(gen.visual_width("ab"), 1.1)
        self.assertEqual(gen.visual_width(""), 0.0)

    def test_estimate_lines(self):
        self.assertEqual(gen.estimate_lines("中" * 41, 40), 2)
        self.assertEqual(gen.estimate_lines("中" * 40, 40), 1)
        self.assertEqual(gen.estimate_lines("a\nb", 40), 2)
        with self.assertRaises(ValueError):
            gen.estimate_lines("x", 0)

    def test_text_stats_partition(self):
        stats = gen.text_stats(["中文ab12，。"])
        self.assertEqual(stats["chars_cjk"], 4)      # 中 文 ， 。
        self.assertEqual(stats["chars_latin"], 2)
        self.assertEqual(stats["chars_digit"], 2)
        self.assertEqual(stats["chars_total"],
                         stats["chars_cjk"] + stats["chars_latin"] +
                         stats["chars_digit"] + stats["chars_symbol"])

    def test_text_stats_ignores_whitespace(self):
        self.assertEqual(gen.text_stats(["a b\nc"])["chars_total"], 3)

    def test_shuffled_is_deterministic(self):
        items = list(range(20))
        self.assertEqual(gen.shuffled(items, 7), gen.shuffled(items, 7))
        self.assertNotEqual(gen.shuffled(items, 7), gen.shuffled(items, 8))
        self.assertEqual(sorted(gen.shuffled(items, 7)), items)

    def test_build_meta_schema(self):
        spec = gen.SPEC_BY_ID["D6"]
        meta = gen.build_meta(spec, lines=100,
                              char_stats={"chars_total": 2000, "chars_cjk": 2000})
        self.assertEqual(meta["schema"], "ocrmini/calib-doc@1")
        self.assertEqual(meta["id"], "D6")
        self.assertEqual(meta["file"], "D6.png")
        self.assertEqual(meta["image"]["width"], 600)
        self.assertEqual(meta["image"]["height"], 3600)
        self.assertEqual(meta["image"]["hw_ratio"], 6.0)
        self.assertEqual(meta["content"]["lines"], 100)
        self.assertEqual(meta["content"]["chars_total"], 2000)
        self.assertEqual(meta["content"]["chars_per_line"], 20.0)
        self.assertEqual(meta["generator"]["version"], gen.DOC_VERSION)

    def test_build_meta_accepts_real_image_size(self):
        spec = gen.SPEC_BY_ID["D1"]
        meta = gen.build_meta(spec, lines=1, char_stats={},
                              image=(800, 600))
        self.assertEqual(meta["image"]["width"], 800)
        self.assertEqual(meta["image"]["hw_ratio"], 0.75)


class DocSpecsTest(unittest.TestCase):
    """D1–D6 规格必须与校准方案一致（防止改规格后文档不更新）。"""

    def test_six_docs_with_expected_ids(self):
        self.assertEqual([s["id"] for s in gen.DOC_SPECS],
                         ["D1", "D2", "D3", "D4", "D5", "D6"])

    def test_kinds_are_covered(self):
        self.assertEqual({s["kind"] for s in gen.DOC_SPECS},
                         {"dense_text", "table", "math", "two_column",
                          "code_mix", "long_shot"})

    def test_page_sizes(self):
        for spec in gen.DOC_SPECS:
            with self.subTest(doc=spec["id"]):
                if spec["id"] == "D6":
                    self.assertEqual((spec["width"], spec["height"]), (600, 3600))
                    self.assertAlmostEqual(
                        spec["height"] / spec["width"], 6.0, places=3)
                else:
                    self.assertEqual((spec["width"], spec["height"]), (1600, 2200))

    def test_d1_density_definition(self):
        p = gen.SPEC_BY_ID["D1"]["params"]
        self.assertEqual(p["target_lines"], 45)
        self.assertEqual(p["target_chars_per_line"], 40)

    def test_d2_table_shape(self):
        p = gen.SPEC_BY_ID["D2"]["params"]
        self.assertEqual((p["rows"], p["cols"]), (40, 12))
        self.assertGreaterEqual(p["merges"], 1)

    def test_d3_has_math_params(self):
        p = gen.SPEC_BY_ID["D3"]["params"]
        self.assertTrue(p["inline_sup_sub"])
        self.assertTrue(p["greek"])

    def test_d4_is_two_column(self):
        p = gen.SPEC_BY_ID["D4"]["params"]
        self.assertEqual(p["layout"], "two-column")
        self.assertEqual(p["columns"], 2)

    def test_d5_has_code_block_config(self):
        p = gen.SPEC_BY_ID["D5"]["params"]
        self.assertTrue(p["code_line_numbers"])
        self.assertGreaterEqual(p["code_blocks"], 1)

    def test_corpus_is_rich_enough_to_fill_pages(self):
        # 语料太少会导致页面重复段落（看起来假），这里给个下限护栏
        self.assertGreaterEqual(len(gen.CORPUS_ZH), 60)
        self.assertGreaterEqual(len(gen.CORPUS_EN), 30)
        self.assertGreaterEqual(len(gen.CORPUS_MATH), 40)


class GenerateGuardTest(unittest.TestCase):
    """generate() 的输入校验（不需要 Pillow 就会先命中 id 校验）。"""

    def test_unknown_only_id_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                gen.generate(tmp, only=["D9"], verbose=False)

    def test_main_rejects_bad_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = gen.main(["--out", tmp, "--only", "D9", "--quiet"])
        self.assertEqual(rc, 1)


@unittest.skipUnless(TRY_PIL and FONTS_OK and SYMBOL_OK,
                     "缺 Pillow 或系统字体，跳过字形自检")
class GlyphAuditTest(unittest.TestCase):
    def test_no_tofu_glyphs(self):
        missing = gen.audit_glyphs()
        self.assertEqual(missing, [], f"缺字形：{missing[:5]}")


@unittest.skipUnless(TRY_PIL and FONTS_OK,
                     "缺 Pillow 或系统字体，跳过渲染集成测试")
class RenderIntegrationTest(unittest.TestCase):
    def test_render_d2_writes_png_and_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            metas = gen.generate(tmp, only=["D2"], verbose=False)
            self.assertEqual(len(metas), 1)
            meta = metas[0]
            png = Path(tmp) / "D2.png"
            self.assertTrue(png.is_file())
            raw = png.read_bytes()
            self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
            # 规格尺寸（未压缩像素）：PNG IHDR 宽高
            width = int.from_bytes(raw[16:20], "big")
            height = int.from_bytes(raw[20:24], "big")
            self.assertEqual((width, height), (1600, 2200))
            self.assertEqual(meta["image"]["width"], width)
            self.assertEqual(meta["image"]["height"], height)
            self.assertGreater(meta["content"]["lines"], 30)
            self.assertGreater(meta["content"]["chars_total"], 1000)
            self.assertGreater(meta["png"]["bytes"], 1024)
            self.assertRegex(meta["png"]["sha256"], r"^[0-9a-f]{64}$")
            # meta.json 落盘且可解析
            on_disk = json.loads((Path(tmp) / "D2.meta.json")
                                 .read_text(encoding="utf-8"))
            self.assertEqual(on_disk["id"], "D2")
            self.assertEqual(on_disk["schema"], "ocrmini/calib-doc@1")
            # index.json 汇总
            index = json.loads((Path(tmp) / "index.json")
                               .read_text(encoding="utf-8"))
            self.assertEqual([d["id"] for d in index["docs"]], ["D2"])


if __name__ == "__main__":
    unittest.main()

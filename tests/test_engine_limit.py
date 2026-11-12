# -*- coding: utf-8 -*-
"""tests/test_engine_limit.py — A2 生成限幅 / A3 合并分组 / A6 切片策略 / E11 页眉页脚

覆盖 task-10 契约：
  A2  max_page_tokens：0=不限；正数夹到 [256, 65536]；合并时
      max_new_tokens = min(单页上限×组页数, 显式值, 131072)。
  A3  max_merged_pages：进推理前分组、组间检查 cancel_event、按原页序拼回、
      进度按「已完成组/总组」推进。
  A6  crop_mode：None=旧行为；auto=单图 h/w>3 → base，其余 gundam，多页 → base。
  E11 drop_headers：只丢 header/footer/page_number，返回丢弃块数，
      convert(str)->str 契约不变。

手法与 test_engine_recognize.py 一致：假 model/tokenizer + sys.modules['torch'] 桩。
"""
from __future__ import annotations

import contextlib
import shutil
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import applog                      # noqa: E402
import ocr_engine                  # noqa: E402
import ocr_postprocess             # noqa: E402
from ocr_engine import EngineError, OcrEngine  # noqa: E402

MODEL_MAX_CONTEXT = 131072


# ── 桩：torch / 假模型 ──────────────────────────────────────────
class _FakeTensor:
    def cuda(self, *args, **kwargs):
        return self


class _FakeCuda:
    def is_available(self):
        return False

    def get_device_name(self, index=0):
        return "Fake GPU"

    def get_device_properties(self, index=0):
        return types.SimpleNamespace(total_memory=0)


def make_fake_torch():
    m = types.ModuleType("torch")
    m.__version__ = "2.8.0"
    m.Tensor = _FakeTensor
    m.bfloat16 = "bfloat16"
    m.cuda = _FakeCuda()
    m.version = types.SimpleNamespace(cuda=None)
    m.inference_mode = contextlib.nullcontext
    return m


def blk(label: str, content: str, bbox: str = "[0,0,100,100]",
        level: str = "") -> str:
    return f"<BLOCK>{label}{level}{bbox}<FORK>{content}"


class _LayoutModel:
    """每次 infer 把 image_file 的 stem 逐个变成一页布局块；可注入组间取消。"""

    def __init__(self, raw_by_page=None, cancel_event=None, cancel_after=None):
        self.calls = []
        self.raw_by_page = raw_by_page or {}
        self.cancel_event = cancel_event
        self.cancel_after = cancel_after

    def infer(self, tokenizer, **kwargs):
        self.calls.append(kwargs)
        stems = [Path(p).stem for p in kwargs.get("image_file") or []]
        chunks = []
        for stem in stems:
            raw = self.raw_by_page.get(stem)
            chunks.append(raw if raw is not None else blk("text", stem))
        text = "".join("<PAGE>" + c for c in chunks) if chunks else "<PAGE>"
        res = {"text": text, "token_ids": [1] * 5, "prompt_token_ids": [0],
               "finish_reason": "stop", "num_pages_out": len(stems),
               "expected_pages": len(stems), "time": 1.0, "prefill_time": 0.5}
        if (self.cancel_event is not None and self.cancel_after is not None
                and len(self.calls) >= self.cancel_after):
            self.cancel_event.set()
        return res


class _EngineBase(unittest.TestCase):
    def setUp(self):
        self._had_torch = "torch" in sys.modules
        self._old_torch = sys.modules.get("torch")
        sys.modules["torch"] = make_fake_torch()
        self._old_orig_cuda = ocr_engine._ORIG_TENSOR_CUDA
        ocr_engine._ORIG_TENSOR_CUDA = None
        self._old_log = applog.log_ocr
        self.logs = []
        applog.log_ocr = self.logs.append
        ocr_engine._STREAM_HOOK.update(cancel_event=None, state=None)
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_engine_limit_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.progress = []

    def tearDown(self):
        if self._had_torch and self._old_torch is not None:
            sys.modules["torch"] = self._old_torch
        else:
            sys.modules.pop("torch", None)
        ocr_engine._ORIG_TENSOR_CUDA = self._old_orig_cuda
        applog.log_ocr = self._old_log
        ocr_engine._STREAM_HOOK.update(cancel_event=None, state=None)

    def _png(self, name: str, width: int, height: int) -> Path:
        from PIL import Image
        p = self.tmp / name
        Image.new("RGB", (width, height), "white").save(p)
        return p

    def _engine(self, model, model_dir=None):
        eng = OcrEngine(model_dir or (self.tmp / "model"))
        eng.ready = True
        eng.device = "cpu"
        eng.tokenizer = object()
        eng.model = model
        return eng

    def _paths(self, n: int):
        return [self.tmp / f"p{i}.png" for i in range(1, n + 1)]

    def _progress_cb(self):
        def cb(stage, done, total, elapsed):
            self.progress.append((done, total))
        return cb


# ── A2：纯函数 ──────────────────────────────────────────────────
class PageTokenMathTest(unittest.TestCase):
    def test_normalize_page_tokens(self):
        cases = {None: 0, 0: 0, "x": 0, -5: 0, 1: 256, 100: 256, 256: 256,
                 2048: 2048, "2048": 2048, 65536: 65536, 10 ** 6: 65536}
        for given, want in cases.items():
            self.assertEqual(ocr_engine._normalize_page_tokens(given), want,
                             f"max_page_tokens={given!r}")

    def test_normalize_merged_pages(self):
        for given, want in {None: 0, 0: 0, "x": 0, -3: 0, 1: 1, 5: 5}.items():
            self.assertEqual(ocr_engine._normalize_merged_pages(given), want)

    def test_normalize_explicit_max_new_tokens(self):
        for given, want in {None: None, 0: None, -1: None, "x": None,
                            512: 512, 10 ** 9: MODEL_MAX_CONTEXT}.items():
            self.assertEqual(
                ocr_engine._normalize_explicit_max_new_tokens(given), want)

    def test_effective_max_new_tokens(self):
        f = ocr_engine._effective_max_new_tokens
        self.assertIsNone(f(0, 1, None))            # 0=不限，不传
        self.assertEqual(f(0, 20, 3000), 3000)      # 只有显式值
        self.assertEqual(f(2048, 1, None), 2048)    # 单页 = 单页上限
        self.assertEqual(f(2048, 20, None), 40960)  # ×20 页
        self.assertEqual(f(5000, 20, None), 100000)
        self.assertEqual(f(10000, 20, None), MODEL_MAX_CONTEXT)   # 夹到 131072
        self.assertEqual(f(2048, 1, 512), 512)      # 显式值更小
        self.assertEqual(f(2048, 20, 512), 512)

    def test_effective_never_exceeds_context(self):
        for pages in (1, 2, 20, 200):
            cap = ocr_engine._effective_max_new_tokens(65536, pages, None)
            self.assertLessEqual(cap, MODEL_MAX_CONTEXT)


# ── A6：切片策略（纯函数 + 尺寸判定）────────────────────────────
class CropModeTest(_EngineBase):
    def test_auto_single_vs_multi(self):
        """auto：单图一律 gundam；多页 base。

        长截图不再被降级成 base——真机实测 600×3600 在 base 下文字不可读、
        模型开始胡编甚至退化重复；同图 gundam 输出连贯正文（见
        docs/实机测试报告.md §3.5 与 docs/token-budget.md）。
        """
        f = ocr_engine._auto_crop_mode
        self.assertTrue(f(1, 100, 100))       # 普通单图 → gundam
        self.assertTrue(f(1, 100, 300))       # h/w == 3 → gundam
        self.assertTrue(f(1, 600, 3600))      # 超长截图 → 仍 gundam（正确性优先）
        self.assertFalse(f(2, 100, 100))      # 多页 → base
        self.assertTrue(f(1, 0, 0))           # 尺寸未知 → gundam

    def test_resolve_none_keeps_old_behavior(self):
        p = self._png("normal.png", 100, 100)
        self.assertIsNone(ocr_engine._resolve_crop_mode([p], None))

    def test_resolve_auto_ignores_aspect(self):
        normal = self._png("normal2.png", 100, 100)
        long_shot = self._png("long.png", 100, 500)
        self.assertTrue(ocr_engine._resolve_crop_mode([normal], "auto"))
        # 长截图也走 gundam：auto 不再按长宽比降级（真机证伪过）
        self.assertTrue(ocr_engine._resolve_crop_mode([long_shot], "auto"))
        self.assertFalse(ocr_engine._resolve_crop_mode([normal, normal], "auto"))

    def test_resolve_explicit_and_degrade(self):
        p = self._png("normal3.png", 100, 100)
        self.assertTrue(ocr_engine._resolve_crop_mode([p], "gundam"))
        self.assertFalse(ocr_engine._resolve_crop_mode([p], "base"))
        self.assertFalse(ocr_engine._resolve_crop_mode([p, p], "gundam"))
        self.assertTrue(ocr_engine._resolve_crop_mode([p], True))     # 旧 bool API
        self.assertFalse(ocr_engine._resolve_crop_mode([p], False))
        self.assertTrue(ocr_engine._resolve_crop_mode([p], "weird"))  # 未知→auto

    def test_engine_passes_resolved_crop_mode(self):
        normal = self._png("n.png", 100, 100)
        long_shot = self._png("l.png", 100, 900)
        m1 = _LayoutModel()
        out = self._engine(m1).recognize([normal], crop_mode="auto")
        self.assertTrue(m1.calls[0]["crop_mode"])
        self.assertEqual(out["groups"], 1)

        # 长截图在 auto 下也是 gundam（不再按长宽比降级，真机证伪过）
        m2 = _LayoutModel()
        self._engine(m2).recognize([long_shot], crop_mode="auto")
        self.assertTrue(m2.calls[0]["crop_mode"])

        m3 = _LayoutModel()
        self._engine(m3).recognize([normal, normal], crop_mode="auto")
        self.assertFalse(m3.calls[0]["crop_mode"])

        m4 = _LayoutModel()
        self._engine(m4).recognize([normal])             # None → 不传 crop_mode
        self.assertNotIn("crop_mode", m4.calls[0])


# ── A3：合并分组 ────────────────────────────────────────────────
class MergeGroupingTest(_EngineBase):
    def _run(self, n_pages, **kwargs):
        model = _LayoutModel()
        eng = self._engine(model)
        out = eng.recognize(self._paths(n_pages),
                            progress_cb=self._progress_cb(), **kwargs)
        return model, out

    def test_no_limit_is_single_group(self):
        model, out = self._run(5, max_merged_pages=0)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(out["groups"], 1)
        self.assertEqual(out["pages"], [f"p{i}" for i in range(1, 6)])
        self.assertEqual(self.progress[-1], (1, 1))

    def test_group_of_one_page(self):
        model, out = self._run(5, max_merged_pages=1)
        self.assertEqual(len(model.calls), 5)
        self.assertEqual(out["groups"], 5)
        self.assertEqual(out["pages"], [f"p{i}" for i in range(1, 6)])
        self.assertEqual(self.progress[-1], (5, 5))
        self.assertEqual(self.progress[0], (0, 5))

    def test_group_of_two_pages(self):
        model, out = self._run(5, max_merged_pages=2)
        self.assertEqual(len(model.calls), 3)          # 2+2+1
        self.assertEqual(out["groups"], 3)
        self.assertEqual([len(c["image_file"]) for c in model.calls], [2, 2, 1])
        self.assertEqual(out["pages"], [f"p{i}" for i in range(1, 6)])
        self.assertEqual(self.progress[-1], (3, 3))

    def test_limit_equal_pages_is_single_group(self):
        model, out = self._run(5, max_merged_pages=5)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(out["groups"], 1)

    def test_limit_above_pages_is_single_group(self):
        model, out = self._run(5, max_merged_pages=99)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(out["groups"], 1)

    def test_single_page_is_one_group(self):
        model, out = self._run(1)
        self.assertEqual(out["groups"], 1)
        self.assertEqual(out["pages"], ["p1"])

    def test_grouping_logs_context_loss(self):
        self._run(5, max_merged_pages=2)
        joined = "\n".join(self.logs)
        self.assertIn("合并分组", joined)
        self.assertIn("跨组不共享上下文", joined)

    def test_text_keeps_page_comments(self):
        _, out = self._run(3, max_merged_pages=2)
        self.assertIn("<!-- page 1 -->", out["text"])
        self.assertIn("<!-- page 3 -->", out["text"])


class GroupCancelTest(_EngineBase):
    def test_cancel_before_second_group(self):
        ev = threading.Event()
        model = _LayoutModel(cancel_event=ev, cancel_after=1)
        eng = self._engine(model)
        with self.assertRaises(EngineError):
            eng.recognize(self._paths(5), max_merged_pages=1,
                          cancel_event=ev, progress_cb=self._progress_cb())
        self.assertEqual(len(model.calls), 1, "取消后不应再跑第二组")
        self.assertEqual(self.progress[-1], (1, 5))
        self.assertIsNone(ocr_engine._STREAM_HOOK["cancel_event"],
                          "取消后钩子必须复位")

    def test_no_cancel_runs_all_groups(self):
        ev = threading.Event()
        model = _LayoutModel()
        eng = self._engine(model)
        out = eng.recognize(self._paths(4), max_merged_pages=1,
                            cancel_event=ev, progress_cb=self._progress_cb())
        self.assertEqual(len(model.calls), 4)
        self.assertEqual(out["groups"], 4)


# ── A2：端到端 token 上限 ───────────────────────────────────────
class MaxPageTokensEndToEndTest(_EngineBase):
    def _run(self, n_pages, **kwargs):
        model = _LayoutModel()
        out = self._engine(model).recognize(self._paths(n_pages), **kwargs)
        return model, out

    def test_single_page_cap(self):
        model, out = self._run(1, max_page_tokens=2048)
        self.assertEqual(model.calls[0]["max_new_tokens"], 2048)
        self.assertEqual(out["effectiveMaxNewTokens"], 2048)

    def test_merged_cap_multiplies_by_pages(self):
        model, out = self._run(20, max_page_tokens=2048)
        self.assertEqual(model.calls[0]["max_new_tokens"], 40960)
        self.assertEqual(out["effectiveMaxNewTokens"], 40960)

    def test_merged_cap_clamped_to_context(self):
        model, out = self._run(20, max_page_tokens=10000)
        self.assertEqual(model.calls[0]["max_new_tokens"], MODEL_MAX_CONTEXT)
        self.assertEqual(out["effectiveMaxNewTokens"], MODEL_MAX_CONTEXT)

    def test_zero_means_model_default(self):
        model, out = self._run(3)
        self.assertNotIn("max_new_tokens", model.calls[0])
        self.assertIsNone(out["effectiveMaxNewTokens"])

    def test_invalid_value_clamped_to_floor(self):
        model, out = self._run(1, max_page_tokens=100)     # 100 → 256（单页）
        self.assertEqual(model.calls[0]["max_new_tokens"], 256)
        self.assertEqual(out["effectiveMaxNewTokens"], 256)
        model2, out2 = self._run(2, max_page_tokens=100)   # 256 × 2 页
        self.assertEqual(model2.calls[0]["max_new_tokens"], 512)
        self.assertEqual(out2["effectiveMaxNewTokens"], 512)

    def test_explicit_value_wins_when_smaller(self):
        model, out = self._run(1, max_page_tokens=2048, max_new_tokens=512)
        self.assertEqual(model.calls[0]["max_new_tokens"], 512)
        self.assertEqual(out["effectiveMaxNewTokens"], 512)

    def test_grouped_caps_per_group_size(self):
        model, out = self._run(5, max_page_tokens=2048, max_merged_pages=2)
        self.assertEqual([c["max_new_tokens"] for c in model.calls],
                         [4096, 4096, 2048])
        self.assertEqual(out["effectiveMaxNewTokens"], 4096)
        for c in model.calls:
            self.assertLessEqual(c["max_new_tokens"], MODEL_MAX_CONTEXT)


# ── E11：页眉页脚丢弃 ───────────────────────────────────────────
class DropHeadersTest(_EngineBase):
    def test_postprocess_opt_in(self):
        raw = (blk("header", "页眉") + blk("text", "正文")
               + blk("footer", "页脚") + blk("page_number", "- 3 -")
               + blk("title", "标题", level="<LEVEL1>"))
        keep = ocr_postprocess.convert(raw)
        self.assertIn("页眉", keep)
        self.assertIn("页脚", keep)
        self.assertIn("- 3 -", keep)
        dropped = ocr_postprocess.convert(raw, drop_headers=True)
        self.assertNotIn("页眉", dropped)
        self.assertNotIn("页脚", dropped)
        self.assertNotIn("- 3 -", dropped)
        self.assertIn("正文", dropped)
        self.assertIn("# 标题", dropped)

    def test_convert_with_stats_and_contract(self):
        raw = blk("header", "页眉") + blk("text", "正文")
        text, stats = ocr_postprocess.convert_with_stats(raw, drop_headers=True)
        self.assertEqual(stats["droppedBlocks"], 1)
        self.assertEqual(ocr_postprocess.convert(raw),
                         ocr_postprocess.convert_with_stats(raw)[0])
        self.assertEqual(ocr_postprocess.convert_with_stats(raw)[1]["droppedBlocks"], 0)

    def test_gui_variant(self):
        raw = blk("header", "页眉") + blk("text", "正文")
        self.assertIn("页眉", ocr_postprocess.to_gui_markdown(raw))
        md, stats = ocr_postprocess.to_gui_markdown_with_stats(
            raw, drop_headers=True)
        self.assertNotIn("页眉", md)
        self.assertEqual(stats["droppedBlocks"], 1)

    def test_text_title_not_dropped(self):
        raw = (blk("text", "正文甲") + blk("title", "标题乙", level="<LEVEL2>")
               + blk("image_caption", "图注") + blk("table", "<table><tr><td>格</td></tr></table>"))
        md = ocr_postprocess.convert(raw, drop_headers=True)
        for keep in ("正文甲", "标题乙", "图注", "格"):
            self.assertIn(keep, md)

    def test_engine_aggregates_dropped_blocks(self):
        raw = {f"p{i}": (blk("header", f"页眉{i}") + blk("text", f"正文{i}")
                         + blk("footer", f"页脚{i}")) for i in range(1, 3)}
        model = _LayoutModel(raw_by_page=raw)
        eng = self._engine(model)
        out = eng.recognize(self._paths(2), drop_headers=True)
        self.assertEqual(out["droppedBlocks"], 4)     # 每页 2 块
        self.assertNotIn("页眉", out["text"])
        self.assertNotIn("页脚", out["text"])
        self.assertIn("正文1", out["text"])
        self.assertIn("正文2", out["text"])

    def test_engine_default_keeps_headers(self):
        raw = {"p1": blk("header", "页眉") + blk("text", "正文")}
        model = _LayoutModel(raw_by_page=raw)
        out = self._engine(model).recognize(self._paths(1))
        self.assertEqual(out["droppedBlocks"], 0)
        self.assertIn("页眉", out["text"])

    def test_engine_logs_drop(self):
        raw = {"p1": blk("header", "页眉") + blk("text", "正文")}
        self._engine(_LayoutModel(raw_by_page=raw)).recognize(
            self._paths(1), drop_headers=True)
        self.assertTrue(any("页眉页脚丢弃" in m for m in self.logs))


# ── 向后兼容 ────────────────────────────────────────────────────
class BackwardCompatTest(_EngineBase):
    def test_defaults_keep_old_kwargs(self):
        model = _LayoutModel()
        out = self._engine(model).recognize(self._paths(1))
        call = model.calls[0]
        self.assertNotIn("crop_mode", call)
        self.assertNotIn("max_new_tokens", call)
        self.assertTrue(call["return_dict"])
        self.assertEqual(call["temperature"], 0.0)
        # 既有字段
        self.assertEqual(out["pages"], ["p1"])
        self.assertIsInstance(out["elapsed"], float)
        self.assertIsInstance(out["text"], str)
        # 新增字段存在且语义中性
        self.assertEqual(out["groups"], 1)
        self.assertEqual(out["droppedBlocks"], 0)
        self.assertIsNone(out["effectiveMaxNewTokens"])

    def test_multipage_default_merges_in_one_request(self):
        model = _LayoutModel()
        out = self._engine(model).recognize(self._paths(3))
        self.assertEqual(len(model.calls), 1)
        self.assertFalse(model.calls[0]["crop_mode"])
        self.assertEqual(out["groups"], 1)

    def test_positional_call_still_works(self):
        model = _LayoutModel()
        eng = self._engine(model)
        out = eng.recognize(self._paths(1), "", None, None, None)
        self.assertEqual(out["pages"], ["p1"])


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""tests/test_engine_recognize.py — recognize() 的度量/截断可见（A1）与 PDF dpi（A7）

A1：`_infer` 传 return_dict=True，把 finish_reason / num_pages_out /
    expected_pages / prefill_time / time 收敛成中性字段回给 recognize()，
    并写 applog；模型若只返回 str（旧实现/桩）必须完全兼容、且绝不误报截断。
A7：PDF 光栅化 dpi 从 recognize 透传到 pdf_to_images；不传时仍是 200（旧行为）。

字段名依据模型源码 models/Unlimited-OCR-2/modeling_unlimitedocr.py：
  infer(return_dict=True) → res（_uocr_generate 的返回 dict + prompt），键为
  text / token_ids / prompt_token_ids / finish_reason / num_pages_out /
  expected_pages / time / prefill_time。
"""
from __future__ import annotations

import contextlib
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import applog                      # noqa: E402
import ocr_engine                  # noqa: E402
from ocr_engine import EngineError, OcrEngine  # noqa: E402


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


class _RecordingModel:
    """记录 infer(**kwargs)；reply 是 str 或 dict（模拟两种返回契约）。"""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def infer(self, tokenizer, **kwargs):
        self.calls.append(kwargs)
        return self.reply


def _dict_reply(text="正文", **overrides):
    res = {"text": text, "token_ids": [7] * 10, "prompt_token_ids": [0, 1],
           "finish_reason": "stop", "num_pages_out": 1, "expected_pages": 1,
           "time": 2.0, "prefill_time": 1.5, "prompt": "Multi page parsing."}
    res.update(overrides)
    return res


class RecognizeMetricsTest(unittest.TestCase):
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

    def tearDown(self):
        if self._had_torch and self._old_torch is not None:
            sys.modules["torch"] = self._old_torch
        else:
            sys.modules.pop("torch", None)
        ocr_engine._ORIG_TENSOR_CUDA = self._old_orig_cuda
        applog.log_ocr = self._old_log
        ocr_engine._STREAM_HOOK.update(cancel_event=None, state=None)

    def _engine(self, reply):
        eng = OcrEngine(Path(tempfile.mkdtemp(prefix="ocr_engine_rec_")))
        eng.ready = True
        eng.device = "cpu"
        eng.tokenizer = object()
        eng.model = _RecordingModel(reply)
        self.addCleanup(shutil.rmtree, eng.model_dir, True)
        return eng

    # ── 返回契约：dict / str 两种都要兼容 ───────────────────────
    def test_return_dict_metrics_wired(self):
        eng = self._engine(_dict_reply())
        out = eng.recognize([Path("page1.png")])
        self.assertEqual(out["text"], "正文")
        self.assertEqual(len(out["pages"]), 1)
        self.assertIsInstance(out["elapsed"], float)
        self.assertEqual(out["tokens"], 10)
        self.assertEqual(out["finishReason"], "stop")
        self.assertFalse(out["truncated"])
        self.assertFalse(out["pagesMismatch"])
        infer = out["infer"]
        self.assertAlmostEqual(infer["prefillSecs"], 1.5)
        self.assertAlmostEqual(infer["decodeSecs"], 0.5)
        self.assertAlmostEqual(infer["tokensPerSec"], 20.0)
        self.assertEqual(infer["numPagesOut"], 1)
        self.assertEqual(infer["expectedPages"], 1)
        # return_dict=True 必须真的传给模型
        self.assertTrue(eng.model.calls[0]["return_dict"])

    def test_str_return_fully_compatible(self):
        eng = self._engine("纯文本结果")
        out = eng.recognize([Path("page1.png")])
        self.assertEqual(out["text"], "纯文本结果")
        self.assertIsNone(out["tokens"])
        self.assertIsNone(out["finishReason"])
        self.assertFalse(out["truncated"])          # 未知 ≠ 截断
        self.assertFalse(out["pagesMismatch"])
        self.assertIsInstance(out["infer"], dict)

    def test_unknown_return_type_falls_back_to_str(self):
        eng = self._engine(12345)
        out = eng.recognize([Path("page1.png")])
        self.assertEqual(out["text"], "12345")
        self.assertFalse(out["truncated"])

    # ── 截断 / 页数不符 的可见性 ────────────────────────────────
    def test_length_finish_marks_truncated(self):
        eng = self._engine(_dict_reply(finish_reason="length", time=9.0,
                                      prefill_time=1.0, token_ids=[3] * 4096))
        out = eng.recognize([Path("page1.png")])
        self.assertTrue(out["truncated"])
        self.assertEqual(out["tokens"], 4096)
        self.assertTrue(any("TRUNCATED" in m for m in self.logs))

    def test_pages_mismatch_flagged(self):
        eng = self._engine(_dict_reply(num_pages_out=1, expected_pages=3))
        out = eng.recognize([Path("page1.png"), Path("page2.png"),
                             Path("page3.png")])
        self.assertTrue(out["pagesMismatch"])
        self.assertTrue(any("PAGES-MISMATCH" in m for m in self.logs))

    def test_metrics_are_logged(self):
        eng = self._engine(_dict_reply())
        eng.recognize([Path("page1.png")])
        self.assertTrue(any("引擎指标" in m and "tps=20.0" in m for m in self.logs))
        self.assertEqual(eng._last_infer["tokensPerSec"], 20.0)

    # ── 多页合并：既有 text/pages 语义不变 ─────────────────────
    def test_multipage_text_and_pages_unchanged(self):
        eng = self._engine(_dict_reply(
            text="<PAGE>第一页<PAGE>第二页", num_pages_out=2, expected_pages=2))
        out = eng.recognize([Path("p1.png"), Path("p2.png")])
        self.assertEqual(out["pages"], ["第一页", "第二页"])
        self.assertIn("<!-- page 1 -->", out["text"])
        self.assertIn("第二页", out["text"])
        self.assertFalse(out["pagesMismatch"])

    def test_crop_mode_and_max_tokens_passthrough(self):
        eng = self._engine(_dict_reply())
        eng.recognize([Path("p1.png")], crop_mode=False, max_new_tokens=2048)
        call = eng.model.calls[0]
        self.assertIs(call["crop_mode"], False)
        self.assertEqual(call["max_new_tokens"], 2048)

    def test_cancel_reported_at_end(self):
        eng = self._engine(_dict_reply())
        import threading
        ev = threading.Event()
        ev.set()
        with self.assertRaises(EngineError):
            eng.recognize([Path("p1.png")], cancel_event=ev)
        # 钩子在 stream_end 不抛（A5）后，取消必须仍由 recognize 兜底上报
        self.assertIsNone(ocr_engine._STREAM_HOOK["cancel_event"])


class PdfDpiTest(RecognizeMetricsTest):
    """A7：dpi 从 recognize 透传到 pdf_to_images，默认值不变。"""

    def setUp(self):
        super().setUp()
        self.seen = []
        outer = self

        def fake_pdf(self_engine, pdf_path, dpi=200, out_dir=None):
            outer.seen.append({"dpi": dpi, "out_dir": Path(out_dir) if out_dir else None})
            # 返回一个不存在的路径即可：假模型不读文件
            return [Path(out_dir) / "p1.png"] if out_dir else [pdf_path]

        self._patcher = mock.patch.object(ocr_engine.OcrEngine, "pdf_to_images",
                                          new=fake_pdf)
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_default_dpi_is_200(self):
        eng = self._engine(_dict_reply())
        eng.recognize([Path("doc.pdf")])
        self.assertEqual(self.seen[0]["dpi"], 200)

    def test_explicit_dpi_passthrough(self):
        eng = self._engine(_dict_reply())
        eng.recognize([Path("doc.pdf")], dpi=150)
        self.assertEqual(self.seen[0]["dpi"], 150)

    def test_invalid_dpi_falls_back_and_clamps(self):
        eng = self._engine(_dict_reply())
        for given, expected in ((None, 200), (0, 200), (-5, 200), ("bad", 200),
                                (9999, 600), (10, 72)):
            self.seen.clear()
            eng.recognize([Path("doc.pdf")], dpi=given)
            self.assertEqual(self.seen[0]["dpi"], expected, f"dpi={given!r}")

    def test_temp_dir_cleaned_after_pdf(self):
        eng = self._engine(_dict_reply())
        eng.recognize([Path("doc.pdf")])
        tmp = self.seen[0]["out_dir"]
        self.assertIsNotNone(tmp)
        self.assertFalse(tmp.exists(), "PDF 临时目录必须在识别后清理")


class NormalizeDpiUnitTest(unittest.TestCase):
    def test_normalize(self):
        from ocr_engine import _normalize_dpi
        self.assertEqual(_normalize_dpi(None), 200)
        self.assertEqual(_normalize_dpi(150), 150)
        self.assertEqual(_normalize_dpi("300"), 300)
        self.assertEqual(_normalize_dpi("x"), 200)
        self.assertEqual(_normalize_dpi(1), 72)
        self.assertEqual(_normalize_dpi(10 ** 6), 600)


class SummarizeInferUnitTest(unittest.TestCase):
    def test_none_and_str(self):
        from ocr_engine import _summarize_infer
        for raw in (None, "text", {}, []):
            s = _summarize_infer(raw)
            self.assertFalse(s["truncated"])
            self.assertFalse(s["pagesMismatch"])
            self.assertIsNone(s["finishReason"])

    def test_decode_never_negative(self):
        from ocr_engine import _summarize_infer
        s = _summarize_infer({"finish_reason": "stop", "time": 1.0,
                              "prefill_time": 3.0, "token_ids": [1]})
        self.assertEqual(s["decodeSecs"], 0.0)
        self.assertIsNone(s["tokensPerSec"])


if __name__ == "__main__":
    unittest.main()

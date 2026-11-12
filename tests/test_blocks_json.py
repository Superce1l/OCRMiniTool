# -*- coding: utf-8 -*-
"""tests/test_blocks_json.py — E2 结构化 blocks（后处理 build_document + 引擎字段）

覆盖：
  - build_document：bbox/level/type/mergePrev 解析、<PAGE> 分页与 page_base、
    drop_headers 排除与空页省略、阅读序与 Markdown 渲染一致、畸形输入降级、
    纯函数（同输入同输出）
  - ocr_engine.recognize 的 `blocks`：跨组汇总、页号连续（max_merged_pages=1
    与 2 两组场景）、drop_headers 透传、既有字段不回归

手法与 test_engine_limit.py 一致：假 model/tokenizer + sys.modules['torch'] 桩。
"""
from __future__ import annotations

import contextlib
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import applog                       # noqa: E402
import ocr_engine                   # noqa: E402
import ocr_postprocess as PP        # noqa: E402
from ocr_engine import OcrEngine    # noqa: E402


def blk(label: str, content: str, level: str = "",
        bbox: str = "[10,20,30,40]") -> str:
    return f"<BLOCK>{label}{level}{bbox}<FORK>{content}"


def _blocks_of(doc: dict) -> list[dict]:
    return [b for page in doc["pages"] for b in page["blocks"]]


class BuildDocumentTest(unittest.TestCase):
    def test_bbox_level_type_parsed(self):
        doc = PP.build_document(blk("title", "标题", level="<LEVEL2>"))
        block = _blocks_of(doc)[0]
        self.assertEqual(block["type"], "title")
        self.assertEqual(block["label"], "title")
        self.assertEqual(block["level"], 2)
        self.assertEqual(block["bbox"], [10, 20, 30, 40])
        self.assertIs(block["mergePrev"], False)
        self.assertEqual(block["text"], "## 标题")

    def test_bbox_none_when_missing(self):
        doc = PP.build_document("<BLOCK>text<FORK>裸正文")
        block = _blocks_of(doc)[0]
        self.assertEqual(block["type"], "text")
        self.assertIsNone(block["bbox"])
        self.assertIsNone(block["level"])

    def test_raw_paragraph_becomes_text(self):
        doc = PP.build_document("裸段落[0,0,9,9]")
        block = _blocks_of(doc)[0]
        self.assertEqual(block["type"], "text")
        self.assertEqual(block["label"], "__raw__")
        self.assertIn("裸段落", block["text"])

    def test_merge_prev_flag(self):
        raw = blk("text", "左栏") + "<MERGE>" + blk("text", "右栏")
        doc = PP.build_document(raw)
        blocks = _blocks_of(doc)
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0]["mergePrev"])
        self.assertEqual(blocks[0]["text"], "左栏右栏")

    def test_page_split_and_page_base(self):
        raw = blk("text", "第一页") + "<PAGE>" + blk("text", "第二页")
        doc = PP.build_document(raw)
        self.assertEqual([p["page"] for p in doc["pages"]], [1, 2])
        doc7 = PP.build_document(raw, page_base=7)
        self.assertEqual([p["page"] for p in doc7["pages"]], [7, 8])
        self.assertEqual(_blocks_of(doc7)[0]["text"], "第一页")

    def test_page_base_invalid_falls_back(self):
        raw = blk("text", "正文")
        self.assertEqual(PP.build_document(raw, page_base="x")["pages"][0]["page"], 1)
        self.assertEqual(PP.build_document(raw, page_base=0)["pages"][0]["page"], 1)

    def test_drop_headers_excludes_three_labels(self):
        raw = (blk("header", "页眉") + blk("text", "正文")
               + blk("footer", "页脚") + blk("page_number", "- 3 -")
               + blk("title", "标题", level="<LEVEL1>"))
        keep = _blocks_of(PP.build_document(raw))
        self.assertEqual([b["type"] for b in keep],
                         ["header", "text", "footer", "page_number", "title"])
        dropped = _blocks_of(PP.build_document(raw, drop_headers=True))
        self.assertEqual([b["type"] for b in dropped], ["text", "title"])
        for block in dropped:
            self.assertNotIn(block["text"], ("页眉", "页脚", "- 3 -"))

    def test_empty_page_omitted_after_drop(self):
        raw = (blk("header", "只有页眉") + "<PAGE>" + blk("text", "第二页正文"))
        doc = PP.build_document(raw, drop_headers=True)
        self.assertEqual([p["page"] for p in doc["pages"]], [2])

    def test_reading_order_matches_markdown(self):
        raw = (blk("title", "一级标题", level="<LEVEL1>")
               + blk("text", "正文甲") + blk("header", "页眉")
               + blk("table", "<table><tr><td>格</td></tr></table>")
               + blk("footer", "页脚") + blk("text", "正文乙"))
        doc = PP.build_document(raw, drop_headers=True)
        md = PP.convert(raw, drop_headers=True)
        pos = -1
        for block in _blocks_of(doc):
            idx = md.find(block["text"], pos + 1)
            self.assertGreater(idx, pos,
                               f"块顺序与 Markdown 不一致：{block['text']!r}")
            pos = idx
        self.assertEqual(md, PP.convert(raw, drop_headers=True))  # 纯函数

    def test_table_and_image_types(self):
        raw = (blk("cross_table", "<table><tr><td>a</td></tr></table>")
               + blk("append_table", "<table><tr><td>b</td></tr></table>")
               + blk("image", "![](page1_0_0_10_10.jpg)")
               + blk("equation", "E=mc^2"))
        blocks = _blocks_of(PP.build_document(raw))
        # cross_/append_ 链按阅读序并成一个渲染块（与 Markdown 渲染一致）
        self.assertEqual([b["type"] for b in blocks],
                         ["table", "image", "equation"])
        self.assertIn("<td>a</td>", blocks[0]["text"])
        self.assertIn("<td>b</td>", blocks[0]["text"])

    def test_empty_and_none_input(self):
        self.assertEqual(PP.build_document(""), {"pages": []})
        self.assertEqual(PP.build_document(None), {"pages": []})
        self.assertEqual(PP.build_document("   "), {"pages": []})

    def test_malformed_input_degrades_to_single_block(self):
        doc = PP.build_document("乱码<BLOCK>没有标签<FORK>残留")
        self.assertEqual(len(doc["pages"]), 1)
        self.assertEqual(doc["pages"][0]["page"], 1)
        blocks = doc["pages"][0]["blocks"]
        self.assertTrue(blocks)
        self.assertIsInstance(blocks[0]["text"], str)

    def test_output_is_stable_across_calls(self):
        raw = blk("text", "正文") + "<PAGE>" + blk("title", "标题", level="<LEVEL3>")
        self.assertEqual(PP.build_document(raw), PP.build_document(raw))


# ── 引擎侧：result["blocks"] ────────────────────────────────────
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


class _BlocksModel:
    """每页一个 <PAGE> + 可配置的块内容；记录调用。"""

    def __init__(self, page_blocks=None):
        self.calls = []
        self.page_blocks = page_blocks     # callable(stem, idx) -> raw text

    def infer(self, tokenizer, **kwargs):
        self.calls.append(kwargs)
        stems = [Path(p).stem for p in kwargs.get("image_file") or []]
        chunks = []
        for idx, stem in enumerate(stems):
            if self.page_blocks is not None:
                chunks.append(self.page_blocks(stem, idx))
            else:
                chunks.append(blk("text", stem))
        text = "".join("<PAGE>" + c for c in chunks) if chunks else "<PAGE>"
        return {"text": text, "token_ids": [1] * 3, "prompt_token_ids": [0],
                "finish_reason": "stop", "num_pages_out": len(stems),
                "expected_pages": len(stems), "time": 1.0, "prefill_time": 0.5}


class EngineBlocksTest(unittest.TestCase):
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
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_blocks_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def tearDown(self):
        if self._had_torch and self._old_torch is not None:
            sys.modules["torch"] = self._old_torch
        else:
            sys.modules.pop("torch", None)
        ocr_engine._ORIG_TENSOR_CUDA = self._old_orig_cuda
        applog.log_ocr = self._old_log
        ocr_engine._STREAM_HOOK.update(cancel_event=None, state=None)

    def _engine(self, model):
        eng = OcrEngine(self.tmp / "model")
        eng.ready = True
        eng.device = "cpu"
        eng.tokenizer = object()
        eng.model = model
        return eng

    def _paths(self, n):
        return [self.tmp / f"p{i}.png" for i in range(1, n + 1)]

    def test_blocks_pages_continuous_group_of_one(self):
        model = _BlocksModel()
        out = self._engine(model).recognize(self._paths(3), max_merged_pages=1)
        self.assertEqual(len(model.calls), 3)
        self.assertEqual([p["page"] for p in out["blocks"]], [1, 2, 3])
        for page in out["blocks"]:
            self.assertEqual(len(page["blocks"]), 1)
            self.assertEqual(page["blocks"][0]["type"], "text")
            self.assertEqual(page["blocks"][0]["label"], "text")

    def test_blocks_pages_continuous_groups_of_two(self):
        out = self._engine(_BlocksModel()).recognize(self._paths(5),
                                                     max_merged_pages=2)
        self.assertEqual([p["page"] for p in out["blocks"]], [1, 2, 3, 4, 5])
        self.assertEqual(out["groups"], 3)

    def test_blocks_single_request_multipage(self):
        out = self._engine(_BlocksModel()).recognize(self._paths(2))
        self.assertEqual(out["groups"], 1)
        self.assertEqual([p["page"] for p in out["blocks"]], [1, 2])
        self.assertEqual(out["pages"], ["p1", "p2"])

    def test_blocks_respect_drop_headers(self):
        def page_blocks(stem, idx):
            return (blk("header", f"页眉{idx}") + blk("text", f"正文{stem}")
                    + blk("page_number", "- 1 -"))
        out = self._engine(_BlocksModel(page_blocks=page_blocks)).recognize(
            self._paths(2), drop_headers=True)
        types = [b["type"] for p in out["blocks"] for b in p["blocks"]]
        self.assertEqual(types, ["text", "text"])
        self.assertGreater(out["droppedBlocks"], 0)
        for page in out["blocks"]:
            for block in page["blocks"]:
                self.assertIn("正文", block["text"])

    def test_existing_fields_not_regressed(self):
        out = self._engine(_BlocksModel()).recognize(self._paths(2))
        for key in ("text", "pages", "elapsed", "groups", "droppedBlocks",
                    "tableWarnings", "effectiveMaxNewTokens", "blocks"):
            self.assertIn(key, out)
        self.assertEqual(out["pages"], ["p1", "p2"])
        self.assertIn("<!-- page 1 -->", out["text"])

    def test_blocks_unaffected_by_text_switches(self):
        out = self._engine(_BlocksModel()).recognize(
            self._paths(2), join_lines=True, cjk_spacing=True)
        self.assertEqual([p["page"] for p in out["blocks"]], [1, 2])
        self.assertTrue(all(b["text"] for p in out["blocks"]
                            for b in p["blocks"]))

    def test_fewer_page_markers_do_not_collide_across_groups(self):
        """模型少发 <PAGE> 时，组间页号仍递增且不重复（允许中间留空号）。"""
        class _Fixed(_BlocksModel):
            def infer(self, tokenizer, **kwargs):
                self.calls.append(kwargs)
                return {"text": "<PAGE>" + blk("text", "只有一页"),
                        "token_ids": [1], "finish_reason": "stop",
                        "num_pages_out": 1, "expected_pages": 1,
                        "time": 1.0, "prefill_time": 0.5}

        out = self._engine(_Fixed()).recognize(self._paths(4),
                                               max_merged_pages=2)
        pages = [p["page"] for p in out["blocks"]]
        self.assertEqual(pages, [1, 3])          # 每组实际只出 1 页
        self.assertEqual(pages, sorted(set(pages)))

    def test_all_dropped_page_yields_no_blocks(self):
        def only_header(stem, idx):
            return blk("header", f"页眉{idx}")
        out = self._engine(_BlocksModel(page_blocks=only_header)).recognize(
            self._paths(1), drop_headers=True)
        self.assertEqual(out["blocks"], [])


if __name__ == "__main__":
    unittest.main()

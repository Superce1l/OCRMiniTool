# -*- coding: utf-8 -*-
"""tests/test_json_export.py — E2：blocks 透传 + JSON 落盘 + outFormat=json + CLI schema

契约（task-23）：
  • 识别响应 payload / 批量 item 透传 `blocks`（引擎给了才带）；
  • `_save_output(..., "json")` 写
    {"version": 1, "name": ..., "pages": [{"page": n, "blocks": [...]}]}
    且原子写（无 .tmp 残留）、同名递增、名字净化；
  • 设置 outFormat ∈ {md, txt, json}，非法值 `_badValue` 且整批不写盘；
  • CLI `_json_item` 含 `blocks`（缺省 []），`--format json` 与 `--json` 同 schema。

全部用临时目录；引擎用桩，不加载模型。
"""
import contextlib
import io
import json
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


def _block(text="正文", typ="paragraph", bbox=(0, 0, 100, 20), page=1,
           **extra):
    b = {"type": typ, "label": typ, "level": 0,
         "bbox": list(bbox), "text": text, "mergePrev": False,
         "page": page}
    b.update(extra)
    return b


class _BackendCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_json_"))
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

    def tearDown(self):
        applog._FH = None
        for p in self._patches:
            p.stop()
        _rmtree(self.tmp)

    def saved(self) -> dict:
        return json.loads(self.settings_file.read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════
# A) 响应 / 批量 item 透传 blocks
# ══════════════════════════════════════════════════════════
class BlocksPayloadTest(_BackendCase):
    def setUp(self):
        super().setUp()
        self.backend.engine.ready = True
        self.img = self.tmp / "p1.png"
        self.img.write_bytes(b"PNG")

    def _opts(self, mode="merged"):
        return {"paths": [str(self.img)], "names": ["p1.png"],
                "name": "p1", "prompt": "", "mode": mode}

    def test_merged_payload_carries_blocks(self):
        blocks = [[_block("第一段")]]
        with mock.patch.object(self.backend.engine, "recognize",
                               side_effect=lambda *a, **k: {
                                   "text": "第一段", "pages": ["第一段"],
                                   "elapsed": 0.1, "blocks": blocks}):
            res = self.backend.recognize(self._opts())
        self.assertEqual(res["blocks"], blocks)
        self.assertEqual(res["blocks"][0][0]["bbox"], [0, 0, 100, 20])

    def test_merged_payload_passes_engine_envelopes(self):
        """引擎真实形态是页信封 [{page, blocks}]，响应应原样透传（不改写）。"""
        env = [{"page": 1, "blocks": [_block("第一段")]}]
        with mock.patch.object(self.backend.engine, "recognize",
                               side_effect=lambda *a, **k: {
                                   "text": "第一段", "pages": ["第一段"],
                                   "elapsed": 0.1, "blocks": env}):
            res = self.backend.recognize(self._opts())
        self.assertEqual(res["blocks"], env)
        self.assertEqual(res["blocks"][0]["page"], 1)

    def test_merged_payload_omits_blocks_when_engine_lacks_them(self):
        with mock.patch.object(self.backend.engine, "recognize",
                               side_effect=lambda *a, **k: {
                                   "text": "t", "pages": ["t"], "elapsed": 0.0}):
            res = self.backend.recognize(self._opts())
        self.assertNotIn("blocks", res, "引擎没给就不该凭空造")

    def test_batch_item_carries_blocks(self):
        blocks = [[_block("批量段")]]
        with mock.patch.object(self.backend.engine, "recognize",
                               side_effect=lambda *a, **k: {
                                   "text": "批量段", "pages": ["批量段"],
                                   "elapsed": 0.0, "blocks": blocks}):
            res = self.backend.recognize(self._opts(mode="batch"))
        self.assertEqual(res["items"][0]["blocks"], blocks)

    def test_batch_item_omits_blocks_when_absent(self):
        with mock.patch.object(self.backend.engine, "recognize",
                               side_effect=lambda *a, **k: {
                                   "text": "t", "pages": ["t"], "elapsed": 0.0}):
            res = self.backend.recognize(self._opts(mode="batch"))
        self.assertNotIn("blocks", res["items"][0])


# ══════════════════════════════════════════════════════════
# B) _save_output(..., "json")
# ══════════════════════════════════════════════════════════
class SaveOutputJsonTest(_BackendCase):
    def _result(self, pages=("第一页", "第二页")):
        return {"text": "\n\n".join(pages), "pages": list(pages),
                "elapsed": 0.2,
                "blocks": [[_block("第一页块", page=1)],
                           [_block("第二页块", page=2, bbox=(1, 2, 30, 40))]]}

    def test_json_structure(self):
        out = self.backend._save_output("报告", self._result(), "json")
        self.assertEqual(out.suffix, ".json")
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["name"], "报告")
        self.assertEqual([p["page"] for p in data["pages"]], [1, 2])
        blk = data["pages"][0]["blocks"][0]
        for key in ("type", "label", "level", "bbox", "text", "mergePrev"):
            self.assertIn(key, blk, f"块字段 {key} 应原样透传")
        self.assertEqual(data["pages"][1]["blocks"][0]["bbox"],
                         [1, 2, 30, 40])

    def test_json_without_blocks_has_empty_block_lists(self):
        out = self.backend._save_output(
            "无块", {"pages": ["a", "b"], "elapsed": 0.0}, "json")
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["pages"],
                         [{"page": 1, "blocks": []}, {"page": 2, "blocks": []}])

    def test_flat_single_page_blocks_are_normalized(self):
        out = self.backend._save_output(
            "扁平", {"pages": ["a"], "blocks": [_block("x")]}, "json")
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(data["pages"]), 1)
        self.assertEqual(data["pages"][0]["blocks"][0]["text"], "x")

    def test_atomic_write_leaves_no_tmp(self):
        self.backend._save_output("原子", self._result(), "json")
        self.assertEqual(list(self.out.glob("*.tmp")), [])
        self.assertEqual(list(self.out.glob("*.json.tmp")), [])

    def test_engine_envelope_shape_preserves_page_numbers(self):
        """引擎真实形态：blocks=[{page, blocks}]（页号来自引擎，不按下标重编）。"""
        result = {"pages": ["一"],
                  "blocks": [{"page": 3, "blocks": [_block("第三页块", page=3)]}]}
        out = self.backend._save_output("信封", result, "json")
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual([p["page"] for p in data["pages"]], [3],
                         "引擎页信封的页号应原样保留")
        self.assertEqual(data["pages"][0]["blocks"][0]["text"], "第三页块")

    def test_engine_envelopes_not_padded_by_string_pages(self):
        """整页块被 drop_headers 清空时引擎不返回该页 → JSON 也不该补空页。"""
        result = {"pages": ["a", "b"],
                  "blocks": [{"page": 1, "blocks": [_block("仅第一页")]}]}
        out = self.backend._save_output("丢页", result, "json")
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(data["pages"]), 1)

    def test_same_name_increments(self):
        p1 = self.backend._save_output("重名", self._result(), "json")
        p2 = self.backend._save_output("重名", self._result(), "json")
        self.assertEqual(p1.name, "重名.json")
        self.assertEqual(p2.name, "重名 (1).json")
        self.assertTrue(p2.is_file())

    def test_name_is_sanitized(self):
        out = self.backend._save_output("../逃逸", self._result(), "json")
        self.assertEqual(out.parent, self.out)
        self.assertNotIn("..", out.name)
        self.assertTrue(out.is_file())

    def test_failed_write_cleans_tmp_and_leaves_no_file(self):
        orig = Path.write_text

        def boom(self, *a, **k):
            if self.name.endswith(".tmp"):
                orig(self, "半截", encoding="utf-8")
                raise OSError("disk full (simulated)")
            return orig(self, *a, **k)

        target = self.out / "崩.json"
        with mock.patch.object(Path, "write_text", boom):
            with self.assertRaises(OSError):
                self.backend._save_output("崩", self._result(), "json")
        self.assertFalse(target.exists())
        self.assertEqual(list(self.out.glob("*.tmp")), [])

    def test_md_txt_unchanged(self):
        md = self.backend._save_output("md测试", self._result(), "md")
        txt = self.backend._save_output("txt测试", self._result(), "txt")
        self.assertEqual(md.suffix, ".md")
        self.assertEqual(txt.suffix, ".txt")
        self.assertIn("# md测试", md.read_text(encoding="utf-8"))

    def test_unknown_fmt_falls_back_to_md(self):
        out = self.backend._save_output("旧值", self._result(), "weird")
        self.assertEqual(out.suffix, ".md", "历史脏值按 md 落盘，不抛")


class JsonPagesUnitTest(unittest.TestCase):
    """`_json_pages` 的四种输入形态（引擎信封 / 每页列表 / 单页扁平 / 缺省）。"""

    def test_engine_envelopes(self):
        pages = wb.WebBackend._json_pages(
            {"blocks": [{"page": 2, "blocks": [_block("b")]}]})
        self.assertEqual(pages, [{"page": 2, "blocks": [_block("b")]}])

    def test_list_of_block_lists(self):
        pages = wb.WebBackend._json_pages(
            {"blocks": [[_block("a")], [_block("b")]]})
        self.assertEqual([p["page"] for p in pages], [1, 2])
        self.assertEqual(pages[1]["blocks"][0]["text"], "b")

    def test_flat_single_page(self):
        pages = wb.WebBackend._json_pages({"blocks": [_block("x")]})
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["page"], 1)

    def test_missing_blocks_uses_page_count(self):
        pages = wb.WebBackend._json_pages({"pages": ["a", "b"]}, page_count=2)
        self.assertEqual(pages, [{"page": 1, "blocks": []},
                                 {"page": 2, "blocks": []}])

    def test_bad_shape_never_raises(self):
        for bad in (None, "x", {"a": 1}, 42):
            self.assertEqual(
                wb.WebBackend._json_pages({"blocks": bad}, page_count=0), [])


# ══════════════════════════════════════════════════════════
# C) outFormat 设置
# ══════════════════════════════════════════════════════════
class OutFormatSettingTest(_BackendCase):
    def test_default_is_md(self):
        self.assertEqual(self.backend.get_settings()["outFormat"], "md")

    def test_json_accepted_and_persisted(self):
        res = self.backend.set_settings({"outFormat": "json"})
        self.assertEqual(res["outFormat"], "json")
        self.assertEqual(self.saved()["outFormat"], "json")

    def test_case_normalized(self):
        res = self.backend.set_settings({"outFormat": "JSON"})
        self.assertEqual(res["outFormat"], "json")

    def test_invalid_rejected_and_not_written(self):
        before = self.settings_file.read_bytes()
        res = self.backend.set_settings({"outFormat": "pdf"})
        self.assertIs(res.get("ok"), False)
        self.assertIn("outFormat", res["error"])
        self.assertEqual(self.settings_file.read_bytes(), before)

    def test_mixed_patch_with_bad_format_writes_nothing(self):
        res = self.backend.set_settings({"theme": "dark",
                                         "outFormat": "xml"})
        self.assertIs(res.get("ok"), False)
        self.assertEqual(self.saved().get("theme", "light"), "light")

    def test_outformat_json_actually_used_by_recognize(self):
        self.backend.engine.ready = True
        img = self.tmp / "p1.png"
        img.write_bytes(b"PNG")
        self.backend.set_settings({"outFormat": "json"})
        with mock.patch.object(self.backend.engine, "recognize",
                               side_effect=lambda *a, **k: {
                                   "text": "t", "pages": ["t"], "elapsed": 0.0,
                                   "blocks": [[_block("t")]]}):
            res = self.backend.recognize({"paths": [str(img)], "name": "导出"})
        out = Path(res["outPath"])
        self.assertEqual(out.suffix, ".json")
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(data["pages"][0]["blocks"][0]["text"], "t")


# ══════════════════════════════════════════════════════════
# D) CLI：_json_item + --format json / --json 同 schema
# ══════════════════════════════════════════════════════════
class _StubEngine:
    """最小桩：按引擎 E2 的真实形态返回（pages + 页信封 blocks），不加载模型。"""

    def __init__(self, blocks=None, pages=("cli块",)):
        self.ready = True
        self.cancel_event = None
        self.pages = list(pages)
        self._blocks = blocks if blocks is not None else [[_block("cli块")]]
        self.calls = []

    def load(self, device_pref="auto", cb=None):
        return None

    def recognize(self, image_paths, prompt="", **kwargs):
        self.calls.append(image_paths)
        envelopes = [{"page": i + 1, "blocks": list(p)}
                     for i, p in enumerate(self._blocks)]
        return {"text": "\n\n".join(self.pages), "pages": list(self.pages),
                "elapsed": 0.1, "truncated": False, "pagesMismatch": False,
                "groups": 1, "blocks": envelopes}


class _NoBlocksEngine(_StubEngine):
    """旧引擎：没有 blocks 字段。"""

    def recognize(self, image_paths, prompt="", **kwargs):
        res = super().recognize(image_paths, prompt=prompt, **kwargs)
        res.pop("blocks", None)
        return res


class CliJsonItemTest(unittest.TestCase):
    def test_blocks_included(self):
        item = cli._json_item({"pages": [{"page": 1, "markdown": "x"}],
                               "blocks": [[_block("a")]]})
        self.assertEqual(item["blocks"],
                         [{"page": 1, "blocks": [_block("a")]}],
                         "CLI 的 blocks 与 HTTP/GUI 同构：页信封数组")

    def test_envelope_keeps_original_page_numbers(self):
        """引擎页信封（--pages 时页号可能不是 1..n）必须保留原页号。"""
        env = [{"page": 3, "blocks": [_block("第三页")]}]
        item = cli._json_item({"pages": [], "blocks": env})
        self.assertEqual(item["blocks"][0]["page"], 3)
        self.assertEqual(item["blocks"][0]["blocks"][0]["text"], "第三页")

    def test_missing_blocks_default_empty(self):
        item = cli._json_item({"pages": []})
        self.assertEqual(item["blocks"], [])

    def test_non_list_blocks_default_empty(self):
        item = cli._json_item({"pages": [], "blocks": {"oops": 1}})
        self.assertEqual(item["blocks"], [])


class CliJsonEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_clijson_"))
        self.png = self.tmp / "a.png"
        self.png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        self._patch = mock.patch.object(applog, "_DIR", self.tmp / "logs")
        self._patch.start()
        applog._FH = None
        applog._CURRENT_MONTH = ""

    def tearDown(self):
        applog._FH = None
        self._patch.stop()
        _rmtree(self.tmp)

    def _run(self, argv, engine=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv, engine_factory=lambda: engine or _StubEngine())
        return rc, out.getvalue(), err.getvalue()

    def test_json_pipe_has_blocks(self):
        rc, stdout, _ = self._run(["--input", str(self.png), "--json"])
        self.assertEqual(rc, cli.EXIT_OK)
        payload = json.loads(stdout)
        item = payload["results"][0]
        self.assertIn("blocks", item)
        self.assertEqual(item["blocks"][0]["blocks"][0]["text"], "cli块")

    def test_format_json_same_schema_as_pipe(self):
        out_dir = self.tmp / "out"
        rc, stdout, _ = self._run(["--input", str(self.png),
                                   "--format", "json", "--out", str(out_dir)])
        self.assertEqual(rc, cli.EXIT_OK)
        file_data = json.loads((out_dir / "a.json").read_text(encoding="utf-8"))
        rc2, stdout2, _ = self._run(["--input", str(self.png), "--json"])
        pipe_item = json.loads(stdout2)["results"][0]
        self.assertEqual(set(file_data), set(pipe_item),
                         "--format json 与 --json 必须同 schema")
        self.assertEqual(file_data["blocks"], pipe_item["blocks"])
        self.assertEqual(file_data["pages"], pipe_item["pages"])

    def test_pages_carry_blocks_per_page(self):
        rc, stdout, _ = self._run(["--input", str(self.png), "--json"])
        item = json.loads(stdout)["results"][0]
        self.assertIn("blocks", item["pages"][0],
                      "pages→blocks（与 GUI 的 .json 同构）")
        self.assertEqual(item["pages"][0]["blocks"][0]["text"], "cli块")

    def test_old_engine_without_blocks_yields_empty_list(self):
        rc, stdout, _ = self._run(["--input", str(self.png), "--json"],
                                  engine=_NoBlocksEngine())
        self.assertEqual(rc, cli.EXIT_OK)
        item = json.loads(stdout)["results"][0]
        self.assertEqual(item["blocks"], [], "旧引擎没 blocks → []（结构稳定）")
        self.assertEqual(item["pages"][0]["blocks"], [])


if __name__ == "__main__":
    unittest.main()

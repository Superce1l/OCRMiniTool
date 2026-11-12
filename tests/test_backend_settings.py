# -*- coding: utf-8 -*-
"""tests/test_backend_settings.py — settings 四键契约 + recognize 限幅接线

覆盖 task-11 契约：
  • 四键默认值（maxPageTokens=4096 / maxMergedPages=20 / cropMode=auto /
    dropHeaders=False）与 get/set 往返；
  • 非法值被拒且**一个键都不写盘**（原子语义），返回 {ok:false,error}；
  • 整型接受数字字符串（"4096"），布尔接受 0/1；
  • recognize 接线：引擎新签名全传、旧签名自动裁剪（签名探测 + TypeError 回退）；
  • 结果透传：merged 响应与批量 item 带 truncated/pagesMismatch 等中性字段。

全部用临时目录，不碰项目 settings.json / outputs/。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import applog  # noqa: E402
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


class _BackendCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_set_"))
        self.out = self.tmp / "outputs"
        self.out.mkdir()
        self.settings_file = self.tmp / "settings.json"
        self.settings_file.write_text(
            json.dumps({"lang": "zh", "theme": "light"}), encoding="utf-8")
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


class DefaultsTest(_BackendCase):
    def test_four_keys_defaults(self):
        s = self.backend.get_settings()
        self.assertEqual(s["maxPageTokens"], 4096)
        self.assertEqual(s["maxMergedPages"], 20)
        self.assertEqual(s["cropMode"], "auto")
        self.assertIs(s["dropHeaders"], False)

    def test_defaults_appear_in_raw_settings(self):
        for key in ("maxPageTokens", "maxMergedPages", "cropMode",
                    "dropHeaders"):
            self.assertIn(key, wb.DEFAULT_SETTINGS)


class ValidSetTest(_BackendCase):
    def test_set_and_persist(self):
        res = self.backend.set_settings({
            "maxPageTokens": 8192, "maxMergedPages": 40,
            "cropMode": "gundam", "dropHeaders": True})
        self.assertNotIn("ok", res)          # 成功：返回完整设置（旧形状）
        self.assertEqual(res["maxPageTokens"], 8192)
        self.assertEqual(res["maxMergedPages"], 40)
        self.assertEqual(res["cropMode"], "gundam")
        self.assertIs(res["dropHeaders"], True)
        saved = self.saved()
        self.assertEqual(saved["maxPageTokens"], 8192)
        self.assertEqual(saved["cropMode"], "gundam")
        self.assertIs(saved["dropHeaders"], True)

    def test_numeric_strings_accepted(self):
        self.backend.set_settings({"maxPageTokens": "4096",
                                   "maxMergedPages": " 20 "})
        saved = self.saved()
        self.assertEqual(saved["maxPageTokens"], 4096)
        self.assertIsInstance(saved["maxPageTokens"], int)
        self.assertEqual(saved["maxMergedPages"], 20)
        self.assertIsInstance(saved["maxMergedPages"], int)

    def test_zero_means_unlimited(self):
        self.backend.set_settings({"maxPageTokens": 0, "maxMergedPages": 0})
        saved = self.saved()
        self.assertEqual(saved["maxPageTokens"], 0)
        self.assertEqual(saved["maxMergedPages"], 0)

    def test_boundaries_accepted(self):
        for v in (256, 65536):
            self.backend.set_settings({"maxPageTokens": v})
            self.assertEqual(self.saved()["maxPageTokens"], v)
        for v in (1, 200):
            self.backend.set_settings({"maxMergedPages": v})
            self.assertEqual(self.saved()["maxMergedPages"], v)

    def test_crop_mode_is_case_insensitive(self):
        self.backend.set_settings({"cropMode": "BASE"})
        self.assertEqual(self.saved()["cropMode"], "base")

    def test_drop_headers_accepts_0_1(self):
        self.backend.set_settings({"dropHeaders": 1})
        self.assertIs(self.saved()["dropHeaders"], True)
        self.backend.set_settings({"dropHeaders": 0})
        self.assertIs(self.saved()["dropHeaders"], False)


class InvalidSetTest(_BackendCase):
    def _assert_rejected(self, patch: dict, key: str):
        before = self.settings_file.read_bytes()
        res = self.backend.set_settings(patch)
        self.assertIs(res.get("ok"), False, f"{patch} 应被拒绝")
        self.assertIn(key, res.get("error", ""), res.get("error"))
        self.assertEqual(self.settings_file.read_bytes(), before,
                         f"{patch} 校验失败时不得写盘")
        return res

    def test_max_page_tokens_out_of_range(self):
        for bad in (255, 65537, -1, "abc", None, True, "", "4096x"):
            self._assert_rejected({"maxPageTokens": bad}, "maxPageTokens")

    def test_max_merged_pages_out_of_range(self):
        for bad in (-1, 201, 1.5, "x", None, False):
            self._assert_rejected({"maxMergedPages": bad}, "maxMergedPages")

    def test_crop_mode_invalid(self):
        for bad in ("", "auto2", 123, None, "GAN"):
            self._assert_rejected({"cropMode": bad}, "cropMode")

    def test_drop_headers_invalid(self):
        for bad in ("yes", "true", 2, None, 1.5):
            self._assert_rejected({"dropHeaders": bad}, "dropHeaders")

    def test_valid_and_invalid_in_one_patch_writes_nothing(self):
        before = self.settings_file.read_bytes()
        res = self.backend.set_settings(
            {"theme": "dark", "maxPageTokens": 4096, "cropMode": "bad"})
        self.assertIs(res.get("ok"), False)
        self.assertEqual(self.settings_file.read_bytes(), before,
                         "混合补丁里有一个非法键 → 合法键也不落盘")
        self.assertEqual(self.saved()["theme"], "light",
                         "同一补丁里的合法键必须一并放弃（不能半套落盘）")

    def test_error_message_uses_i18n_key(self):
        res = self.backend.set_settings({"maxPageTokens": 999999})
        self.assertIn("maxPageTokens", res["error"])
        self.assertIn("999999", res["error"])

    def test_unknown_keys_still_filtered(self):
        res = self.backend.set_settings({"noSuchKey": 1, "theme": "dark"})
        self.assertEqual(res.get("theme"), "dark")
        self.assertNotIn("noSuchKey", self.saved())


class NormalizePatchUnitTest(unittest.TestCase):
    """纯函数层（不起 backend，不需要临时设置文件）。"""

    def test_empty_patch_no_error(self):
        out, bad = wb.normalize_settings_patch({})
        self.assertEqual(out, {})
        self.assertIsNone(bad)

    def test_other_keys_pass_through(self):
        out, bad = wb.normalize_settings_patch({"theme": "dark"})
        self.assertEqual(out, {"theme": "dark"})
        self.assertIsNone(bad)

    def test_coercion(self):
        out, bad = wb.normalize_settings_patch(
            {"maxPageTokens": "512", "maxMergedPages": "3",
             "cropMode": " Gundam ", "dropHeaders": 1})
        self.assertIsNone(bad)
        self.assertEqual(out["maxPageTokens"], 512)
        self.assertEqual(out["maxMergedPages"], 3)
        self.assertEqual(out["cropMode"], "gundam")
        self.assertIs(out["dropHeaders"], True)

    def test_bool_is_not_int(self):
        _, bad = wb.normalize_settings_patch({"maxPageTokens": True})
        self.assertIsNotNone(bad)


# ══════════════════ recognize 接线 ══════════════════
class EngineWiringTest(_BackendCase):
    def setUp(self):
        super().setUp()
        self.backend.engine.ready = True
        self.img = self.tmp / "p1.png"
        self.img.write_bytes(b"PNG")

    def _opts(self, **extra):
        opts = {"paths": [str(self.img)], "names": ["p1.png"],
                "name": "p1", "prompt": "", "mode": "merged"}
        opts.update(extra)
        return opts

    def test_old_engine_signature_via_introspection(self):
        """旧引擎（没有新参数）→ 按签名裁剪，不多传、不炸。"""
        calls = []

        def old_recognize(image_paths, prompt="", progress_cb=None,
                          cancel_event=None, max_new_tokens=None,
                          crop_mode=None, dpi=None):
            calls.append({"crop_mode": crop_mode})
            return {"text": "正文", "pages": ["正文"], "elapsed": 0.1,
                    "truncated": True, "pagesMismatch": False,
                    "finishReason": "length"}

        with mock.patch.object(self.backend.engine, "recognize",
                               new=old_recognize):
            res = self.backend.recognize(self._opts())
        self.assertEqual(res["mode"], "merged")
        self.assertEqual(res["text"], "正文")
        self.assertTrue(res["outPath"].endswith("p1.md"))
        self.assertIs(res["truncated"], True)
        self.assertIs(res["pagesMismatch"], False)
        self.assertEqual(res["finishReason"], "length")
        self.assertEqual(len(calls), 1)

    def test_typeerror_fallback_when_signature_unknown(self):
        """引擎签名探测不出（*args/**kwargs 包装）→ TypeError 自动回退旧参数。"""
        seen = {}

        def old_recognize(image_paths, prompt="", progress_cb=None,
                          cancel_event=None, max_new_tokens=None,
                          crop_mode=None, dpi=None):
            seen["ok"] = True
            return {"text": "正文", "pages": ["正文"], "elapsed": 0.1}

        fake = mock.MagicMock(side_effect=old_recognize)
        with mock.patch.object(self.backend.engine, "recognize", fake):
            res = self.backend.recognize(self._opts())
        self.assertTrue(seen.get("ok"), "TypeError 回退应把旧参数子集传进去")
        self.assertEqual(res["text"], "正文")
        self.assertEqual(fake.call_count, 2)

    def test_new_engine_signature_receives_all_group_kwargs(self):
        seen = {}

        def new_recognize(image_paths, prompt="", progress_cb=None,
                          cancel_event=None, max_new_tokens=None,
                          crop_mode=None, max_page_tokens=None,
                          max_merged_pages=None, drop_headers=None,
                          dpi=None):
            seen.update(max_page_tokens=max_page_tokens,
                        max_merged_pages=max_merged_pages,
                        drop_headers=drop_headers, crop_mode=crop_mode)
            return {"text": "正文", "pages": ["正文"], "elapsed": 0.1,
                    "groups": [{"name": "g", "pages": [1]}],
                    "droppedBlocks": 3}

        # 先改设置，再把引擎换新签名 → 断言四个参数一路传到引擎
        self.backend.set_settings({"maxPageTokens": 2048,
                                   "maxMergedPages": 5,
                                   "cropMode": "gundam",
                                   "dropHeaders": True})
        with mock.patch.object(self.backend.engine, "recognize",
                               new=new_recognize):
            res = self.backend.recognize(self._opts())
        self.assertEqual(seen["max_page_tokens"], 2048)
        self.assertEqual(seen["max_merged_pages"], 5)
        self.assertIs(seen["drop_headers"], True)
        self.assertEqual(seen["crop_mode"], "gundam")
        self.assertEqual(res["groups"], [{"name": "g", "pages": [1]}])
        self.assertEqual(res["droppedBlocks"], 3)

    def test_crop_mode_auto_passes_through_as_auto(self):
        """auto 必须原样透传给引擎，不能映射成 None。

        引擎语义：None = 沿用旧行为（单图 gundam / 多页 base），**不会**触发
        长截图自动降级；字符串 "auto" 才会按 h/w > 3 走 base（CPU 上 2–5×）。
        早前后端把 auto 译成 None，等于让这个开关在真实界面路径上永久失效。
        """
        seen = {}

        def new_recognize(image_paths, prompt="", progress_cb=None,
                          cancel_event=None, max_new_tokens=None,
                          crop_mode=None, max_page_tokens=None,
                          max_merged_pages=None, drop_headers=None,
                          dpi=None):
            seen["crop_mode"] = crop_mode
            return {"text": "t", "pages": ["t"], "elapsed": 0.0}

        with mock.patch.object(self.backend.engine, "recognize",
                               new=new_recognize):
            self.backend.recognize(self._opts())
        self.assertEqual(seen["crop_mode"], "auto",
                         "auto 应原样透传，映射成 None 会让长截图降级失效")

    def test_batch_items_carry_truncation_flags(self):
        def fake_recognize(image_paths, **kwargs):
            return {"text": "t", "pages": ["t"], "elapsed": 0.0,
                    "truncated": True, "pagesMismatch": True}

        with mock.patch.object(self.backend.engine, "recognize",
                               side_effect=fake_recognize):
            res = self.backend.recognize(self._opts(mode="batch"))
        self.assertEqual(res["mode"], "batch")
        self.assertEqual(len(res["items"]), 1)
        item = res["items"][0]
        self.assertIs(item["ok"], True)
        self.assertIs(item["truncated"], True)
        self.assertIs(item["pagesMismatch"], True)

    def test_result_missing_optional_keys_is_fine(self):
        def bare_recognize(image_paths, **kwargs):
            return {"text": "t", "pages": ["t"], "elapsed": 0.0}

        with mock.patch.object(self.backend.engine, "recognize",
                               side_effect=bare_recognize):
            res = self.backend.recognize(self._opts())
        self.assertNotIn("truncated", res, "引擎没给的字段不应凭空造出来")
        self.assertEqual(res["text"], "t")

    def test_real_engine_signature_end_to_end(self):
        """用**真实** OcrEngine.recognize（只桩掉 _infer，无需 torch）验证接线。

        新签名已落地时它会真正消费 max_page_tokens 等参数：maxPageTokens=1024
        应变成单页生成上限 1024，且引擎返回的 truncated/droppedBlocks 透传到
        后端响应。若引擎还没改完（旧签名），本用例跳过——兼容性已由上面的
        旧签名用例覆盖。
        """
        import inspect
        params = inspect.signature(self.backend.engine.recognize).parameters
        if "max_page_tokens" not in params:
            self.skipTest("引擎仍是旧签名（engine-perf 未落地），兼容路径已覆盖")
        self.backend.set_settings({"maxPageTokens": 1024,
                                   "maxMergedPages": 20,
                                   "cropMode": "gundam",
                                   "dropHeaders": True})
        seen = {}

        def fake_infer(images, prompt, crop_mode=None, max_new_tokens=None):
            seen.update(crop_mode=crop_mode, max_new_tokens=max_new_tokens)
            return ("正文第一段", {"outputTokens": 7, "truncated": True,
                                  "pagesMismatch": False,
                                  "finishReason": "length"})

        with mock.patch.object(self.backend.engine, "_infer", new=fake_infer):
            res = self.backend.recognize(self._opts())
        self.assertEqual(seen["max_new_tokens"], 1024,
                         "maxPageTokens 应变成本次请求的生成上限")
        self.assertIs(seen["crop_mode"], True,
                      "cropMode=gundam 经引擎解析后应为动态切片(True)")
        self.assertIs(res["truncated"], True)
        self.assertIn("droppedBlocks", res,
                      "引擎返回的 droppedBlocks 应透传到响应")
        self.assertEqual(res["text"], "正文第一段")


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""tests/test_backend_batch2.py — E7 三键设置 + A9 页范围联动 + C2 busy→409

覆盖 task-20 契约：
  • settings 三键 pdfPageRange / joinLines / cjkSpacing：默认值、往返、校验
    （非法规格/非 bool 拒绝且**整批不写盘**）；
  • `_engine_recognize_kwargs()` 把三键翻译成 page_range / join_lines /
    cjk_spacing；
  • A9 护栏与页范围联动：500 页 PDF 只选 5 页不再被 150 页上限误拦；
    非法规格跳过页范围（交给引擎报 err.badPageRange）；
  • C2：BusyError.status=409 且四个动作入口在忙时抛它；真实 server 上并发
    第二次请求拿到 HTTP 409 + {"error":{"message":...}}，日志记 INFO 不记 ERROR；
  • tableWarnings 在识别响应与批量 items 里透传。

全部用临时目录；server 真起随机端口（同 test_server_smoke 的手法）。
"""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import applog  # noqa: E402
import webview_backend as wb  # noqa: E402
import webview_server as ws  # noqa: E402
from ocr_engine import EngineError  # noqa: E402


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
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_b2_"))
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


# ══════════════════════════════════════════════════════════
# C) settings 三键
# ══════════════════════════════════════════════════════════
class SettingsThreeKeysTest(_BackendCase):
    def test_defaults(self):
        s = self.backend.get_settings()
        self.assertEqual(s["pdfPageRange"], "")
        self.assertIs(s["joinLines"], False)
        self.assertIs(s["cjkSpacing"], False)
        for key in ("pdfPageRange", "joinLines", "cjkSpacing"):
            self.assertIn(key, wb.DEFAULT_SETTINGS)

    def test_set_and_persist(self):
        res = self.backend.set_settings({"pdfPageRange": " 1-3,5 ",
                                         "joinLines": True,
                                         "cjkSpacing": 1})
        self.assertNotIn("ok", res)
        self.assertEqual(res["pdfPageRange"], "1-3,5", "入库前应 strip")
        self.assertIs(res["joinLines"], True)
        self.assertIs(res["cjkSpacing"], True)
        saved = self.saved()
        self.assertEqual(saved["pdfPageRange"], "1-3,5")
        self.assertIs(saved["joinLines"], True)
        self.assertIs(saved["cjkSpacing"], True)

    def test_open_range_is_valid_spec_for_settings(self):
        res = self.backend.set_settings({"pdfPageRange": "10-"})
        self.assertEqual(res.get("pdfPageRange"), "10-",
                         "open range 没有总页数也算语法合法")

    def test_empty_range_allowed(self):
        self.backend.set_settings({"pdfPageRange": "1-3"})
        res = self.backend.set_settings({"pdfPageRange": "   "})
        self.assertEqual(res["pdfPageRange"], "")
        self.assertEqual(self.saved()["pdfPageRange"], "")

    def test_invalid_values_rejected_and_not_written(self):
        cases = [({"pdfPageRange": "abc"}, "pdfPageRange"),
                 ({"pdfPageRange": "3-1"}, "pdfPageRange"),
                 ({"pdfPageRange": "0"}, "pdfPageRange"),
                 ({"joinLines": "yes"}, "joinLines"),
                 ({"joinLines": 2}, "joinLines"),
                 ({"cjkSpacing": None}, "cjkSpacing")]
        for patch, key in cases:
            with self.subTest(patch=patch):
                before = self.settings_file.read_bytes()
                res = self.backend.set_settings(patch)
                self.assertIs(res.get("ok"), False, patch)
                self.assertIn(key, res.get("error", ""))
                self.assertEqual(self.settings_file.read_bytes(), before,
                                 "校验失败不得写盘")

    def test_mixed_patch_with_invalid_writes_nothing(self):
        res = self.backend.set_settings({"theme": "dark",
                                         "joinLines": True,
                                         "pdfPageRange": "bad-range"})
        self.assertIs(res.get("ok"), False)
        self.assertEqual(self.saved()["theme"], "light",
                         "同一补丁里的合法键也要一并放弃")

    def test_normalize_patch_unit(self):
        out, bad = wb.normalize_settings_patch(
            {"pdfPageRange": " 1-2 ", "joinLines": 0, "cjkSpacing": True})
        self.assertIsNone(bad)
        self.assertEqual(out["pdfPageRange"], "1-2")
        self.assertIs(out["joinLines"], False)
        self.assertIs(out["cjkSpacing"], True)


class EngineKwargsTest(_BackendCase):
    def test_defaults_are_neutral(self):
        kw = self.backend._engine_recognize_kwargs()
        self.assertEqual(kw["page_range"], "")
        self.assertIs(kw["join_lines"], False)
        self.assertIs(kw["cjk_spacing"], False)

    def test_follow_settings(self):
        self.backend.set_settings({"pdfPageRange": "2-4",
                                   "joinLines": True,
                                   "cjkSpacing": True})
        kw = self.backend._engine_recognize_kwargs()
        self.assertEqual(kw["page_range"], "2-4")
        self.assertIs(kw["join_lines"], True)
        self.assertIs(kw["cjk_spacing"], True)


# ══════════════════════════════════════════════════════════
# D) A9 护栏与页范围联动
# ══════════════════════════════════════════════════════════
class LimitPageRangeTest(_BackendCase):
    def _pdf(self, name="big.pdf") -> Path:
        p = self.tmp / name
        p.write_bytes(b"%PDF-1.4 fake")
        return p

    def test_selected_page_count_unit(self):
        png = self.tmp / "a.png"
        png.write_bytes(b"x")
        self.assertEqual(wb._selected_page_count(png, "1-5"), 1)
        pdf = self._pdf()
        with mock.patch.object(wb, "_pdf_page_count", return_value=500):
            self.assertEqual(wb._selected_page_count(pdf), 500)
            self.assertEqual(wb._selected_page_count(pdf, "1-5"), 5)
            self.assertEqual(wb._selected_page_count(pdf, "10-"), 491)
            self.assertEqual(wb._selected_page_count(pdf, "abc"), 500,
                             "非法规格跳过页范围（交给引擎报错）")
        with mock.patch.object(wb, "_pdf_page_count", return_value=0):
            self.assertEqual(wb._selected_page_count(pdf, "1-5"), 0,
                             "页数未知 → 不计入（不拦）")

    def test_guard_not_tripped_when_range_selects_few_pages(self):
        pdf = self._pdf()
        with mock.patch.object(wb, "_pdf_page_count", return_value=500):
            with self.assertRaises(wb.RecognizeLimitError):
                wb._check_recognize_limits([pdf])
            wb._check_recognize_limits([pdf], page_range="1-5")   # 不抛
            wb._check_recognize_limits([pdf], batch=True,
                                       page_range="1-5")        # 不抛

    def test_invalid_range_does_not_shield_limit(self):
        """非法规格不改变页数口径：500 页还是超限（引擎会报 badPageRange）。"""
        pdf = self._pdf()
        with mock.patch.object(wb, "_pdf_page_count", return_value=500):
            with self.assertRaises(wb.RecognizeLimitError):
                wb._check_recognize_limits([pdf], page_range="abc")

    def test_recognize_end_to_end_uses_page_range(self):
        pdf = self._pdf()
        self.backend.engine.ready = True
        seen = {}

        def fake_recognize(image_paths, **kwargs):
            seen.update(kwargs)
            return {"text": "t", "pages": ["t"], "elapsed": 0.0}

        with mock.patch.object(wb, "_pdf_page_count", return_value=500), \
                mock.patch.object(self.backend.engine, "recognize",
                                  side_effect=fake_recognize):
            self.backend.set_settings({"pdfPageRange": "1-5"})
            res = self.backend.recognize({"paths": [str(pdf)], "name": "big"})
            self.assertEqual(res["text"], "t")
            self.assertEqual(seen.get("page_range"), "1-5",
                             "页范围应一路传到引擎")

            self.backend.set_settings({"pdfPageRange": ""})
            with self.assertRaises(wb.RecognizeLimitError):
                self.backend.recognize({"paths": [str(pdf)], "name": "big"})


# ══════════════════════════════════════════════════════════
# E) C2 busy → 409
# ══════════════════════════════════════════════════════════
class BusyErrorContractTest(_BackendCase):
    def test_busy_error_shape(self):
        self.assertTrue(issubclass(wb.BusyError, EngineError))
        self.assertEqual(wb.BusyError.status, 409)
        self.assertEqual(wb.BusyError("x").status, 409)

    def test_start_download_raises_busy(self):
        self.backend.loading = True
        try:
            with self.assertRaises(wb.BusyError):
                self.backend.start_download()
            self.assertFalse(self.backend.downloading, "忙拒绝不能置忙")
        finally:
            self.backend.loading = False

    def test_start_load_raises_busy(self):
        self.backend.downloading = True
        try:
            with self.assertRaises(wb.BusyError):
                self.backend.start_load()
            self.assertFalse(self.backend.loading)
        finally:
            self.backend.downloading = False

    def test_start_env_download_raises_busy(self):
        self.backend.recognizing = True
        try:
            with self.assertRaises(wb.BusyError):
                self.backend.start_env_download("cpu")
            self.assertFalse(self.backend.env_downloading)
        finally:
            self.backend.recognizing = False

    def test_recognize_raises_busy(self):
        self.backend.engine.ready = True
        self.backend.loading = True
        try:
            with self.assertRaises(wb.BusyError):
                self.backend.recognize({"paths": [str(self.tmp / "a.png")]})
        finally:
            self.backend.loading = False

    def test_server_status_mapping_helper(self):
        self.assertEqual(ws._exc_status(wb.BusyError("x")), 409)
        self.assertEqual(ws._exc_status(wb.RecognizeLimitError("x")), 400)
        self.assertEqual(ws._exc_status(RuntimeError("x")), 500)

        class Weird(Exception):
            status = "not-a-number"
        self.assertEqual(ws._exc_status(Weird()), 500, "非法 status 回落 500")


# ══════════════════════════════════════════════════════════
# F) tableWarnings 透传
# ══════════════════════════════════════════════════════════
class TableWarningsPassthroughTest(_BackendCase):
    def setUp(self):
        super().setUp()
        self.backend.engine.ready = True
        self.img = self.tmp / "p1.png"
        self.img.write_bytes(b"PNG")

    def _opts(self, mode="merged"):
        return {"paths": [str(self.img)], "names": ["p1.png"],
                "name": "p1", "prompt": "", "mode": mode}

    def test_merged_passthrough(self):
        def fake(image_paths, **kwargs):
            return {"text": "t", "pages": ["t"], "elapsed": 0.0,
                    "tableWarnings": 3}

        with mock.patch.object(self.backend.engine, "recognize",
                               side_effect=fake):
            res = self.backend.recognize(self._opts())
        self.assertEqual(res["tableWarnings"], 3)

    def test_batch_item_passthrough(self):
        def fake(image_paths, **kwargs):
            return {"text": "t", "pages": ["t"], "elapsed": 0.0,
                    "tableWarnings": 2, "truncated": True}

        with mock.patch.object(self.backend.engine, "recognize",
                               side_effect=fake):
            res = self.backend.recognize(self._opts(mode="batch"))
        item = res["items"][0]
        self.assertEqual(item["tableWarnings"], 2)
        self.assertIs(item["truncated"], True)

    def test_missing_field_not_invented(self):
        def fake(image_paths, **kwargs):
            return {"text": "t", "pages": ["t"], "elapsed": 0.0}

        with mock.patch.object(self.backend.engine, "recognize",
                               side_effect=fake):
            res = self.backend.recognize(self._opts())
        self.assertNotIn("tableWarnings", res)


# ══════════════════════════════════════════════════════════
# E) 真实 server：HTTP 409 + 日志 INFO
# ══════════════════════════════════════════════════════════
class ServerBusy409Test(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ocr_409_"))
        cls._patches = [
            mock.patch.object(wb, "SETTINGS_FILE", cls.tmp / "settings.json"),
            mock.patch.object(wb, "OUTPUT_DIR", cls.tmp / "outputs"),
            mock.patch.object(applog, "_DIR", cls.tmp / "logs"),
        ]
        for p in cls._patches:
            p.start()
        applog._FH = None
        applog._CURRENT_MONTH = ""
        (cls.tmp / "outputs").mkdir(exist_ok=True)
        cls.server = ws.WebViewServer(port=0)
        cls.server.start()
        cls.port = cls.server.port
        cls.img = cls.tmp / "p1.png"
        cls.img.write_bytes(b"PNG")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.stop()
        finally:
            applog._FH = None
            for p in cls._patches:
                p.stop()
            _rmtree(cls.tmp)

    def setUp(self):
        self.server.backend.recognizing = True
        self.addCleanup(self._clear_busy)

    def _clear_busy(self):
        self.server.backend.recognizing = False

    def _post(self, path: str, body: dict):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _log_text(self) -> str:
        out = []
        for p in (self.tmp / "logs").glob("*.log"):
            try:
                out.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        return "\n".join(out)

    def test_load_returns_409(self):
        code, data = self._post("/api/load", {})
        self.assertEqual(code, 409)
        self.assertIn("error", data)
        self.assertTrue(data["error"]["message"])
        self.assertNotIn("ok", data, "409 走统一 error 包装")

    def test_download_returns_409(self):
        code, data = self._post("/api/download", {})
        self.assertEqual(code, 409)
        self.assertTrue(data["error"]["message"])

    def test_recognize_returns_409(self):
        code, data = self._post("/api/recognize",
                                {"paths": [str(self.img)]})
        self.assertEqual(code, 409)
        self.assertTrue(data["error"]["message"])

    def test_busy_logged_as_info_not_error(self):
        self._post("/api/load", {})
        time.sleep(0.2)
        text = self._log_text()
        self.assertIn("409", text, "忙拒绝必须留一行日志")
        busy_lines = [ln for ln in text.splitlines() if "409" in ln]
        self.assertTrue(busy_lines, text)
        self.assertTrue(all("[INFO]" in ln for ln in busy_lines),
                        f"409 应记 INFO 不是 ERROR：{busy_lines}")
        self.assertNotIn("[ERROR]", "\n".join(busy_lines))


if __name__ == "__main__":
    unittest.main()

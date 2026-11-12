# -*- coding: utf-8 -*-
"""tests/test_integration_contract.py — 跨域契约测试

各模块自己的测试只保证"内部逻辑对"，本文件保证**前端 / 后端 / 引擎三方在
真实 HTTP 上对得上**。它存在的理由：本次迭代真出现过「引擎实现了 auto、
后端却把 auto 映射成 None」这种各自都绿、合起来失效的缺陷。

覆盖：settings 四键默认值/校验/落盘、scan-dir 边界（含恰好满额不误报截断）、
recognize 路径直读的入参校验、引擎新字段（截断/页数/分组/丢弃块/期望页数）
在响应里的透传。
"""
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import webview_backend as wb  # noqa: E402
import webview_server as ws  # noqa: E402


class ContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ocr_contract_"))
        cls.outputs = cls.tmp / "outputs"
        cls.outputs.mkdir()
        cls.src = cls.tmp / "imgs"
        cls.src.mkdir()
        for name in ("a.png", "b.jpg", "c.pdf", "skip.txt"):
            (cls.src / name).write_bytes(b"x")
        cls._patches = [
            mock.patch.object(wb, "SETTINGS_FILE", cls.tmp / "settings.json"),
            mock.patch.object(wb, "OUTPUT_DIR", cls.outputs),
        ]
        for p in cls._patches:
            p.start()
        cls.server = ws.WebViewServer(port=0)
        cls.server.start()
        cls.base = f"http://127.0.0.1:{cls.server.port}"

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.stop()
        finally:
            for p in cls._patches:
                p.stop()
            for f in sorted(cls.tmp.rglob("*"), reverse=True):
                try:
                    f.unlink() if f.is_file() else f.rmdir()
                except OSError:
                    pass
            try:
                cls.tmp.rmdir()
            except OSError:
                pass

    # ── 辅助 ─────────────────────────────────────────────
    def setUp(self):
        # 每个用例从「无 settings.json」开始：否则前一个用例写进去的
        # maxPageTokens/cropMode 会污染后面的默认值断言（unittest 按方法名排序）
        p = self.tmp / "settings.json"
        try:
            p.unlink()
        except OSError:
            pass

    def post(self, path, obj):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(obj).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"raw": raw[:200].decode("utf-8", "replace")}

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=30) as r:
            return r.status, json.loads(r.read())

    # ── settings 四键 ────────────────────────────────────
    def test_settings_defaults_present(self):
        """前端设置页依赖这四个键存在，缺一个就会渲染出空控件。"""
        _, st = self.get("/api/settings")
        self.assertEqual(st.get("maxPageTokens"), 4096)
        self.assertEqual(st.get("maxMergedPages"), 20)
        self.assertEqual(st.get("cropMode"), "auto")
        self.assertIs(st.get("dropHeaders"), False)

    def test_invalid_value_rejected_and_not_persisted(self):
        _, res = self.post("/api/settings", {"maxPageTokens": 5})
        self.assertIs(res.get("ok"), False)
        saved = self.tmp / "settings.json"
        if saved.exists():
            data = json.loads(saved.read_text(encoding="utf-8"))
            self.assertNotEqual(data.get("maxPageTokens"), 5)

    def test_valid_values_normalized(self):
        self.post("/api/settings", {"maxPageTokens": "2048",
                                    "maxMergedPages": 5,
                                    "cropMode": "GUNDAM",
                                    "dropHeaders": True})
        _, st = self.get("/api/settings")
        self.assertEqual(st.get("maxPageTokens"), 2048, "字符串数字应被接受")
        self.assertEqual(st.get("cropMode"), "gundam", "cropMode 应大小写归一")
        self.assertIs(st.get("dropHeaders"), True)

    def test_crop_mode_auto_reaches_engine_verbatim(self):
        """auto 必须原样到达引擎；映射成 None 会让长截图降级失效。"""
        seen = {}

        def fake(image_paths, **kwargs):
            seen.update(kwargs)
            return {"text": "t", "pages": ["t"], "elapsed": 0.0}

        self.post("/api/settings", {"cropMode": "auto", "maxMergedPages": 5,
                                    "maxPageTokens": 1024, "dropHeaders": True})
        with mock.patch.object(self.server.backend.engine, "recognize",
                               new=fake):
            self.server.backend.engine.ready = True
            self.post("/api/recognize",
                      {"paths": [str(self.src / "a.png")], "mode": "merged"})
        self.assertEqual(seen.get("crop_mode"), "auto")
        self.assertEqual(seen.get("max_page_tokens"), 1024)
        self.assertEqual(seen.get("max_merged_pages"), 5)
        self.assertIs(seen.get("drop_headers"), True)

    # ── scan-dir 边界 ────────────────────────────────────
    def test_scan_dir_filters_and_sorts(self):
        _, res = self.post("/api/scan-dir", {"path": str(self.src),
                                             "maxFiles": 10})
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("count"), 3, "1 个 .txt 应被过滤")
        self.assertEqual([f["name"] for f in res["files"]],
                         ["a.png", "b.jpg", "c.pdf"])

    def test_scan_dir_exact_limit_is_not_truncated(self):
        """恰好等于上限时不能报截断——否则界面会对用户谎称"只取了前 N 个"。"""
        _, res = self.post("/api/scan-dir", {"path": str(self.src),
                                             "maxFiles": 3})
        self.assertEqual(res.get("count"), 3)
        self.assertIs(res.get("truncated"), False)

    def test_scan_dir_over_limit_is_truncated(self):
        _, res = self.post("/api/scan-dir", {"path": str(self.src),
                                             "maxFiles": 2})
        self.assertEqual(res.get("count"), 2)
        self.assertIs(res.get("truncated"), True)

    def test_scan_dir_rejects_relative_and_missing(self):
        _, res = self.post("/api/scan-dir", {"path": "imgs"})
        self.assertIs(res.get("ok"), False)
        _, res = self.post("/api/scan-dir", {"path": str(self.tmp / "nope")})
        self.assertIs(res.get("ok"), False)

    # ── recognize 路径直读 ───────────────────────────────
    def test_recognize_paths_rejects_missing_file(self):
        code, res = self.post("/api/recognize",
                              {"paths": [str(self.tmp / "nope.png")]})
        self.assertTrue(res.get("ok") is False or code == 400, res)

    def test_engine_fields_pass_through(self):
        """引擎的截断/页数/分组信息必须原样到前端，否则提示条是瞎的。"""
        def fake(image_paths, **kwargs):
            return {"text": "t", "pages": ["t"], "elapsed": 0.1,
                    "truncated": True, "pagesMismatch": True,
                    "finishReason": "length", "groups": 3, "droppedBlocks": 7,
                    "infer": {"expectedPages": 9, "numPagesOut": 6},
                    "tokens": 123}

        with mock.patch.object(self.server.backend.engine, "recognize",
                               new=fake):
            self.server.backend.engine.ready = True
            _, res = self.post("/api/recognize",
                               {"paths": [str(self.src / "a.png")],
                                "mode": "merged"})
        self.assertIs(res.get("truncated"), True)
        self.assertIs(res.get("pagesMismatch"), True)
        self.assertEqual(res.get("finishReason"), "length")
        self.assertEqual(res.get("groups"), 3)
        self.assertEqual(res.get("droppedBlocks"), 7)
        self.assertEqual(res.get("expectedPages"), 9)
        self.assertEqual(res.get("actualPages"), 6)
        self.assertEqual(res.get("tokens"), 123)


if __name__ == "__main__":
    unittest.main()

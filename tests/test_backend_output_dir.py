# -*- coding: utf-8 -*-
"""tests/test_backend_output_dir.py — E19：可配置输出目录（settings.outputDir）

契约（task-27）：
  • `outputDir` 默认空 = 程序目录下 outputs（模块级 OUTPUT_DIR，既有 mock 依赖）；
  • 非空必须能展开成绝对路径（expandvars/expanduser），相对路径 → err.badValue
    且整批不写盘；
  • 写盘前做运行期校验：忙时拒绝（err.busy）、mkdir+探针写失败 →
    err.outDirUnwritable，均不写盘；
  • 所有消费点（_save_output / list_history / history_read / history_delete /
    open_output_dir / get_status.outDir / get_settings.outDir）都跟随设置；
  • 空串恢复默认。

全部用临时目录，不碰项目 settings.json / outputs/。
"""
import json
import os
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


class OutputDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_outdir_"))
        self.default_dir = self.tmp / "outputs_default"
        self.default_dir.mkdir()
        self.settings_file = self.tmp / "settings.json"
        self.settings_file.write_text(json.dumps({"lang": "zh"}),
                                      encoding="utf-8")
        self._patches = [
            mock.patch.object(wb, "SETTINGS_FILE", self.settings_file),
            mock.patch.object(wb, "OUTPUT_DIR", self.default_dir),
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

    def _new_backend(self) -> wb.WebBackend:
        """重开一个 backend（模拟重启后读手工改过的 settings.json）。"""
        return wb.WebBackend()

    # ── 默认值 / 回落 ────────────────────────────────────────
    def test_default_is_empty_and_falls_back_to_module_output_dir(self):
        self.assertEqual(self.saved().get("outputDir", ""), "")
        self.assertEqual(self.backend.output_dir(), self.default_dir)
        self.assertEqual(self.backend.get_status()["outDir"],
                         str(self.default_dir))
        s = self.backend.get_settings()
        self.assertEqual(s["outputDir"], "")
        self.assertEqual(s["outDir"], str(self.default_dir),
                         "get_settings.outDir 必须是解析后的绝对目录")

    def test_dirty_relative_setting_falls_back(self):
        """手改 settings.json 成相对路径：不炸，回落程序目录。"""
        self.settings_file.write_text(
            json.dumps({"outputDir": "relative/dir"}), encoding="utf-8")
        be = self._new_backend()
        self.assertEqual(be.output_dir(), self.default_dir)
        self.assertEqual(be.get_settings()["outDir"], str(self.default_dir))

    # ── 自定义目录 ──────────────────────────────────────────
    def test_custom_dir_saved_normalized_and_created(self):
        target = self.tmp / "custom_out"        # 不存在 → 应被 mkdir
        res = self.backend.set_settings({"outputDir": str(target)})
        self.assertNotIn("ok", res)
        self.assertTrue(target.is_dir(), "设置成功时应创建目录")
        self.assertEqual(self.saved()["outputDir"], str(target))
        self.assertEqual(self.backend.output_dir(), target)
        self.assertEqual(res["outDir"], str(target))
        self.assertEqual(res["outputDir"], str(target))

    def test_env_var_and_user_expansion(self):
        env_dir = self.tmp / "envdir"
        with mock.patch.dict(os.environ, {"OCR_TEST_OUTDIR": str(env_dir)}):
            res = self.backend.set_settings(
                {"outputDir": "$OCR_TEST_OUTDIR/sub"})
        want = env_dir / "sub"
        self.assertEqual(res["outputDir"], str(want))
        self.assertEqual(self.backend.output_dir(), want)
        self.assertTrue(want.is_dir())

    def test_relative_path_rejected_atomically(self):
        before = self.settings_file.read_bytes()
        res = self.backend.set_settings({"theme": "dark",
                                         "outputDir": "relative/out"})
        self.assertIs(res.get("ok"), False)
        self.assertIn("outputDir", res["error"])
        self.assertEqual(self.settings_file.read_bytes(), before,
                         "非法 outputDir 时整批不写盘")
        self.assertEqual(self.backend.output_dir(), self.default_dir)

    def test_uncreatable_path_rejected(self):
        """用一个「已存在的文件」当目录 → mkdir 失败 → 拒绝且不写盘。"""
        blocker = self.tmp / "afile"
        blocker.write_bytes(b"x")
        before = self.settings_file.read_bytes()
        res = self.backend.set_settings({"outputDir": str(blocker)})
        self.assertIs(res.get("ok"), False)
        self.assertIn(str(blocker), res["error"],
                      "错误信息要带出问题路径")
        self.assertEqual(self.settings_file.read_bytes(), before)
        self.assertEqual(self.backend.output_dir(), self.default_dir)

    def test_busy_blocks_change(self):
        target = self.tmp / "busy_out"
        self.backend.downloading = True
        try:
            res = self.backend.set_settings({"outputDir": str(target)})
        finally:
            self.backend.downloading = False
        self.assertIs(res.get("ok"), False)
        self.assertEqual(res["error"], wb.tr("err.busy"))
        self.assertFalse(target.exists(), "忙拒绝不应副作用建目录")
        self.assertEqual(self.saved().get("outputDir", ""), "")

    def test_busy_rejection_keeps_other_keys_unwritten(self):
        before = self.settings_file.read_bytes()
        self.backend.recognizing = True
        try:
            res = self.backend.set_settings({"theme": "dark",
                                             "outputDir": str(self.tmp / "x")})
        finally:
            self.backend.recognizing = False
        self.assertIs(res.get("ok"), False)
        self.assertEqual(self.settings_file.read_bytes(), before,
                         "同一补丁里的 theme 也不能落盘")

    def test_empty_string_restores_default(self):
        custom = self.tmp / "custom2"
        self.backend.set_settings({"outputDir": str(custom)})
        self.assertEqual(self.backend.output_dir(), custom)
        res = self.backend.set_settings({"outputDir": "  "})
        self.assertEqual(res["outputDir"], "")
        self.assertEqual(self.saved()["outputDir"], "")
        self.assertEqual(self.backend.output_dir(), self.default_dir)

    # ── 消费点全链路 ────────────────────────────────────────
    def test_save_output_uses_custom_dir(self):
        custom = self.tmp / "out_custom"
        self.backend.set_settings({"outputDir": str(custom)})
        out = self.backend._save_output("结果", {"pages": ["正文"]}, "md")
        self.assertEqual(out.parent, custom)
        self.assertTrue(out.is_file())
        self.assertEqual(list(self.default_dir.iterdir()), [],
                         "默认目录不应再被写入")

    def test_json_format_also_lands_in_custom_dir(self):
        custom = self.tmp / "out_json"
        self.backend.set_settings({"outputDir": str(custom),
                                   "outFormat": "json"})
        out = self.backend._save_output(
            "序列化", {"pages": ["a"], "blocks": []}, "json")
        self.assertEqual(out.suffix, ".json")
        self.assertEqual(out.parent, custom)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")),
                         {"version": 1, "name": "序列化",
                          "pages": [{"page": 1, "blocks": []}]})

    def test_list_history_and_read_follow_custom_dir(self):
        custom = self.tmp / "history_custom"
        self.backend.set_settings({"outputDir": str(custom)})
        # 自定义目录里放一份历史；默认目录里也放一份（不应被列出）
        (custom / "新结果.md").write_text("新目录正文", encoding="utf-8")
        (self.default_dir / "旧结果.md").write_text("旧目录正文",
                                                    encoding="utf-8")
        names = [i["name"] for i in self.backend.list_history()["items"]]
        self.assertEqual(names, ["新结果.md"])
        got = self.backend.history_read("新结果.md")
        self.assertTrue(got["ok"])
        self.assertEqual(got["text"], "新目录正文")
        # 默认目录里的同名/其它文件读不到（历史目录已切换）
        self.assertFalse(self.backend.history_read("旧结果.md")["ok"])

    def test_history_delete_follows_custom_dir(self):
        custom = self.tmp / "delete_custom"
        self.backend.set_settings({"outputDir": str(custom)})
        victim = custom / "待删.md"
        victim.write_text("x", encoding="utf-8")
        res = self.backend.history_delete("待删.md")
        self.assertTrue(res["ok"], res)
        self.assertFalse(victim.exists())

    def test_history_traversal_still_blocked_in_custom_dir(self):
        custom = self.tmp / "trav_custom"
        self.backend.set_settings({"outputDir": str(custom)})
        (self.tmp / "secret.md").write_text("secret", encoding="utf-8")
        res = self.backend.history_read("../../secret.md")
        self.assertIs(res["ok"], False)
        self.assertNotEqual(res.get("text"), "secret")

    def test_open_output_dir_uses_custom_dir(self):
        custom = self.tmp / "open_custom"
        self.backend.set_settings({"outputDir": str(custom)})
        _rmtree(custom)                                # 模拟目录被删
        calls = []
        with mock.patch.object(wb.os, "startfile",
                               side_effect=lambda p: calls.append(p)):
            ok = self.backend.open_output_dir()
        self.assertTrue(ok)
        self.assertEqual(calls, [str(custom)])
        self.assertTrue(custom.is_dir(), "打开前应确保目录存在")

    def test_open_output_dir_failure_returns_false(self):
        with mock.patch.object(wb.os, "startfile",
                               side_effect=OSError("no shell")):
            self.assertFalse(self.backend.open_output_dir())


if __name__ == "__main__":
    unittest.main()

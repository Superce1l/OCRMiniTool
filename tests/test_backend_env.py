# -*- coding: utf-8 -*-
"""tests/test_backend_env.py — 推理引擎「就绪」判定的两条合法来源

真机实测发现（v1.3.0）：源码运行时 venv 里明明装着 torch 2.8.0，但点「加载模型」
会被拒绝，提示「尚未安装推理引擎组件」——因为 start_load/env_ready 只认冻结版
的 runtime/ 组件标记（torch_build.json）。CLI 绕过该守卫所以没暴露。

本文件把两条来源都锁住：
  ① 冻结版：runtime/ 组件标记（quick_check_torch）
  ② 源码运行：venv 里的 torch 可导入（sys.frozen 为假）
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webview_backend as wb  # noqa: E402


class _FakeThread:
    """模块级替身线程：让 start_* 不真的起后台 worker。"""

    def __init__(self, *a, **k):
        self.daemon = True

    def start(self):
        pass


class EnvReadyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_env_"))
        self._patches = [
            mock.patch.object(wb, "SETTINGS_FILE", self.tmp / "settings.json"),
            mock.patch.object(wb, "OUTPUT_DIR", self.tmp / "outputs"),
        ]
        (self.tmp / "outputs").mkdir()
        for p in self._patches:
            p.start()
        self.backend = wb.WebBackend()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        for f in sorted(self.tmp.rglob("*"), reverse=True):
            try:
                f.unlink() if f.is_file() else f.rmdir()
            except OSError:
                pass
        try:
            self.tmp.rmdir()
        except OSError:
            pass

    def test_component_marker_counts(self):
        with mock.patch.object(wb, "quick_check_torch", return_value=True):
            self.assertTrue(self.backend.env_ready())

    def test_source_venv_torch_counts(self):
        """源码运行：没有 runtime/ 组件，但 venv 能 import torch → 就绪。"""
        fake_spec = types.SimpleNamespace(origin="C:/venv/torch/__init__.py")
        with mock.patch.object(wb, "quick_check_torch", return_value=False), \
                mock.patch("importlib.util.find_spec", return_value=fake_spec):
            self.assertTrue(self.backend.env_ready())

    def test_frozen_requires_component(self):
        """冻结版不吃 venv（安装包内没有 torch），必须用组件。"""
        fake_spec = types.SimpleNamespace(origin="C:/venv/torch/__init__.py")
        with mock.patch.object(wb, "quick_check_torch", return_value=False), \
                mock.patch("importlib.util.find_spec", return_value=fake_spec), \
                mock.patch.object(sys, "frozen", True, create=True):
            self.assertFalse(self.backend.env_ready())

    def test_namespace_package_not_counted(self):
        """没有 origin 的命名空间包（残缺 runtime 目录）不能当就绪。"""
        broken = types.SimpleNamespace(origin=None)
        with mock.patch.object(wb, "quick_check_torch", return_value=False), \
                mock.patch("importlib.util.find_spec", return_value=broken):
            self.assertFalse(self.backend.env_ready())

    def test_start_load_not_blocked_when_source_torch_present(self):
        """核心回归：源码运行点「加载模型」不能再被组件守卫挡住。"""
        class _NoopThread:
            def __init__(self, *a, **k):
                self.daemon = True

            def start(self):
                pass

        fake_spec = types.SimpleNamespace(origin="C:/venv/torch/__init__.py")
        with mock.patch.object(wb, "quick_check", return_value=True), \
                mock.patch.object(wb, "quick_check_torch", return_value=False), \
                mock.patch("importlib.util.find_spec", return_value=fake_spec), \
                mock.patch.object(wb.threading, "Thread", _NoopThread):
            res = self.backend.start_load()
        self.assertTrue(res.get("ok"), f"不应被组件守卫拒绝：{res}")
        self.backend.loading = False

    def test_start_load_still_blocks_frozen_without_component(self):
        class _NoopThread:
            def __init__(self, *a, **k):
                self.daemon = True

            def start(self):
                pass

        with mock.patch.object(wb, "quick_check", return_value=True), \
                mock.patch.object(wb, "quick_check_torch", return_value=False), \
                mock.patch.object(wb.threading, "Thread", _NoopThread), \
                mock.patch.object(sys, "frozen", True, create=True):
            res = self.backend.start_load()
        self.assertFalse(res.get("ok"))
        # 错误文案是 tr() 翻译后的句子（随语言变化），只断言"被组件守卫挡住"
        self.assertTrue(res.get("error"))
        self.assertIn("组件", str(res.get("error")))

    def test_env_switch_refused_while_torch_loaded(self):
        """运行中切换组件必然失败：进程已把旧 runtime 的 pyd 当 DLL 载入。

        真机实测（打包版 CPU→cu128）：覆盖 PIL/_imaging...pyd 报 PermissionError，
        marker 仍是 cpu、还白留 3.5 GB 安装包。现在提前拒绝并给出可操作的提示。
        """
        installed = {"flavor": "cpu", "torch": "2.8.0"}
        with mock.patch("downloader.installed_torch_info", return_value=installed), \
                mock.patch.object(wb, "quick_check_torch", return_value=False), \
                mock.patch.dict(sys.modules, {"torch": types.SimpleNamespace()}):
            res = self.backend.start_env_download("cu128")
        self.assertFalse(res.get("ok"), res)
        self.assertTrue(res.get("error"))
        # 文案是本地化后的句子（随语言变化），断言它指向「重启」这件事
        text = str(res.get("error"))
        self.assertTrue("重启" in text or "重新打开" in text or "restart" in text.lower(),
                        f"错误提示应告诉用户重启应用：{text}")
        self.assertFalse(self.backend.env_downloading)

    def test_env_switch_allowed_when_torch_not_loaded(self):
        """还没加载 torch（刚启动、未点加载）时可以换组件。"""
        installed = {"flavor": "cpu", "torch": "2.8.0"}
        with mock.patch("downloader.installed_torch_info", return_value=installed), \
                mock.patch.object(wb, "quick_check_torch", return_value=False), \
                mock.patch.object(wb.threading, "Thread", _FakeThread):
            sys.modules.pop("torch", None)
            res = self.backend.start_env_download("cu128")
        self.assertTrue(res.get("ok"), res)
        self.backend.env_downloading = False


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""tests/test_backend_hardening.py — 后端加固回归（C4/C7/C9/C12/C13）

  C4  `_torch_flavor()` 纯查询：GET /api/env 不写 settings.json
  C7  保留设备名/NUL 过滤 + 识别结果原子落盘（不留 .tmp / 半截文件）
  C9  `wait_for_tasks()` 有界等待（退出路径用）
  C12 子进程 stderr 解码（GBK 不再乱码）
  C13 单实例命名互斥（拿不到 API 时静默放行）

全部用临时目录，不碰项目数据。
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app_webview as av  # noqa: E402
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


class _TmpBackendCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_be_"))
        self.out = self.tmp / "outputs"
        self.out.mkdir()
        self.settings = self.tmp / "settings.json"
        self.settings.write_text(json.dumps({"lang": "zh"}), encoding="utf-8")
        self._patches = [
            mock.patch.object(wb, "SETTINGS_FILE", self.settings),
            mock.patch.object(wb, "OUTPUT_DIR", self.out),
        ]
        for p in self._patches:
            p.start()
        self.backend = wb.WebBackend()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        _rmtree(self.tmp)


class TorchFlavorReadOnlyTest(_TmpBackendCase):
    """C4：读路径（GET /api/env）绝不写 settings.json。"""

    _REC = {"recommended": "cpu", "reason": "no nv", "hardware": {},
            "options": []}

    def _mocks(self):
        return (
            mock.patch("downloader.recommend_torch_flavor",
                       return_value=dict(self._REC)),
            mock.patch("downloader.installed_torch_info", return_value=None),
        )

    def test_env_status_is_read_only(self):
        before = self.settings.read_bytes()
        m1, m2 = self._mocks()
        with m1, m2:
            data = self.backend.env_status()
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("selected"), "cpu")
        self.assertEqual(self.settings.read_bytes(), before,
                         "GET /api/env 不应改写 settings.json（只读介质会 500）")
        self.assertNotIn("torchFlavor", json.loads(
            self.settings.read_text(encoding="utf-8")))

    def test_action_path_still_persists_recommendation(self):
        m1, m2 = self._mocks()
        with m1, m2:
            self.assertEqual(self.backend._torch_flavor(persist=True), "cpu")
        saved = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(saved.get("torchFlavor"), "cpu",
                         "动作路径（start_load）仍应记住推荐值")


class SanitizeAndSaveOutputTest(_TmpBackendCase):
    """C7：保留设备名/NUL + 原子落盘。"""

    def test_reserved_device_names_get_prefixed(self):
        cases = {
            "aux": "_aux",
            "CON": "_CON",
            "nul.png": "_nul.png",
            "com1": "_com1",
            "LPT9": "_LPT9",
            "aux.txt": "_aux.txt",
            "con .md": "_con .md",
            "com0": "com0",          # 不是保留名
            "console": "console",    # 只是前缀相同
            "扫描件 2024": "扫描件 2024",
        }
        for raw, want in cases.items():
            self.assertEqual(wb.WebBackend._sanitize_name(raw), want, raw)

    def test_nul_and_control_chars_removed(self):
        self.assertEqual(wb.WebBackend._sanitize_name("a\x00b"), "ab")
        self.assertEqual(wb.WebBackend._sanitize_name("a\nb\tc"), "abc")

    def test_save_output_with_reserved_name_succeeds(self):
        out = self.backend._save_output("aux",
                                        {"text": "x", "pages": ["正文"]})
        self.assertEqual(out.name, "_aux.md")
        self.assertTrue(out.is_file())
        self.assertIn("正文", out.read_text(encoding="utf-8"))
        self.assertEqual(list(self.out.glob("*.tmp")), [],
                         "原子落盘后不应残留 .tmp")

    def test_save_output_sanitizes_path_traversal(self):
        out = self.backend._save_output("../evil",
                                        {"text": "x", "pages": ["x"]})
        self.assertEqual(out.parent, self.out)
        self.assertEqual(out.name, "evil.md")

    def test_failed_write_leaves_no_partial_file(self):
        orig = Path.write_text

        def boom(self, *a, **k):
            if self.name.endswith(".tmp"):
                orig(self, "半截", encoding="utf-8")
                raise OSError("disk full (simulated)")
            return orig(self, *a, **k)

        with mock.patch.object(Path, "write_text", boom):
            with self.assertRaises(OSError):
                self.backend._save_output("crash",
                                          {"text": "x", "pages": ["x"]})
        self.assertFalse((self.out / "crash.md").exists(),
                         "写失败不应留下半截结果文件")
        self.assertEqual(list(self.out.glob("*.tmp")), [])


class WaitForTasksTest(_TmpBackendCase):
    """C9：退出前的有界等待。"""

    def test_idle_returns_true_immediately(self):
        t0 = time.time()
        self.assertTrue(self.backend.wait_for_tasks(1.0))
        self.assertLess(time.time() - t0, 0.2)

    def test_times_out_on_stuck_task(self):
        self.backend.downloading = True
        try:
            t0 = time.time()
            self.assertFalse(self.backend.wait_for_tasks(0.3))
            self.assertGreaterEqual(time.time() - t0, 0.25)
        finally:
            self.backend.downloading = False

    def test_returns_when_task_finishes(self):
        self.backend.downloading = True
        timer = threading.Timer(0.2,
                                lambda: setattr(self.backend, "downloading", False))
        timer.start()
        try:
            self.assertTrue(self.backend.wait_for_tasks(2.0))
        finally:
            timer.cancel()
            self.backend.downloading = False


class DecodeChildTextTest(unittest.TestCase):
    """C12：目标解释器/程序 stderr 解码——GBK/UTF-8 都不许乱码。"""

    def test_gbk_stderr(self):
        raw = "信息: 系统找不到指定的文件。".encode("gbk")
        self.assertIn("信息", wb.decode_child_text(raw))

    def test_utf8_stderr(self):
        raw = "错误: 找不到文件".encode("utf-8")
        self.assertIn("错误", wb.decode_child_text(raw))

    def test_empty_and_garbage_never_raise(self):
        self.assertEqual(wb.decode_child_text(b""), "")
        out = wb.decode_child_text(b"\xff\xfe\x00abc")
        self.assertIsInstance(out, str)


class SingleInstanceTest(unittest.TestCase):
    """C13：命名互斥 + 失败静默降级。"""

    def tearDown(self):
        av.release_single_instance()

    def test_second_process_reports_running(self):
        if sys.platform != "win32":
            self.skipTest("仅 Windows")
        self.assertTrue(av.acquire_single_instance())
        code = (
            "import sys;"
            f"sys.path.insert(0, {str(ROOT)!r});"
            "import app_webview as av;"
            "print('RESULT', av.acquire_single_instance())"
        )
        import subprocess
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace")
        self.assertIn("RESULT False", r.stdout,
                      f"第二个进程必须看到已在运行：{r.stdout} {r.stderr}")
        av.release_single_instance()
        self.assertTrue(av.acquire_single_instance())

    def test_api_failure_degrades_to_allow(self):
        av._MUTEX_HANDLE = None
        with mock.patch.object(av.ctypes, "WinDLL",
                               side_effect=OSError("no kernel32")):
            self.assertTrue(av.acquire_single_instance(),
                            "检测失败必须静默放行，不能让应用起不来")

    def test_release_is_idempotent(self):
        av._MUTEX_HANDLE = None
        av.release_single_instance()      # 不抛即可
        av.release_single_instance()


if __name__ == "__main__":
    unittest.main()

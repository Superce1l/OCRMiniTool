# -*- coding: utf-8 -*-
"""tests/test_log_rotation.py — applog 日志轮转（D12）

覆盖点：
  • 按月文件命名 + 正常追加
  • 超过 MAX_BYTES 时轮转为 app-YYYYMM.1.log，旧档后移
  • 最多保留 MAX_BACKUPS 份（不出现 .4）
  • 轮转/打开失败时静默降级，绝不抛异常（磁盘满/只读目录场景的防线）
  • MAX_BACKUPS=0 时关闭轮转
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import applog  # noqa: E402

_DEFAULT_BYTES = 8 * 1024 * 1024
_DEFAULT_BACKUPS = 3


class LogRotationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_log_"))
        self.logdir = self.tmp / "logs"
        applog.configure(log_dir=self.logdir, max_bytes=512, max_backups=3)

    def tearDown(self):
        applog.close()
        # 恢复模块默认状态：后续测试仍写项目 logs/
        applog._DIR = None
        applog.MAX_BYTES = _DEFAULT_BYTES
        applog.MAX_BACKUPS = _DEFAULT_BACKUPS
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _files(self):
        return sorted(p.name for p in self.logdir.glob("*.log"))

    def _base(self, month: str) -> Path:
        return self.logdir / f"app-{month}.log"

    def test_month_file_named_app_yyyymm(self):
        applog.log_info("hello")
        files = self._files()
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0], r"^app-\d{6}\.log$")
        self.assertIn("[INFO] hello", self._base(files[0][4:10]).read_text(encoding="utf-8"))

    def test_rotates_when_file_exceeds_limit(self):
        for i in range(30):
            applog.log_info(f"line-{i:03d}-{'x' * 20}")
        month = self._files()[0][4:10]
        self.assertTrue((self.logdir / f"app-{month}.1.log").is_file(),
                        f"应轮转出 .1：{self._files()}")

    def test_keeps_at_most_backups(self):
        for i in range(120):
            applog.log_info(f"line-{i:03d}-{'y' * 20}")
        month = next(p.name for p in self.logdir.glob("app-??????.log"))[4:10]
        backups = sorted(self.logdir.glob(f"app-{month}.*.log"))
        self.assertLessEqual(len(backups), 3, f"份数超限：{self._files()}")
        self.assertEqual(len(backups), 3, f"应保留 3 份：{self._files()}")
        self.assertFalse(
            any(p.name == f"app-{month}.4.log" for p in backups), "不应出现 .4")

    def test_write_does_not_raise_when_dir_unusable(self):
        # 用一个普通文件充当父目录 → mkdir 必然失败；写日志必须静默
        blocker = self.tmp / "blocker"
        blocker.write_text("x", encoding="utf-8")
        applog.configure(log_dir=blocker / "logs", max_bytes=64, max_backups=1)
        for i in range(5):
            applog.log_info(f"should-not-raise-{i}")
        self.assertTrue(blocker.is_file(), "不能把同名文件覆盖成目录")

    def test_rotation_disabled_when_zero_backups(self):
        applog.configure(log_dir=self.logdir, max_bytes=200, max_backups=0)
        for i in range(40):
            applog.log_info(f"no-rotate-{i:03d}-{'z' * 20}")
        self.assertEqual(self._files(), [p.name for p in self.logdir.glob("app-??????.log")],
                         "MAX_BACKUPS=0 时不应产生任何备份文件")
        self.assertEqual(len(self._files()), 1)


if __name__ == "__main__":
    unittest.main()

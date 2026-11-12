# -*- coding: utf-8 -*-
"""tests/test_build_downloader_parts.py — D6「分卷续传坏包」的回归测试

背景（docs/explore/04-build-dist.md §4.5）：旧实现把每卷直接写到**最终文件名**
（`...zip.part01-of-2`），下次运行只按「文件是否存在」跳过，不校验大小。因此：

  • 下载中被取消/进程被杀 → 留下短卷；
  • 下次运行把短卷当完整 → 拼接出坏 zip → 解压 CRC 失败，且不会自愈；
  • 发布方重切片后（v1.2.0 实况 1437.3MB → 1900.0MB），旧卷被复用同样出错。

D6 修复（backend-api 批次）：`downloader._download_part` 先写 `<part>.part`
暂存、与 HEAD 字节数核对后才 `os.replace` 成正式名；「已存在」不再等于「已完整」。
本文件原先用 `@expectedFailure` 记录待修行为，**修复后已改为正式断言**（见各例
注释里的"修复前/修复后"）。不接网络、不写项目目录。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import downloader  # noqa: E402

FLAVOR = "cu128"   # 唯一的双卷 flavor（downloader._TORCH_FLAVORS["cu128"]["parts"] == 2）


class MultipartResumeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_dl_parts_"))
        self.zip_name = downloader._TORCH_FLAVORS[FLAVOR]["zip"]
        self.part1 = self.tmp / f"{self.zip_name}.part01-of-2"
        self.part2 = self.tmp / f"{self.zip_name}.part02-of-2"
        self._patches = [mock.patch.object(downloader, "RUNTIME_DIR", self.tmp)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_with_fake_network(self, expect_part_bytes: int):
        """跑 download_torch，用假的 HEAD/下载/解压代替网络。

        返回 (fetched, part_state, snapshot)：
          fetched     —— `_download_file` 收到的目标名（D6 后是**暂存名** <part>.part）
          part_state  —— 每卷 `_download_part` 返回后的 (存在?, 字节数)  ← 拼接前快照
          snapshot    —— 交给解压器的 zip 内容（拼接结果）
        """
        fetched: list[str] = []
        part_state: dict[str, tuple[bool, int]] = {}
        snapshot: dict[str, bytes] = {}
        real_part = downloader._download_part

        def fake_download(url, dest, progress_cb=None, cancel_check=None):
            fetched.append(Path(dest).name)
            Path(dest).write_bytes(b"F" * expect_part_bytes)
            return expect_part_bytes

        def spy_part(url, pdest, progress_cb=None, cancel_check=None):
            got = real_part(url, pdest, progress_cb=progress_cb,
                            cancel_check=cancel_check)
            p = Path(pdest)
            part_state[p.name] = (p.is_file(),
                                  p.stat().st_size if p.is_file() else -1)
            return got

        def fake_extract(zip_path, dest, progress_cb=None, cancel_check=None):
            snapshot["zip"] = Path(zip_path).read_bytes()

        with mock.patch.object(downloader, "_remote_size",
                               lambda url, timeout=30.0: expect_part_bytes), \
                mock.patch.object(downloader, "_download_file", fake_download), \
                mock.patch.object(downloader, "_download_part", spy_part), \
                mock.patch.object(downloader, "_extract_zip_checked",
                                  fake_extract):
            downloader.download_torch(FLAVOR, force=True)
        return fetched, part_state, snapshot

    def test_short_stale_part_is_refetched(self):
        """短卷（上次取消留下的）必须重新下载，而不是直接拿去拼接。

        修复前：`if not pdest.exists()` 直接跳过 → part_state[part1]=(True, 7)，
        拼接结果只有 7+1024 字节 → 解压必坏。
        修复后：发现 7 != HEAD(1024) → 走暂存重下 → (True, 1024)。
        """
        self.part1.write_bytes(b"partial")          # 故意短（7 字节）
        fetched, part_state, snapshot = self._run_with_fake_network(1024)
        self.assertEqual(part_state[self.part1.name], (True, 1024),
                         "残留下的短卷应被重下并替换为完整卷")
        self.assertEqual(part_state[self.part2.name], (True, 1024))
        self.assertIn(self.part1.name + ".part", fetched,
                      "D6 后下载先落暂存名 <part>.part，核对字节数再改名")
        self.assertEqual(len(snapshot.get("zip", b"")), 2048,
                         "拼接结果必须是两卷完整内容")

    def test_stale_part_with_wrong_size_is_replaced(self):
        """大小与发布清单不符的卷（重切片场景）必须重下，不能原样拼接。

        修复前：只按 exists() 跳过 → part_state[part1]=(True, 100)。
        修复后：100 != HEAD(1024) → 重下 → (True, 1024)。
        """
        self.part1.write_bytes(b"X" * 100)          # 与预期 1024 不符
        fetched, part_state, snapshot = self._run_with_fake_network(1024)
        self.assertEqual(part_state[self.part1.name], (True, 1024),
                         "大小不符的卷应被重下（期望值来自 HEAD）")
        self.assertEqual(len(snapshot.get("zip", b"")), 2048)
        self.assertIn(self.part1.name + ".part", fetched)

    def test_fresh_parts_are_both_downloaded(self):
        """没有残留卷时两卷都会下载。

        修复后断言：①两卷最终名存在且字节数＝期望；②没有 `<part>.part` 暂存
        残留；③拼接内容为两卷之和；④拼接完成后分卷被清理。
        （D6 之后 `_download_file` 收到的是暂存名，故不再断言它收到最终名。）
        """
        fetched, part_state, snapshot = self._run_with_fake_network(1024)
        self.assertEqual(part_state[self.part1.name], (True, 1024),
                         "最终分卷名必须存在且大小正确")
        self.assertEqual(part_state[self.part2.name], (True, 1024))
        self.assertEqual(sorted(fetched),
                         [self.part1.name + ".part", self.part2.name + ".part"],
                         "每卷都先写 <part>.part 暂存")
        self.assertEqual(snapshot.get("zip"), b"F" * 2048)
        leftovers = [p.name for p in self.tmp.iterdir() if ".part" in p.name]
        self.assertEqual(leftovers, [],
                         "暂存 .part 不应残留（拼接完成后分卷也已清理）")
        self.assertFalse(self.part1.exists() or self.part2.exists(),
                         "拼接完成后应清理分卷")


if __name__ == "__main__":
    unittest.main()

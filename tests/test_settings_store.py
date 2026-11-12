# -*- coding: utf-8 -*-
"""tests/test_settings_store.py — settings.json 读写层

覆盖点（都是"用户配置被写坏"这类真实故障的防线）：
  • 原子落盘：不留 .tmp、写好即读到
  • 损坏文件不缓存：外部修复后能自愈
  • 外部修改检测：mtime_ns + size 双信号
  • 并发 update 不丢更新
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from settings_store import SettingsStore  # noqa: E402


class SettingsStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_settings_"))
        self.path = self.tmp / "settings.json"
        self.store = SettingsStore(self.path)

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            self.tmp.rmdir()
        except OSError:
            pass

    def test_missing_file_reads_empty(self):
        self.assertEqual(self.store.read(), {})

    def test_update_creates_file(self):
        out = self.store.update({"theme": "dark"})
        self.assertEqual(out, {"theme": "dark"})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"theme": "dark"})

    def test_update_shallow_merges(self):
        self.store.update({"a": 1, "b": 2})
        self.assertEqual(self.store.update({"b": 3}), {"a": 1, "b": 3})

    def test_remove(self):
        self.store.update({"a": 1, "b": 2})
        self.assertEqual(self.store.remove("a"), {"b": 2})
        self.assertEqual(self.store.read(), {"b": 2})

    def test_remove_missing_key_is_noop(self):
        self.store.update({"a": 1})
        before = self.path.stat().st_mtime_ns
        self.assertEqual(self.store.remove("zzz"), {"a": 1})
        self.assertEqual(self.path.stat().st_mtime_ns, before,
                         "未命中键时不应重写文件")

    def test_atomic_write_leaves_no_tmp(self):
        self.store.update({"a": 1})
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()),
                         ["settings.json"])

    def test_corrupt_file_returns_empty_and_recovers(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.read(), {})
        # 损坏态不缓存：外部修好后必须能读到新内容
        self.path.write_text('{"lang": "en"}', encoding="utf-8")
        self.assertEqual(self.store.read(), {"lang": "en"})

    def test_non_dict_json_treated_as_empty(self):
        self.path.write_text("[1,2,3]", encoding="utf-8")
        self.assertEqual(self.store.read(), {})

    def test_external_change_detected(self):
        self.store.update({"a": 1})
        self.assertEqual(self.store.read(), {"a": 1})
        self.path.write_text('{"a": 2}', encoding="utf-8")
        self.assertEqual(self.store.read(), {"a": 2})

    def test_unicode_preserved(self):
        self.store.update({"模型": "中文路径"})
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("模型", raw, "不应写成 \\uXXXX 转义")

    def test_concurrent_updates_lose_nothing(self):
        keys = [f"k{i}" for i in range(40)]
        barrier = threading.Barrier(8)

        def worker(chunk):
            barrier.wait()
            for k in chunk:
                self.store.update({k: True})

        threads = [threading.Thread(target=worker, args=(keys[i::8],))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(set(self.store.read()), set(keys))


if __name__ == "__main__":
    unittest.main()

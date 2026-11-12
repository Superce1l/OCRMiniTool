# -*- coding: utf-8 -*-
"""tests/test_settings_robust.py — settings.json 抗损坏加固（D13）

与既有 tests/test_settings_store.py 互补（那份守住原子写/并发/自愈的旧契约）：
  • 坏文件被改名 settings.json.corrupt-<ts> 留证，原路径不再存在，内容可取证
  • corrupt 现场只留最近 2 份
  • 隔离后再次写入可正常重建，且读回正常
  • 覆盖已有文件前留一份 settings.json.bak（只 1 份，内容为上一版）
  • 落盘补 settingsVersion（纯增量）；首次创建不写；read() 业务视图不暴露
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from settings_store import SCHEMA_VERSION, SettingsStore  # noqa: E402


class SettingsRobustTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_settings_robust_"))
        self.path = self.tmp / "settings.json"
        self.store = SettingsStore(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _corrupts(self):
        return sorted(self.tmp.glob("settings.json.corrupt-*"))

    def _raw(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _make_corrupt(self, text="{not json"):
        self.path.write_text(text, encoding="utf-8")

    def test_corrupt_file_quarantined_and_original_gone(self):
        self._make_corrupt('{"lang": "broken"')
        self.assertEqual(self.store.read(), {})
        self.assertFalse(self.path.exists(), "坏文件必须被改名，原路径不再存在")
        quarantined = self._corrupts()
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(encoding="utf-8"),
                         '{"lang": "broken"', "留证内容应与原文件一致")

    def test_corrupt_keeps_only_two_backups(self):
        for _ in range(3):
            self._make_corrupt()
            self.assertEqual(self.store.read(), {})
        self.assertEqual(len(self._corrupts()), 2, "corrupt 现场最多留 2 份")

    def test_write_after_corruption_rebuilds_normally(self):
        self._make_corrupt()
        self.assertEqual(self.store.read(), {})
        out = self.store.update({"lang": "en"})
        self.assertEqual(out, {"lang": "en"})
        self.assertTrue(self.path.is_file(), "隔离后新写入必须重建 settings.json")
        self.assertEqual(self._raw(), {"lang": "en"}, "首次创建保持旧口径（不写版本键）")
        self.assertEqual(self.store.read(), {"lang": "en"})

    def test_bak_keeps_previous_version(self):
        self.store.update({"a": 1})
        bak = self.tmp / "settings.json.bak"
        self.assertFalse(bak.exists(), "首次创建没有上一版，不应产生 .bak")
        self.store.update({"b": 2})
        self.assertTrue(bak.is_file(), "覆盖已有文件前应留 .bak")
        self.assertEqual(json.loads(bak.read_text(encoding="utf-8")), {"a": 1})
        self.assertEqual(self.store.read(), {"a": 1, "b": 2})

    def test_schema_version_stamped_on_existing_file(self):
        self.path.write_text('{"lang": "zh"}', encoding="utf-8")
        self.store.update({"theme": "dark"})
        raw = self._raw()
        self.assertEqual(raw["settingsVersion"], SCHEMA_VERSION)
        self.assertEqual(raw["lang"], "zh", "补版本号不得改动其他键")
        self.assertEqual(self.store.schema_version(), SCHEMA_VERSION)

    def test_schema_version_not_leaked_into_read(self):
        self.path.write_text('{"settingsVersion": 1, "lang": "en"}',
                             encoding="utf-8")
        self.assertEqual(self.store.read(), {"lang": "en"},
                         "read() 返回业务视图，不含落盘元数据")
        self.assertEqual(self.store.read_raw().get("settingsVersion"), 1)

    def test_remove_keeps_schema_version(self):
        self.store.update({"a": 1})
        self.store.update({"b": 2})          # 第二次写 → 补版本
        self.assertEqual(self.store.remove("a"), {"b": 2})
        self.assertEqual(self._raw()["settingsVersion"], SCHEMA_VERSION)
        self.assertNotIn("a", self.store.read())

    def test_list_json_treated_as_empty_and_not_quarantined(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(self.store.read(), {})
        self.assertTrue(self.path.exists(), "非对象 JSON 维持旧行为（不隔离）")

    def test_new_instance_reads_versioned_file(self):
        self.store.update({"a": 1})
        self.store.update({"b": 2})
        other = SettingsStore(self.path)
        self.assertEqual(other.schema_version(), SCHEMA_VERSION)
        self.assertEqual(other.read(), {"a": 1, "b": 2})


if __name__ == "__main__":
    unittest.main()

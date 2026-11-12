# -*- coding: utf-8 -*-
"""tests/test_downloader.py — 下载器/组件管理的纯逻辑

不联网、不跑 pip：只测"判断类"逻辑（镜像改写、完整性下限、组件标记、
按硬件推荐）。这些函数出错的表现都是用户下载 6.8GB 后加载失败，代价极高。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import downloader  # noqa: E402


class MirrorTest(unittest.TestCase):
    def tearDown(self):
        downloader.set_mirror("")

    def test_default_is_official(self):
        downloader.set_mirror("")
        self.assertEqual(downloader.get_mirror(), "")
        self.assertTrue(downloader.file_url("config.json").startswith(
            downloader.HF_OFFICIAL))

    def test_scheme_added(self):
        downloader.set_mirror("hf-mirror.com")
        self.assertEqual(downloader.get_mirror(), "https://hf-mirror.com")

    def test_trailing_slash_stripped(self):
        downloader.set_mirror("https://hf-mirror.com/")
        self.assertEqual(downloader.get_mirror(), "https://hf-mirror.com")

    def test_url_rewritten(self):
        downloader.set_mirror("https://hf-mirror.com")
        url = downloader.file_url("config.json")
        self.assertTrue(url.startswith("https://hf-mirror.com/baidu/"))
        self.assertIn("config.json", url)

    def test_foreign_url_untouched(self):
        downloader.set_mirror("https://hf-mirror.com")
        other = "https://example.com/x.bin"
        self.assertEqual(downloader._apply_mirror(other), other)

    def test_none_resets(self):
        downloader.set_mirror("https://hf-mirror.com")
        downloader.set_mirror(None)
        self.assertEqual(downloader.get_mirror(), "")


class ModelIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_dl_"))

    def tearDown(self):
        for p in sorted(self.tmp.rglob("*"), reverse=True):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            self.tmp.rmdir()
        except OSError:
            pass

    def test_empty_dir_is_incomplete(self):
        self.assertFalse(downloader.quick_check(self.tmp))
        self.assertEqual(downloader.missing_files(self.tmp),
                         downloader.ALL_FILES)

    def test_files_below_floor_rejected(self):
        with mock.patch.dict(downloader.MIN_SIZE_BYTES,
                             {"config.json": 100}, clear=False):
            for name in downloader.ALL_FILES:
                (self.tmp / name).write_bytes(b"x" * 10)
            self.assertFalse(downloader.quick_check(self.tmp),
                             "小于完整性下限的文件不能算完整")
            self.assertIn("config.json", downloader.missing_files(self.tmp))

    def test_all_files_present_with_floor(self):
        with mock.patch.dict(downloader.MIN_SIZE_BYTES,
                             {n: 1 for n in downloader.ALL_FILES}, clear=False):
            for name in downloader.ALL_FILES:
                (self.tmp / name).write_bytes(b"x" * 8)
            self.assertTrue(downloader.quick_check(self.tmp))
            self.assertEqual(downloader.missing_files(self.tmp), [])

    def test_all_files_known(self):
        self.assertEqual(len(set(downloader.ALL_FILES)),
                         len(downloader.ALL_FILES), "文件清单不应有重复项")
        self.assertTrue(downloader.ALL_FILES)


class TorchComponentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_rt_"))
        (self.tmp / "torch" / "lib").mkdir(parents=True)
        (self.tmp / "torch" / "__init__.py").write_text("", encoding="utf-8")

    def tearDown(self):
        for p in sorted(self.tmp.rglob("*"), reverse=True):
            try:
                p.unlink() if p.is_file() else p.rmdir()
            except OSError:
                pass
        try:
            self.tmp.rmdir()
        except OSError:
            pass

    def _marker(self, flavor="cpu", ver=None):
        (self.tmp / "torch_build.json").write_text(json.dumps({
            "flavor": flavor,
            "torch": ver or downloader._TORCH_VERSION,
        }), encoding="utf-8")

    def test_not_installed(self):
        self.assertIsNone(downloader.installed_torch_info(self.tmp))
        self.assertFalse(downloader.quick_check_torch("cpu", self.tmp))

    def test_installed_cpu(self):
        self._marker("cpu")
        self.assertEqual(downloader.installed_torch_info(self.tmp)["flavor"],
                         "cpu")
        self.assertTrue(downloader.quick_check_torch("cpu", self.tmp))

    def test_flavor_mismatch_rejected(self):
        self._marker("cpu")
        self.assertFalse(downloader.quick_check_torch("cu128", self.tmp))

    def test_version_mismatch_rejected(self):
        self._marker("cpu", ver="0.0.1")
        self.assertFalse(downloader.quick_check_torch("cpu", self.tmp))

    def test_cu128_requires_cuda_dll(self):
        self._marker("cu128")
        self.assertFalse(downloader.quick_check_torch("cu128", self.tmp),
                         "缺 torch_cuda.dll 的 cu128 组件不能算就绪")
        (self.tmp / "torch" / "lib" / "torch_cuda.dll").write_bytes(b"x")
        self.assertTrue(downloader.quick_check_torch("cu128", self.tmp))


class RecommendTest(unittest.TestCase):
    def test_nvidia_recommends_cu128(self):
        hw = {"gpus": [{"vendor": "nvidia", "name": "RTX 4090",
                        "driver": "570.1"}],
              "vendors": ["nvidia"], "nvidia_driver": 570.1,
              "has_discrete": True, "error": None}
        rec = downloader.recommend_torch_flavor(hw)
        self.assertEqual(rec["recommended"], "cu128")
        opts = {o["key"]: o for o in rec["options"]}
        self.assertEqual(set(opts), {"cpu", "cu128"})
        self.assertTrue(opts["cu128"]["recommended"])
        self.assertTrue(opts["cu128"]["suitable"])

    def test_no_nvidia_recommends_cpu(self):
        hw = {"gpus": [], "vendors": [], "nvidia_driver": None,
              "has_discrete": False, "error": None}
        rec = downloader.recommend_torch_flavor(hw)
        self.assertEqual(rec["recommended"], "cpu")
        opts = {o["key"]: o for o in rec["options"]}
        self.assertFalse(opts["cu128"]["suitable"],
                         "没有 NVIDIA 卡时不应推荐 CUDA 组件")
        self.assertTrue(opts["cu128"]["note"], "不适用时要给出原因")

    def test_old_driver_noted(self):
        hw = {"gpus": [{"vendor": "nvidia", "name": "GTX 1080",
                        "driver": "560.0"}],
              "vendors": ["nvidia"], "nvidia_driver": 560.0,
              "has_discrete": True, "error": None}
        rec = downloader.recommend_torch_flavor(hw)
        opts = {o["key"]: o for o in rec["options"]}
        self.assertEqual(rec["recommended"], "cu128")
        self.assertTrue(opts["cu128"]["note"], "驱动过旧必须提示升级")

    def test_flavors_registry_is_copied(self):
        a = downloader.torch_flavors()
        a["cpu"]["size_mb"] = -1
        self.assertNotEqual(downloader.torch_flavors()["cpu"]["size_mb"], -1)

    def test_flavor_metadata_complete(self):
        for key, meta in downloader.torch_flavors().items():
            self.assertIn("zip", meta)
            self.assertIn("size_mb", meta)
            self.assertIn("label_key", meta)
            self.assertIn("desc_key", meta)
            if meta.get("parts", 1) > 1:
                self.assertGreater(meta["size_mb"], 2000,
                                   "分卷发布是为绕开 GitHub 单资产 2GB 上限")


if __name__ == "__main__":
    unittest.main()

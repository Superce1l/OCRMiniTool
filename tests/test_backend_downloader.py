# -*- coding: utf-8 -*-
"""tests/test_backend_downloader.py — 模型完整性清单（C8）与分卷续传（D6）

C8：下载完成后把每个文件的真实字节数记进 `<model_dir>/_manifest.json`；
    quick_check/missing_files 优先按清单精确比对——1 GiB 下限挡不住"下到
    2GB 就被关掉"的截断权重（真实权重 4.66GB / 1.57GB）。旧目录无清单时
    退回 MIN_SIZE_BYTES 下限（向后兼容，现有 test_downloader.py 依赖它）。
D6：分卷先写 `<part>.part` 暂存、与 HEAD 字节数核对后才改名；短卷不会被
    `if not exists` 永远跳过，拼接结果因此不会短一截。

不联网、不写项目目录（RUNTIME_DIR / 模型目录全部重定向到临时目录）。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import applog  # noqa: E402
import downloader  # noqa: E402


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


ALL = list(downloader.ALL_FILES)
WEIGHT1 = "model-00001-of-00002.safetensors"


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_dlm_"))
        self._patches = [mock.patch.object(applog, "_DIR", self.tmp / "logs")]
        for p in self._patches:
            p.start()
        applog._FH = None
        applog._CURRENT_MONTH = ""

    def tearDown(self):
        applog._FH = None
        for p in self._patches:
            p.stop()
        _rmtree(self.tmp)

    def fill(self, size=8):
        for name in ALL:
            (self.tmp / name).write_bytes(b"x" * size)


class ManifestIntegrityTest(_TmpCase):
    """C8"""

    def test_old_dir_without_manifest_uses_floor(self):
        with mock.patch.dict(downloader.MIN_SIZE_BYTES,
                             {n: 1 for n in ALL}, clear=False):
            self.fill(8)
            self.assertTrue(downloader.quick_check(self.tmp),
                            "无清单的旧目录必须继续用下限判定（兼容）")
            self.assertEqual(downloader.missing_files(self.tmp), [])

    def test_manifest_detects_truncated_weight(self):
        with mock.patch.dict(downloader.MIN_SIZE_BYTES, {WEIGHT1: 1},
                             clear=False):
            self.fill(8)
            downloader.write_manifest(self.tmp, {n: 8 for n in ALL})
            self.assertTrue(downloader.quick_check(self.tmp))
            (self.tmp / WEIGHT1).write_bytes(b"x" * 4)     # 被截断
            self.assertFalse(downloader.quick_check(self.tmp),
                             "清单说有 8 字节，实际 4 字节 → 不完整")
            self.assertIn(WEIGHT1, downloader.missing_files(self.tmp))

    def test_corrupt_manifest_falls_back_to_floor(self):
        with mock.patch.dict(downloader.MIN_SIZE_BYTES,
                             {n: 1 for n in ALL}, clear=False):
            self.fill(8)
            (self.tmp / downloader.MANIFEST_NAME).write_text(
                "{not json", encoding="utf-8")
            self.assertTrue(downloader.quick_check(self.tmp))

    def test_foreign_repo_manifest_ignored(self):
        with mock.patch.dict(downloader.MIN_SIZE_BYTES,
                             {n: 1 for n in ALL}, clear=False):
            self.fill(8)
            (self.tmp / downloader.MANIFEST_NAME).write_text(json.dumps(
                {"repo": "some/other-model", "files": {WEIGHT1: 999}}),
                encoding="utf-8")
            self.assertTrue(downloader.quick_check(self.tmp),
                            "别的仓库的清单不能拿来判定本模型")

    def test_write_manifest_merges_and_is_atomic(self):
        downloader.write_manifest(self.tmp, {ALL[0]: 11})
        downloader.write_manifest(self.tmp, {ALL[1]: 22})
        data = json.loads((self.tmp / downloader.MANIFEST_NAME)
                          .read_text(encoding="utf-8"))
        self.assertEqual(data["repo"], downloader.REPO_ID)
        self.assertEqual(data["files"][ALL[0]], 11)
        self.assertEqual(data["files"][ALL[1]], 22)
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])


class PartResumeTest(_TmpCase):
    """D6：短分卷必须重下、暂存不残留。"""

    def _fake_writer(self, written: bytes, calls: list):
        def fake(url, dest, progress_cb=None, cancel_check=None):
            calls.append(Path(dest).name)
            Path(dest).write_bytes(written)
            return len(written)
        return fake

    def test_short_stale_part_is_refetched(self):
        pdest = self.tmp / "pkg.zip.part01-of-2"
        pdest.write_bytes(b"S" * 7)                     # 上次被杀留下的短卷
        calls: list[str] = []
        with mock.patch.object(downloader, "_remote_size",
                               return_value=1024), \
                mock.patch.object(downloader, "_download_file",
                                  self._fake_writer(b"F" * 1024, calls)):
            got = downloader._download_part("https://example.invalid/p1", pdest)
        self.assertEqual(got, 1024)
        self.assertEqual(pdest.stat().st_size, 1024,
                         "短卷必须被重下并替换为完整卷")
        self.assertIn(pdest.name + ".part", calls, "必须先写暂存文件")
        self.assertFalse((pdest.parent / (pdest.name + ".part")).exists(),
                         "改名后暂存文件不应残留")

    def test_complete_part_is_skipped(self):
        pdest = self.tmp / "pkg.zip.part01-of-2"
        data = b"F" * 1024
        pdest.write_bytes(data)
        calls: list[str] = []
        with mock.patch.object(downloader, "_remote_size",
                               return_value=len(data)), \
                mock.patch.object(downloader, "_download_file",
                                  self._fake_writer(b"WRONG", calls)):
            got = downloader._download_part("https://example.invalid/p1", pdest)
        self.assertEqual(got, len(data))
        self.assertEqual(pdest.read_bytes(), data)
        self.assertEqual(calls, [], "大小已符合期望的分卷不应重下")

    def test_wrong_size_after_download_raises(self):
        pdest = self.tmp / "pkg.zip.part02-of-2"
        calls: list[str] = []
        with mock.patch.object(downloader, "_remote_size",
                               return_value=1024), \
                mock.patch.object(downloader, "_download_file",
                                  self._fake_writer(b"F" * 512, calls)):
            with self.assertRaises(RuntimeError):
                downloader._download_part("https://example.invalid/p2", pdest)
        self.assertFalse(pdest.exists(), "大小不符的残档不能留在正式名下")
        self.assertFalse((pdest.parent / (pdest.name + ".part")).exists())

    def test_concat_uses_all_parts_and_cleans_up(self):
        rt = self.tmp / "runtime"
        rt.mkdir()
        zip_name = downloader._TORCH_FLAVORS["cu128"]["zip"]
        part_names = [f"{zip_name}.part01-of-2", f"{zip_name}.part02-of-2"]
        contents = {part_names[0]: b"A" * 1000, part_names[1]: b"B" * 24}
        seen: dict[str, bytes] = {}

        def fake_remote(url, timeout=30.0):
            return len(contents[url.rsplit("/", 1)[-1]])

        def fake_download(url, dest, progress_cb=None, cancel_check=None):
            payload = contents[url.rsplit("/", 1)[-1]]
            Path(dest).write_bytes(payload)
            return len(payload)

        def fake_extract(zip_path, dest, progress_cb=None, cancel_check=None):
            seen["zip"] = Path(zip_path).read_bytes()

        with mock.patch.object(downloader, "RUNTIME_DIR", rt), \
                mock.patch.object(downloader, "_remote_size", fake_remote), \
                mock.patch.object(downloader, "_download_file", fake_download), \
                mock.patch.object(downloader, "_extract_zip_checked",
                                  fake_extract):
            downloader.download_torch("cu128", force=True)

        self.assertEqual(seen.get("zip"), contents[part_names[0]] +
                         contents[part_names[1]], "两卷拼接内容必须完整")
        leftovers = [p.name for p in rt.iterdir() if ".part" in p.name]
        self.assertEqual(leftovers, [], "拼完/装完不应留下分卷或暂存文件")
        self.assertTrue((rt / downloader._TORCH_MARKER).is_file(),
                        "安装标记最后写入")


if __name__ == "__main__":
    unittest.main()

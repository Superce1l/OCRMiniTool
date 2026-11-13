# -*- coding: utf-8 -*-
"""tests/test_build_components_split.py — D7 组件构建可复现性（分卷/manifest/版本）

全部本地：不联网、不真跑 uv、不真构建 3.3GB 组件。
  • 用假 runner 在临时 stage 造文件，验证 build_flavor 的打包/分卷/manifest；
  • split_file 纯函数：命名（downloader 的 `<zip>.part01-of-N`）、大小、拼接 sha；
  • version_mismatch 与 downloader 的版本单一来源校验；
  • --no-deps 必须配 --pins、pins 自动启用 --no-deps；
  • SHA256SUMS 含 zip + parts + manifest.json 行（向后兼容旧 manifest）。
"""
import hashlib
import json
import random
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import build_components as bc  # noqa: E402

DEFAULT_STAGE = {
    "torch/__init__.py": b"# torch",
    "torchvision/__init__.py": b"# tv",
    "sympy/__init__.py": b"# sympy",
}


def make_runner(stage_files: dict[str, bytes] | None = None,
                calls: list | None = None):
    """假 runner：解析 cmd 里的 --target，把假 stage 文件写进去。"""
    files = DEFAULT_STAGE if stage_files is None else stage_files

    def run(cmd, check=True):
        if calls is not None:
            calls.append(list(cmd))
        target = Path(cmd[cmd.index("--target") + 1])
        for rel, data in files.items():
            p = target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return mock.Mock(returncode=0)

    return run


def big_stage(min_bytes: int = 1_500_000) -> dict[str, bytes]:
    """加一组不可压缩数据，让 zip 体积可预期地超过 1 MiB。"""
    data = random.Random(0).randbytes(min_bytes)
    return dict(DEFAULT_STAGE, **{"torch/big.bin": data})


class SplitFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_split_"))

    def tearDown(self):
        for p in self.tmp.iterdir():
            p.unlink()
        self.tmp.rmdir()

    def test_naming_sizes_concat_and_keeps_zip(self):
        src = self.tmp / "pkg.zip"
        payload = random.Random(42).randbytes(1_300_000)
        src.write_bytes(payload)
        parts = bc.split_file(src, 1)
        self.assertEqual([p.name for p in parts],
                         ["pkg.zip.part01-of-2", "pkg.zip.part02-of-2"])
        self.assertEqual([p.stat().st_size for p in parts],
                         [1 << 20, 1_300_000 - (1 << 20)])
        rebuilt = b"".join(p.read_bytes() for p in parts)
        self.assertEqual(rebuilt, payload)
        self.assertEqual(hashlib.sha256(rebuilt).hexdigest(),
                         hashlib.sha256(payload).hexdigest())
        self.assertTrue(src.is_file(), "切完必须保留原 zip（sidecar/整包哈希用）")

    def test_zero_or_negative_skips(self):
        src = self.tmp / "pkg.zip"
        src.write_bytes(b"x" * 100)
        self.assertEqual(bc.split_file(src, 0), [])
        self.assertEqual(bc.split_file(src, -5), [])
        self.assertEqual([p for p in self.tmp.iterdir() if ".part" in p.name], [])

    def test_single_part_when_file_fits(self):
        src = self.tmp / "pkg.zip"
        src.write_bytes(b"x" * 100)
        parts = bc.split_file(src, 1)
        self.assertEqual([p.name for p in parts], ["pkg.zip.part01-of-1"])


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_manifest_"))

    def tearDown(self):
        for p in self.tmp.iterdir():
            p.unlink()
        self.tmp.rmdir()

    def test_manifest_entry_fields(self):
        z = self.tmp / "pkg.zip"
        z.write_bytes(b"Z" * 1000)
        p1 = self.tmp / "pkg.zip.part01-of-2"
        p1.write_bytes(b"Z" * 600)
        p2 = self.tmp / "pkg.zip.part02-of-2"
        p2.write_bytes(b"Z" * 400)
        entry = bc.manifest_entry("cu128", z, ["torch", "torchvision"], [p1, p2])
        self.assertEqual(entry["flavor"], "cu128")
        self.assertEqual(entry["torch"], bc.TORCH_VER)
        self.assertEqual(entry["torchvision"], bc.TV_VER)
        self.assertEqual(entry["size"], 1000)
        self.assertEqual(entry["size_mb"], 0)
        self.assertEqual(entry["sha256"], bc.sha256_of(z))
        self.assertEqual(entry["tops"], ["torch", "torchvision"])
        self.assertEqual([p["name"] for p in entry["parts"]],
                         [p1.name, p2.name])
        self.assertEqual([p["size"] for p in entry["parts"]], [600, 400])
        self.assertEqual(entry["parts"][0]["sha256"], bc.sha256_of(p1))

    def test_sidecars_sums_include_parts(self):
        z = self.tmp / "pkg.zip"
        z.write_bytes(b"Z" * 100)
        part = self.tmp / "pkg.zip.part01-of-2"
        part.write_bytes(b"Z" * 50)
        manifest = {"pkg.zip": bc.manifest_entry("cpu", z, ["torch"], [part])}
        (self.tmp / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        bc.write_sidecars(self.tmp, manifest)
        sums = (self.tmp / "SHA256SUMS").read_text(encoding="utf-8")
        self.assertIn(f"{bc.sha256_of(z)}  pkg.zip", sums)
        self.assertIn(f"{bc.sha256_of(part)}  {part.name}", sums)
        man_sha = bc.sha256_of(self.tmp / "manifest.json")
        self.assertIn(f"{man_sha}  manifest.json", sums)
        self.assertEqual((self.tmp / "pkg.zip.sha256").read_text(encoding="utf-8"),
                         f"{bc.sha256_of(z)}  pkg.zip\n")

    def test_sidecars_tolerates_legacy_manifest(self):
        """旧 manifest（没有 parts/tops）也要能写 sidecar。"""
        z = self.tmp / "old.zip"
        z.write_bytes(b"O" * 10)
        manifest = {"old.zip": {"flavor": "cpu", "size": 10,
                                "sha256": bc.sha256_of(z)}}
        (self.tmp / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        bc.write_sidecars(self.tmp, manifest)
        sums = (self.tmp / "SHA256SUMS").read_text(encoding="utf-8")
        self.assertIn(f"{bc.sha256_of(z)}  old.zip", sums)


class VersionSourceTest(unittest.TestCase):
    def test_matches_real_downloader(self):
        self.assertIsNone(bc.version_mismatch(),
                          "build_components 与 downloader 的组件版本必须一致")
        self.assertIsNone(bc.version_mismatch(bc.TORCH_VER, bc.TV_VER))

    def test_injected_mismatch_reports_both(self):
        msg = bc.version_mismatch("2.7.0", "0.22.0")
        self.assertIsNotNone(msg)
        self.assertIn("torch", msg)
        self.assertIn("torchvision", msg)
        self.assertIn(bc.TORCH_VER, msg)


class BuildFlavorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ocr_build_flavor_")
        self.out = Path(self._tmp.name)
        self._patch = mock.patch.object(bc, "OUT_DIR", self.out)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_fake_runner_creates_zip_and_tops(self):
        res = bc.build_flavor("cpu", split_mib=0, runner=make_runner())
        z = res["zip"]
        self.assertTrue(z.is_file())
        self.assertEqual(z.name, f"ocrmini-runtime-torch{bc.TORCH_VER}-cpu.zip")
        self.assertIn("torch", res["tops"])
        self.assertIn("torchvision", res["tops"])
        self.assertIn("sympy", res["tops"])
        self.assertEqual(res["parts"], [])
        with zipfile.ZipFile(z) as zf:
            names = set(zf.namelist())
        self.assertIn("torch/__init__.py", names)
        self.assertIn("sympy/__init__.py", names)

    def test_default_no_no_deps_and_no_pins(self):
        calls = []
        bc.build_flavor("cpu", split_mib=0, runner=make_runner(calls=calls))
        cmd = calls[0]
        self.assertNotIn("--no-deps", cmd)
        self.assertNotIn("sympy==1.14.0", cmd)

    def test_no_deps_with_pins_in_command(self):
        calls = []
        pins = ["sympy==1.14.0", "networkx==3.7"]
        bc.build_flavor("cpu", split_mib=0, no_deps=True, pins=pins,
                        runner=make_runner(calls=calls))
        cmd = calls[0]
        self.assertIn("--no-deps", cmd)
        for pin in pins:
            self.assertIn(pin, cmd)
        self.assertIn(f"torch=={bc.TORCH_VER}", cmd)

    def test_cu128_default_split_via_dict(self):
        # 默认 1900 MiB 在测试里放不下 → 临时把默认改成 1 MiB 验证走默认分支
        with mock.patch.dict(bc.DEFAULT_SPLIT_MIB, {"cu128": 1}):
            res = bc.build_flavor("cu128", runner=make_runner(big_stage()))
        self.assertGreaterEqual(len(res["parts"]), 2)
        self.assertRegex(res["parts"][0].name,
                         rf"{res['zip'].name}\.part01-of-{len(res['parts'])}$")
        self.assertTrue(res["zip"].is_file())

    def test_incomplete_stage_raises(self):
        only_torch = {"torch/__init__.py": b"# torch"}
        with self.assertRaises(SystemExit):
            bc.build_flavor("cpu", split_mib=0,
                            runner=make_runner(only_torch))


class MainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ocr_bc_main_")
        self.out = Path(self._tmp.name)
        self._patches = [mock.patch.object(bc, "OUT_DIR", self.out)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def _fake_build(self, captured):
        def fake(flavor, **kw):
            captured.append((flavor, kw))
            z = self.out / f"ocrmini-runtime-torch{bc.TORCH_VER}-{flavor}.zip"
            z.write_bytes(b"Z" * 2000)
            part = self.out / (z.name + ".part01-of-2")
            part.write_bytes(b"Z" * 1000)
            return {"zip": z, "tops": ["torch", "torchvision"], "parts": [part]}
        return fake

    def test_no_deps_without_pins_exit_2(self):
        with mock.patch.object(bc, "build_flavor",
                               side_effect=AssertionError("不应构建")):
            code = bc.main(["cpu", "--no-deps"])
        self.assertEqual(code, 2)

    def test_pins_implies_no_deps_and_writes_manifest(self):
        captured: list = []
        with mock.patch.object(bc, "build_flavor",
                               side_effect=self._fake_build(captured)):
            code = bc.main(["cpu", "--pins", "sympy==1.14.0,networkx==3.7"])
        self.assertEqual(code, 0)
        self.assertEqual(captured[0][1]["no_deps"], True)
        self.assertEqual(captured[0][1]["pins"], ["sympy==1.14.0", "networkx==3.7"])
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        entry = manifest[f"ocrmini-runtime-torch{bc.TORCH_VER}-cpu.zip"]
        self.assertEqual(entry["tops"], ["torch", "torchvision"])
        self.assertEqual(len(entry["parts"]), 1)
        self.assertTrue((self.out / "SHA256SUMS").is_file())

    def test_version_mismatch_blocks_build(self):
        with mock.patch.object(bc, "version_mismatch", return_value="torch 不一致"), \
                mock.patch.object(bc, "build_flavor",
                                  side_effect=AssertionError("不应构建")):
            with self.assertRaises(SystemExit) as ctx:
                bc.main(["cpu"])
        self.assertIn("版本不一致", str(ctx.exception))

    def test_unknown_flavor_exit_2(self):
        self.assertEqual(bc.main(["bogus"]), 2)

    def test_legacy_manifest_entries_preserved(self):
        legacy = {"old-runtime.zip": {"flavor": "cpu", "size": 1,
                                      "sha256": "a" * 64}}
        (self.out / "manifest.json").write_text(json.dumps(legacy), encoding="utf-8")
        with mock.patch.object(bc, "build_flavor",
                               side_effect=self._fake_build([])):
            code = bc.main(["cpu"])
        self.assertEqual(code, 0)
        manifest = json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertIn("old-runtime.zip", manifest)
        self.assertEqual(manifest["old-runtime.zip"]["sha256"], "a" * 64)
        new_key = f"ocrmini-runtime-torch{bc.TORCH_VER}-cpu.zip"
        self.assertIn(new_key, manifest)
        self.assertIn("parts", manifest[new_key])


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""tests/test_build_release.py — D15（EXE 版本资源）+ D4（发布前检查）单测

不跑 PyInstaller、不联网、不碰真实 git 仓库：
  • render_version_info：filevers/prodvers、版本字符串、产品/版权、Translation
  • version_tuple 解析（含后缀版）
  • build_exe.main：构建前先写 build/OCRMiniTool_version_info.txt；--dry-run 不落盘
  • OCRMiniTool.spec：动态 version=_verfile、无写死版本号、既有预检未被破坏
  • tools/release_check.py：纯函数 + mock git 的 CLI 退出码/检查项/--offline/--write-sums
"""
import hashlib
import io
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_exe  # noqa: E402
import release_check  # noqa: E402


class TempRepo:
    """最小仓库骨架：version.py（+ 可选 dist 产物 / git stub 由用例提供）。"""

    def __init__(self, version: str = "1.6.0"):
        self._tmp = tempfile.TemporaryDirectory(prefix="ocr_release_")
        self.root = Path(self._tmp.name)
        (self.root / "version.py").write_text(
            f'"""version.py"""\n\nAPP_VERSION = "{version}"\n', encoding="utf-8")
        (self.root / build_exe.SPEC_NAME).write_text("# spec\n", encoding="utf-8")
        (self.root / "dist").mkdir()

    def make_artifact(self):
        art = self.root / "dist" / build_exe.APP_DIR_NAME
        (art / "_internal").mkdir(parents=True, exist_ok=True)
        (art / build_exe.EXE_NAME).write_bytes(b"MZ")
        return art

    def make_zip(self, version: str | None = None, name: str | None = None) -> Path:
        p = self.root / "dist" / (name or release_check.zip_name(version or "1.6.0"))
        p.write_bytes(b"fake-zip-bytes")
        return p

    def close(self):
        self._tmp.cleanup()


def _git_stub(*, clean=True, head="a" * 40, tag_commit="a" * 40,
              tag_exists=True, remote=True):
    """返回可替换 release_check._git 的假实现（按参数分派）。"""

    def _fake(args, cwd=None):
        if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return 0, "true"
        if args[0] == "status":
            return 0, "" if clean else " M x.py\n?? y.txt"
        if args[:2] == ["rev-parse", "HEAD"]:
            return 0, head
        if args[0] == "rev-parse" and args[1].endswith("^{commit}"):
            return (0, tag_commit) if tag_exists else (1, "")
        if args[0] == "ls-remote":
            if remote is None:
                return 1, ""
            return 0, ("deadbeef\trefs/tags/x" if remote else "")
        return 1, ""

    return _fake


def _run_cli(argv, root, *, git=None):
    out = io.StringIO()
    ctx = [redirect_stdout(out)]
    if git is not None:
        ctx.append(mock.patch.object(release_check, "_git", git))
    for c in ctx:
        c.__enter__()
    try:
        code = release_check.main(argv, root=root)
    finally:
        for c in reversed(ctx):
            c.__exit__(None, None, None)
    return code, out.getvalue()


class RenderVersionInfoTest(unittest.TestCase):
    def test_fixed_file_info_and_translation(self):
        text = build_exe.render_version_info("1.6.0")
        self.assertIn("VSVersionInfo(", text)
        self.assertRegex(text, r"filevers=\(1, 6, 0, 0\)")
        self.assertRegex(text, r"prodvers=\(1, 6, 0, 0\)")
        self.assertIn("VarStruct('Translation', [1033, 1200])", text)
        self.assertIn("'040904B0'", text)

    def test_string_table_fields(self):
        text = build_exe.render_version_info("1.6.0")
        self.assertIn("StringStruct('FileVersion', '1.6.0')", text)
        self.assertIn("StringStruct('ProductVersion', '1.6.0')", text)
        self.assertIn("StringStruct('ProductName', 'OCR Mini Tool')", text)
        self.assertIn("StringStruct('CompanyName', 'Superce1l')", text)
        self.assertIn("StringStruct('FileDescription'", text)
        self.assertIn("StringStruct('LegalCopyright'", text)
        self.assertIn("StringStruct('OriginalFilename', 'OCRMiniTool.exe')", text)

    def test_version_tuple_parsing(self):
        self.assertEqual(build_exe.version_tuple("1.6.0"), (1, 6, 0, 0))
        self.assertEqual(build_exe.version_tuple("2.0"), (2, 0, 0, 0))
        self.assertEqual(build_exe.version_tuple("1.6.0-beta.1"), (1, 6, 0, 0))
        self.assertEqual(build_exe.version_tuple("10.2.3.4"), (10, 2, 3, 4))

    def test_write_version_info_file_path_and_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            p1 = build_exe.write_version_info_file("1.6.0", root)
            self.assertEqual(p1, root / "build" / build_exe.VERSION_INFO_NAME)
            self.assertIn("'1.6.0'", p1.read_text(encoding="utf-8"))
            p2 = build_exe.write_version_info_file("1.7.0", root)
            self.assertEqual(p1, p2)
            text = p2.read_text(encoding="utf-8")
            self.assertIn("'1.7.0'", text)
            self.assertNotIn("'1.6.0'", text, "幂等覆盖，不应残留旧版本")


class BuildMainVersionInfoTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo(version="9.9.9")
        self._patches = [
            mock.patch.object(build_exe, "git_dirty", return_value=[]),
            mock.patch.object(build_exe, "torch_visible", return_value=None),
            mock.patch.object(build_exe, "pyinstaller_version",
                              return_value="6.22.3"),
            mock.patch.object(build_exe, "missing_runtime_modules",
                              return_value=[]),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self.repo.close()

    def _main(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = build_exe.main(argv, root=self.repo.root)
        return code, out.getvalue()

    def test_version_info_written_before_build(self):
        self.repo.make_artifact()
        seen = {}

        def fake_build(cmd, root, opts):
            vi = Path(root) / "build" / build_exe.VERSION_INFO_NAME
            seen["exists"] = vi.is_file()
            seen["text"] = vi.read_text(encoding="utf-8") if vi.is_file() else ""
            return 0

        with mock.patch.object(build_exe, "run_build", side_effect=fake_build):
            code, out = self._main(["--allow-dirty"])
        self.assertEqual(code, build_exe.EXIT_OK, out)
        self.assertTrue(seen["exists"], "必须在调用 PyInstaller 之前写版本资源")
        self.assertIn("'9.9.9'", seen["text"])
        self.assertIn("版本资源已写入", out)

    def test_dry_run_does_not_write_version_info(self):
        called = []
        with mock.patch.object(build_exe, "run_build",
                               side_effect=lambda *a, **k: called.append(1) or 0):
            code, out = self._main(["--dry-run", "--allow-dirty"])
        self.assertEqual(code, build_exe.EXIT_OK, out)
        self.assertEqual(called, [], "dry-run 不应构建")
        self.assertFalse((self.repo.root / "build" / build_exe.VERSION_INFO_NAME).exists())
        self.assertIn("dry-run", out)


class SpecStaticTest(unittest.TestCase):
    def test_spec_dynamic_version_and_guards(self):
        text = (ROOT / "OCRMiniTool.spec").read_text(encoding="utf-8")
        # D15：EXE(version=...) 走动态变量，文件由 build_exe 生成
        self.assertIn("version=_verfile", text)
        self.assertIn("OCRMiniTool_version_info.txt", text)
        self.assertIn("SPECPATH", text)
        # 不许写死版本号：version= 后不能直接跟引号版本
        self.assertNotRegex(text, r"version\s*=\s*['\"][\d.]")
        app_version = build_exe.parse_version(
            (ROOT / "version.py").read_text(encoding="utf-8"))
        self.assertNotIn(app_version, text, "spec 里不应写死当前 APP_VERSION")
        # 既有预检/排除逻辑不能被破坏
        self.assertIn('find_spec("torch")', text)
        self.assertIn("OCRMIT_ALLOW_BUNDLED_TORCH", text)
        self.assertIn("excludes=['torch'", text.replace(" ", ""))


class ReleasePureTest(unittest.TestCase):
    def test_tag_and_zip_helpers(self):
        self.assertEqual(release_check.derive_tag("1.6.0"), "v1.6.0")
        self.assertTrue(release_check.is_valid_tag("v1.6.0"))
        for bad in ("1.6.0", "v1.6", "v1.6.0.1", "release-v1.6.0", ""):
            self.assertFalse(release_check.is_valid_tag(bad), bad)
        self.assertEqual(release_check.version_from_tag("v2.3.4"), "2.3.4")
        self.assertIsNone(release_check.version_from_tag("2.3.4"))

        name = release_check.zip_name("1.6.0")
        self.assertEqual(name, "OCRMiniTool-v1.6.0-windows-x64.zip")
        self.assertTrue(release_check.zip_name_matches(Path("dist") / name, "1.6.0"))
        self.assertFalse(release_check.zip_name_matches(
            Path("dist") / "OCRMiniTool-v1.5.0-windows-x64.zip", "1.6.0"))
        self.assertFalse(release_check.zip_name_matches(
            Path("dist") / "OCRMiniTool-1.6.0.zip.txt", "1.6.0"))

    def test_parse_and_read_version(self):
        self.assertEqual(release_check.parse_version('APP_VERSION = "2.1.0"'), "2.1.0")
        self.assertIsNone(release_check.parse_version("no version"))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(release_check.ReleaseError):
                release_check.read_version(Path(d))


class ReleaseCliTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo(version="1.6.0")
        self.zip = self.repo.make_zip()

    def tearDown(self):
        self.repo.close()

    def test_happy_path_exit_0_and_sha(self):
        code, out = _run_cli([], self.repo.root, git=_git_stub())
        self.assertEqual(code, release_check.EXIT_OK, out)
        self.assertIn("全部通过", out)
        sha = hashlib.sha256(b"fake-zip-bytes").hexdigest()
        self.assertIn(f"SHA256：{sha}", out)
        self.assertIn("远端 tag", out)

    def test_dirty_tree_fails(self):
        code, out = _run_cli([], self.repo.root, git=_git_stub(clean=False))
        self.assertEqual(code, release_check.EXIT_FAIL, out)
        self.assertRegex(out, r"✗ 工作区干净")
        self.assertIn("2 项未提交", out)

    def test_missing_tag_fails(self):
        code, out = _run_cli([], self.repo.root,
                             git=_git_stub(tag_exists=False))
        self.assertEqual(code, release_check.EXIT_FAIL, out)
        self.assertRegex(out, r"✗ tag 存在")

    def test_tag_not_at_head_fails(self):
        code, out = _run_cli([], self.repo.root,
                             git=_git_stub(tag_commit="b" * 40))
        self.assertEqual(code, release_check.EXIT_FAIL, out)
        self.assertRegex(out, r"✗ tag 指向 HEAD")

    def test_version_tag_mismatch_fails(self):
        code, out = _run_cli(["--tag", "v1.5.0"], self.repo.root, git=_git_stub())
        self.assertEqual(code, release_check.EXIT_FAIL, out)
        self.assertRegex(out, r"✗ 版本 ↔ tag")

    def test_invalid_tag_format_exit_2(self):
        for bad in ("1.6.0", "v1.6", "v1.6.0.1"):
            code, out = _run_cli(["--tag", bad], self.repo.root, git=_git_stub())
            self.assertEqual(code, release_check.EXIT_USAGE, out)
            self.assertIn("tag 格式非法", out)

    def test_not_a_git_repo_exit_2(self):
        code, out = _run_cli([], self.repo.root, git=lambda args, cwd=None: (1, ""))
        self.assertEqual(code, release_check.EXIT_USAGE, out)
        self.assertIn("不是 git 工作区", out)

    def test_offline_skips_remote_check(self):
        def boom(args, cwd=None):
            if args[0] == "ls-remote":
                raise AssertionError("--offline 不应查询远端")
            return _git_stub()(args, cwd)

        code, out = _run_cli(["--offline"], self.repo.root, git=boom)
        self.assertEqual(code, release_check.EXIT_OK, out)
        self.assertIn("已跳过（--offline）", out)

    def test_zip_name_without_version_fails(self):
        bad = self.repo.root / "dist" / "OCRMiniTool-windows-x64.zip"
        bad.write_bytes(b"x")
        code, out = _run_cli(["--zip", str(bad)], self.repo.root, git=_git_stub())
        self.assertEqual(code, release_check.EXIT_FAIL, out)
        self.assertRegex(out, r"✗ zip 文件名含版本")

    def test_write_sums_creates_file(self):
        code, out = _run_cli(["--write-sums"], self.repo.root, git=_git_stub())
        self.assertEqual(code, release_check.EXIT_OK, out)
        sums = self.zip.with_name(release_check.SUMS_NAME)
        self.assertTrue(sums.is_file(), out)
        sha = hashlib.sha256(b"fake-zip-bytes").hexdigest()
        self.assertEqual(sums.read_text(encoding="utf-8"),
                         f"{sha}  {self.zip.name}\n")
        self.assertIn("SHA256SUMS", out)

    def test_write_sums_without_zip_exit_2(self):
        self.zip.unlink()
        code, out = _run_cli(["--write-sums"], self.repo.root, git=_git_stub())
        self.assertEqual(code, release_check.EXIT_USAGE, out)
        self.assertIn("--write-sums 需要已存在的发行 zip", out)


if __name__ == "__main__":
    unittest.main()

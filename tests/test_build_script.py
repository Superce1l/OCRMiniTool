# -*- coding: utf-8 -*-
"""tests/test_build_script.py — tools/build_exe.py 的单元测试

覆盖点（全部不需要真的跑 PyInstaller、不需要 torch、不联网）：
  • version.py 解析 / 构建命令拼装 / 产物路径与自检
  • sha256、目录统计、发行 zip 结构（根目录 OCRMiniTool\\）
  • 预检拒绝条件：脏工作区、可见 torch、缺 PyInstaller；对应的绕过开关
  • main() 的退出码：0 成功 / 2 预检失败 / 3 构建失败 / 4 产物缺失
  • --dry-run 只预检不构建；--zip 产物 + SHA256
"""
import io
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_exe  # noqa: E402


class TempRepo:
    """最小仓库骨架：version.py + OCRMiniTool.spec。"""

    def __init__(self, version: str = "9.9.9"):
        self._tmp = tempfile.TemporaryDirectory(prefix="ocr_build_")
        self.root = Path(self._tmp.name)
        (self.root / "version.py").write_text(
            f'"""version.py"""\n\nAPP_VERSION = "{version}"\n', encoding="utf-8")
        (self.root / "OCRMiniTool.spec").write_text("# spec\n", encoding="utf-8")

    def close(self):
        self._tmp.cleanup()


def _fake_spec(origin: str):
    return mock.Mock(origin=origin, submodule_search_locations=None)


class VersionAndCommandTest(unittest.TestCase):
    def test_parse_version_double_quotes(self):
        self.assertEqual(build_exe.parse_version('APP_VERSION = "1.3.0"\n'), "1.3.0")

    def test_parse_version_single_quotes(self):
        self.assertEqual(build_exe.parse_version("APP_VERSION='2.0.0-beta.1'"),
                         "2.0.0-beta.1")

    def test_parse_version_missing(self):
        self.assertIsNone(build_exe.parse_version("APP_NAME = 'x'"))

    def test_read_version_from_real_repo(self):
        text = (ROOT / "version.py").read_text(encoding="utf-8")
        self.assertEqual(build_exe.read_version(ROOT),
                         build_exe.parse_version(text))

    def test_read_version_missing_file(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(build_exe.BuildError):
                build_exe.read_version(Path(d))

    def test_zip_name(self):
        self.assertEqual(build_exe.zip_name("1.3.0"),
                         "OCRMiniTool-v1.3.0-windows-x64.zip")

    def test_build_command_shape(self):
        dist = Path("C:/repo/dist")
        work = Path("C:/repo/build")
        cmd = build_exe.build_command("py.exe", Path("C:/repo"), dist, work)
        self.assertEqual(cmd[:3], ["py.exe", "-m", "PyInstaller"])
        self.assertIn("--noconfirm", cmd)
        self.assertIn("--clean", cmd)
        self.assertEqual(cmd[cmd.index("--distpath") + 1], str(dist))
        self.assertEqual(cmd[cmd.index("--workpath") + 1], str(work))
        self.assertTrue(cmd[-1].endswith("OCRMiniTool.spec"))

    def test_build_command_no_clean(self):
        cmd = build_exe.build_command("py.exe", Path("r"), Path("d"), Path("w"),
                                      clean=False)
        self.assertNotIn("--clean", cmd)

    def test_powershell_zip_command(self):
        s = build_exe.powershell_zip_command(Path("dist"),
                                             Path("dist/OCRMiniTool-v9.9.9-windows-x64.zip"))
        self.assertIn("Compress-Archive", s)
        self.assertIn("OCRMiniTool-v9.9.9-windows-x64.zip", s)


class ArtifactTest(unittest.TestCase):
    def test_artifact_problems_missing_dir(self):
        with tempfile.TemporaryDirectory() as d:
            problems = build_exe.artifact_problems(Path(d))
            self.assertEqual(len(problems), 1)
            self.assertIn("不存在", problems[0])

    def test_artifact_problems_missing_exe(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "OCRMiniTool").mkdir()
            problems = build_exe.artifact_problems(Path(d))
            self.assertTrue(any("缺少主程序" in p for p in problems))

    def test_artifact_problems_ok(self):
        with tempfile.TemporaryDirectory() as d:
            art = Path(d) / "OCRMiniTool"
            (art / "_internal").mkdir(parents=True)
            (art / "OCRMiniTool.exe").write_bytes(b"MZ")
            self.assertEqual(build_exe.artifact_problems(Path(d)), [])

    def test_sha256_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.bin"
            p.write_bytes(b"abc")
            self.assertEqual(build_exe.sha256_file(p),
                             "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_dir_stats(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "a").mkdir()
            (base / "a" / "1.txt").write_bytes(b"12345")
            (base / "2.txt").write_bytes(b"12")
            total, files = build_exe.dir_stats(base)
            self.assertEqual((total, files), (7, 2))

    def test_make_release_zip_structure(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            art = base / "dist" / "OCRMiniTool"
            (art / "_internal").mkdir(parents=True)
            (art / "OCRMiniTool.exe").write_bytes(b"MZ")
            (art / "_internal" / "x.pyd").write_bytes(b"bin")
            zip_path = base / "dist" / build_exe.zip_name("9.9.9")
            build_exe.make_release_zip(base / "dist", zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                names = set(zf.namelist())
            self.assertIn("OCRMiniTool/OCRMiniTool.exe", names)
            self.assertIn("OCRMiniTool/_internal/x.pyd", names)
            self.assertEqual(len(build_exe.sha256_file(zip_path)), 64)


class ProbeTest(unittest.TestCase):
    def test_git_dirty_parses(self):
        r = mock.Mock(returncode=0, stdout=" M a.py\n?? b.txt\n")
        with mock.patch.object(subprocess, "run", return_value=r):
            self.assertEqual(build_exe.git_dirty(Path(".")), [" M a.py", "?? b.txt"])

    def test_git_dirty_not_repo(self):
        r = mock.Mock(returncode=128, stdout="")
        with mock.patch.object(subprocess, "run", return_value=r):
            self.assertIsNone(build_exe.git_dirty(Path(".")))

    def test_git_dirty_git_missing(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError("no git")):
            self.assertIsNone(build_exe.git_dirty(Path(".")))

    def test_torch_visible_none(self):
        with mock.patch.object(build_exe.importlib.util, "find_spec",
                               return_value=None):
            self.assertIsNone(build_exe.torch_visible())

    def test_torch_visible_path(self):
        with mock.patch.object(build_exe.importlib.util, "find_spec",
                               return_value=_fake_spec("C:/v/torch/__init__.py")):
            self.assertEqual(build_exe.torch_visible(), "C:/v/torch/__init__.py")

    def test_pyinstaller_version_present(self):
        with mock.patch.object(build_exe.importlib.metadata, "version",
                               return_value="6.22.3"):
            self.assertEqual(build_exe.pyinstaller_version(), "6.22.3")

    def test_pyinstaller_version_missing(self):
        err = build_exe.importlib.metadata.PackageNotFoundError("pyinstaller")
        with mock.patch.object(build_exe.importlib.metadata, "version",
                               side_effect=err):
            self.assertIsNone(build_exe.pyinstaller_version())

    def test_missing_runtime_modules_detects_absent(self):
        def fake_find_spec(name):
            return None if name in {"webview", "transformers"} else _fake_spec(name)

        with mock.patch.object(build_exe.importlib.util, "find_spec",
                               side_effect=fake_find_spec):
            missing = build_exe.missing_runtime_modules(("webview", "numpy",
                                                         "transformers"))
        self.assertEqual(missing, ["webview", "transformers"])

    def test_missing_runtime_modules_empty_when_all_present(self):
        with mock.patch.object(build_exe.importlib.util, "find_spec",
                               return_value=_fake_spec("x")):
            self.assertEqual(build_exe.missing_runtime_modules(("webview",)), [])


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo()
        self.opts = build_exe.build_argparser().parse_args([])

    def tearDown(self):
        self.repo.close()

    def _preflight(self, dirty=None, torch=None, pi="6.22.3", opts=None,
                   missing_runtime=None):
        with mock.patch.object(build_exe, "git_dirty", return_value=dirty), \
                mock.patch.object(build_exe, "torch_visible", return_value=torch), \
                mock.patch.object(build_exe, "pyinstaller_version",
                                  return_value=pi), \
                mock.patch.object(build_exe, "missing_runtime_modules",
                                  return_value=missing_runtime or []):
            return build_exe.preflight(self.repo.root, opts or self.opts)

    def test_clean_passes(self):
        problems, _ = self._preflight(dirty=[])
        self.assertEqual(problems, [])

    def test_dirty_blocks(self):
        problems, _ = self._preflight(dirty=[" M a.py"])
        self.assertTrue(any("未提交改动" in p for p in problems))

    def test_dirty_allowed(self):
        opts = build_exe.build_argparser().parse_args(["--allow-dirty"])
        problems, _ = self._preflight(dirty=[" M a.py"], opts=opts)
        self.assertEqual(problems, [])

    def test_torch_blocks(self):
        problems, _ = self._preflight(torch="C:/v/torch/__init__.py")
        joined = " ".join(problems)
        self.assertIn("torch", joined)
        self.assertIn("构建 venv", joined)

    def test_bundled_torch_allowed(self):
        opts = build_exe.build_argparser().parse_args(["--allow-bundled-torch"])
        problems, _ = self._preflight(torch="C:/v/torch/__init__.py", opts=opts)
        self.assertEqual(problems, [])

    def test_missing_pyinstaller_blocks(self):
        problems, _ = self._preflight(pi=None)
        self.assertTrue(any("requirements-build.txt" in p for p in problems))

    def test_missing_runtime_deps_block(self):
        problems, _ = self._preflight(missing_runtime=["webview", "transformers"])
        joined = " ".join(problems)
        self.assertIn("运行时依赖", joined)
        self.assertIn("webview", joined)
        self.assertIn("transformers", joined)

    def test_git_unavailable_is_warning_only(self):
        problems, warnings = self._preflight(dirty=None)
        self.assertEqual(problems, [])
        self.assertTrue(any("无法确认工作区" in w for w in warnings))


class MainTest(unittest.TestCase):
    def setUp(self):
        self.repo = TempRepo(version="9.9.9")

    def tearDown(self):
        self.repo.close()

    def _run(self, argv, run_rc=0):
        out = io.StringIO()
        with mock.patch.object(build_exe, "git_dirty", return_value=[]), \
                mock.patch.object(build_exe, "torch_visible", return_value=None), \
                mock.patch.object(build_exe, "pyinstaller_version",
                                  return_value="6.22.3"), \
                mock.patch.object(build_exe, "missing_runtime_modules",
                                  return_value=[]), \
                mock.patch.object(build_exe, "run_build",
                                  return_value=run_rc) as rb, \
                redirect_stdout(out):
            code = build_exe.main(argv, root=self.repo.root)
        return code, out.getvalue(), rb

    def test_dry_run_does_not_build(self):
        code, out, rb = self._run(["--dry-run"])
        self.assertEqual(code, build_exe.EXIT_OK)
        self.assertIn("dry-run", out)
        self.assertIn("-m PyInstaller", out)
        rb.assert_not_called()

    def test_preflight_failure_code(self):
        out = io.StringIO()
        with mock.patch.object(build_exe, "git_dirty",
                               return_value=[" M a.py"]), \
                mock.patch.object(build_exe, "torch_visible", return_value=None), \
                mock.patch.object(build_exe, "pyinstaller_version",
                                  return_value="6.22.3"), \
                mock.patch.object(build_exe, "missing_runtime_modules",
                                  return_value=[]), \
                redirect_stdout(out):
            code = build_exe.main([], root=self.repo.root)
        self.assertEqual(code, build_exe.EXIT_PREFLIGHT)

    def test_build_failure_code(self):
        code, _, rb = self._run([], run_rc=1)
        self.assertEqual(code, build_exe.EXIT_BUILD)
        rb.assert_called_once()

    def test_artifact_missing_code(self):
        code, out, _ = self._run([])
        self.assertEqual(code, build_exe.EXIT_ARTIFACT)
        self.assertIn("产物目录不存在", out)

    def test_success_with_zip(self):
        art = self.repo.root / "dist" / "OCRMiniTool"
        (art / "_internal").mkdir(parents=True)
        (art / "OCRMiniTool.exe").write_bytes(b"MZ")
        (art / "_internal" / "a.pyd").write_bytes(b"bin")
        code, out, _ = self._run(["--zip"])
        self.assertEqual(code, build_exe.EXIT_OK)
        zpath = self.repo.root / "dist" / "OCRMiniTool-v9.9.9-windows-x64.zip"
        self.assertTrue(zpath.is_file(), out)
        self.assertIn("SHA256", out)
        self.assertEqual(len(build_exe.sha256_file(zpath)), 64)
        self.assertIn("9.9.9", out)

    def test_dry_run_prints_version_from_version_py(self):
        code, out, _ = self._run(["--dry-run"])
        self.assertEqual(code, build_exe.EXIT_OK)
        self.assertIn("9.9.9", out)


if __name__ == "__main__":
    unittest.main()

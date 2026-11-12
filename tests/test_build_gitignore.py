# -*- coding: utf-8 -*-
"""tests/test_build_gitignore.py — .gitignore 覆盖（D14）

用 `git check-ignore` 断言真实生效的规则（不靠读文本猜）：
  • 发行包 zip、PyInstaller 的 _internal/、工具链缓存、Windows 杂项应被忽略
  • 根目录 settings.json 被忽略（运行产物），但子目录同名文件**不应**被忽略
    （旧规则 `settings.json` 无前导斜杠，会误伤任意层级同名文件）
"""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")


def _is_git_repo() -> bool:
    if not GIT:
        return False
    r = subprocess.run([GIT, "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == "true"


class GitignoreTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _is_git_repo():
            raise unittest.SkipTest("不是 git 工作区（或没有 git），跳过")

    def ignored(self, rel: str) -> bool:
        r = subprocess.run([GIT, "-C", str(ROOT), "check-ignore", "-q", rel],
                           capture_output=True, text=True)
        return r.returncode == 0

    def test_ignored_patterns(self):
        cases = {
            "OCRMiniTool-v1.2.0-windows-x64.zip": "发行包 zip",
            "_internal/x.dll": "PyInstaller onedir 产物",
            "Thumbs.db": "Windows 杂项",
            "desktop.ini": "Windows 杂项",
            ".ruff_cache/x": "ruff 缓存",
            ".pytest_cache/x": "pytest 缓存",
            ".mypy_cache/x": "mypy 缓存",
            ".vscode/settings.json": "编辑器目录",
            "settings.json.bak": "设置备份（运行产物）",
            "settings.json.corrupt-20260101-000000": "损坏设置留证",
            "runtime/torch/x.py": "推理引擎组件目录",
            "dist_components/x.zip": "组件产物",
            "logs/app-202601.log": "运行日志",
        }
        for rel, why in cases.items():
            with self.subTest(rel=rel):
                self.assertTrue(self.ignored(rel), f"应被忽略（{why}）：{rel}")

    def test_root_settings_ignored_but_nested_not(self):
        self.assertTrue(self.ignored("settings.json"), "根目录 settings.json 是运行产物")
        for rel in ("webview/settings.json", "tests/settings.json"):
            with self.subTest(rel=rel):
                self.assertFalse(
                    self.ignored(rel),
                    "子目录同名文件不应被 `settings.json` 规则误伤（规则应为 /settings.json）")


if __name__ == "__main__":
    unittest.main()

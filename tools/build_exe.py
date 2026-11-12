# -*- coding: utf-8 -*-
"""tools/build_exe.py — 一条命令构建 OCRMiniTool 的 onedir 发行包（PyInstaller）

为什么需要它
------------
  • **构建必须发生在不含 torch 的解释器里**：`OCRMiniTool.spec` 顶部预检会
    `find_spec("torch")` 并直接 SystemExit（安装包不内置推理引擎，用户按显卡下载组件）。
    借用本机 `.venv`（含 torch 2.8.0）的 site-packages 走 PYTHONPATH 会让预检立刻拒绝。
  • **版本只有一个来源**：`version.py` 的 `APP_VERSION`，不手写。
  • **工作区必须干净**：脏工作区构建出的 EXE 与 tag 对不上（v1.2.0 曾如此）。
  • 构建环境与命令写在一处，避免 README 与实际漂移。

用法
----
    python tools/build_exe.py                 # 预检 + 构建 + 自检产物
    python tools/build_exe.py --dry-run       # 只预检，打印将执行的命令（不构建）
    python tools/build_exe.py --zip           # 构建后打发行 zip 并打印 SHA256
    python tools/build_exe.py --allow-dirty   # 允许脏工作区（仅本地调试）
    python tools/build_exe.py --allow-bundled-torch   # 硬绕过 spec 预检（不推荐）

退出码：0 成功；2 预检失败；3 构建失败；4 产物缺失。

只写 `dist/`、`build/`（均已 gitignore），不改任何源文件。
构建环境怎么搭（干净 venv）见 `docs/build-notes.md` §3.2。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

APP_DIR_NAME = "OCRMiniTool"          # COLLECT(name=...) 产出的目录名
SPEC_NAME = "OCRMiniTool.spec"
EXE_NAME = "OCRMiniTool.exe"
VERSION_FILE = "version.py"
DEFAULT_DIST = "dist"
DEFAULT_WORK = "build"

# 构建 venv 必须装好「运行时依赖（除 torch/torchvision 外）」：PyInstaller 只会
# 收集解释器里真实存在的包，缺一个就是产物启动即 ModuleNotFoundError
# （实测：只装 requirements-build.txt 时 certifi/PIL/transformers/fitz 全缺，
#  dist 只有 32.7MB 且 app_webview 顶层 import certifi 直接崩）。
REQUIRED_RUNTIME_MODULES = (
    "webview", "pythonnet", "clr", "clr_loader", "proxy_tools", "bottle",
    "transformers", "tokenizers", "huggingface_hub", "safetensors",
    "pymupdf", "fitz", "PIL", "numpy", "addict", "easydict", "einops",
    "tqdm", "certifi", "send2trash",
)

EXIT_OK = 0
EXIT_PREFLIGHT = 2
EXIT_BUILD = 3
EXIT_ARTIFACT = 4


class BuildError(RuntimeError):
    """构建前/构建中可预期的失败（给使用者看的消息）。"""


# ── 纯函数（可单测，不碰文件系统之外的世界）──────────────────────────
def parse_version(text: str) -> str | None:
    """从 version.py 文本里取 APP_VERSION；找不到返回 None。"""
    import re
    m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', text)
    return m.group(1).strip() if m else None


def read_version(root: Path = ROOT) -> str:
    path = Path(root) / VERSION_FILE
    if not path.is_file():
        raise BuildError(f"找不到版本文件：{path}")
    version = parse_version(path.read_text(encoding="utf-8", errors="replace"))
    if not version:
        raise BuildError(f"{VERSION_FILE} 里没有 APP_VERSION")
    return version


def zip_name(version: str) -> str:
    return f"OCRMiniTool-v{version}-windows-x64.zip"


def artifact_dir(distpath: Path) -> Path:
    return Path(distpath) / APP_DIR_NAME


def artifact_exe(distpath: Path) -> Path:
    return artifact_dir(distpath) / EXE_NAME


def artifact_problems(distpath: Path) -> list[str]:
    """产物自检：返回硬失败列表（空 = 通过）。"""
    d = artifact_dir(distpath)
    problems: list[str] = []
    if not d.is_dir():
        problems.append(f"产物目录不存在：{d}")
        return problems
    exe = d / EXE_NAME
    if not exe.is_file():
        problems.append(f"缺少主程序：{exe}")
    if not (d / "_internal").is_dir():
        problems.append(f"缺少 PyInstaller 6 的 _internal 目录：{d / '_internal'}")
    return problems


def build_command(python: str, root: Path, distpath: Path, workpath: Path,
                  clean: bool = True) -> list[str]:
    cmd = [python, "-m", "PyInstaller", "--noconfirm"]
    if clean:
        cmd.append("--clean")
    cmd += ["--distpath", str(distpath), "--workpath", str(workpath),
            str(Path(root) / SPEC_NAME)]
    return cmd


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dir_stats(path: Path) -> tuple[int, int]:
    """返回 (总字节数, 文件数)。"""
    total = 0
    files = 0
    for base, _dirs, names in os.walk(path):
        for n in names:
            p = Path(base) / n
            try:
                total += p.stat().st_size
                files += 1
            except OSError:
                continue
    return total, files


def human_mib(n: int) -> str:
    return f"{n / 1_048_576:.1f} MiB"


def powershell_zip_command(distpath: Path, zip_path: Path) -> str:
    """给没加 --zip 的人用的等价压缩命令（发行 zip 根是 OCRMiniTool\\ 文件夹）。"""
    return ('powershell -NoProfile -Command "Compress-Archive -Path '
            f"'{Path(distpath) / APP_DIR_NAME}' -DestinationPath '{zip_path}' -Force\"")


# ── 环境探测（测试里会被 mock）──────────────────────────────────────
def git_dirty(root: Path) -> list[str] | None:
    """工作区未提交改动列表；None = 无法判断（无 git 或不在仓库里）。"""
    try:
        r = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "-uall"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return [line for line in r.stdout.splitlines() if line.strip()]


def torch_visible() -> str | None:
    """当前解释器能否找到 torch；返回其源路径（找不到 = None）。

    只做 find_spec，不 import torch（构建前不应加载 2GB 的 torch DLL）。
    """
    try:
        spec = importlib.util.find_spec("torch")
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    return str(spec.origin or spec.submodule_search_locations or "torch")


def pyinstaller_version() -> str | None:
    try:
        return importlib.metadata.version("pyinstaller")
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def missing_runtime_modules(modules: tuple[str, ...] = REQUIRED_RUNTIME_MODULES) -> list[str]:
    """构建解释器里缺失的运行时依赖（torch 除外，torch 必须缺席）。"""
    missing: list[str] = []
    for name in modules:
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(name)
    return missing


# ── 预检 ────────────────────────────────────────────────────────────
def preflight(root: Path, opts: argparse.Namespace) -> tuple[list[str], list[str]]:
    """返回 (problems, warnings)；problems 非空即拒绝构建。"""
    problems: list[str] = []
    warnings: list[str] = []

    if not (Path(root) / SPEC_NAME).is_file():
        problems.append(f"找不到 spec：{Path(root) / SPEC_NAME}")

    py = sys.version_info
    if (py.major, py.minor) != (3, 12):
        warnings.append(
            f"当前解释器是 Python {py.major}.{py.minor}，项目按 3.12 开发/打包；"
            "产物未在这个版本上验证过。")

    dirty = git_dirty(root)
    if dirty is None:
        warnings.append("无法确认工作区是否干净（没装 git 或不在 git 仓库）——"
                        "请自行确保产物与提交/tag 对应。")
    elif dirty and not opts.allow_dirty:
        head = "、".join(x[:60] for x in dirty[:5])
        problems.append(
            f"工作区有 {len(dirty)} 项未提交改动（{head}…）。"
            "从脏工作区构建出的 EXE 无法与 tag 对应；请先提交/暂存，"
            "或仅本地调试时加 --allow-dirty。")

    origin = torch_visible()
    if origin and not opts.allow_bundled_torch:
        problems.append(
            f"当前解释器能看到 torch（{origin}）——{SPEC_NAME} 顶部预检会拒绝打包。"
            "请用**不含 torch 的构建 venv**（docs/build-notes.md §3.2），"
            "不要用 PYTHONPATH 借用 .venv；确需硬绕过加 --allow-bundled-torch。")

    if pyinstaller_version() is None:
        problems.append(
            "当前解释器没有 PyInstaller：先 `pip install -r requirements-build.txt`，"
            "或按 docs/build-notes.md §3.2 建一个干净构建 venv。")

    missing = missing_runtime_modules()
    if missing:
        problems.append(
            "构建 venv 缺运行时依赖："
            + "、".join(missing)
            + "。PyInstaller 只收集解释器里装了的包，缺了产物启动即 "
            "ModuleNotFoundError。安装方式：requirements-lock.txt 去掉 "
            "torch/torchvision 两行后 pip install（docs/build-notes.md §3.2）。")

    return problems, warnings


# ── 构建 / 自检 ─────────────────────────────────────────────────────
def run_build(cmd: list[str], root: Path, opts: argparse.Namespace) -> int:
    env = os.environ.copy()
    if opts.allow_bundled_torch:
        env["OCRMIT_ALLOW_BUNDLED_TORCH"] = "1"
    print(f"[build] 执行：{' '.join(cmd)}", flush=True)
    try:
        r = subprocess.run(cmd, cwd=str(root), env=env)
    except OSError as e:
        print(f"[build] 无法启动 PyInstaller：{e}")
        return 1
    return r.returncode


def make_release_zip(distpath: Path, zip_path: Path) -> Path:
    """把 dist\\OCRMiniTool\\ 打成 zip（归档根目录 = OCRMiniTool\\）。"""
    src = artifact_dir(distpath)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        for base, _dirs, names in os.walk(src):
            for n in sorted(names):
                p = Path(base) / n
                arc = Path(APP_DIR_NAME) / p.relative_to(src)
                zf.write(p, arc.as_posix())
    return zip_path


def _say(msg: str = ""):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:      # 极老控制台（cp437 等）：降级不炸
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


def _configure_stdio() -> None:
    """保证中文/路径在 GBK 控制台下也不抛错（构建脚本不该因 print 失败）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="构建 OCRMiniTool onedir 发行包（PyInstaller）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法\n----")[-1].strip())
    ap.add_argument("--dry-run", action="store_true",
                    help="只做预检并打印将执行的命令，不构建")
    ap.add_argument("--zip", action="store_true",
                    help="构建后打发行 zip 并打印 SHA256")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="允许脏工作区（仅本地调试；破坏产物与 tag 的对应关系）")
    ap.add_argument("--allow-bundled-torch", action="store_true",
                    help="把 OCRMIT_ALLOW_BUNDLED_TORCH=1 传给构建（不推荐）")
    ap.add_argument("--no-clean", action="store_true",
                    help="不传 --clean（复用 build/ 缓存；默认清理）")
    ap.add_argument("--distpath", default=DEFAULT_DIST,
                    help=f"产物目录（默认 {DEFAULT_DIST}/，相对仓库根）")
    ap.add_argument("--workpath", default=DEFAULT_WORK,
                    help=f"PyInstaller 工作目录（默认 {DEFAULT_WORK}/）")
    return ap


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    _configure_stdio()
    opts = build_argparser().parse_args(argv)
    root = Path(root).resolve()
    distpath = (root / opts.distpath).resolve()
    workpath = (root / opts.workpath).resolve()

    try:
        version = read_version(root)
    except BuildError as e:
        _say(f"[FAIL] {e}")
        return EXIT_PREFLIGHT

    problems, warnings = preflight(root, opts)
    for w in warnings:
        _say(f"[WARN] {w}")
    if problems:
        for p in problems:
            _say(f"[FAIL] {p}")
        _say("[FAIL] 预检未通过，未执行构建。")
        return EXIT_PREFLIGHT

    cmd = build_command(sys.executable, root, distpath, workpath,
                        clean=not opts.no_clean)
    _say(f"[build] 版本：{version}（来自 {VERSION_FILE}）")
    _say(f"[build] 解释器：{sys.executable}")
    _say(f"[build] PyInstaller：{pyinstaller_version()}")
    _say(f"[build] 产物目录：{distpath}")
    _say(f"[build] 工作目录：{workpath}")

    if opts.dry_run:
        _say(f"[build] dry-run，将执行：{' '.join(cmd)}")
        _say("[build] 预检通过；未构建（去掉 --dry-run 才会真的打包）。")
        return EXIT_OK

    started = time.monotonic()
    rc = run_build(cmd, root, opts)
    elapsed = time.monotonic() - started
    if rc != 0:
        _say(f"[FAIL] PyInstaller 退出码 {rc}（耗时 {elapsed:.0f}s）。")
        return EXIT_BUILD

    problems = artifact_problems(distpath)
    if problems:
        for p in problems:
            _say(f"[FAIL] {p}")
        return EXIT_ARTIFACT

    exe = artifact_exe(distpath)
    total, files = dir_stats(artifact_dir(distpath))
    _say(f"[build] 构建完成：{elapsed:.0f}s")
    _say(f"[build] exe ：{exe}（{human_mib(exe.stat().st_size)}，"
         f"sha256={sha256_file(exe)}）")
    _say(f"[build] dist：{human_mib(total)}，{files} 个文件")

    zip_path = distpath / zip_name(version)
    if opts.zip:
        make_release_zip(distpath, zip_path)
        _say(f"[build] zip ：{zip_path}（{human_mib(zip_path.stat().st_size)}）")
        _say(f"[build] SHA256：{sha256_file(zip_path)}  {zip_name(version)}")
    else:
        _say(f"[build] 发行 zip 未生成；可执行：\n  "
             f"{powershell_zip_command(distpath, zip_path)}\n"
             "  或加 --zip 让脚本直接产出 zip 并打印 SHA256")

    lowered = str(root).lower()
    if lowered.startswith("d:\\project"):
        _say("[WARN] 产物位于受管工作区（D:\\Project）：新文件带 Low 完整性标签，"
             ".NET 可能拒载 pythonnet/WebView2 程序集。冒烟前请把 "
             f"{artifact_dir(distpath)} 拷出工作区（如 %LOCALAPPDATA%\\OCRMiniTool-smoke），"
             "或 icacls /setintegritylevel M /t 抬升标签。")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

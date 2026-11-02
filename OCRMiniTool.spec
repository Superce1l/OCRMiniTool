# -*- mode: python ; coding: utf-8 -*-
# OCRMiniTool — PyInstaller onedir spec
#
# 2.2.9 式改造（v1.1.2）：onefile → onedir。onefile 每次启动把整个运行时
# （数百 MB，含 torch）解压到 %TEMP%\_MEIxxxx——启动慢 10～30 秒、C 盘被占、
# 进程被强杀还留垃圾；onedir 的文件直接躺在 exe 旁（PyInstaller 6 的
# _internal\ 布局），零解压。
#
# 当初选 onefile 的两个理由的现状：
#   ① 终端用户的 MOTW（zip 解压的 Zone.Identifier 让 .NET 拒载程序集 →
#      pywebview 炸）：motw_unblock.py 启动自清解决——在任何 clr 载入前
#      DeleteFileW("<f>:Zone.Identifier")，用户目录可写、无需管理员。
#   ② 受管工作区（D:\Project）新建文件的低完整性标签：开发机现象，与
#      打包布局无关（源码运行同样中招），解法见 run.bat 顶部注释
#      （icacls /setintegritylevel M，或把 dist 拷到工作区外运行）。
#
# 另注意：exe 应放在 models/ 的父目录运行（BASE_DIR = exe 所在目录）；
# 发行 zip 压整个 OCRMiniTool\ 文件夹，解压即用。
from PyInstaller.utils.hooks import collect_data_files, collect_all, collect_submodules

import os

# ── 构建预检（issue #3 的最终答案）───────────────────────────────
# 安装包本体不含 torch/torchvision（连 CPU 版也不带）：使用者按硬件在
# 「模型」页下载推理引擎组件（cpu / cu128，见 downloader._TORCH_FLAVORS
# 与 build_components.py）。这条预检防止构建 venv 里的 torch 悄悄混进包：
#   • 正确姿势：构建 venv 不装 torch（或设 OCRMIT_ALLOW_BUNDLED_TORCH=1
#     硬绕过——Analysis 仍会排除，只为兼容旧构建脚本）。
#   • 历史：v1.1.2 把构建机默认源装来的 CPU 版 torch 打进包（4.4G 的
#     cu128 全量包是 issue #3 修复过程的中间产物，已废弃）。
import importlib.util as _ilu
if _ilu.find_spec("torch") and os.environ.get("OCRMIT_ALLOW_BUNDLED_TORCH") != "1":
    raise SystemExit(
        "[OCRMiniTool] 拒绝打包：构建 venv 里装着 torch——安装包不再内置 "
        "torch（用户在模型页按硬件自选下载）。请在不带 torch 的环境打包，"
        "或设 OCRMIT_ALLOW_BUNDLED_TORCH=1 硬绕过（仍会被排除）。")

datas = [
    ('assets/icon.ico', '.'),
    ('webview', 'webview_assets'),
]
binaries = []
hiddenimports = [
    'certifi', 'webview',
    'webview.platforms.edgechromium', 'webview.platforms.winforms',
    'clr', 'clr_loader', 'pythonnet', 'proxy_tools',
    # 模型自定义代码（trust_remote_code 运行时导入，PyInstaller 分析不到）
    'addict', 'easydict', 'einops', 'tqdm',
]
datas += collect_data_files('certifi')
for pkg in ('webview', 'pythonnet', 'clr_loader', 'tokenizers', 'pymupdf'):
    tmp = collect_all(pkg)
    datas += tmp[0]; binaries += tmp[1]; hiddenimports += tmp[2]
# 模型代码走 transformers auto 映射/家族导入懒加载模型子模块（llama、
# deepseek_v3、qwen3……），transformers 钩子不收录 → 运行时 No module
# named。直接收录 transformers.models 全部子模块，一劳永逸。
hiddenimports += collect_submodules('transformers.models')
# torch 运行时组件（runtime/，非打包）会用到的 stdlib 冷门模块：torch 被
# 排除后 PyInstaller 看不到它的 import 图，stdlib 只打包应用自身用到的。
# 组件在冻结环境里 import torch 时就会缺件（首个翻车案例：timeit——
# torch/_strobelight/cli_function_profiler.py 顶层 import）。宁可多列，
# 每个几十 KB。
hiddenimports += [
    'timeit', 'shlex', 'getopt', 'fnmatch', 'glob', 'pickle', 'copyreg',
    'shutil', 'zipfile', 'gzip', 'bz2', 'lzma', 'tarfile', 'sqlite3',
    'csv', 'configparser', 'uuid', 'ipaddress', 'secrets', 'base64',
    'binascii', 'struct', 'array', 'queue', 'selectors', 'socket',
    'dataclasses', 'numbers', 'decimal', 'fractions', 'random', 'bisect',
    'heapq', 'textwrap', 'string', 'inspect', 'linecache', 'tokenize',
    'token', 'keyword', 'dis', 'difflib', 'weakref', 'contextvars',
    'platform', 'tempfile',
    # unittest.mock：torch/distributed/_config_module 顶层 `from unittest
    # import mock`；PyInstaller 只收 unittest 本体不带 mock 子模块
    'unittest', 'unittest.mock', 'unittest.async_case',
    # torch/transformers/torchvision 全量 import 图扫描后 PYZ/base 仍缺的
    # stdlib（stdlib_scan 法：无 torch 的 venv 里导入 runtime 组件，收集
    # sys.modules ∩ stdlib，与 PYZ-00.toc/base_library.zip 差集）。select/
    # struct/unicodedata/pyexpat 是 C 扩展 .pyd，hiddenimports 会一并收二进制。
    'pickletools', 'select', 'struct', 'unicodedata', 'pyexpat',
    'cProfile', 'profile', 'pstats', 'modulefinder',
]

a = Analysis(
    ['app_webview.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['runtime_hook_utf8.py'],
    # torch/torchvision 一律不打包：ocr_engine 在运行时把 runtime/ 组件
    # 目录插到 sys.path 最前（见 downloader._TORCH_FLAVORS）。这里显式
    # 排除，防止经 transformers 等模块的静态 import 引用被顺手收集。
    excludes=['torch', 'torchvision', 'torchgen', 'functorch'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # onedir：二进制／数据交给下面的 COLLECT
    name='OCRMiniTool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/icon.ico'],
)

# onedir 目录包：EXE 旁 + _internal\（PyInstaller 6 布局）。
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='OCRMiniTool',
    icon=['assets/icon.ico'],
)

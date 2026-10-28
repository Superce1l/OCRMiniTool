# -*- mode: python ; coding: utf-8 -*-
# OCRMiniTool — PyInstaller onefile spec
#
# 为什么 onefile 而非 onedir：受管工作区（D:\Project）里新建的文件会继承
# 「低完整性」NTFS 标签，.NET 拒绝加载低完整性程序集 → onedir 的散装
# pythonnet/clr_loader DLL 会在运行时炸掉 pywebview。onefile 把全部文件
# 打进单个 exe，运行时解压到 %TEMP%\_MEIxxxx（用户进程创建，无标签）。
# 另注意：exe 应放在 models/ 的父目录运行（BASE_DIR = exe 所在目录）。
from PyInstaller.utils.hooks import collect_data_files, collect_all, collect_submodules, collect_dynamic_libs

import os
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
# torchvision 0.29+ 的 C++ 扩展改名 _C_stable.pyd / image_stable.pyd（stable
# ABI 版），PyInstaller 钩子仍找旧名 _C → 不会收集 → 运行时报
# "operator torchvision::nms does not exist"。显式补上扩展与图像 DLL。
import importlib.util as _ilu
_tv_dir = os.path.dirname(_ilu.find_spec('torchvision').origin)
for _pyd in ('_C_stable.pyd', 'image_stable.pyd'):
    _p = os.path.join(_tv_dir, _pyd)
    if os.path.exists(_p):
        binaries.append((_p, 'torchvision'))
binaries += collect_dynamic_libs('torchvision')

a = Analysis(
    ['app_webview.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['runtime_hook_utf8.py'],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='OCRMiniTool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets/icon.ico'],
)

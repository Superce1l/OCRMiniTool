# -*- mode: python ; coding: utf-8 -*-
# OCRMiniTool — onedir 测试构建（仅用于本机冒烟测试，不作为交付物）
# onedir 不需要 onefile 的解压临时目录，可在沙箱内直接运行；
# 拷贝到工作区外（如 D:\ocrtest）运行可避开低完整性标签，完整验证原生窗口。
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
    'addict', 'easydict', 'einops', 'tqdm',
]
datas += collect_data_files('certifi')
for pkg in ('webview', 'pythonnet', 'clr_loader', 'tokenizers', 'pymupdf'):
    tmp = collect_all(pkg)
    datas += tmp[0]; binaries += tmp[1]; hiddenimports += tmp[2]
hiddenimports += collect_submodules('transformers.models')
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
    [],
    exclude_binaries=True,
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

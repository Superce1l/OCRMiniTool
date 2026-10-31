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

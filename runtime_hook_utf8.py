# runtime_hook_utf8.py
# PyInstaller runtime hook — runs before ANY user code or imports.
#
# Set PYTHONUTF8=1: 简繁中文 Windows（cp936/cp950）下，第三方库不指定
# encoding 的 open() 会用系统默认编码读 UTF-8 文件而炸
# UnicodeDecodeError。等价于 python -X utf8，须在解释器初始化前生效。
import os
import sys

os.environ["PYTHONUTF8"] = "1"

# ── SSL CA 凭证修复（冻结 EXE 特有）──────────────────────────────────
# 冻结 EXE 中 ssl 模块可能找不到 CA bundle，HTTPS 下载报
# SSLCertVerificationError。优先用 certifi 的 cacert.pem。
if getattr(sys, "frozen", False):
    try:
        import certifi
        ca = certifi.where()
        os.environ.setdefault("SSL_CERT_FILE", ca)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", ca)
    except ImportError:
        pass   # 缺 certifi 时由 downloader 的证书降级逻辑兜底

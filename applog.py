"""applog.py — 轻量应用日志（识别/下载/模型加载事件 → logs/ 目录）

设计：
  • 单文件模块、无第三方依赖；GUI 与脚本共用。
  • 日志目录：<程序目录>/logs/（frozen 时 EXE 旁；开发时项目根）。
  • 按月分文件（app-YYYYMM.log），UTF-8，逐行追加；同月重复打开复用句柄。
  • 行格式：`2026-10-27 16:20:33 [INFO] 识别完成 …`。
  • 写日志永不抛异常——磁盘满/权限问题静默忽略，绝不影响主流程。
"""
from __future__ import annotations

import datetime
import sys
import threading
import traceback
from pathlib import Path

_LOCK = threading.Lock()
_DIR: Path | None = None
_CURRENT_MONTH: str = ""
_FH = None


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _get_fh():
    """打开（或复用）当前月份的日志文件句柄；失败回 None。线程安全。"""
    global _DIR, _CURRENT_MONTH, _FH
    now = datetime.datetime.now()
    month = now.strftime("%Y%m")
    with _LOCK:
        if _FH is not None and month == _CURRENT_MONTH:
            return _FH
        try:
            if _FH is not None:
                try:
                    _FH.close()
                except Exception:
                    pass
                _FH = None
            if _DIR is None:
                _DIR = _base_dir() / "logs"
            _DIR.mkdir(parents=True, exist_ok=True)
            _FH = open(_DIR / f"app-{month}.log", "a", encoding="utf-8")
            _CURRENT_MONTH = month
            return _FH
        except Exception:
            _FH = None
            return None


def _write(level: str, msg: str):
    fh = _get_fh()
    if fh is None:
        return
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _LOCK:
            fh.write(f"{ts} [{level}] {msg}\n")
            fh.flush()
    except Exception:
        pass


def log_info(msg: str):
    _write("INFO", msg)


def log_warn(msg: str):
    _write("WARN", msg)


def log_error(msg: str, exc: BaseException | None = None):
    if exc is not None:
        msg = f"{msg}：{type(exc).__name__}: {exc}"
        tb = "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__)).rstrip()
        msg += "\n    " + tb.replace("\n", "\n    ")
    _write("ERROR", msg)


def log_download(msg: str):
    """下载事件（开始/完成/重试），独立标记便于检索。"""
    _write("DOWN", msg)


def log_ocr(msg: str):
    """识别事件（开始/完成/失败）。"""
    _write("OCR", msg)


def log_model(msg: str):
    """模型加载事件。"""
    _write("MODEL", msg)

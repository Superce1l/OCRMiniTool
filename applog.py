"""applog.py — 轻量应用日志（识别/下载/模型加载事件 → logs/ 目录）

设计：
  • 单文件模块、无第三方依赖；GUI 与脚本共用。
  • 日志目录：<程序目录>/logs/（frozen 时 EXE 旁；开发时项目根）。
  • 按月分文件（app-YYYYMM.log），UTF-8，逐行追加；同月重复打开复用句柄。
  • 单文件超过 MAX_BYTES（默认 8 MiB）时轮转：app-YYYYMM.log → app-YYYYMM.1.log，
    旧档依次后移，最多保留 MAX_BACKUPS（默认 3）份（.1 最新、.3 最旧）。
    轮转失败（文件被占用/只读/磁盘满）静默降级为继续追加，绝不抛异常。
  • 行格式：`2026-10-27 16:20:33 [INFO] [pid=1234 MainThread] 识别完成 …`
    （pid 与线程名便于多开/并发排查；线程名超过 _THREAD_NAME_MAX 截断）。
  • 写日志永不抛异常——磁盘满/权限问题静默忽略，绝不影响主流程。
"""
from __future__ import annotations

import datetime
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import IO

_LOCK = threading.RLock()
_DIR: Path | None = None
_CURRENT_MONTH = ""
_FH: IO[str] | None = None
_FH_BYTES = 0
_ROTATE_FAILED_MONTH = ""      # 本月轮转已失败 → 本月不再重试，继续追加

# 轮转阈值/份数（configure() 可覆盖：测试用；应用本身不改）
MAX_BYTES = 8 * 1024 * 1024
MAX_BACKUPS = 3

# 线程名截断长度：除 MainThread 外还有 ocr-ticker 等任务线程，名字可能很长
_THREAD_NAME_MAX = 24


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def configure(*, log_dir: Path | str | None = None,
              max_bytes: int | None = None,
              max_backups: int | None = None) -> None:
    """重定向日志目录 / 调整轮转阈值（测试与脚本用；应用运行期不调用）。"""
    global _DIR, MAX_BYTES, MAX_BACKUPS
    with _LOCK:
        _close_locked()
        if log_dir is not None:
            _DIR = Path(log_dir)
        if max_bytes is not None:
            MAX_BYTES = max(1, int(max_bytes))
        if max_backups is not None:
            MAX_BACKUPS = max(0, int(max_backups))


def close() -> None:
    """关闭当前句柄（测试清理 / 进程退出兜底）。永不抛异常。"""
    with _LOCK:
        _close_locked()


def _close_locked() -> None:
    global _FH, _CURRENT_MONTH, _FH_BYTES
    if _FH is not None:
        try:
            _FH.close()
        except Exception:
            pass
    _FH = None
    _CURRENT_MONTH = ""
    _FH_BYTES = 0


def _month() -> str:
    return datetime.datetime.now().strftime("%Y%m")


def _open_locked(month: str) -> IO[str] | None:
    """确保 _DIR 存在并追加打开当月日志；失败回 None。调用方须持锁。"""
    global _DIR, _FH, _CURRENT_MONTH, _FH_BYTES
    try:
        if _DIR is None:
            _DIR = _base_dir() / "logs"
        _DIR.mkdir(parents=True, exist_ok=True)
        path = _DIR / f"app-{month}.log"
        fh = open(path, "a", encoding="utf-8")
        try:
            _FH_BYTES = path.stat().st_size
        except OSError:
            _FH_BYTES = 0
        _FH = fh
        _CURRENT_MONTH = month
        return fh
    except Exception:
        _FH = None
        _FH_BYTES = 0
        return None


def _get_fh() -> IO[str] | None:
    """打开（或复用）当前月份的日志文件句柄；失败回 None。线程安全。"""
    with _LOCK:
        month = _month()
        if _FH is not None and month == _CURRENT_MONTH:
            return _FH
        _close_locked()
        return _open_locked(month)


def _rotate_locked(month: str) -> IO[str] | None:
    """当月日志轮转：base → .1，旧档后移，最多 MAX_BACKUPS 份。调用方须持锁。"""
    global _ROTATE_FAILED_MONTH
    if MAX_BACKUPS <= 0:
        return _FH
    try:
        assert _DIR is not None
        _close_locked()
        for i in range(MAX_BACKUPS - 1, 0, -1):
            src = _DIR / f"app-{month}.{i}.log"
            if src.exists():
                os.replace(src, _DIR / f"app-{month}.{i + 1}.log")
        base = _DIR / f"app-{month}.log"
        if base.exists():
            os.replace(base, _DIR / f"app-{month}.1.log")
        _ROTATE_FAILED_MONTH = ""
        return _open_locked(month)
    except Exception:
        # 降级：本月不再尝试轮转，重新打开原文件继续追加
        _ROTATE_FAILED_MONTH = month
        return _open_locked(month)


def _thread_tag() -> str:
    """`[pid=1234 MainThread]`：进程 + 线程上下文（多开/并发排查用）。"""
    name = threading.current_thread().name or "MainThread"
    if len(name) > _THREAD_NAME_MAX:
        name = name[:_THREAD_NAME_MAX]
    return f"[pid={os.getpid()} {name}]"


def _write(level: str, msg: str):
    global _FH_BYTES
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        tag = _thread_tag()
    except Exception:
        tag = "[pid=? ?]"
    line = f"{ts} [{level}] {tag} {msg}\n"
    # 文本模式在 Windows 会把 \n 写成 \r\n，按实际落盘字节数估算
    nbytes = len(line.encode("utf-8")) + line.count("\n")
    try:
        with _LOCK:
            fh = _get_fh()
            if fh is None:
                return
            month = _CURRENT_MONTH
            if (month and month != _ROTATE_FAILED_MONTH
                    and _FH_BYTES > 0 and _FH_BYTES + nbytes > MAX_BYTES):
                fh = _rotate_locked(month)
                if fh is None:
                    return
            fh.write(line)
            fh.flush()
            _FH_BYTES += nbytes
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


def log_startup(extra: str = "") -> None:
    """进程启动头（C11）：版本 / pid / python / frozen / 平台 / cwd（+ extra）。

    version / platform / sys 在函数内 import：applog 位于启动路径最前端，
    模块级 import 它们会拖慢冷启动，也容易和 version.py 形成循环依赖。
    本函数绝不抛异常——日志不可用不能影响启动。
    """
    try:
        import platform
        import sys as _sys

        try:
            from version import APP_VERSION as _ver
        except Exception:
            _ver = "?"
        parts = [
            f"==== 启动 v{_ver}",
            f"pid={os.getpid()}",
            f"python={_sys.version.split()[0]}",
            f"frozen={'是' if getattr(_sys, 'frozen', False) else '否'}",
        ]
        try:
            parts.append(platform.platform())
        except Exception:
            parts.append("platform=?")
        try:
            parts.append(f"cwd={os.getcwd()}")
        except Exception:
            parts.append("cwd=?")
        if extra:
            parts.append(str(extra))
        log_info(" · ".join(parts) + " ====")
    except Exception:
        pass

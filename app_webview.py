"""app_webview.py — OCRMiniTool 桌面启动器（本机 HTTP server + 原生 WebView2 窗口）

与 STTMiniTool 同一架构：pywebview 只加载本机网址（不用 js_api，规避
pythonnet 序列化递归），前端经 HTTP/SSE 与 server 通讯。WebView2 不可用时
fallback：系统 Edge --app 无痕窗口 → 默认浏览器。

启动顺序：起本机 server（只绑 127.0.0.1）→ 模型已下载则背景自动加载 →
开原生窗口。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
import webbrowser
from ctypes import wintypes
from pathlib import Path

from i18n import tr
from proc_guard import setup_kill_on_close_job
from webview_server import WebViewServer

APP_NAME = "OCR MiniTool"     # main() 里按 settings.json 的 lang 经 i18n 重设
WIN_W, WIN_H = 1024, 720
MIN_W, MIN_H = 900, 640


# ════════════════════════════════════════════════════════
# 屏幕适配：启动时按工作区收敛窗口尺寸（与 STTMiniTool 同一套实现）
# ════════════════════════════════════════════════════════
_SPI_GETWORKAREA = 0x0030
_BOTTOM_MARGIN = 8


def _workarea_logical() -> tuple[int, int, int, int]:
    """主屏工作区（任务栏以外），逻辑像素，回 (x, y, w, h)。"""
    if sys.platform != "win32":
        return (0, 0, 0, 0)
    try:
        from ctypes import wintypes
        u = ctypes.windll.user32
        rect = wintypes.RECT()
        if not u.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return (0, 0, 0, 0)
        return (rect.left, rect.top,
                rect.right - rect.left,
                max(0, (rect.bottom - rect.top) - _BOTTOM_MARGIN))
    except Exception:
        return (0, 0, 0, 0)


def fit_window_to_screen(w: int, h: int, min_w: int, min_h: int):
    wa_x, wa_y, wa_w, wa_h = _workarea_logical()
    if wa_w > 0 and wa_h > 0:
        w, h = min(w, wa_w), min(h, wa_h)
        min_w, min_h = min(min_w, w), min(min_h, h)
        x = wa_x + max(0, (wa_w - w) // 2)
        y = wa_y + max(0, (wa_h - h) // 2)
    else:
        x = y = None
    return x, y, w, h, min_w, min_h


# ════════════════════════════════════════════════════════
# 标题栏配色 + 窗口图标（与 STTMiniTool 同一套 Win32 手法）
# ════════════════════════════════════════════════════════
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36
_COLOR_WHITE = 0x00FFFFFF
_COLOR_BLACK = 0x00000000
_COLOR_DARK_BG = 0x00261D1E      # COLORREF 0x00BBGGRR ← #1E1D26

_initial_theme = "light"
_BACKEND_REF = [None]


def _os_dark() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return v == 0
    except Exception:
        return False


def _resolve_dark(theme: str) -> bool:
    if theme == "dark":
        return True
    if theme == "light":
        return False
    return _os_dark()


def _set_titlebar(hwnd: int, dark: bool) -> bool:
    if not hwnd or sys.platform != "win32":
        return False
    try:
        dwm = ctypes.windll.dwmapi
    except Exception:
        return False

    def _set(attr: int, value: int) -> int:
        v = ctypes.c_int(value)
        return dwm.DwmSetWindowAttribute(
            wintypes_hwnd(hwnd), attr, ctypes.byref(v), ctypes.sizeof(v))

    cap = _COLOR_DARK_BG if dark else _COLOR_WHITE
    txt = _COLOR_WHITE if dark else _COLOR_BLACK
    ok = False
    try:
        _set(_DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if dark else 0)
    except Exception:
        pass
    try:
        if _set(_DWMWA_CAPTION_COLOR, cap) == 0:
            ok = True
        _set(_DWMWA_TEXT_COLOR, txt)
    except Exception:
        pass
    return ok


def apply_titlebar_theme(theme: str):
    dark = _resolve_dark(theme or "light")
    hwnd = _find_app_hwnd()
    if hwnd:
        _set_titlebar(hwnd, dark)


def wintypes_hwnd(hwnd: int):
    return ctypes.c_void_p(int(hwnd))


def _find_app_hwnd() -> int:
    if sys.platform != "win32":
        return 0
    try:
        return int(ctypes.windll.user32.FindWindowW(None, APP_NAME) or 0)
    except Exception:
        return 0


_APP_USER_MODEL_ID = "OCRMiniTool"
_WM_SETICON = 0x0080
_ICON_SMALL, _ICON_BIG = 0, 1
_IMAGE_ICON = 1
_LR_LOADFROMFILE, _LR_DEFAULTSIZE = 0x00000010, 0x00000040


def set_app_user_model_id():
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_APP_USER_MODEL_ID)
    except Exception:
        pass


def _resolve_icon_path() -> Path | None:
    cands = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cands.append(Path(meipass) / "icon.ico")
        cands.append(Path(sys.executable).parent / "icon.ico")
    cands.append(Path(__file__).resolve().parent / "assets" / "icon.ico")
    for c in cands:
        if c and c.exists():
            return c
    return None


def _set_window_icon(hwnd: int, ico: Path) -> bool:
    if not hwnd or sys.platform != "win32":
        return False
    try:
        u = ctypes.windll.user32
        u.LoadImageW.restype = wintypes.HANDLE
        u.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.SendMessageW.restype = ctypes.c_ssize_t
        u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
        path = str(ico)
        big = u.LoadImageW(None, path, _IMAGE_ICON, 0, 0,
                           _LR_LOADFROMFILE | _LR_DEFAULTSIZE)
        small = u.LoadImageW(None, path, _IMAGE_ICON, 16, 16, _LR_LOADFROMFILE)
        h = wintypes_hwnd(hwnd)
        if big:
            u.SendMessageW(h, _WM_SETICON, _ICON_BIG, big)
        if small:
            u.SendMessageW(h, _WM_SETICON, _ICON_SMALL, small)
        return bool(big or small)
    except Exception:
        return False


def _decorate_window_async():
    ico = _resolve_icon_path()
    dark = _resolve_dark(_initial_theme)

    def worker():
        themed = False
        iconed = ico is None
        for _ in range(40):
            hwnd = _find_app_hwnd()
            if hwnd:
                if not themed:
                    themed = _set_titlebar(hwnd, dark)
                if not iconed:
                    iconed = _set_window_icon(hwnd, ico)
                if themed and iconed:
                    return
            time.sleep(0.15)
    threading.Thread(target=worker, name="window-decorate", daemon=True).start()


# ════════════════════════════════════════════════════════
# WebView2 Runtime 检测（缺失时 pywebview 会卡空白窗，须提前绕开）
# ════════════════════════════════════════════════════════
_WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_available() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    for hive, path in (
        (winreg.HKEY_LOCAL_MACHINE,
         rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT}"),
        (winreg.HKEY_LOCAL_MACHINE,
         rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT}"),
        (winreg.HKEY_CURRENT_USER,
         rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_CLIENT}"),
    ):
        try:
            with winreg.OpenKey(hive, path) as k:
                pv, _ = winreg.QueryValueEx(k, "pv")
                if pv and pv not in ("", "0.0.0.0"):
                    return True
        except OSError:
            continue
    return False


# ════════════════════════════════════════════════════════
# 原生窗口 / Edge fallback
# ════════════════════════════════════════════════════════
def _confirm_quit_with_task(label: str) -> bool:
    if sys.platform != "win32":
        return True
    import ctypes as _ct
    MB_OKCANCEL, MB_ICONWARNING = 0x1, 0x30
    msg = tr("dlg.quitBody", label=label)
    r = _ct.windll.user32.MessageBoxW(0, msg, APP_NAME, MB_OKCANCEL | MB_ICONWARNING)
    return r == 1


def run_native_window(url: str) -> bool:
    try:
        import webview
    except Exception:
        return False
    try:
        try:
            webview.settings['ALLOW_DOWNLOADS'] = True
        except Exception:
            pass
        x, y, w, h, mn_w, mn_h = fit_window_to_screen(WIN_W, WIN_H, MIN_W, MIN_H)
        window = webview.create_window(
            APP_NAME, url=url,
            x=x, y=y, width=w, height=h, min_size=(mn_w, mn_h),
        )

        def _on_closing():
            backend = _BACKEND_REF[0]
            if backend is None or not backend.has_running_tasks():
                return True
            if not _confirm_quit_with_task(backend.running_task_label()):
                return False
            try:
                backend.cancel_all()
            except Exception:
                pass
            return True

        window.events.closing += _on_closing
        _decorate_window_async()
        webview.start()
        return True
    except Exception as e:
        print(f"[{APP_NAME}] 原生窗口失败，改用 Edge：{e}")
        return False


def _find_edge() -> str | None:
    for c in [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]:
        if c and Path(c).is_file():
            return str(c)
    return None


def _profile_dir() -> str:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "OCRMiniTool" / "edge-profile"
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def open_edge_app(url: str) -> subprocess.Popen | None:
    edge = _find_edge()
    if edge:
        _, _, w, h, _, _ = fit_window_to_screen(WIN_W, WIN_H, MIN_W, MIN_H)
        args = [
            edge, f"--app={url}", "--inprivate",
            f"--user-data-dir={_profile_dir()}",
            f"--window-size={w},{h}",
            "--no-first-run", "--no-default-browser-check", "--disable-sync",
            "--disable-background-networking",
            "--disable-features=msImplicitSignin,msEdgeSyncEnabled,msEdgeWelcomePage,EdgeFollowEnabled",
        ]
        flags = 0x08000000 if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(args, creationflags=flags)
            _decorate_window_async()
            return proc
        except Exception:
            pass
    webbrowser.open(url)
    return None


def main():
    # UTF-8 模式：避免简中 Windows（GBK）控制台 print 中文/符号出错
    os.environ.setdefault("PYTHONUTF8", "1")

    setup_kill_on_close_job()
    set_app_user_model_id()

    srv = WebViewServer(host="127.0.0.1", port=0)
    srv.start()
    _BACKEND_REF[0] = srv.backend

    # 窗口标题/退出弹窗按 settings.json 的语言（WebBackend 构造时已 set_lang）
    global APP_NAME
    try:
        APP_NAME = tr("app.name")
    except Exception:
        pass

    global _initial_theme
    try:
        _initial_theme = srv.backend.persisted_theme()
        srv.backend.set_theme_callback(apply_titlebar_theme)
    except Exception:
        pass

    # 模型已下载 → 背景自动加载；未下载 → 停在模型页等使用者点下载
    try:
        if srv.backend.model_files_present():
            srv.backend.start_load()
        else:
            print(f"[{APP_NAME}] 模型尚未下载 → 等使用者于模型页下载。")
    except Exception:
        pass

    url = srv.url
    print(f"[{APP_NAME}] {url}")

    has_wv2 = webview2_available()
    if not has_wv2:
        print(f"[{APP_NAME}] 未检测到 WebView2 Runtime，改用 Edge --app 窗口。")
    native_url = url + ("&" if "?" in url else "?") + "native=1"
    native_ok = has_wv2 and run_native_window(native_url)
    try:
        if not native_ok:
            proc = open_edge_app(url)
            if proc is not None:
                proc.wait()
            else:
                threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        # os._exit：跳过 atexit/GC/DLL 卸载（torch 常驻资源清理可能卡死成
        # 僵尸进程），由 OS 直接回收；Job handle 关闭连带终止残余子进程。
        try:
            srv.stop()
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(0)


if __name__ == "__main__":
    main()

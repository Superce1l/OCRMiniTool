"""webview_backend.py — OCRMiniTool 的 WebView 后端逻辑

持有：SettingsStore（settings.json 单点读写）、OcrEngine（Unlimited-OCR-2）、
下载/加载/识别三类后台任务（同一时刻各至多一个，互斥保护）。所有进度经
on_event 回调（webview_server 转成 SSE）推给前端：
  {"event": "dl-progress",  "payload": {"pct", "status"}}
  {"event": "load-progress","payload": {"pct", "status"}}
  {"event": "progress",     "payload": {"pct", "status"}}      # 识别
  {"event": "status",       "payload": {...}}                  # 状态变化广播

模型状态机（modelState）：
  absent → downloading → downloaded → loading → ready（error 可从多处进入）
"""
from __future__ import annotations

import json
import inspect
import locale
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from applog import log_download, log_error, log_info, log_model, log_ocr
from downloader import (CancelledError, _TORCH_VERSION, download_model,
                        installed_torch_info, quick_check, quick_check_torch,
                        set_mirror)
from i18n import available_languages, set_lang, tr
from ocr_engine import EngineError, OcrEngine
from settings_store import SettingsStore
from version import APP_VERSION

# ── 路径（PyInstaller 冻结时在 EXE 旁）────────────────────────────
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

SETTINGS_FILE = BASE_DIR / "settings.json"
MODEL_SUBDIR = "models/Unlimited-OCR-2"
OUTPUT_DIR = BASE_DIR / "outputs"
OUTPUT_DIR.mkdir(exist_ok=True)

DEFAULT_SETTINGS = {
    "modelDir": "",        # 空 = 程序目录下 models/Unlimited-OCR-2
    "mirror": "",          # HuggingFace 镜像，如 hf-mirror.com
    "devicePref": "auto",  # auto | cuda | cpu
    "torchFlavor": "",     # 推理引擎组件：""=auto(按硬件推荐) | cpu | cu128
    "outFormat": "md",     # 识别结果保存格式：md | txt
    "theme": "light",
    "scale": 100,
    "lang": "zh",          # 界面语言，默认中文（en | zh；i18n.py / i18n.js）
    "pythonPath": "",      # 自定义 Python 解释器（源码运行时）；空 = 启动器选择的 venv
    # ── 识别限幅 / 版面参数（P0 契约）──────────────────────────
    "maxPageTokens": 4096,   # 单页输出 token 上限；0 = 不限；合法 0 或 [256, 65536]
    "maxMergedPages": 20,    # 合并请求的页数上限；0 = 不限；合法 0 或 [1, 200]
    "cropMode": "auto",      # auto | gundam | base
    "dropHeaders": False,    # 是否丢弃页眉页脚块
}

# 目录扫描 / 路径直读共用：允许的扩展名（与 ocr_engine 的图片扩展 + pdf 对齐）
SCAN_EXTS = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".webp",
                       ".tif", ".tiff", ".pdf"})
MAX_SCAN_FILES = 10_000      # /api/scan-dir 的硬上限（防止一次列爆内存）
MAX_SCAN_DEPTH = 64          # 目录深度硬上限
MAX_RECOGNIZE_PATHS = 200    # 一次识别允许的路径数上限（与上传链路同量级）

_CROP_MODES = ("auto", "gundam", "base")
_ENGINE_GROUP_KWARGS = ("max_page_tokens", "max_merged_pages", "drop_headers")


def _as_int(value) -> int | None:
    """宽松取整：int 原样，纯数字字符串转换，其余（含 bool）返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"[+-]?\d+", s or ""):
            try:
                return int(s, 10)
            except ValueError:
                return None
    return None


def _as_bool(value) -> bool | None:
    """宽松取 bool：bool 原样，0/1 映射，其余返回 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    return None


def _bad_value(key: str, value) -> dict:
    """`err.badValue` 错误响应。

    注意：i18n.tr(key, **params) 的第一个形参就叫 key，不能写
    `tr("err.badValue", key=...)`（TypeError: multiple values）。这里取模板后
    自行替换占位符，语义一致且不会因为占位符缺失抛异常。
    """
    msg = tr("err.badValue")
    return {"ok": False,
            "error": msg.replace("{key}", str(key)).replace("{value}",
                                                            str(value))}


def _is_link_like(path) -> bool:
    """是否符号链接或目录联接（junction）。

    只判 `is_symlink()` 不够：Windows 的目录联接（mklink /J、PowerShell
    -ItemType Junction）不是 symlink，`DirEntry.is_dir(follow_symlinks=False)`
    对它是 True——扫描会顺着它走出去。Python 3.12+ 有 os.path.isjunction。
    """
    try:
        if os.path.islink(path):
            return True
        isjunction = getattr(os.path, "isjunction", None)
        if isjunction is not None:
            return bool(isjunction(path))
    except OSError:
        return True
    return False


def normalize_settings_patch(patch: dict) -> tuple[dict, dict | None]:
    """校验并归一化四个识别参数键。

    返回 (归一后的 patch, 错误响应)。**错误时调用方不得写盘**（一个键都不写，
    避免"一半合法一半非法"的补丁落成半套设置）。整型接受数字字符串。
    """
    out = dict(patch or {})
    if "maxPageTokens" in out:
        v = _as_int(out["maxPageTokens"])
        if v is None or not (v == 0 or 256 <= v <= 65536):
            return {}, _bad_value("maxPageTokens", out["maxPageTokens"])
        out["maxPageTokens"] = v
    if "maxMergedPages" in out:
        v = _as_int(out["maxMergedPages"])
        if v is None or not (v == 0 or 1 <= v <= 200):
            return {}, _bad_value("maxMergedPages", out["maxMergedPages"])
        out["maxMergedPages"] = v
    if "cropMode" in out:
        v = str(out["cropMode"] if out["cropMode"] is not None else "").strip().lower()
        if v not in _CROP_MODES:
            return {}, _bad_value("cropMode", out["cropMode"])
        out["cropMode"] = v
    if "dropHeaders" in out:
        v = _as_bool(out["dropHeaders"])
        if v is None:
            return {}, _bad_value("dropHeaders", out["dropHeaders"])
        out["dropHeaders"] = v
    return out, None


def normalize_recognize_paths(raw_paths) -> tuple[list[Path], dict | None]:
    """校验「按路径直读」的输入（与 /api/scan-dir 同一套边界）。

    只接受存在的绝对文件、扩展名白名单内、数量不超限、且自身不是符号链接
    （符号链接/目录联接一律不跟随）。返回 (路径列表, 错误响应)。
    """
    if not isinstance(raw_paths, (list, tuple)) or not raw_paths:
        return [], _bad_value("paths", str(raw_paths)[:80])
    if len(raw_paths) > MAX_RECOGNIZE_PATHS:
        return [], _bad_value("paths",
                              f"{len(raw_paths)} > {MAX_RECOGNIZE_PATHS}")
    out: list[Path] = []
    for raw in raw_paths:
        text = str(raw or "").strip()
        p = Path(os.path.expandvars(text)).expanduser()
        try:
            if (not text or not p.is_absolute() or _is_link_like(p)
                    or not p.is_file()
                    or p.suffix.lower() not in SCAN_EXTS):
                return [], {"ok": False,
                            "error": tr("err.badPath", path=text)}
        except OSError:
            return [], {"ok": False, "error": tr("err.badPath", path=text)}
        out.append(p)
    return out, None

# 首个启动进程（run.bat 选定的 venv python）写入环境变量；「恢复默认解释器」
# 重启时回退到它——因为彼时本进程可能已重挂到自定义解释器，sys.executable
# 不再是默认环境。
BASE_PYTHON_ENV = "OCRMT_BASE_PYTHON"


def decode_child_text(raw: bytes) -> str:
    """子进程输出解码：先 UTF-8，失败按系统 ANSI 代码页（zh-CN 上是 GBK）。

    目标解释器/程序按自己的 locale 输出 stderr；一律按 UTF-8 解会把
    "信息: 系统找不到指定的文件" 变成乱码塞进界面（实测 where.exe 探测）。
    只有全部编码都失败才用 replace 保底，保证永不抛。
    """
    if not raw:
        return ""
    for enc in ("utf-8", locale.getpreferredencoding(False), "mbcs"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError, ValueError):
            continue
    return raw.decode("utf-8", errors="replace")


def reexec_if_custom_python():
    """settings.json 配了 pythonPath 且 ≠ 当前解释器 → 重挂过去（源码运行）。

    在任何重依赖 import 之前调用：本进程只负责拉起新进程后即刻退出。
    新进程以 CREATE_BREAKAWAY_FROM_JOB 脱离 KILL_ON_JOB_CLOSE 的 Job（否则
    本进程退出 = Job 关闭 = 新进程连带被杀）。目标解释器若缺依赖起不来，
    控制台会显示其报错；把 settings.json 的 pythonPath 清空即可还原。
    """
    if getattr(sys, "frozen", False):
        return
    os.environ.setdefault(BASE_PYTHON_ENV, sys.executable)
    try:
        data = json.loads(
            (BASE_DIR / "settings.json").read_text(encoding="utf-8"))
        target = str(data.get("pythonPath") or "").strip()
    except Exception:
        return
    if not target:
        return
    cur = Path(sys.executable).resolve()
    tgt = Path(os.path.expandvars(target)).expanduser().resolve()
    if sys.platform == "win32":
        same = cur.as_posix().lower() == tgt.as_posix().lower()
    else:
        same = cur == tgt
    if same or not tgt.is_file():
        return
    print(f"[OCR MiniTool] 按设置切换解释器：{sys.executable} → {target}")
    # BREAKAWAY：脱离 KILL_ON_JOB_CLOSE 的 Job（否则本进程退出＝Job 关闭＝
    # 新进程连带被杀）；不带 CREATE_NO_WINDOW，让新进程的控制台报错可见。
    flags = subprocess.CREATE_BREAKAWAY_FROM_JOB if sys.platform == "win32" else 0
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    try:
        subprocess.Popen([target] + [str(a) for a in sys.argv],
                         creationflags=flags, close_fds=False)
        os._exit(0)
    except OSError as e:
        print(f"[OCR MiniTool] 解释器切换失败（{e}），继续用当前解释器启动。")


class WebBackend:
    """WebView 后端：状态、设置、模型下载/加载、OCR 识别。"""

    def __init__(self, on_event=None):
        self._on_event = on_event or (lambda ev, payload: None)
        self._theme_cb = None
        self.settings = SettingsStore(SETTINGS_FILE)
        self.engine = OcrEngine(self._model_dir())

        self._lock = threading.RLock()          # 任务状态互斥
        self.downloading = False
        self.loading = False
        self.recognizing = False
        self.env_downloading = False            # 推理引擎组件下载中
        self._task_label = ""
        self.cancel_event = threading.Event()
        self.env_cancel = threading.Event()
        # 界面语言（后端 SSE 进度/错误文案与原生对话框同语言）
        set_lang(self._settings_raw().get("lang"))
        # 下载/加载期间用户改了设置 → 需要重读 mirror/model dir
        self._apply_mirror_from_settings()
        # 记录加载期快照：状态字段反映真正加载的设备/目录
        self._loaded_device = None
        # probe_python 结果短缓存（「检测→应用」两步复用；60s 失效，
        # 用户中途给目标环境补装包后重新「检测」即可拿到新结果）
        self._probe_cache = ("", 0.0, None)
        # 硬件推荐短缓存：一次 GET /api/env 会在 env_status 与 _torch_flavor
        # 各取一次，缓存 60s 避免重复跑显卡枚举（PowerShell，~0.5s）
        self._rec_cache = (0.0, None)

    # ── 事件与设置 ───────────────────────────────────────────
    def _emit(self, event: str, payload: dict):
        try:
            self._on_event(event, payload)
        except Exception:
            pass

    def _publish_status(self):
        self._emit("status", self.get_status())

    def _model_dir(self) -> Path:
        d = (self._settings_raw().get("modelDir") or "").strip()
        return Path(d) if d else BASE_DIR / MODEL_SUBDIR

    def _settings_raw(self) -> dict:
        # SettingsStore 自带缓存；这里只补默认值
        cur = dict(DEFAULT_SETTINGS)
        cur.update(self.settings.read())
        return cur

    def _apply_mirror_from_settings(self):
        set_mirror(self._settings_raw().get("mirror", ""))

    def get_settings(self) -> dict:
        cur = self._settings_raw()
        return {"modelDir": cur.get("modelDir", ""),
                "mirror": cur.get("mirror", ""),
                "devicePref": cur.get("devicePref", "auto"),
                "torchFlavor": cur.get("torchFlavor", ""),
                "outFormat": cur.get("outFormat", "md"),
                "theme": cur.get("theme", "light"),
                "scale": cur.get("scale", 100),
                "lang": cur.get("lang", "zh"),
                "pythonPath": cur.get("pythonPath", ""),
                # 识别限幅 / 版面参数（原样返回，缺省用 DEFAULT_SETTINGS）
                "maxPageTokens": cur.get("maxPageTokens", 4096),
                "maxMergedPages": cur.get("maxMergedPages", 20),
                "cropMode": cur.get("cropMode", "auto"),
                "dropHeaders": bool(cur.get("dropHeaders", False)),
                "languages": available_languages(),
                "defaultModelDir": str(BASE_DIR / MODEL_SUBDIR),
                # 源码运行才可切换解释器；PyInstaller 冻结版前端隐藏该设置
                "frozen": bool(getattr(sys, "frozen", False)),
                "interpreter": sys.executable}

    def set_settings(self, patch: dict) -> dict:
        patch = {k: v for k, v in (patch or {}).items() if k in DEFAULT_SETTINGS}
        # 四个识别参数先校验/归一：任何一个非法 → 整个补丁不落盘（原子语义）
        patch, bad = normalize_settings_patch(patch)
        if bad is not None:
            return bad
        # 模型目录变化即时同步给引擎（仅空闲时切，避免下载/识别中途换目录——
        # 否则会出现「下载到新目录、加载却找旧目录」的分裂）
        if "modelDir" in patch and not self.has_running_tasks():
            new_dir = Path(str(patch["modelDir"]).strip() or (BASE_DIR / MODEL_SUBDIR))
            if new_dir != self.engine.model_dir:
                self.engine.model_dir = new_dir
        old_theme = self.persisted_theme()
        self.settings.update(patch)
        self._apply_mirror_from_settings()
        if "lang" in patch:
            set_lang(patch["lang"])
        new_theme = self.persisted_theme()
        if new_theme != old_theme and self._theme_cb:
            try:
                self._theme_cb(new_theme)
            except Exception:
                pass
        return self.get_settings()

    def persisted_theme(self) -> str:
        return self._settings_raw().get("theme", "light")

    def set_theme_callback(self, cb):
        self._theme_cb = cb

    def model_files_present(self) -> bool:
        return quick_check(self._model_dir())

    # ── 状态 ─────────────────────────────────────────────────
    def model_state(self) -> str:
        with self._lock:
            if self.loading:
                return "loading"
            if self.downloading:
                return "downloading"
        if self.engine.ready:
            return "ready"
        if self.engine.error:
            return "error"
        return "downloaded" if quick_check(self._model_dir()) else "absent"

    def get_status(self) -> dict:
        st = self.model_state()
        dev = self.engine.device_info()
        cur = self._settings_raw()
        return {
            "appName": tr("app.name"),
            "version": APP_VERSION,
            "modelState": st,
            "modelDir": str(self._model_dir()),
            "outDir": str(OUTPUT_DIR),
            "modelError": self.engine.error,
            "loadedDevice": self._loaded_device,
            "devicePref": cur.get("devicePref", "auto"),
            "mirror": cur.get("mirror", ""),
            "torchFlavor": cur.get("torchFlavor", ""),
            "envInstalled": installed_torch_info(),
            "envDownloading": self.env_downloading,
            "torchPin": _TORCH_VERSION,
            "torch": dev.get("torch"),
            "transformers": dev.get("transformers"),
            "cuda": dev.get("cuda", False),
            "cudaBuild": dev.get("cudaBuild", ""),
            "cudaName": dev.get("cudaName", ""),
            "vramGb": dev.get("vramGb", 0.0),
            "interpreter": sys.executable,
            "frozen": bool(getattr(sys, "frozen", False)),
            "busy": self.has_running_tasks(),
            "busyLabel": self._task_label or "",
        }

    def has_running_tasks(self) -> bool:
        with self._lock:
            return (self.downloading or self.loading or self.recognizing
                    or self.env_downloading)

    def running_task_label(self) -> str:
        with self._lock:
            return self._task_label or ""

    # ── 模型下载 ─────────────────────────────────────────────
    def start_download(self) -> dict:
        with self._lock:
            if self.downloading or self.loading or self.recognizing \
                    or self.env_downloading:
                return {"ok": False, "error": tr("err.busy")}
            self.downloading = True
            self._task_label = tr("task.download")
        self.cancel_event.clear()

        def worker():
            t0 = time.time()
            try:
                model_dir = self._model_dir()
                last = [0.0]

                def cb(pct, msg):
                    now = time.time()
                    if pct >= 1.0 or now - last[0] > 0.25 or pct == 0:
                        last[0] = now
                        self._emit("dl-progress", {"pct": pct, "status": msg})

                download_model(model_dir, progress_cb=cb,
                               cancel_check=self.cancel_event.is_set)
                log_download(f"模型下载完成（{model_dir}，{time.time() - t0:.0f}s）")
                self._emit("dl-progress", {"pct": 1.0, "status": tr("dl.done")})
            except CancelledError:
                log_info("模型下载已取消")
                self._emit("dl-progress",
                           {"pct": 0, "status": tr("dl.cancelledResumable")})
            except Exception as e:
                log_error("模型下载失败", exc=e)
                self._emit("dl-progress",
                           {"pct": 0, "status": tr("dl.failed", err=e)})
            finally:
                with self._lock:
                    self.downloading = False
                    self._task_label = ""
                self._publish_status()

        threading.Thread(target=worker, name="model-download", daemon=True).start()
        return {"ok": True, "downloading": True}

    # ── 模型加载 ─────────────────────────────────────────────
    def start_load(self) -> dict:
        with self._lock:
            if self.loading or self.downloading or self.recognizing:
                return {"ok": False, "error": tr("err.busy")}
            if self.env_downloading:
                return {"ok": False, "error": tr("err.envBusy")}
            if not quick_check(self._model_dir()):
                return {"ok": False, "error": tr("err.modelMissing")}
            if not (quick_check_torch(self._torch_flavor(persist=True))
                    or self._torch_from_source_env()):
                return {"ok": False, "error": tr("err.noTorchComponent")}
            self.loading = True
            self._task_label = tr("task.load")
        self.cancel_event.clear()

        def worker():
            t0 = time.time()
            dev_pref = self._settings_raw().get("devicePref", "auto")

            def cb(msg):
                self._emit("load-progress", {"pct": 0.5, "status": msg})

            try:
                log_model(f"开始加载 Unlimited-OCR-2（device={dev_pref}）")
                self.engine.load(device_pref=dev_pref, cb=cb)
                self._loaded_device = self.engine.device
                log_model(f"模型加载完成（{self.engine.device}，"
                          f"{time.time() - t0:.0f}s）")
                self._emit("load-progress",
                           {"pct": 1.0, "status": tr("load.done")})
            except Exception as e:
                log_error("模型加载失败", exc=e)
                self.engine.report_error(str(e))
                self._emit("load-progress",
                           {"pct": 0, "status": tr("load.failed", err=e)})
            finally:
                with self._lock:
                    self.loading = False
                    self._task_label = ""
                self._publish_status()

        threading.Thread(target=worker, name="model-load", daemon=True).start()
        return {"ok": True, "loading": True}

    # ── 推理引擎组件（torch 运行时，按硬件选装）─────────────────
    def _recommend(self) -> dict:
        """硬件推荐（进程内 60s 缓存，供 env_status / _torch_flavor 共用）。"""
        from downloader import recommend_torch_flavor
        ts, rec = self._rec_cache
        if rec is not None and time.time() - ts < 60:
            return rec
        rec = recommend_torch_flavor()
        self._rec_cache = (time.time(), rec)
        return rec

    def _torch_flavor(self, persist: bool = False) -> str:
        """生效的组件 flavor：设置值合法用之；其次已装者；否则按硬件推荐。

        **纯查询**：推荐值只在 persist=True 的动作路径（如 start_load）写回
        设置；读路径（GET /api/env）绝不写盘——只读介质上也不该因此 500。
        """
        from downloader import installed_torch_info, torch_flavors
        s = self._settings_raw().get("torchFlavor") or ""
        if s in torch_flavors():
            return s
        info = installed_torch_info()
        if info and info.get("flavor") in torch_flavors():
            return info["flavor"]
        rec = self._recommend().get("recommended") or "cpu"
        if persist:
            try:
                self.settings.update({"torchFlavor": rec})
            except Exception as e:      # 写失败不影响本次查询结果
                log_error("保存推荐组件类型失败", exc=e)
        return rec

    def _torch_from_source_env(self) -> bool:
        """源码运行时，torch 来自 venv（README 的 `pip install -r requirements.txt`）。

        冻结版必须走 runtime/ 组件（安装包不含 torch）；但源码运行没有 runtime/
        目录，若只认组件标记，用户点「加载模型」会直接被告知"尚未安装推理引擎
        组件"——组件是打包发行版的机制，源码用户按文档装好依赖后仍被挡住。
        真机实测复现（v1.3.0：venv 有 torch 2.8.0+cpu 却无法加载模型）。
        """
        if getattr(sys, "frozen", False):
            return False
        import importlib.util as _ilu
        try:
            spec = _ilu.find_spec("torch")
        except (ImportError, ValueError):
            return False
        return bool(spec and spec.origin)

    def env_ready(self) -> bool:
        """推理引擎是否就绪（app 启动时决定要不要自动加载模型）。

        两条合法来源：① 冻结版的 runtime/ 组件；② 源码运行的 venv torch。
        """
        return (quick_check_torch(self._torch_flavor())
                or self._torch_from_source_env())

    def env_status(self) -> dict:
        """硬件检测 + 组件菜单 + 安装状态（前端「推理引擎」卡片渲染）。"""
        import shutil as _shutil
        from downloader import installed_torch_info
        try:
            rec = self._recommend()
        except Exception as e:
            log_error("硬件检测失败", exc=e)
            return {"ok": False, "error": str(e), "options": []}
        installed = installed_torch_info()
        options = rec["options"]
        for o in options:
            o["installed"] = bool(installed and installed.get("flavor") == o["key"])
        try:
            free_gb = round(_shutil.disk_usage(str(BASE_DIR)).free / 1e9, 1)
        except Exception:
            free_gb = None
        return {"ok": True,
                "selected": self._torch_flavor(),
                "recommended": rec["recommended"],
                "reason": rec["reason"],
                "options": options,
                "installed": installed,
                "envDownloading": self.env_downloading,
                "freeGb": free_gb}

    def set_env_flavor(self, flavor: str) -> dict:
        """记住使用者选的组件 flavor（下载是显式的另一动作）。"""
        from downloader import _TORCH_VERSION, installed_torch_info, torch_flavors
        if flavor not in torch_flavors():
            return {"ok": False, "message": tr("env.unknownFlavor", flavor=flavor)}
        self.settings.update({"torchFlavor": flavor})
        installed = installed_torch_info()
        fresh = bool(installed and installed.get("flavor") == flavor
                     and installed.get("torch") == _TORCH_VERSION)
        meta = torch_flavors()[flavor]
        label = tr(meta["label_key"], ver=_TORCH_VERSION)
        return {"ok": True, "flavor": flavor,
                "message": tr("env.flavorSet" if fresh else "env.flavorSetNeedDl",
                              label=label, size=meta["size_mb"])}

    def start_env_download(self, flavor: str) -> dict:
        """后台下载并安装推理引擎组件（进度走 SSE env-progress 事件）。"""
        from downloader import _TORCH_VERSION, installed_torch_info, \
            quick_check_torch, torch_flavors
        if flavor not in torch_flavors():
            return {"ok": False, "error": tr("env.unknownFlavor", flavor=flavor)}
        with self._lock:
            if self.env_downloading or self.downloading or self.loading \
                    or self.recognizing:
                return {"ok": False, "error": tr("err.busy")}
            if quick_check_torch(flavor):
                return {"ok": True, "already": True}
            # 切换组件（已装 A、要装 B）在运行中必然失败：进程已把旧 runtime 下的
            # .pyd 当 DLL 载入，Windows 不允许覆盖。真机实测（CPU→cu128）报
            # PermissionError 覆盖 PIL/_imaging...pyd，marker 不变、还残留 3.5GB
            # 安装包。这里提前拒绝并说清该怎么办，别让用户等到下载完才失败。
            cur = installed_torch_info() or {}
            if (cur.get("flavor") and cur["flavor"] != flavor
                    and "torch" in sys.modules):
                return {"ok": False, "error": tr("err.envSwitchNeedsRestart")}
            self.env_downloading = True
            self._task_label = tr("task.envDl")
        self.env_cancel.clear()

        def worker():
            def cb(pct, msg):
                self._emit("env-progress", {"pct": pct, "status": msg})
            try:
                from downloader import download_torch
                t0 = time.time()
                marker = download_torch(flavor, progress_cb=cb,
                                        cancel_check=self.env_cancel.is_set)
                log_download(f"推理引擎组件就绪：{flavor} / torch {marker['torch']}"
                             f"（{time.time() - t0:.0f}s）")
                self._emit("env-progress",
                           {"pct": 1.0, "status": tr("env.done", ver=marker["torch"])})
            except CancelledError:
                log_info("推理引擎组件下载已取消")
                self._emit("env-progress",
                           {"pct": 0, "status": tr("env.cancelled")})
            except Exception as e:
                log_error("推理引擎组件下载失败", exc=e)
                self._emit("env-progress",
                           {"pct": 0, "status": tr("dl.failed", err=e)})
            finally:
                with self._lock:
                    self.env_downloading = False
                    self._task_label = ""
                self._publish_status()

        threading.Thread(target=worker, name="env-download", daemon=True).start()
        return {"ok": True, "downloading": True}

    def cancel_env_download(self) -> bool:
        if self.env_downloading:
            self.env_cancel.set()
        return True

    # ── OCR 识别 ─────────────────────────────────────────────
    def _engine_recognize_kwargs(self) -> dict:
        """把四个识别参数设置翻译成引擎关键字。

        cropMode 原样透传给引擎（"auto" / "gundam" / "base"）：引擎的 "auto"
        才会按图片形状自动降级长截图（高宽比 > 3 → base，CPU 上 2–5×），
        映射成 None 等于退回旧行为、让这个开关失效（实测过的缺陷）。
        脏值（空/未知）一律回落 "auto"。
        """
        cur = self._settings_raw()
        crop = str(cur.get("cropMode") or "auto").strip().lower()
        if crop not in ("auto", "gundam", "base"):
            crop = "auto"
        return {
            "max_page_tokens": _as_int(cur.get("maxPageTokens")) or 0,
            "max_merged_pages": _as_int(cur.get("maxMergedPages")) or 0,
            "drop_headers": bool(cur.get("dropHeaders", False)),
            "crop_mode": crop,
        }

    def _call_engine_recognize(self, image_paths, **kwargs) -> dict:
        """调用 engine.recognize，兼容新旧签名（engine-perf 正在改签名）。

        做法：①用 inspect 探测引擎实际接受的参数名，只传它认识的（新引擎全传、
        旧引擎自动裁掉 max_page_tokens/max_merged_pages/drop_headers）；②探测
        失败（C 扩展/装饰器）按"全传"处理；③万一是绑定期 TypeError（引擎签名
        比探测到的还老），只在报文明确是"unexpected keyword argument"时用旧
        签名子集重试一次——绝不吞掉推理过程中的 TypeError 造成二次推理。
        """
        kwargs.update(self._engine_recognize_kwargs())
        try:
            params = inspect.signature(self.engine.recognize).parameters
            accepted = set(params)
            has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD
                             for p in params.values())
        except (TypeError, ValueError):
            accepted, has_var_kw = set(), True
        call = kwargs if has_var_kw else {
            k: v for k, v in kwargs.items() if k in accepted}
        try:
            return self.engine.recognize(image_paths, **call)
        except TypeError as e:
            msg = str(e)
            if "unexpected keyword argument" not in msg and \
                    "got an unexpected" not in msg:
                raise
            minimal = {k: v for k, v in call.items()
                       if k not in _ENGINE_GROUP_KWARGS}
            return self.engine.recognize(image_paths, **minimal)

    def recognize(self, opts: dict, progress_cb=None) -> dict:
        """opts: {paths: [str], name: 原始主文件名, prompt: str, mode: "merged"|"batch"}

        进度经 progress_cb(pct, status)（server 转 SSE）；返回结果 dict。

        merged（默认）：全部文件作为同一文档的多页合并为一次请求（跨页上下文）。
        batch：批量识别——每个文件独立识别、独立出结果，失败隔离，可随时取消
        （当前文件即时中断，其余跳过）。
        """
        with self._lock:
            if self.recognizing or self.loading or self.downloading \
                    or self.env_downloading:
                raise EngineError(tr("err.busy"))
            if not self.engine.ready:
                raise EngineError(tr("err.modelNotLoaded"))
            paths = [Path(p) for p in (opts.get("paths") or [])]
            if not paths:
                raise EngineError(tr("err.noFiles"))
            self.recognizing = True
            self._task_label = tr("task.ocr")
        self.cancel_event.clear()
        mode = "batch" if opts.get("mode") == "batch" else "merged"

        try:
            if mode == "batch":
                return self._recognize_batch(paths, opts, progress_cb)

            name = (opts.get("name") or "result").strip() or "result"
            name = self._sanitize_name(name)
            prompt = opts.get("prompt") or ""
            t0 = time.time()
            total = len(paths)
            last = {"pct": 0.02, "stage": tr("prog.running")}

            def _cb(stage, done, tot, elapsed):
                pct = 0.05 + 0.9 * ((done / max(1, tot)) / max(1, total))
                last["pct"] = min(pct, 0.97)
                last["stage"] = (stage or tr("prog.running")).rstrip("…")
                mins = elapsed / 60
                time_s = tr("time.min", m=f"{mins:.1f}") if mins >= 1 else \
                    tr("time.sec", s=f"{elapsed:.0f}")
                if progress_cb:
                    progress_cb(last["pct"],
                                tr("prog.merged", stage=last["stage"], time=time_s))

            # 心跳：单次模型请求内部无法回报百分比，但流式钩子会累计已生成
            # 字数——每 5 秒刷新「已生成 N 字 + 已用时」，界面清晰可见进展。
            stop_ticker = threading.Event()

            def _ticker():
                while not stop_ticker.wait(5.0):
                    if progress_cb:
                        chars = self.engine.generated_chars()
                        mins = (time.time() - t0) / 60
                        progress_cb(last["pct"],
                                    tr("prog.ticker", stage=last["stage"],
                                       chars=chars,
                                       time=tr("time.min", m=f"{mins:.1f}")))

            threading.Thread(target=_ticker, name="ocr-ticker",
                             daemon=True).start()
            try:
                log_ocr(f"开始识别：{name}（{total} 个文件）")
                result = self._call_engine_recognize(
                    paths, prompt=prompt, progress_cb=_cb,
                    cancel_event=self.cancel_event)
            finally:
                stop_ticker.set()

            out_path = self._save_output(
                name, result, self._settings_raw().get("outFormat", "md"))
            elapsed = result.get("elapsed", time.time() - t0)
            log_ocr(f"识别完成：{name}（{len(result.get('pages', []))} 页，"
                    f"{elapsed:.0f}s）→ {out_path.name}")
            if progress_cb:
                progress_cb(1.0, tr("prog.done"))
            payload = {
                "mode": "merged",
                "text": result.get("text", ""),
                "pages": result.get("pages", []),
                "outPath": str(out_path),
                "elapsed": elapsed,
                "cancelled": False,
            }
            # 引擎新增的中性字段原样透传（截断/页数不符/分组/丢弃块）；旧引擎
            # 没有这些键就自然缺省，不报错。
            for key in ("truncated", "pagesMismatch", "finishReason",
                        "groups", "droppedBlocks", "tokens"):
                if key in result:
                    payload[key] = result[key]
            # 期望页数 / 实际页数：界面提示条要说清「期望 N 页、实际 M 页」，
            # 只靠 truncated/pagesMismatch 布尔值说不清差多少。
            inf = result.get("infer")
            if isinstance(inf, dict):
                if inf.get("expectedPages") is not None:
                    payload["expectedPages"] = inf["expectedPages"]
                if inf.get("numPagesOut") is not None:
                    payload["actualPages"] = inf["numPagesOut"]
            return payload
        except EngineError as e:
            if self.cancel_event.is_set():
                log_ocr("识别已取消")
                if progress_cb:
                    progress_cb(0, tr("prog.cancelled"))
                return {"text": "", "pages": [], "outPath": "",
                        "elapsed": 0, "cancelled": True}
            log_error("识别失败", exc=e)
            raise
        finally:
            with self._lock:
                self.recognizing = False
                self._task_label = ""
            self._publish_status()

    def _recognize_batch(self, paths: list[Path], opts: dict,
                         progress_cb=None) -> dict:
        """批量识别：逐文件独立推理。返回 {items, cancelled, elapsed}。

        • 每个文件单独保存 outputs/<文件名>.md；单个文件失败不中断队列
        • 取消：当前文件即时中断（流式钩子），其余未开始文件跳过
        • 进度：第 i/N 个文件 × 文件内进度 + 已生成字数（心跳刷新）
        """
        prompt = opts.get("prompt") or ""
        names = opts.get("names") or []
        fmt = self._settings_raw().get("outFormat", "md")
        total = len(paths)
        t0 = time.time()
        items: list[dict] = []
        last = {"pct": 0.02, "label": ""}

        def _report():
            if progress_cb:
                progress_cb(last["pct"], last["label"])

        def _skip_item(idx: int):
            # 展示名一律用原始文件名（上传临时名 part_N_upload 不对外）
            name = (names[idx] if idx < len(names) and names[idx]
                    else paths[idx].name)
            items.append({"name": name, "ok": False, "skipped": True,
                          "text": "", "elapsed": 0, "chars": 0})

        # 心跳：把「第 i/N 个 + 已生成字数 + 已用时」每 5 秒推给界面
        stop_ticker = threading.Event()

        def _ticker():
            while not stop_ticker.wait(5.0):
                if progress_cb:
                    chars = self.engine.generated_chars()
                    mins = (time.time() - t0) / 60
                    progress_cb(last["pct"],
                                tr("prog.ticker", stage=last["label"],
                                   chars=chars,
                                   time=tr("time.min", m=f"{mins:.1f}")))

        threading.Thread(target=_ticker, name="ocr-batch-ticker",
                         daemon=True).start()
        cancelled = False
        try:
            log_ocr(f"开始批量识别：{total} 个文件")
            for i, path in enumerate(paths):
                if self.cancel_event.is_set():
                    cancelled = True
                    for j in range(i, len(paths)):
                        _skip_item(j)
                    break

                # 展示名用原始文件名（上传临时文件名 part_N_upload 不对外）
                display = (names[i] if i < len(names) and names[i]
                           else path.name)
                label = tr("prog.batchItem", i=i + 1, n=total, name=display)
                last["pct"] = 0.02 + 0.95 * (i / total)
                last["label"] = label
                _report()
                f_start = time.time()

                def _file_cb(stage, done, tot, elapsed, _i=i, _n=total):
                    frac = 0.05 + 0.9 * (done / max(1, tot))
                    last["pct"] = min(0.02 + 0.95 * ((_i + frac) / _n), 0.97)
                    mins = elapsed / 60
                    time_s = tr("time.min", m=f"{mins:.1f}") if mins >= 1 else \
                        tr("time.sec", s=f"{elapsed:.0f}")
                    if progress_cb:
                        progress_cb(last["pct"],
                                    tr("prog.batchFile", label=label,
                                       stage=stage, time=time_s))

                try:
                    result = self._call_engine_recognize(
                        [path], prompt=prompt, progress_cb=_file_cb,
                        cancel_event=self.cancel_event)
                    out_path = self._save_output(
                        self._sanitize_name(Path(display).stem), result, fmt)
                    chars = len(result.get("text", ""))
                    item = {"name": display, "ok": True,
                            "text": result.get("text", ""),
                            "pages": result.get("pages", []),
                            "outPath": str(out_path),
                            "elapsed": result.get("elapsed", 0),
                            "chars": chars}
                    # 批量条目也带截断/页数不符标记（引擎缺省时自然不带）
                    for key in ("truncated", "pagesMismatch", "finishReason"):
                        if key in result:
                            item[key] = result[key]
                    inf = result.get("infer")
                    if isinstance(inf, dict):
                        if inf.get("expectedPages") is not None:
                            item["expectedPages"] = inf["expectedPages"]
                        if inf.get("numPagesOut") is not None:
                            item["actualPages"] = inf["numPagesOut"]
                    items.append(item)
                    log_ocr(f"批量 {i + 1}/{total} 完成：{display}"
                            f"（{chars} 字，{result.get('elapsed', 0):.0f}s）")
                except EngineError as e:
                    if self.cancel_event.is_set():
                        cancelled = True
                        items.append({"name": display, "ok": False,
                                      "skipped": True, "text": "",
                                      "elapsed": 0, "chars": 0})
                        for j in range(i + 1, len(paths)):
                            _skip_item(j)
                        break
                    log_error(f"批量 {i + 1}/{total} 失败：{display}", exc=e)
                    # 完整错误交给界面「错误详情」面板展示；只做兜底上限，
                    # 避免单个超长堆栈把 SSE/JSON 撑爆（原先 300 字让用户看不懂）
                    items.append({"name": display, "ok": False,
                                  "error": str(e)[:4000], "text": "",
                                  "elapsed": time.time() - f_start,
                                  "chars": 0})

            if progress_cb:
                progress_cb(1.0, tr("prog.cancelled") if cancelled
                            else tr("prog.done"))
            done_n = sum(1 for it in items if it.get("ok"))
            log_ocr(f"批量识别结束：{done_n}/{total} 成功"
                    f"{'（已取消）' if cancelled else ''}，"
                    f"总耗时 {time.time() - t0:.0f}s")
            return {"mode": "batch", "items": items,
                    "text": "\n\n".join(it.get("text", "") for it in items
                                        if it.get("ok")),
                    "elapsed": time.time() - t0,
                    "cancelled": cancelled,
                    "total": total, "done": done_n}
        finally:
            stop_ticker.set()

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """文件名净化：去非法字符 / 控制字符，避开 Windows 保留设备名。

        保留设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9，带任何扩展名与前后空格
        都同样保留）在 Windows 上打不开：`aux.png` 会让落盘抛
        FileNotFoundError、NUL 字符会让 Path 抛 ValueError——识别跑完才在
        写盘阶段整体失败（实测）。这里统一加下划线前缀绕开。
        """
        banned = '<>:"/\\|?*'
        name = "".join(c for c in name
                       if c not in banned and c != "\x00" and c.isprintable())
        name = name.strip().strip(".")      # 前后空白与点：避免 "..evil"/隐藏名
        name = name or "result"
        if WebBackend._is_reserved_windows_name(name):
            name = "_" + name
        return name[:80]

    @staticmethod
    def _is_reserved_windows_name(name: str) -> bool:
        """是否是 Windows 保留设备名（含 "aux.png"、"CON " 这类写法）。"""
        stem = name.split(".", 1)[0].strip().upper()
        if stem in {"CON", "PRN", "AUX", "NUL"}:
            return True
        return bool(re.fullmatch(r"(?:COM|LPT)[1-9]", stem))

    @staticmethod
    def _md_to_plain(md: str) -> str:
        """Markdown → 纯文本：剥 HTML 表格标签（单元格转 tab）、标题井号、
        粗斜体、链接图片、代码围栏与列表符，保留行结构。"""
        s = md or ""
        s = re.sub(r"</t[hd]>", "\t", s, flags=re.I)
        s = re.sub(r"</tr>", "\n", s, flags=re.I)
        s = re.sub(r"<[^>\n]{1,120}>", "", s)                 # 剩余 HTML 标签
        s = re.sub(r"^#{1,6}\s+", "", s, flags=re.M)          # 标题
        s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)              # 粗体
        s = re.sub(r"\*([^*\n]+)\*", r"\1", s)                # 斜体
        # 围栏必须先于行内代码处理：否则 ```py 会被行内代码规则吃掉一对反引号，
        # 语言标记（py / c++ / javascript）残留在正文里（实测）。
        s = re.sub(r"(?:```|~~~)[A-Za-z0-9_+#.\-]*[ \t]*\r?\n?", "", s)
        s = re.sub(r"`([^`]+)`", r"\1", s)                    # 行内代码
        s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)      # 链接 / 图片
        s = re.sub(r"^[\s>]*[-*+]\s+", "", s, flags=re.M)     # 列表符
        s = re.sub(r"[ \t]+\n", "\n", s)
        return s.strip()

    def _save_output(self, name: str, result: dict, fmt: str = "md") -> Path:
        """识别结果落盘。fmt="md"（默认）或 "txt"（纯文本）。

        先写 `<out>.tmp` 再 os.replace：中途崩溃/被杀不会留下半截结果文件
        （旧实现直接 write_text，断电或 os._exit 会留下截断的 .md）。
        """
        OUTPUT_DIR.mkdir(exist_ok=True)
        name = self._sanitize_name(name)          # 兜底：调用方已净化也不亏
        ext = "txt" if fmt == "txt" else "md"
        out = OUTPUT_DIR / f"{name}.{ext}"
        i = 1
        while out.exists():
            out = OUTPUT_DIR / f"{name} ({i}).{ext}"
            i += 1
        pages = result.get("pages") or [result.get("text", "")]
        if fmt == "txt":
            lines = [name, ""] if len(pages) > 1 else []
            if len(pages) > 1:
                for i, p in enumerate(pages):
                    lines += [tr("out.pageTxt", n=i + 1),
                              self._md_to_plain(p), ""]
            else:
                lines += [self._md_to_plain(pages[0]), ""]
        else:
            lines = [f"# {name}", ""]
            if len(pages) > 1:
                for i, p in enumerate(pages):
                    lines += [tr("out.pageMd", n=i + 1), "", p.strip(), ""]
            else:
                lines += [pages[0].strip(), ""]
        tmp = out.with_name(out.name + ".tmp")
        try:
            tmp.write_text("\n".join(lines), encoding="utf-8")
            os.replace(tmp, out)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return out

    def cancel(self) -> bool:
        """请求取消当前任务（下载/识别页间生效）。"""
        self.cancel_event.set()
        return True

    def wait_for_tasks(self, timeout: float = 2.0) -> bool:
        """有界等待后台任务收尾，返回 True＝已全部结束。

        退出路径专用：先 cancel_all() 再最多等 timeout 秒——下载因此留下可
        续传残档、识别不静默丢结果；随后调用方仍是 os._exit 硬退出（torch/
        DLL 正常卸载有卡死实录，不能改回 sys.exit）。
        """
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            if not self.has_running_tasks():
                return True
            time.sleep(0.05)
        return not self.has_running_tasks()

    # ── 目录扫描（前端「选目录批量」）──────────────────────────
    def scan_dir(self, path: str, recursive: bool = True,
                 max_files: int = 500, max_depth: int = 8) -> dict:
        """列出目录下的可识别文件（只回路径，不回传内容）。

        前端拿到路径后走 `/api/recognize` 的 `{"paths": [...]}` 直读形式。
        安全边界（对应 E5）：只接受存在的绝对目录；不跟随符号链接/目录联接
        （`os.scandir` + `follow_symlinks=False`，链接项直接跳过）；深度与
        文件数双限；扩展名白名单（SCAN_EXTS）；单个目录读取失败跳过而不整体
        失败；结果按路径排序，稳定可复现。
        """
        bad = {"ok": False, "error": tr("err.badPath", path=path if path else "")}
        raw = str(path or "").strip().strip('"')
        if not raw:
            return bad
        root = Path(os.path.expandvars(raw)).expanduser()
        try:
            if (not root.is_absolute() or _is_link_like(root)
                    or not root.is_dir()):
                return bad
        except OSError:
            return bad

        limit = max(1, min(_as_int(max_files) or 500, MAX_SCAN_FILES))
        depth_limit = max(1, min(_as_int(max_depth) or 8, MAX_SCAN_DEPTH))

        files: list[dict] = []
        truncated = False
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and not truncated:
            cur, depth = stack.pop()
            try:
                entries = list(os.scandir(cur))
            except OSError:
                continue                       # 单个目录读不了 → 跳过
            entries.sort(key=lambda e: e.name.lower())
            for entry in entries:
                try:
                    if _is_link_like(entry.path):
                        continue               # 不跟随符号链接/目录联接
                    if entry.is_dir(follow_symlinks=False):
                        if recursive and depth < depth_limit:
                            stack.append((Path(entry.path), depth + 1))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    ext = Path(entry.name).suffix.lower()
                    if ext not in SCAN_EXTS:
                        continue
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                if len(files) >= limit:
                    # 只有真的还有「第 limit+1 个」符合条件的文件才算截断；
                    # 恰好等于上限时不能误报（前端会提示"只取了前 N 个"）。
                    truncated = True
                    break
                files.append({"path": entry.path, "name": entry.name,
                              "size": int(size), "ext": ext})
        files.sort(key=lambda f: f["path"].lower())
        return {"ok": True, "files": files, "count": len(files),
                "truncated": truncated}

    # ── 历史输出 ─────────────────────────────────────────────
    def list_history(self) -> dict:
        try:
            items = []
            for p in sorted((q for q in OUTPUT_DIR.iterdir()
                             if q.suffix.lower() in (".md", ".txt")),
                            key=lambda x: x.stat().st_mtime,
                            reverse=True)[:50]:
                items.append({"name": p.name, "path": str(p),
                              "mtime": int(p.stat().st_mtime),
                              "size": p.stat().st_size})
            return {"items": items}
        except Exception:
            return {"items": []}

    def history_read(self, name: str) -> dict:
        """按文件名读取历史结果（防路径穿越：只允许 outputs/ 下的 .md/.txt）。"""
        p = self._history_path(name)
        if not p.is_file():
            return {"ok": False, "error": tr("err.noRecord")}
        try:
            text = p.read_text(encoding="utf-8")
            return {"ok": True, "name": p.name, "text": text}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def history_delete(self, name: str) -> dict:
        p = self._history_path(name)
        if not p.is_file():
            return {"ok": False, "error": tr("err.noRecord")}
        try:
            recycle = self._recycle_file(p)
            return {"ok": True, "recycled": recycle}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _history_path(self, name: str) -> Path:
        """历史文件名 → outputs/ 下的实际路径（保持 .md/.txt 后缀）。"""
        safe = self._sanitize_name(Path(name).name)
        if not safe.lower().endswith((".md", ".txt")):
            safe += ".md"
        return OUTPUT_DIR / safe

    @staticmethod
    def _recycle_file(p: Path) -> bool:
        """删除到回收站（优先）；失败回退直接删除。"""
        try:
            try:
                from send2trash import send2trash
                send2trash(str(p))
                return True
            except ImportError:
                pass
            # PowerShell VisualBasic 删除到回收站（免第三方依赖）；
            # 文件名单引号翻倍转义，防注入 / 提前闭合字符串
            ps = ("Add-Type -AssemblyName Microsoft.VisualBasic; "
                  f"[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
                  f"'{str(p).replace(chr(39), chr(39) * 2)}', "
                  f"'OnlyErrorDialogs', 'SendToRecycleBin')")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=30)
            return not p.exists()
        except Exception:
            p.unlink(missing_ok=True)
            return False

    # ── Python 解释器切换（源码运行；issue #2）────────────────
    @staticmethod
    def normalize_python_path(raw: str) -> str:
        """用户输入 → python.exe 绝对路径。接受 exe 或 venv 根目录。

        目录形式自动补 Scripts\\python.exe（Windows）/ bin/python（其他）；
        路径不存在抛 EngineError，由调用方转成界面提示。
        """
        raw = (raw or "").strip().strip('"')
        if not raw:
            return ""
        p = Path(os.path.expandvars(raw)).expanduser()
        if not p.exists():
            raise EngineError(tr("py.pathMissing", path=str(p)))
        if p.is_dir():
            exe = p / ("Scripts/python.exe" if sys.platform == "win32"
                       else "bin/python")
            if not exe.is_file():
                raise EngineError(tr("py.notVenvDir", path=str(p)))
            p = exe
        return str(p.resolve())

    def probe_python(self, raw: str) -> dict:
        """隔离探测目标解释器：版本 + torch/CUDA + transformers + pywebview。

        子进程用 -I（隔离模式）起，cwd 指向临时目录——不继承本项目目录，
        也不会误加载用户 PYTHONPATH 上的同名模块。torch 冷导入可达数十秒，
        超时放宽到 120s。返回 dict（含 error 字段＝探测失败说明）。
        """
        try:
            exe = self.normalize_python_path(raw)
        except EngineError as e:
            return {"ok": False, "path": raw, "error": str(e)}
        if not exe:
            return {"ok": False, "path": "", "error": tr("py.emptyPath")}
        cached = self._probe_cache
        if cached[0] == exe and time.time() - cached[1] < 60:
            return dict(cached[2])
        code = (
            "import json, sys\n"
            "info = {'python': sys.executable,\n"
            "         'version': sys.version.split()[0], 'ok': True}\n"
            "for m, key in (('torch', 'torch'),\n"
            "                ('transformers', 'transformers'),\n"
            "                ('webview', 'pywebview')):\n"
            "    try:\n"
            "        mod = __import__(m)\n"
            "        info[key] = getattr(mod, '__version__', '') or 'ok'\n"
            "    except Exception:\n"
            "        info[key] = ''\n"
            "try:\n"
            "    import torch\n"
            "    info['cudaBuild'] = torch.version.cuda or ''\n"
            "    info['cuda'] = bool(torch.cuda.is_available())\n"
            "    info['cudaName'] = torch.cuda.get_device_name(0) if info['cuda'] else ''\n"
            "    info['vramGb'] = round(torch.cuda.get_device_properties(0)"
            ".total_memory / 1e9, 1) if info['cuda'] else 0.0\n"
            "except Exception as e:\n"
            "    info['cuda'] = False\n"
            "    info['cudaBuild'] = ''\n"
            "    info['probeError'] = type(e).__name__ + ': ' + str(e)[:200]\n"
            "print(json.dumps(info))\n"
        )
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            r = subprocess.run(
                [exe, "-I", "-c", code], capture_output=True, timeout=120,
                creationflags=flags, cwd=tempfile.gettempdir())
        except subprocess.TimeoutExpired:
            return {"ok": False, "path": exe, "error": tr("py.probeTimeout")}
        except OSError as e:
            return {"ok": False, "path": exe, "error": str(e)}
        if r.returncode != 0:
            tail = decode_child_text(r.stderr or b"").strip()
            return {"ok": False, "path": exe,
                    "error": tr("py.probeFailed", err=tail.splitlines()[-1][:200]
                                if tail else f"exit {r.returncode}")}
        try:
            info = json.loads(decode_child_text(r.stdout)
                              .strip().splitlines()[-1])
        except Exception:
            return {"ok": False, "path": exe, "error": tr("py.probeBadOutput")}
        info["path"] = info.get("python", exe)
        # 三件套齐全才算「可用于重启」：torch（含 CUDA 与否交给界面展示）、
        # transformers、pywebview（缺了起不了原生窗口，仅降级 Edge）
        info["depsOk"] = bool(info.get("torch")) and bool(info.get("transformers"))
        self._probe_cache = (exe, time.time(), dict(info))
        return info

    def browse_file(self, start: str = "", title: str = "") -> dict:
        """原生「选择文件」对话框（选 python.exe 用）。"""
        try:
            import tkinter as tk
            from tkinter import filedialog
            if not title:
                title = tr("dlg.pickPython")
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                initial = str(start) if start and Path(start).exists() else str(BASE_DIR)
                initialfile = Path(initial).name if Path(initial).suffix.lower() == ".exe" else ""
                path = filedialog.askopenfilename(
                    title=title, initialdir=initial,
                    initialfile=initialfile,
                    filetypes=[("Python", "python*.exe"), ("EXE", "*.exe"),
                               ("All", "*.*")],
                    parent=root)
            finally:
                root.destroy()
            return {"ok": bool(path), "path": path or ""}
        except Exception as e:
            return {"ok": False, "path": "", "error": str(e)}

    def apply_python(self, raw: str) -> dict:
        """保存 pythonPath 并以目标解释器重启应用。

        raw 为空 = 恢复默认（重启回启动器选定的 venv）。重启 = 先以
        CREATE_BREAKAWAY_FROM_JOB 拉起新进程（本进程在 KILL_ON_JOB_CLOSE 的
        Job 里，新进程若继承会被连带杀掉），应答返回后再退出本进程。
        """
        if getattr(sys, "frozen", False):
            return {"ok": False, "error": tr("py.frozenUnsupported")}
        try:
            exe = self.normalize_python_path(raw)
        except EngineError as e:
            return {"ok": False, "error": str(e)}
        if exe and os.path.normcase(exe) == os.path.normcase(sys.executable) \
                and not self._settings_raw().get("pythonPath"):
            return {"ok": False, "error": tr("py.sameInterpreter")}
        # 依赖未过探测（前端「检测」按钮已跑过）也放行重启——探测是建议性
        # 前置；但这里再拦一次「torch/transformers 双缺」的明显坏选择，
        # 避免一键把应用重启进起不来的环境。
        if exe:
            probe = self.probe_python(exe)
            if not probe.get("depsOk"):
                return {"ok": False, "error": tr("py.probeNotOk", err=probe.get("error") or "")}
        self.settings.update({"pythonPath": exe})
        target = exe or os.environ.get(BASE_PYTHON_ENV, "") or sys.executable
        log_info(f"切换 Python 解释器 → {target or '(默认)'}；重启进程")
        threading.Thread(target=self._restart_into, args=(target,),
                         name="python-restart", daemon=True).start()
        return {"ok": True, "restarting": True, "pythonPath": exe}

    def _restart_into(self, target_exe: str):
        """拉起 target_exe 下的同参数新进程，然后硬退出本进程。"""
        time.sleep(0.4)                       # 让 HTTP 应答先落到前端
        argv = [target_exe] + [str(a) for a in sys.argv]
        flags = subprocess.CREATE_BREAKAWAY_FROM_JOB \
            if sys.platform == "win32" else 0
        try:
            subprocess.Popen(argv, creationflags=flags, close_fds=False)
        except OSError as e:
            # 拉不起来（多见于 Job 不容脱离）：不退出，保留当前会话；设置已
            # 落盘，下次手动重启即按新解释器走（app_webview 的 reexec 检查）。
            log_error("重启失败（新解释器进程未能启动），请手动重启应用", exc=e)
            return
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(0)

    # ── 桌面辅助 ─────────────────────────────────────────────
    def open_output_dir(self) -> bool:
        try:
            os.startfile(str(OUTPUT_DIR))   # noqa: S606
            return True
        except Exception:
            return False

    def browse_folder(self, start: str = "", title: str = "") -> dict:
        try:
            import tkinter as tk
            from tkinter import filedialog
            if not title:
                title = tr("dlg.pickFolder")
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                start_dir = start if start and Path(start).exists() else str(BASE_DIR)
                path = filedialog.askdirectory(
                    title=title, initialdir=start_dir, parent=root)
            finally:
                root.destroy()
            return {"ok": bool(path), "path": path or ""}
        except Exception as e:
            return {"ok": False, "path": "", "error": str(e)}

    # 关窗确认（app_webview 的 closing 用）
    def cancel_all(self):
        try:
            self.cancel_event.set()
        except Exception:
            pass

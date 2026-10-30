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

import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from applog import log_download, log_error, log_info, log_model, log_ocr
from downloader import CancelledError, download_model, quick_check, set_mirror
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
    "outFormat": "md",     # 识别结果保存格式：md | txt
    "theme": "light",
    "scale": 100,
    "lang": "zh",          # 界面语言，默认中文（en | zh | es；i18n.py / i18n.js）
}


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
        self._task_label = ""
        self.cancel_event = threading.Event()
        # 界面语言（后端 SSE 进度/错误文案与原生对话框同语言）
        set_lang(self._settings_raw().get("lang"))
        # 下载/加载期间用户改了设置 → 需要重读 mirror/model dir
        self._apply_mirror_from_settings()
        # 记录加载期快照：状态字段反映真正加载的设备/目录
        self._loaded_device = None

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
                "outFormat": cur.get("outFormat", "md"),
                "theme": cur.get("theme", "light"),
                "scale": cur.get("scale", 100),
                "lang": cur.get("lang", "zh"),
                "languages": available_languages(),
                "defaultModelDir": str(BASE_DIR / MODEL_SUBDIR)}

    def set_settings(self, patch: dict) -> dict:
        patch = {k: v for k, v in (patch or {}).items() if k in DEFAULT_SETTINGS}
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
            "torch": dev.get("torch"),
            "transformers": dev.get("transformers"),
            "cuda": dev.get("cuda", False),
            "cudaName": dev.get("cudaName", ""),
            "vramGb": dev.get("vramGb", 0.0),
            "busy": self.has_running_tasks(),
            "busyLabel": self._task_label or "",
        }

    def has_running_tasks(self) -> bool:
        with self._lock:
            return self.downloading or self.loading or self.recognizing

    def running_task_label(self) -> str:
        with self._lock:
            return self._task_label or ""

    # ── 模型下载 ─────────────────────────────────────────────
    def start_download(self) -> dict:
        with self._lock:
            if self.downloading or self.loading or self.recognizing:
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
            if not quick_check(self._model_dir()):
                return {"ok": False, "error": tr("err.modelMissing")}
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

    # ── OCR 识别 ─────────────────────────────────────────────
    def recognize(self, opts: dict, progress_cb=None) -> dict:
        """opts: {paths: [str], name: 原始主文件名, prompt: str, mode: "merged"|"batch"}

        进度经 progress_cb(pct, status)（server 转 SSE）；返回结果 dict。

        merged（默认）：全部文件作为同一文档的多页合并为一次请求（跨页上下文）。
        batch：批量识别——每个文件独立识别、独立出结果，失败隔离，可随时取消
        （当前文件即时中断，其余跳过）。
        """
        with self._lock:
            if self.recognizing or self.loading or self.downloading:
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
                result = self.engine.recognize(paths, prompt=prompt,
                                               progress_cb=_cb,
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
            return {
                "mode": "merged",
                "text": result.get("text", ""),
                "pages": result.get("pages", []),
                "outPath": str(out_path),
                "elapsed": elapsed,
                "cancelled": False,
            }
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
                    result = self.engine.recognize([path], prompt=prompt,
                                                   progress_cb=_file_cb,
                                                   cancel_event=self.cancel_event)
                    out_path = self._save_output(
                        self._sanitize_name(Path(display).stem), result, fmt)
                    chars = len(result.get("text", ""))
                    items.append({"name": display, "ok": True,
                                  "text": result.get("text", ""),
                                  "pages": result.get("pages", []),
                                  "outPath": str(out_path),
                                  "elapsed": result.get("elapsed", 0),
                                  "chars": chars})
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
                    items.append({"name": display, "ok": False,
                                  "error": str(e)[:300], "text": "",
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
        banned = '<>:"/\\|?*'
        name = "".join(c for c in name if c not in banned).strip().rstrip(".")
        return (name or "result")[:80]

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
        s = re.sub(r"`([^`]+)`", r"\1", s)                    # 行内代码
        s = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", s)      # 链接 / 图片
        s = re.sub(r"```[a-zA-Z0-9]*\n?", "", s)              # 代码围栏
        s = re.sub(r"^[\s>]*[-*+]\s+", "", s, flags=re.M)     # 列表符
        s = re.sub(r"[ \t]+\n", "\n", s)
        return s.strip()

    def _save_output(self, name: str, result: dict, fmt: str = "md") -> Path:
        """识别结果落盘。fmt="md"（默认）或 "txt"（纯文本）。"""
        OUTPUT_DIR.mkdir(exist_ok=True)
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
        out.write_text("\n".join(lines), encoding="utf-8")
        return out

    def cancel(self) -> bool:
        """请求取消当前任务（下载/识别页间生效）。"""
        self.cancel_event.set()
        return True

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

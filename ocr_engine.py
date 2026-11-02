"""ocr_engine.py — Unlimited-OCR-2 推理引擎（transformers trust_remote_code）

模型：baidu/Unlimited-OCR-2（MIT，~3.3B 参数 MoE + 视觉编码器，BF16 safetensors）
  https://github.com/baidu/Unlimited-OCR
  https://huggingface.co/baidu/Unlimited-OCR-2

加载：AutoTokenizer / AutoModel + trust_remote_code=True，从本地模型目录读取
（目录内容由 downloader.py 预先下载齐）。上游验证的版本组合：
  torch 2.13 / transformers 4.52.1 / huggingface-hub 0.36.0。

设备：CUDA 可用 → cuda + bfloat16；否则 CPU + bfloat16（模型自定义代码全程
按 BF16 设计，上游 CPU 稀疏注意力路径带 is_cuda 门控的 triton 内核回退，
纯 CPU 可跑，速度约每秒数 token 属正常）。

识别：model.infer() 单入口——传文件列表即「多页合并解析」（一次请求带跨页
上下文，输出按 <PAGE> 分页）；传单图默认 gundam 动态切片。取消在逐页模式
的页间生效；单次大请求内不可中断（与引擎实现一致，界面会如实提示）。
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path

from downloader import RUNTIME_DIR, installed_torch_info, quick_check
from i18n import tr
from ocr_postprocess import PAGE_SPLIT_RE, to_gui_markdown

DEFAULT_PROMPT = "Multi page parsing."   # 上游 vLLM 部署的默认指令

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


def _setup_hf_cache(cache_root: Path):
    """把 transformers 动态模块缓存重定向到程序目录（便携部署）。

    trust_remote_code 会把模型 .py 复制到 HF_MODULES_CACHE（默认
    ~/.cache/huggingface/modules）。重定向到 <程序目录>/models/_hf_cache：
    ① 不污染用户主目录；② 受限环境（无主目录写权限）也能运行。
    必须在 transformers **首次 import 之前**调用（其缓存路径常量在
    import 时固化）——故在模块顶层执行；用户显式设置的同名环境变量优先。
    """
    os.environ.setdefault("HF_MODULES_CACHE", str(cache_root / "modules"))
    os.environ.setdefault("HF_HOME", str(cache_root))


_setup_hf_cache(Path(__file__).resolve().parent / "models" / "_hf_cache")

# ── 流式解码钩子：实时进度 + 真正可中断 ──────────────────────────
# 模型解码循环每个 token 调一次 streamer.put()（TextStreamer 标准扩展点
# on_finalized_text）。钩子子类在 load() 末尾注入 modeling 模块的全局名
# （`TextStreamer` 在 _uocr_generate 里是调用时查表）。on_finalized_text：
#   • 累计已生成可见字数 → stream_state，供界面显示「已生成 N 字」
#   • cancel_event 置位时抛 OcrCancelledError → 异常沿解码循环向外传播，
#     单次合并请求即刻中断（模型侧 try/finally 会恢复注意力配置）
# 钩子经模块级 _STREAM_HOOK 间接引用 cancel_event/state（类对象在 load 时
# 创建，而二者是每次识别变化的对象）。

_STREAM_HOOK: dict = {"cancel_event": None, "state": None}
_TAG_RE = re.compile(r"<[^>\n]{1,40}>")


class OcrCancelledError(RuntimeError):
    """解码中检测到用户取消（由流式钩子抛出以中断推理）。"""


def _make_hooked_streamer(base_cls):
    class _HookedTextStreamer(base_cls):
        def on_finalized_text(self, text: str, stream_end: bool = False):
            state = _STREAM_HOOK.get("state")
            if state is not None and text:
                state["chars"] += len(_TAG_RE.sub("", text))
            event = _STREAM_HOOK.get("cancel_event")
            if event is not None and event.is_set():
                print(f"[stream-hook] 取消生效，已生成 {state['chars'] if state else '?'} 字",
                      flush=True)
                raise OcrCancelledError("用户取消")
    return _HookedTextStreamer


# ── 推理引擎组件（torch）的运行时接线 ────────────────────────────
# 安装包本体不含 torch：使用者在「模型」页按硬件下载 cpu/cu128 组件到
# runtime/（见 downloader._TORCH_FLAVORS）。这里把该目录插到 sys.path 最前，
# 让 import torch 命中外部组件而非冻结 PYZ／venv；目录不存在时是空操作
# （dev 环境走 venv 的 torch）。进程中途装完组件也要生效：重扫 sys.path。
def _ensure_runtime_path():
    """runtime/ 组件目录存在 → 插到 sys.path 最前并刷新模块查找缓存。"""
    from downloader import RUNTIME_DIR
    if str(RUNTIME_DIR) in sys.path or not (RUNTIME_DIR / "torch").is_dir():
        return
    sys.path.insert(0, str(RUNTIME_DIR))
    if sys.platform == "win32":
        lib = RUNTIME_DIR / "torch" / "lib"
        if lib.is_dir():
            os.add_dll_directory(str(lib))   # torch/lib 的 CUDA DLL 解析目录
    import importlib
    importlib.invalidate_caches()
    # transformers 在其模块导入时就固化 is_torch_available（4.52 于
    # import_utils 顶层求值一次）。若那次求值发生在组件安装前（启动早期的
    # 状态轮询就会 import transformers），这里 torch 刚可见，必须把缓存
    # 纠正回来，否则 AutoModel 会报「requires the PyTorch library」。
    try:
        from transformers.utils import import_utils as _iu
        if not getattr(_iu, "_torch_available", False):
            import importlib.util as _ilu
            if _ilu.find_spec("torch") is not None:
                import torch as _torch_mod
                _iu._torch_available = True
                _iu._torch_version = _torch_mod.__version__
    except Exception:
        pass


_ensure_runtime_path()   # transformers 的 is_torch_available 首次求值会缓存
                         # 结果——必须让 torch spec 在其之前可见

_transformers = None


def _ensure_transformers():
    """懒加载 transformers（torch 组件可能进程中途才装好，不能在模块导入期
    就固化「torch 不可用」的判断）。返回 None＝transformers 本体缺失。"""
    global _transformers
    if _transformers is None:
        _ensure_runtime_path()
        try:
            import transformers
            _transformers = transformers
        except Exception:                  # pragma: no cover
            _transformers = None
    return _transformers


class EngineError(RuntimeError):
    pass


# ── CPU 兼容补丁 ─────────────────────────────────────────────────
# 模型源码有两处硬编码 .cuda()（masked_scatter_ 的掩码搬运，
# modeling_unlimitedocr.py:785/789），CPU 设备上会直接抛错。CPU 加载时把
# Tensor.cuda 临时替换为 no-op（张量本来就在目标设备）；切回 CUDA 加载时
# 恢复原实现。仅影响本进程，不改写下载的模型源码。
_ORIG_TENSOR_CUDA = None


def _patch_tensor_cuda(enable: bool):
    global _ORIG_TENSOR_CUDA
    import torch
    if enable and _ORIG_TENSOR_CUDA is None:
        _ORIG_TENSOR_CUDA = torch.Tensor.cuda

        def _cuda_noop(self, *args, **kwargs):
            return self
        torch.Tensor.cuda = _cuda_noop
    elif not enable and _ORIG_TENSOR_CUDA is not None:
        torch.Tensor.cuda = _ORIG_TENSOR_CUDA
        _ORIG_TENSOR_CUDA = None


def _torch():
    _ensure_runtime_path()
    try:
        import torch
        return torch
    except Exception as e:   # pragma: no cover
        # 原始异常必须留痕：组件装好却仍导入失败时（如缺 sympy 等 torch 的
        # 纯 Python 依赖），只看 EngineError 文案无从定位。
        from applog import log_error
        log_error("import torch 失败（runtime 组件）", exc=e)
        raise EngineError(tr("err.noTorchComponent")) from e


class OcrEngine:
    """封装 Unlimited-OCR-2。recognize() 加互斥锁，多线程安全。"""

    def __init__(self, model_dir: Path):
        self.model_dir = Path(model_dir)
        self.ready = False
        self._lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.model = None
        self.tokenizer = None
        self.device = None
        self.dtype_name = "bfloat16"
        self._error = None
        self.stream_state = {"chars": 0}   # 当前识别已生成字数（流式钩子更新）

    def generated_chars(self) -> int:
        """当前（或最近一次）识别已生成的可见字数。"""
        return self.stream_state.get("chars", 0)

    # ── 状态 ─────────────────────────────────────────────────
    def files_ready(self) -> bool:
        return quick_check(self.model_dir)

    @property
    def error(self) -> str | None:
        return self._error

    def report_error(self, msg: str):
        """加载线程等外部写入失败信息（error 是只读属性，别直接碰 _error）。"""
        self._error = str(msg)

    def device_info(self) -> dict:
        """torch/CUDA 检测（不依赖模型已加载）。

        cudaBuild 是 torch.version.cuda（CPU 版 torch 为 None/""）——issue #3
        的教训：安装包打进 CPU 版 torch 时 cuda 恒为 False，必须和「有 CUDA
        版 torch 但没驱动/没卡」区分开，提示语才指得到正解。

        torch 不可导入（组件未装）时，torch 和 transformers **都不碰**：
          • 反复 import torch 部分初始化在冻结环境会 native abort（无痕杀
            进程，实录 ucrtbase 0xc0000409）；失败后 60s 内不重试。
          • import transformers 更致命——4.52 在导入期按「torch 是否可见」
            固化 is_torch_available 并注册懒加载类，先导入会让之后装好组件
            也救不回（AutoModel 永远 requires the PyTorch library）。
        torch 可见性用 find_spec 探测（零导入成本）：dev 命中 venv 的 torch、
        冻结环境命中 runtime/ 组件，两侧语义一致。
        """
        info = {"torch": None, "cuda": False, "cudaName": "", "vramGb": 0.0,
                "cudaBuild": "", "transformers": None}
        _ensure_runtime_path()
        import importlib.util as _ilu
        if 'torch' not in sys.modules:
            spec = _ilu.find_spec("torch")
            if spec is None:
                return info
            # 组件解压进行中 find_spec 也会命中（torch/__init__.py 早期落
            # 地），此时导入只会半途失败并毒化 transformers 的可用性缓存
            # （它以这次导入为准）。runtime 来源必须等安装标记落地。
            origin = spec.origin or ""
            if origin.startswith(str(RUNTIME_DIR)) \
                    and installed_torch_info() is None:
                return info
        frozen = bool(getattr(sys, "frozen", False))
        if frozen and 'torch' not in sys.modules \
                and time.time() - device_info._torch_fail_at < 60:
            return info
        try:
            import transformers
            info["transformers"] = transformers.__version__
        except Exception:
            pass
        try:
            torch = _torch()
            info["torch"] = torch.__version__
            info["cudaBuild"] = torch.version.cuda or ""
            info["cuda"] = bool(torch.cuda.is_available())
            if info["cuda"]:
                try:
                    info["cudaName"] = torch.cuda.get_device_name(0)
                    props = torch.cuda.get_device_properties(0)
                    info["vramGb"] = round(props.total_memory / 1e9, 1)
                except Exception:
                    pass
        except EngineError:
            if frozen:
                device_info._torch_fail_at = time.time()
        return info

    _torch_fail_at = 0.0   # 冻结环境 import torch 最近失败时刻（负缓存）

    @staticmethod
    def _runtime_flavor_hint() -> str:
        """device_info 用的组件 flavor 线索（纯文件级，不跑硬件检测）。"""
        from downloader import installed_torch_info
        return (installed_torch_info() or {}).get("flavor") or "cpu"

    # ── 加载 ─────────────────────────────────────────────────
    def load(self, device_pref: str = "auto", cb=None):
        """从背景线程调用；cb(msg) 更新界面状态。

        device_pref: "auto" | "cuda" | "cpu"
        """
        torch = _torch()
        if not self.files_ready():
            raise EngineError(tr("err.modelIncomplete"))

        def _s(msg):
            if cb:
                cb(msg)

        self._error = None
        # 设备解析放最前（issue #3）：选了 CUDA 但用不了时，在加载任何权重
        # 之前就快速失败，并区分两种病因——
        #   • torch.version.cuda 为空：安装包打进去的是 CPU 版 torch，GPU 永远
        #     不可见（v1.1.2 发行包即此病），换驱动无用，得换包；
        #   • 否则是有 CUDA 版 torch 但没驱动/没卡，走原 noCudaDevice 提示。
        cuda_ok = bool(torch.cuda.is_available())
        if device_pref == "cpu":
            device = "cpu"
        elif device_pref == "cuda" and not cuda_ok:
            if not getattr(torch.version, "cuda", None):
                raise EngineError(
                    tr("err.torchCpuBuild", ver=torch.__version__))
            raise EngineError(tr("err.noCudaDevice"))
        else:
            device = "cuda" if cuda_ok else "cpu"
        _patch_tensor_cuda(device == "cpu")

        transformers_mod = _ensure_transformers()
        if transformers_mod is None:
            raise EngineError(tr("err.noTransformers"))
        _s(tr("eng.loadingTokenizer"))
        AutoTokenizer = transformers_mod.AutoTokenizer
        AutoModel = transformers_mod.AutoModel
        self.tokenizer = AutoTokenizer.from_pretrained(
            str(self.model_dir), trust_remote_code=True)

        _s(tr("eng.loadingWeights", device=device))
        t0 = time.time()
        model = AutoModel.from_pretrained(
            str(self.model_dir), trust_remote_code=True, use_safetensors=True,
            torch_dtype=torch.bfloat16)
        model = model.eval()
        model.to(device)

        # 流式钩子注入：modeling 模块的 `from transformers import TextStreamer`
        # 走 transformers 动态模块加载器，包属性补丁对它无效（实测绑定仍是原
        # 类）。但 `_uocr_generate` 是**调用时**才查模块全局名——加载完成后直接
        # 改写该模块字典，确定生效。
        hooked = _make_hooked_streamer(transformers_mod.TextStreamer)
        for name, mod in list(sys.modules.items()):
            if name.endswith("modeling_unlimitedocr") and hasattr(mod, "TextStreamer"):
                mod.TextStreamer = hooked
                break

        with self._lock:
            self.model = model
            self.device = device
            self.ready = True
        _s(tr("eng.readyInfo", device=device, secs=f"{time.time() - t0:.0f}"))

    # ── PDF → 页图 ───────────────────────────────────────────
    @staticmethod
    def pdf_to_images(pdf_path: str | Path, dpi: int = 200,
                      out_dir: Path | None = None) -> list[Path]:
        """PDF 逐页转 PNG（pymupdf）。缺库时抛带指引的 EngineError。"""
        try:
            import fitz   # pymupdf
        except Exception as e:
            raise EngineError(tr("err.noPymupdf")) from e
        pdf_path = Path(pdf_path)
        out_dir = Path(out_dir or pdf_path.parent)
        doc = fitz.open(str(pdf_path))
        try:
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pages = []
            for i, page in enumerate(doc):
                p = out_dir / f"_ocr_page_{pdf_path.stem}_{i + 1:04d}.png"
                page.get_pixmap(matrix=mat).save(str(p))
                pages.append(p)
            return pages
        finally:
            doc.close()

    # ── 识别 ─────────────────────────────────────────────────
    def recognize(self, image_paths: list[Path], prompt: str = "",
                  progress_cb=None, cancel_event: threading.Event | None = None,
                  max_new_tokens: int | None = None,
                  crop_mode=None) -> dict:
        """识别图片（可多页）。返回 {text, pages: [str], elapsed, tokens}。

        image_paths: 一或多张图片路径（按页序）。
        prompt: 空串 → DEFAULT_PROMPT。
        progress_cb(stage: str, done: int, total: int, elapsed: float)
          —— merged 模式 done/total 只有整体一条；paged 模式逐图推进。

        merged（默认）：一次请求合并全部页（跨页上下文，<PAGE> 分隔输出）。
        单张图片由 model.infer 的 gundam 动态切片处理。
        """
        if not self.ready:
            raise EngineError(tr("err.notLoaded"))
        if not image_paths:
            raise EngineError(tr("err.noImages"))
        prompt = (prompt or "").strip() or DEFAULT_PROMPT

        cancel_event = cancel_event or self.cancel_event
        t0 = time.time()
        results: list[str] = []
        # 流式钩子：识别期间 on_finalized_text 会累计字数并检查取消
        self.stream_state = {"chars": 0}
        _STREAM_HOOK["cancel_event"] = cancel_event
        _STREAM_HOOK["state"] = self.stream_state
        try:
            # PDF 不是单张位图：先逐页转 PNG（临时文件，推理后清理）。
            import tempfile as _tempfile
            expanded: list[Path] = []
            tmp_dirs: list[str] = []
            try:
                for p in image_paths:
                    if p.suffix.lower() == ".pdf":
                        tmp_dir = Path(_tempfile.mkdtemp(prefix="ocr_pdf_"))
                        tmp_dirs.append(str(tmp_dir))
                        expanded.extend(self.pdf_to_images(p, out_dir=tmp_dir))
                    else:
                        expanded.append(p)
                if not expanded:
                    raise EngineError(tr("err.noPages"))

                if len(expanded) == 1:
                    if progress_cb:
                        progress_cb(tr("eng.stageGundam"), 0, 1, 0.0)
                    text = self._infer([expanded[0]], prompt, crop_mode=crop_mode,
                                       max_new_tokens=max_new_tokens)
                    results = [to_gui_markdown(text)]
                    if progress_cb:
                        progress_cb(tr("prog.recognizeDone"), 1, 1, time.time() - t0)
                else:
                    # 多页：合并为一次请求（模型的核心能力——跨页上下文）。
                    if progress_cb:
                        progress_cb(tr("eng.stageMerged", n=len(expanded)), 0, 1, 0.0)
                    text = self._infer(expanded, prompt, crop_mode=False,
                                       max_new_tokens=max_new_tokens)
                    parts = [p for p in PAGE_SPLIT_RE.split(text) if p.strip()]
                    results = [to_gui_markdown(p) for p in parts] or [text.strip()]
                    if progress_cb:
                        progress_cb(tr("prog.recognizeDone"), 1, 1, time.time() - t0)
            finally:
                for d in tmp_dirs:
                    try:
                        import shutil
                        shutil.rmtree(d, ignore_errors=True)
                    except OSError:
                        pass
        finally:
            _STREAM_HOOK["cancel_event"] = None
            _STREAM_HOOK["state"] = None

        # 兜底：如果请求被取消（页间检查点），不假装完成
        if cancel_event.is_set():
            raise EngineError(tr("prog.cancelled"))
        elapsed = time.time() - t0
        return {
            "text": "\n\n".join(
                f"<!-- page {i + 1} -->\n{t}" for i, t in enumerate(results))
                if len(results) > 1 else results[0],
            "pages": results,
            "elapsed": elapsed,
        }

    def _infer(self, images: list[Path], prompt: str, crop_mode=None,
               max_new_tokens: int | None = None) -> str:
        """调 model.infer()（加锁串行）。

        eval_mode=False → 解码走流式钩子（实时字数 + 取消中断）；
        钩子子类不打印，控制台保持安静。
        """
        torch = _torch()
        with self._lock:
            try:
                kwargs = dict(
                    prompt=prompt,
                    image_file=[str(p) for p in images],
                    eval_mode=False,
                    temperature=0.0,
                    save_results=False,
                )
                if crop_mode is not None:
                    kwargs["crop_mode"] = crop_mode
                if max_new_tokens:
                    kwargs["max_new_tokens"] = max_new_tokens
                with torch.inference_mode():
                    out = self.model.infer(self.tokenizer, **kwargs)
                return out if isinstance(out, str) else str(out)
            except OcrCancelledError:
                raise EngineError(tr("prog.cancelled"))
            except EngineError:
                raise
            except Exception as e:
                self._error = f"{type(e).__name__}: {e}"
                raise EngineError(
                    tr("err.inference", err=f"{type(e).__name__}: {e}")) from e

    @staticmethod
    def _strip_markers(text: str) -> str:
        """兜底清理（正常路径已由 ocr_postprocess.to_gui_markdown 处理）。"""
        return text.replace("<PAGE>", "").strip()

    def unload(self):
        """释放模型（切换设备/目录时用）。"""
        with self._lock:
            self.model = None
            self.tokenizer = None
            self.ready = False

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
上下文，输出按 <PAGE> 分页）；传单图默认 gundam 动态切片。取消在解码过程中
逐 token 生效；视觉编码/prefill 的单次前向内部不可中断（单页可达数十秒）。
每次推理的 prefill/decode 耗时、token 数、finish_reason 记入 applog 的 OCR
通道，并作为中性字段回给 recognize()（truncated / pagesMismatch / infer）。
"""
from __future__ import annotations

import inspect
import os
import re
import sys
import threading
import time
from pathlib import Path

from downloader import RUNTIME_DIR, installed_torch_info, quick_check
from i18n import tr
from ocr_postprocess import PAGE_SPLIT_RE, to_gui_markdown_with_stats
from page_range import parse_pages as _parse_page_spec

DEFAULT_PROMPT = "Multi page parsing."   # 上游 vLLM 部署的默认指令

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}


def _render_with_stats(raw_text: str, *, drop_headers: bool,
                       join_lines: bool, cjk_spacing: bool):
    """调后处理并返回 (markdown, droppedBlocks, tableWarnings)。

    后处理（ocr_postprocess）由另一路同事加 `join_lines` / `cjk_spacing` 两个
    kwarg 与 `stats["tableWarnings"]`。这里用签名探测**只传它认识的参数**，
    因此任一侧先落地都不炸：对方没落地时两个开关被静默忽略、tableWarnings
    按 0 返回（GUI 侧仍能拿到该字段，语义为"没有告警"）。
    """
    extra = {"join_lines": bool(join_lines), "cjk_spacing": bool(cjk_spacing)}
    kwargs = {"drop_headers": bool(drop_headers)}
    try:
        params = inspect.signature(to_gui_markdown_with_stats).parameters
        names = set(params)
        has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD
                         for p in params.values())
    except (TypeError, ValueError):        # 探测失败：只传最老的安全参数
        names, has_var_kw = set(), False
    for key, value in extra.items():
        if has_var_kw or key in names:
            kwargs[key] = value
    md, stats = to_gui_markdown_with_stats(raw_text, **kwargs)
    return (md, int(stats.get("droppedBlocks", 0) or 0),
            int(stats.get("tableWarnings", 0) or 0))


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
            # 只在解码过程中抛取消。stream_end=True 是模型侧 `_uocr_generate`
            # 的 finally 里 streamer.end() 触发的收尾调用
            # （modeling_unlimitedocr.py:1196-1200）：那里再抛会把先发生的
            # 真实异常（OOM/断言/CUDA 错误）替换成「已取消」，排障时丢真因。
            # 取消与否由 recognize() 末尾的兜底判定（ocr_engine.py 内
            # `if cancel_event.is_set()`）统一裁决，这里只负责尽早中断解码。
            if stream_end:
                return
            event = _STREAM_HOOK.get("cancel_event")
            if event is not None and event.is_set():
                print(f"[stream-hook] 取消生效，已生成 {state['chars'] if state else '?'} 字",
                      flush=True)
                raise OcrCancelledError("用户取消")
    return _HookedTextStreamer


# ── 设备探测缓存（状态轮询高频调用，torch/CUDA 探测不必每次跑）──────
# device_info() 由 backend.get_status() 每次返回都调用（webview_backend.py
# get_status → engine.device_info），而它要 import torch、查 CUDA 可用性、
# 取显存规格。结果缓存 5s；缓存键必须包含「torch 是否可见 + runtime 组件
# 安装标记」——用户中途装完/卸掉 torch 组件后必须立刻失效，否则界面会一直
# 显示「未装 torch」。_ensure_runtime_path() 把组件目录插进 sys.path 的
# 那一刻也主动作废缓存（双保险：标记 mtime 变化 + 显式失效）。
_DEVICE_INFO_TTL = 5.0
_DEVICE_INFO_CACHE: dict = {"ts": -1e18, "key": None, "value": None}


def _invalidate_device_info_cache():
    """作废设备探测缓存（组件安装/卸载、测试隔离时调用）。"""
    _DEVICE_INFO_CACHE.update(ts=-1e18, key=None, value=None)


def _torch_visible() -> bool:
    """torch 是否可见（零导入成本）：已在 sys.modules，或 spec 可解析。"""
    if "torch" in sys.modules:
        return True
    import importlib.util as _ilu
    try:
        return _ilu.find_spec("torch") is not None
    except (ImportError, ValueError):     # 半初始化/命名空间包的怪状态
        return False


def _torch_marker_sig():
    """runtime 组件安装标记（torch_build.json）的 (mtime_ns, size)；未装为 None。

    只 stat 不解析：安装/覆盖组件必然改写该文件，mtime 变化即让缓存失效。
    """
    try:
        st = (RUNTIME_DIR / "torch_build.json").stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


# ── 推理引擎组件（torch）的运行时接线 ────────────────────────────
# 安装包本体不含 torch：使用者在「模型」页按硬件下载 cpu/cu128 组件到
# runtime/（见 downloader._TORCH_FLAVORS）。这里把该目录插到 sys.path 最前，
# 让 import torch 命中外部组件而非冻结 PYZ／venv；目录不存在时是空操作
# （dev 环境走 venv 的 torch）。进程中途装完组件也要生效：重扫 sys.path。
def _ensure_runtime_path():
    """runtime/ 组件目录存在且安装标记落地 → 插到 sys.path 最前并刷新缓存。

    必须以标记为准：解压中途/残缺的 torch 目录（如清理被占用半删）一旦
    上 sys.path，import torch 会得到命名空间包或半残模块——静默失败比
    干脆的 ModuleNotFoundError 更难排查（实测 AttributeError 实录）。
    """
    from downloader import RUNTIME_DIR, installed_torch_info
    if str(RUNTIME_DIR) in sys.path \
            or not (RUNTIME_DIR / "torch").is_dir() \
            or installed_torch_info() is None:
        return
    sys.path.insert(0, str(RUNTIME_DIR))
    _invalidate_device_info_cache()      # 组件刚可见：作废「未装 torch」的探测结果
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


# ── 识别度量与 PDF dpi 归一（A1 / A7）────────────────────────────
_DEFAULT_PDF_DPI = 200
_DPI_MIN, _DPI_MAX = 72, 600


def _normalize_dpi(dpi) -> int:
    """PDF 光栅化 dpi 归一：None/非法 → 200，越界夹到 [72, 600]。

    默认 200 与旧行为完全一致（调用方不传 dpi 时）；上限 600 只是防呆，
    200 dpi 已覆盖模型 1×2 tile（1280 px 高）所需的分辨率。
    """
    try:
        v = _DEFAULT_PDF_DPI if dpi is None else int(dpi)
    except (TypeError, ValueError):
        return _DEFAULT_PDF_DPI
    if v <= 0:
        return _DEFAULT_PDF_DPI
    return max(_DPI_MIN, min(_DPI_MAX, v))


def _summarize_infer(raw: dict | None) -> dict:
    """把 model.infer(return_dict=True) 的返回值收敛成中性度量字段。

    真实键名以模型源码为准：models/Unlimited-OCR-2/modeling_unlimitedocr.py
    的 `_uocr_generate` 返回处（1202-1205 行）——
    text / token_ids / prompt_token_ids / finish_reason / num_pages_out /
    expected_pages / time / prefill_time；`infer` 在 1105-1108 行再补 prompt 后
    原样返回该 dict。模型返回纯 str（兼容分支）时全部度量置 None/False，
    绝不误报截断。
    """
    stats = {"finishReason": None, "outputTokens": None, "prefillSecs": None,
             "decodeSecs": None, "tokensPerSec": None, "numPagesOut": None,
             "expectedPages": None, "truncated": False, "pagesMismatch": False}
    if not isinstance(raw, dict):
        return stats
    finish = raw.get("finish_reason")
    stats["finishReason"] = finish if isinstance(finish, str) else None
    toks = raw.get("token_ids")
    if isinstance(toks, (list, tuple)):
        stats["outputTokens"] = len(toks)
    for src, dst in (("prefill_time", "prefillSecs"), ("time", "decodeSecs")):
        v = raw.get(src)
        if isinstance(v, (int, float)):
            stats[dst] = float(v)
    if stats["prefillSecs"] is not None and stats["decodeSecs"] is not None:
        # 模型侧 time 是「生成总耗时」，prefill_time 是「到首 token」的耗时，
        # 故解码段 = time - prefill_time（模型注释见 _uocr_generate 返回处）。
        dec = max(0.0, stats["decodeSecs"] - stats["prefillSecs"])
        stats["decodeSecs"] = dec
        if dec > 0 and stats["outputTokens"]:
            stats["tokensPerSec"] = round(stats["outputTokens"] / dec, 1)
    for src, dst in (("num_pages_out", "numPagesOut"),
                     ("expected_pages", "expectedPages")):
        v = raw.get(src)
        if isinstance(v, int):
            stats[dst] = v
    # finish_reason 初值是 'length'（1156 行），只有遇到 EOS 才改成 'stop'
    # （1179-1181 行）。因此 'length' = 撞到 max_new_tokens / max_length，
    # 即输出被截断。
    stats["truncated"] = stats["finishReason"] == "length"
    exp, out = stats["expectedPages"], stats["numPagesOut"]
    stats["pagesMismatch"] = bool(exp and exp > 0 and out is not None and out != exp)
    return stats


def _log_infer(stats: dict, n_images: int):
    """把一次推理的度量写进 applog 的 OCR 通道；永不抛异常（applog 约定）。"""
    try:
        from applog import log_ocr
        bits = [f"images={n_images}"]
        if stats.get("prefillSecs") is not None:
            bits.append(f"prefill={stats['prefillSecs']:.1f}s")
        if stats.get("decodeSecs") is not None:
            bits.append(f"decode={stats['decodeSecs']:.1f}s")
        if stats.get("outputTokens") is not None:
            bits.append(f"tokens={stats['outputTokens']}")
        if stats.get("tokensPerSec") is not None:
            bits.append(f"tps={stats['tokensPerSec']}")
        if stats.get("finishReason"):
            bits.append(f"finish={stats['finishReason']}")
        if stats.get("expectedPages") is not None:
            bits.append(f"pages={stats.get('numPagesOut')}/{stats['expectedPages']}")
        if stats.get("truncated"):
            bits.append("TRUNCATED")
        if stats.get("pagesMismatch"):
            bits.append("PAGES-MISMATCH")
        log_ocr("引擎指标：" + " ".join(bits))
    except Exception:
        pass


# ── 生成限幅 / 合并分组 / 切片策略（A2 / A3 / A6）─────────────────
_MODEL_MAX_CONTEXT = 131072          # config.json 的 max_position_embeddings
_PAGE_TOKENS_FLOOR, _PAGE_TOKENS_CEIL = 256, 65536

# CUDA 显存下限（GB）。真机实测（GTX 1660 SUPER 6144 MiB + cu128）：
# 6.3 GB 权重能加载（WDDM 允许超卖到共享内存），VRAM 峰值 5875/6144，
# 但代价是性能悬崖——小图 32s（与 CPU 30s 持平）、长图 1017s / 1.6 tok/s，
# 比 CPU fp32 还慢。8 GB 卡按 README 是推荐档，留 ~1.7 GB 给激活/KV。
_MIN_CUDA_VRAM_GB = 7.5


def _cuda_vram_gb(torch) -> float:
    """当前 CUDA 设备总显存（GB）。不可用/异常 → 0.0。"""
    try:
        if not torch.cuda.is_available():
            return 0.0
        return round(torch.cuda.get_device_properties(0).total_memory / 1e9, 1)
    except Exception:
        return 0.0


def _pick_device(device_pref, cuda_ok, vram_gb) -> tuple:
    """纯函数：解析设备偏好 → (device, note)。

    note ∈ {"", "vram-low-auto", "vram-low-forced", "cuda-unavailable"}：
      · auto + 显存不足 → 用 CPU（"vram-low-auto"）
      · 用户显式选 cuda + 显存不足 → 尊重选择，但打日志（"vram-low-forced"）
      · 选 cuda 但不可用 → 由调用方转成具体错误（这里返回 "cuda-unavailable"）
    """
    if device_pref == "cpu":
        return "cpu", ""
    if not cuda_ok:
        return ("cpu", "") if device_pref != "cuda" else ("cuda", "cuda-unavailable")
    if vram_gb and vram_gb < _MIN_CUDA_VRAM_GB:
        return ("cuda", "vram-low-forced") if device_pref == "cuda" \
            else ("cpu", "vram-low-auto")
    return "cuda", ""


def _normalize_page_tokens(value) -> int:
    """单页生成上限归一：0/None/非法 → 0（不限）；正数夹到 [256, 65536]。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    return max(_PAGE_TOKENS_FLOOR, min(_PAGE_TOKENS_CEIL, n))


def _normalize_merged_pages(value) -> int:
    """合并单请求页数上限归一：0/None/非法/负数 → 0（不限）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _normalize_explicit_max_new_tokens(value) -> int | None:
    """调用方显式给的 max_new_tokens：非法/非正 → None；正数夹到上下文上限。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return max(1, min(_MODEL_MAX_CONTEXT, n))


def _effective_max_new_tokens(page_tokens: int, n_pages: int,
                              explicit: int | None) -> int | None:
    """本次请求实际传给模型的 max_new_tokens。

    None = 不传（模型侧默认 max_length - prompt_len，仍受 131072 约束）；
    否则 = min(单页上限 × 组内页数, 调用方显式值, 模型上下文上限)。
    合并模式按该组页数线性放宽；单页时就是单页上限本身。
    """
    if not page_tokens and not explicit:
        return None
    caps = []
    if page_tokens:
        caps.append(page_tokens * max(1, int(n_pages)))
    if explicit:
        caps.append(int(explicit))
    return max(1, min(min(caps), _MODEL_MAX_CONTEXT))


def _auto_crop_mode(n_images: int, width: int = 0, height: int = 0) -> bool:
    """auto 切片策略（纯函数，可单测）：True=gundam 动态切片，False=base 整页。

    多页合并 → base（跨页上下文优先，且上游只为单图验证过切片）；
    单图 → gundam（与上游默认一致）。

    **不再按长宽比把长截图降级成 base**（v1.3.2 修正）：真机实测 600×3600 的超长图
    在 base 下被压到文字不可读，模型开始胡编（"不要将指导书用绳子或金属棒串…"），
    甚至退化重复（8192 上限内跑了 36 分钟仍在输出）；同一张图 gundam 输出是连贯
    正文。base 省下的 prefill（12.8s vs 40.3s）不值得用正确性换——base 保留为
    用户显式选项，但 auto 不再替用户做这个取舍。
    """
    if n_images > 1:
        return False
    return True


def _resolve_crop_mode(paths, pref):
    """crop_mode 设置 → 传给 model.infer 的值（None 表示不给、用模型默认）。

    pref: None（沿用旧行为，原样返回 None）| "auto" | "gundam" | "base"；
    未知取值按 "auto" 处理（宽容前端传值）。多页时 gundam 降级为 base：
    上游只为单图验证过动态切片，多页切片属于未验证组合。
    """
    if pref is None:
        return None
    if isinstance(pref, bool):          # 兼容旧调用：直接给模型的 True/False
        if not pref:
            return False
        return True if len(paths) <= 1 else False
    p = str(pref).strip().lower()
    if p == "gundam":
        want = True
    elif p == "base":
        want = False
    else:                               # "auto" 及未知取值
        want = None
    if want is None:
        # auto 不再读图片尺寸（长截图规则的取舍已被真机证伪，见 _auto_crop_mode）
        return _auto_crop_mode(len(paths))
    if want and len(paths) > 1:         # 多页不支持 gundam
        return False
    return want


def _plan_groups(expanded: list, merge_limit: int):
    """在**进入推理前**把页切成组：返回 [[page,...], ...]。

    单页不分组（一组一页）；多页按 merge_limit 切（0=不限 → 全部一组）。
    分组是「可取消 + 内存可控」的前提：模型内部无法在中途停下。
    """
    n = len(expanded)
    if n <= 1:
        return [list(expanded)] if n == 1 else []
    size = merge_limit if merge_limit > 0 else n
    return [expanded[i:i + size] for i in range(0, n, size)]


def _log_plan(n_pages: int, total_groups: int, group_size: int,
              crop_mode, page_tokens: int, effective_new, drop_headers: bool):
    """把本次识别的策略写进日志（OCR 通道）；永不抛异常。"""
    try:
        from applog import log_ocr
        crop = {None: "default", True: "gundam", False: "base"}.get(
            crop_mode, str(crop_mode))
        bits = [f"pages={n_pages}", f"groups={total_groups}",
                f"group_size={group_size}", f"crop={crop}",
                f"max_page_tokens={page_tokens or 'unlimited'}",
                f"max_new_tokens={effective_new or 'model-default'}",
                f"drop_headers={bool(drop_headers)}"]
        log_ocr("识别计划：" + " ".join(bits))
        if total_groups > 1:
            log_ocr(f"合并分组：{n_pages} 页 → {total_groups} 组（每组≤{group_size} 页，"
                    f"组间检查取消；跨组不共享上下文，跨页表格/续页可能在组边界断开）")
    except Exception:
        pass


def _log_ocr_safe(msg: str):
    """写一条 OCR 通道日志；永不抛异常（applog 约定 + 本处再兜一层）。"""
    try:
        from applog import log_ocr
        log_ocr(msg)
    except Exception:
        pass


def _sum_tokens(group_stats: list[dict]):
    """多组输出 token 求和；一组都没回传时为 None（而不是 0）。"""
    vals = [g["outputTokens"] for g in group_stats
            if g.get("outputTokens") is not None]
    return sum(vals) if vals else None


def _last_finish(group_stats: list[dict]):
    """最后一线 finish_reason（未知/纯文本返回时 None）。"""
    for g in reversed(group_stats):
        if g.get("finishReason"):
            return g["finishReason"]
    return None


# ── 视觉塔 dtype（A12）：CPU 上 bf16 vs fp32 的实测开关 ──────────
# 背景（报告 01 + 实机数据）：CPU 上单页 prefill:decode ≈ 13:1，瓶颈是视觉塔
# （sam_model + vision_model + projector）。这里把视觉塔单独切到 fp32 做 A/B，
# 主干与 lm_head 仍是 bf16（换整模型 fp32 需 13.4 GB，16 GB 机器会换页）。
VISION_DTYPE_ENV = "OCRMIT_VISION_DTYPE"
_VISION_DTYPE_MODES = ("auto", "fp32", "bf16")
_VISION_MODULE_NAMES = ("sam_model", "vision_model", "projector")

# A/B 结论（2026-11-12 真机实测，产物 D:/Project/_work/a12/）：
#   i7-10700（无 AVX512-BF16）CPU、crop_mode=base、max_page_tokens=256，
#   同进程交错 bf16/fp32 各 3 样本（另有 1 次 warmup 不计）：
#     test_small 520×160 : prefill 中位 47.3s(bf16) → 10.9s(fp32) = 4.35×
#     test_long 600×2400 : prefill 中位 46.3s(bf16) → 10.9s(fp32) = 4.26×
#   两次输出逐字节完全相同；decode 不受影响（8.3s/29.0s，两 dtype 一致）。
#   ⇒ bf16 在这类 CPU 上走的是仿真路径（非原生），CPU 默认切 fp32 稳赚；
#     GPU 上 bf16 有张量核，保持 bf16 不变。
#   注意：结论的硬件前提是「无 AVX512-BF16/AMX 的 x86」。带原生 bf16 的新 CPU
#   若实测 fp32 不快，可用 OCRMIT_VISION_DTYPE=bf16 覆盖。
_AUTO_VISION_DTYPE_CPU = "fp32"


def _normalize_vision_dtype_mode(value) -> str:
    """OCRMIT_VISION_DTYPE 归一：auto / fp32 / bf16；空值/非法 → auto。"""
    s = str(value or "").strip().lower()
    return s if s in _VISION_DTYPE_MODES else "auto"


def _resolve_vision_dtype(mode, device) -> str:
    """把设置解析成视觉塔实际 dtype（"fp32" | "bf16"）。

    auto：CPU → _AUTO_VISION_DTYPE_CPU（A/B 实测后定）；CUDA/其它 → bf16
    （GPU 上 bf16 有原生算力，换 fp32 只会更慢也更占显存）。
    显式 fp32/bf16 一律照办（供 A/B 与排障）。
    """
    mode = _normalize_vision_dtype_mode(mode)
    if mode in ("fp32", "bf16"):
        return mode
    dev = str(device or "").lower()
    return _AUTO_VISION_DTYPE_CPU if dev.startswith("cpu") else "bf16"


def _map_tensor_tree(obj, fn):
    """对 pytree（list/tuple/dict）里所有张量应用 fn，其余原样返回。

    torch 延迟导入：本模块必须在没有 torch 的环境（测试/体检）也能 import。
    """
    import torch
    if isinstance(obj, torch.Tensor):
        return fn(obj)
    if isinstance(obj, tuple):
        return tuple(_map_tensor_tree(x, fn) for x in obj)
    if isinstance(obj, list):
        return [_map_tensor_tree(x, fn) for x in obj]
    if isinstance(obj, dict):
        return {k: _map_tensor_tree(v, fn) for k, v in obj.items()}
    return obj


_BRIDGE_CLS = None


def _dtype_bridge_cls():
    """懒构造 _DtypeBridge（torch 可用时才定义 nn.Module 子类，便于无 torch 环境导入）。"""
    global _BRIDGE_CLS
    if _BRIDGE_CLS is None:
        import torch
        from torch import nn

        class _DtypeBridge(nn.Module):
            """视觉子模块的「入口转 fp32 / 出口转指定 dtype」适配层。

            为什么不能简单 .float()：视觉塔输出要 scatter 进 **bf16** 的
            inputs_embeds（modeling_unlimitedocr.py:785/:789），并与 bf16 的
            image_newline / view_seperator 拼接（:752/:758/:762）。只转视觉塔
            而不把输出转回模型 dtype 会 dtype 不匹配；反过来只转输入又会静默
            降精度。故：
              • 入口：模型传来的图/特征张量（bf16）→ fp32；
              • 出口：projector 的输出 → 模型 dtype（bf16）；
                sam/vision 的出口仍 fp32（内部整段 fp32 计算）。
            张量实参/返回值按 pytree 递归转换，非张量（int/None/str）原样透传。
            """

            def __init__(self, inner, out_dtype):
                super().__init__()
                self.inner = inner
                self.out_dtype = out_dtype

            def forward(self, *args, **kwargs):
                args = _map_tensor_tree(args, lambda t: t.to(torch.float32))
                kwargs = _map_tensor_tree(kwargs, lambda t: t.to(torch.float32))
                out = self.inner(*args, **kwargs)
                return _map_tensor_tree(out, lambda t: t.to(self.out_dtype))

        _BRIDGE_CLS = _DtypeBridge
    return _BRIDGE_CLS


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
        # 幂等加载（A10）：记录本次成功加载的模型目录，判断「已加载且口径未变」
        # 时 load() 直接返回，不再重复读 6.69 GB 权重。unload() 会清空。
        self._loaded_dir: Path | None = None
        self._last_infer: dict | None = None   # 最近一次 _infer 的度量（A1）
        # 视觉塔 dtype（A12）：当前生效值 + 未被包桥的原模块（切换时复用，
        # 避免套娃包装；unload() 里清空以免吊着旧模型子模块）。
        self._vision_dtype = "bf16"
        self._vision_orig: dict = {}
        self._vision_mode = "auto"

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
        """torch/CUDA 检测（不依赖模型已加载），结果按 5s 缓存。

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

        缓存（5s）：命中条件除 TTL 外还要求「torch 可见性 + 组件安装标记」
        未变；`_ensure_runtime_path()` 装上组件时还会主动作废缓存，避免
        界面上「装完组件仍显示未装 torch」。返回结构不变（每次给副本）。
        """
        _ensure_runtime_path()
        key = (_torch_visible(), _torch_marker_sig())
        cached = _DEVICE_INFO_CACHE
        now = time.time()
        if (cached["value"] is not None and cached["key"] == key
                and now - cached["ts"] < _DEVICE_INFO_TTL):
            return dict(cached["value"])
        info = self._device_info_probe()
        _DEVICE_INFO_CACHE.update(ts=time.time(), key=key, value=dict(info))
        return info

    def _device_info_probe(self) -> dict:
        """真正跑一次 torch/CUDA 探测（无缓存；device_info 的缓存后端）。"""
        info = {"torch": None, "cuda": False, "cudaName": "", "vramGb": 0.0,
                "cudaBuild": "", "transformers": None}
        _ensure_runtime_path()
        import importlib.util as _ilu
        if 'torch' not in sys.modules:
            spec = _ilu.find_spec("torch")
            if spec is None or not spec.origin:
                return info
            # ① 组件解压进行中 find_spec 也会命中（torch/__init__.py 早期落
            #    地），此时导入只会半途失败并毒化 transformers 的可用性缓存
            #    （它以这次导入为准）→ runtime 来源必须等安装标记落地。
            # ② 命名空间包（无 origin，如残缺的 runtime/torch 目录）同样
            #    不可信——import 会成功但没有 __version__（实测 AttributeError）。
            if spec.origin.startswith(str(RUNTIME_DIR)) \
                    and installed_torch_info() is None:
                return info
        frozen = bool(getattr(sys, "frozen", False))
        # 负缓存读的是类属性：旧实现写成模块级名 `device_info._torch_fail_at`，
        # 而 device_info 只是类方法名（不是模块全局）——冻结环境走到这一行会
        # 抛 NameError，把 /api/status 打成 500。改用 type(self) 访问。
        if frozen and 'torch' not in sys.modules \
                and time.time() - type(self)._torch_fail_at < 60:
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
        except Exception as e:
            # 捕一切：torch 半残/命名空间包等怪状态下 AttributeError 之类
            # 不该把 /api/status 打成 500。留痕后按「不可用」呈现。
            if frozen:
                type(self)._torch_fail_at = time.time()
            from applog import log_error
            log_error("device_info 探测 torch 异常", exc=e)
        return info

    _torch_fail_at = 0.0   # 冻结环境 import torch 最近失败时刻（负缓存）

    @staticmethod
    def _runtime_flavor_hint() -> str:
        """device_info 用的组件 flavor 线索（纯文件级，不跑硬件检测）。"""
        from downloader import installed_torch_info
        return (installed_torch_info() or {}).get("flavor") or "cpu"

    # ── 加载 ─────────────────────────────────────────────────
    def load(self, device_pref: str = "auto", cb=None, force: bool = False):
        """从背景线程调用；cb(msg) 更新界面状态。

        device_pref: "auto" | "cuda" | "cpu"
        force: 即使「已加载且设备/目录未变」也强制重载（替换模型文件后用）。

        幂等：已 ready、目标设备相同、模型目录未变时直接返回，不重复读
        6.69 GB 权重——重复加载会让两份权重同时常驻（16 GB 机器换页/被杀）。
        设备或目录变了则照常重载；unload() 之后 ready=False，同样会重载。
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
        if device_pref == "cuda" and not cuda_ok:
            if not getattr(torch.version, "cuda", None):
                raise EngineError(
                    tr("err.torchCpuBuild", ver=torch.__version__))
            raise EngineError(tr("err.noCudaDevice"))
        device, note = _pick_device(device_pref, cuda_ok, _cuda_vram_gb(torch))
        if note:
            from applog import log_model
            vram = _cuda_vram_gb(torch) or 0.0
            if note == "vram-low-auto":
                log_model(f"显存仅 {vram:.1f} GB < {_MIN_CUDA_VRAM_GB:g} GB，"
                          f"auto 改用 CPU（fp32 视觉塔），避免显存超卖导致的缓慢")
            elif note == "vram-low-forced":
                log_model(f"显存仅 {vram:.1f} GB（建议 ≥{_MIN_CUDA_VRAM_GB:g} GB），"
                          f"按用户显式选择仍用 CUDA；实测此档位可能比 CPU 更慢")
        _patch_tensor_cuda(device == "cpu")

        # 幂等短路（A10）：判定条件必须是「已真正加载 + 设备一致 + 目录一致」，
        # 不能只看 ready——切设备（cpu↔cuda）或换模型目录时必须重新加载。
        if not force and self.ready and self.model is not None \
                and self.device == device \
                and self._loaded_dir == Path(self.model_dir):
            from applog import log_model
            log_model(f"模型已在 {device} 加载且目录未变，跳过重复加载：{self.model_dir}")
            return

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
            self._loaded_dir = Path(self.model_dir)
        _s(tr("eng.readyInfo", device=device, secs=f"{time.time() - t0:.0f}"))
        # A12：视觉塔 dtype 在加载完成后按 OCRMIT_VISION_DTYPE 生效
        self._apply_vision_dtype_from_env()

    # ── 视觉塔 dtype（A12）────────────────────────────────────
    @property
    def vision_dtype(self) -> str:
        """视觉塔当前实际 dtype（"fp32" | "bf16"）。"""
        return getattr(self, "_vision_dtype", "bf16")

    def _model_dtype(self):
        """主干参数 dtype（本模型恒为 bf16）；取不到时回退 torch.bfloat16。"""
        torch = _torch()
        try:
            return next(self.model.parameters()).dtype
        except Exception:
            return torch.bfloat16

    @staticmethod
    def _vision_root(model):
        """视觉塔挂载点：UnlimitedOCRForCausalLM.model 或裸 UnlimitedOCRModel。"""
        if model is None:
            return None
        inner = getattr(model, "model", None)
        if inner is not None and getattr(inner, "sam_model", None) is not None:
            return inner
        if getattr(model, "sam_model", None) is not None:
            return model
        return None

    def _vision_modules(self):
        """返回 (视觉塔根, {name: 当前挂载对象})；根缺失时 (None, {})。"""
        root = self._vision_root(self.model)
        if root is None:
            return None, {}
        mods = {n: getattr(root, n) for n in _VISION_MODULE_NAMES
                if getattr(root, n, None) is not None}
        return root, mods

    def set_vision_dtype(self, mode=None) -> dict:
        """切换视觉塔 dtype（不重载权重，只转换 sam/vision/projector）。

        mode: None=读环境变量 OCRMIT_VISION_DTYPE；accept auto/fp32/bf16。

        返回 {"mode","dtype","device","applied"}。bf16 是**完全 no-op**：
        子模块恢复原对象、参数转回模型 dtype（若之前是 fp32），不做任何包装。
        可反复调用（dtype 未变时立即返回），供同进程内交错 A/B 使用。
        """
        torch = _torch()
        env_mode = os.environ.get(VISION_DTYPE_ENV) if mode is None else mode
        mode_norm = _normalize_vision_dtype_mode(env_mode)
        target = _resolve_vision_dtype(mode_norm, self.device)
        info = {"mode": mode_norm, "dtype": target, "device": self.device,
                "applied": False}
        root, mods = self._vision_modules()
        if root is None or not mods:
            return info
        if not self._vision_orig:       # 第一次切换：留下未被包桥的原模块
            self._vision_orig = dict(mods)
        if target == self._vision_dtype:
            info["applied"] = True
            return info
        base_dtype = self._model_dtype()
        if target == "fp32":
            bridge_cls = _dtype_bridge_cls()
            for name, orig in self._vision_orig.items():
                orig.to(torch.float32)
                # 只有 projector 的输出要回到模型 dtype（scatter/cat 的边界）
                out_dtype = base_dtype if name == "projector" else torch.float32
                setattr(root, name, bridge_cls(orig, out_dtype))
        else:
            for name, orig in self._vision_orig.items():
                orig.to(base_dtype)
                setattr(root, name, orig)
        self._vision_dtype = target
        self._vision_mode = mode_norm
        info["applied"] = True
        return info

    def _apply_vision_dtype_from_env(self):
        """load() 末尾按 OCRMIT_VISION_DTYPE 生效；失败不拖垮加载（记日志）。"""
        try:
            info = self.set_vision_dtype(None)
            from applog import log_model
            log_model(f"视觉塔 dtype 策略：mode={info['mode']} → {info['dtype']}"
                      f"（device={info['device']}）")
        except Exception as e:
            from applog import log_error
            log_error("视觉塔 dtype 切换失败，回退 bf16", exc=e)
            self._vision_dtype = "bf16"
            self._vision_orig = {}

    # ── PDF → 页图 ───────────────────────────────────────────
    @staticmethod
    def pdf_to_images(pdf_path: str | Path, dpi: int = 200,
                      out_dir: Path | None = None,
                      page_range: str = "") -> list[Path]:
        """PDF 逐页转 PNG（pymupdf）。缺库时抛带指引的 EngineError。

        dpi: 光栅化分辨率，默认 200（recognize 传入前会经 _normalize_dpi
        归一：None/非法 → 200，越界夹到 [72, 600]）。200 dpi 的 A4 ≈ 1654×2339，
        已覆盖模型 gundam 1×2 tile（1280 px 高）所需；更高 dpi 只增加转图
        时间与临时盘，不带来额外版面细节（模型会把页面缩到 640/1024 网格）。
        out_dir: 输出目录（默认为 PDF 同目录）。临时目录由 recognize 创建并清理。
        page_range: 只导出选中页（"1-3,5,10-"；空 = 全部页）。语法非法 →
          EngineError(tr("err.badPageRange"))；越界页号按"夹取后去重"处理
          （与 CLI --pages 同一份 page_range 实现）。输出文件名保留**原页号**
          （`_ocr_page_<stem>_0007.png`），调用方据此知道页的来源。
        """
        try:
            import fitz   # pymupdf
        except Exception as e:
            raise EngineError(tr("err.noPymupdf")) from e
        pdf_path = Path(pdf_path)
        out_dir = Path(out_dir or pdf_path.parent)
        doc = fitz.open(str(pdf_path))
        try:
            total = int(getattr(doc, "page_count", 0) or 0)
            spec = str(page_range or "").strip()
            if spec:
                try:
                    numbers = _parse_page_spec(spec, total)
                except ValueError as e:
                    raise EngineError(
                        tr("err.badPageRange", spec=spec)) from e
            else:
                numbers = list(range(1, total + 1))
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pages = []
            for n in numbers:
                if n < 1 or n > total:
                    continue
                p = out_dir / f"_ocr_page_{pdf_path.stem}_{n:04d}.png"
                doc[n - 1].get_pixmap(matrix=mat).save(str(p))
                pages.append(p)
            return pages
        finally:
            doc.close()

    def _pdf_to_images_compat(self, pdf_path, *, out_dir, dpi, page_range) -> list[Path]:
        """调 `pdf_to_images`，兼容尚未支持 page_range 的实现/测试桩。

        只在 page_range 非空时才尝试传该关键字：签名里有 page_range（或
        **kwargs）才传，否则按旧调用（只 out_dir/dpi）——保证旧桩不炸。
        """
        kwargs = {"out_dir": out_dir, "dpi": dpi}
        spec = str(page_range or "").strip()
        if spec:
            try:
                params = inspect.signature(self.pdf_to_images).parameters
                names = set(params)
                has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD
                                 for p in params.values())
            except (TypeError, ValueError):
                names, has_var_kw = set(), False
            if has_var_kw or "page_range" in names:
                kwargs["page_range"] = spec
        return self.pdf_to_images(pdf_path, **kwargs)

    # ── 识别 ─────────────────────────────────────────────────
    def recognize(self, image_paths: list[Path], prompt: str = "",
                  progress_cb=None, cancel_event: threading.Event | None = None,
                  max_new_tokens: int | None = None,
                  crop_mode=None, max_page_tokens: int = 0,
                  max_merged_pages: int = 0, drop_headers: bool = False,
                  dpi: int | None = None,
                  page_range: str = "", join_lines: bool = False,
                  cjk_spacing: bool = False) -> dict:
        """识别图片（可多页）。返回 {text, pages: [str], elapsed, ...}。

        image_paths: 一或多张图片路径（按页序）。
        prompt: 空串 → DEFAULT_PROMPT。
        progress_cb(stage, done, total, elapsed)——total 现在是**组数**：
          merged 模式按「已完成组/总组」推进；单页仍是 0/1 → 1/1。
        max_page_tokens: 单页生成上限（0=不限）。正数静默夹到 [256, 65536]；
          合并模式下单请求上限 = min(max_page_tokens × 该组页数, 131072)。
        max_merged_pages: 合并模式单次请求最大页数（0=不限）。页数超过时在
          **进入推理前**切组（每组 ≤N 页），组间检查 cancel_event（可取消），
          各组结果按原页序拼回。取舍：**跨组不共享上下文**——跨页表格/续页
          可能在组边界断开；组数与这一取舍会写入 log_ocr。
        crop_mode: None=沿用旧行为（不给模型，单图 gundam / 多页 base）；
          "gundam"/"base"=强制；"auto"=单图 h/w>3（长截图）→ base，其余 → gundam，
          多页 → base。多页请求 gundam 会降级为 base（上游只验证过单图切片）。
        drop_headers: True 时后处理丢弃 header/footer/page_number 三类版面块（E11）。
        dpi: PDF 逐页光栅化分辨率；None/非法 → 200（旧行为不变），仅对 .pdf 生效。
        page_range: PDF 页范围（"1-3,5,10-"；空 = 全部页）。非 PDF 输入忽略；
          语法非法 → EngineError(err.badPageRange)。**只导出选中页**，因此
          后的分组/上限都按选中页计算。
        join_lines: 后处理把被硬换行拆开的同段文本接回去（E12）。
        cjk_spacing: 后处理给中英文/数字之间补空格（E14）。
          两者与 tableWarnings 由 ocr_postprocess 提供；尚未落地时被静默忽略
          （见 _render_with_stats 的签名兼容）。

        merged（默认）：尽量一次请求合并多页（跨页上下文，<PAGE> 分隔输出）；
        仅当页数超过 max_merged_pages 时才分组。

        既有返回字段语义不变：text（多页时带 <!-- page N --> 注释）、
        pages（逐页 Markdown）、elapsed（秒）。此外只增不改：
          tokens          输出 token 数（多组时求和；模型不回传时 None）
          truncated       任一组被生成上限截断（finish_reason == 'length'）
          pagesMismatch   任一组模型出的页数 != 提示里的页数
          finishReason    最后一组的 'stop' / 'length' / None
          infer           最后一组的度量明细（prefill/decode 秒、tokens/s 等）
          groups          实际分组数（未分组=1）
          droppedBlocks   被 drop_headers 丢弃的版面块数
          tableWarnings   后处理发现的表格问题数（各组之和；E12/E14 同批）
          effectiveMaxNewTokens 本次请求实际用的生成上限（None=模型默认）
        """
        if not self.ready:
            raise EngineError(tr("err.notLoaded"))
        if not image_paths:
            raise EngineError(tr("err.noImages"))
        prompt = (prompt or "").strip() or DEFAULT_PROMPT
        pdf_dpi = _normalize_dpi(dpi)
        page_tokens = _normalize_page_tokens(max_page_tokens)     # A2
        merge_limit = _normalize_merged_pages(max_merged_pages)   # A3
        explicit_new = _normalize_explicit_max_new_tokens(max_new_tokens)
        want_drop = bool(drop_headers)                            # E11
        want_join = bool(join_lines)                              # E12
        want_cjk = bool(cjk_spacing)                              # E14
        page_spec = str(page_range or "").strip()

        cancel_event = cancel_event or self.cancel_event
        t0 = time.time()
        results: list[str] = []
        stats: dict = {}
        group_stats: list[dict] = []
        dropped_total = 0
        table_warnings_total = 0
        total_groups = 1
        effective_new = None
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
                        expanded.extend(self._pdf_to_images_compat(
                            p, out_dir=tmp_dir, dpi=pdf_dpi,
                            page_range=page_spec))
                    else:
                        expanded.append(p)
                if not expanded:
                    raise EngineError(tr("err.noPages"))

                n_pages = len(expanded)
                # A3：分组必须在进入推理前切好——模型内部无法中途停下
                page_groups = _plan_groups(expanded, merge_limit)
                total_groups = len(page_groups)
                group_size = len(page_groups[0]) if page_groups else 0

                # A6：切片策略（单图可 auto；多页/分组一律 base）
                if n_pages == 1:
                    resolved_crop = _resolve_crop_mode(page_groups[0], crop_mode)
                else:
                    resolved_crop = False
                    if str(crop_mode or "").strip().lower() == "gundam":
                        _log_ocr_safe("crop_mode=gundam 在多页合并下降级为 base"
                                      "（上游只为单图验证过动态切片）")
                # A2：本次请求的生成上限按「满组」口径（最后一组更小、实际更紧）
                effective_new = _effective_max_new_tokens(
                    page_tokens, group_size, explicit_new)
                _log_plan(n_pages, total_groups, group_size, resolved_crop,
                          page_tokens, effective_new, want_drop)
                if page_spec or want_join or want_cjk:
                    _log_ocr_safe(
                        f"页范围/后处理：page_range={page_spec or '全部'} "
                        f"join_lines={want_join} cjk_spacing={want_cjk}")

                if progress_cb:
                    stage = (tr("eng.stageGundam") if n_pages == 1
                             else tr("eng.stageMerged", n=n_pages))
                    progress_cb(stage, 0, total_groups, 0.0)

                def _render(raw_text: str):
                    """单页原始输出 → (GUI Markdown, 丢弃块数)；表格告警累加。"""
                    nonlocal table_warnings_total
                    md, dropped, warns = _render_with_stats(
                        raw_text, drop_headers=want_drop,
                        join_lines=want_join, cjk_spacing=want_cjk)
                    table_warnings_total += warns
                    return md, dropped

                groups_done = 0
                for group in page_groups:
                    if cancel_event.is_set():
                        break           # A3 组间检查点：取消在下一组推理前生效
                    cap = _effective_max_new_tokens(
                        page_tokens, len(group), explicit_new)
                    text, gstats = self._infer(
                        group, prompt, crop_mode=resolved_crop,
                        max_new_tokens=cap)
                    stats = gstats
                    group_stats.append(gstats)
                    if n_pages == 1:
                        md, dropped = _render(text)
                        results = [md]
                        dropped_total += dropped
                    else:
                        parts = [p for p in PAGE_SPLIT_RE.split(text) if p.strip()]
                        for part in (parts or [text.strip()]):
                            md, dropped = _render(part)
                            results.append(md)
                            dropped_total += dropped
                    groups_done += 1
                    if progress_cb:
                        progress_cb(tr("prog.recognizeDone"), groups_done,
                                    total_groups, time.time() - t0)

                if total_groups > 1 and groups_done:
                    _log_ocr_safe(f"分组完成：{groups_done}/{total_groups} 组，"
                                  f"{n_pages} 页")
                if want_drop:
                    _log_ocr_safe(
                        f"页眉页脚丢弃：{dropped_total} 块（drop_headers=True）")
                if table_warnings_total:
                    _log_ocr_safe(
                        f"表格告警：{table_warnings_total} 处（tableWarnings）")
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
            # 以下为新增的中性字段（既有字段语义不变）
            "tokens": _sum_tokens(group_stats),
            "truncated": any(g.get("truncated") for g in group_stats),
            "pagesMismatch": any(g.get("pagesMismatch") for g in group_stats),
            "finishReason": _last_finish(group_stats),
            "infer": dict(stats),
            # A2 / A3 / E11
            "groups": total_groups,
            "droppedBlocks": dropped_total,
            "effectiveMaxNewTokens": effective_new,
            # E12 / E14：后处理（join_lines / cjk_spacing）发现的表格问题总数
            "tableWarnings": table_warnings_total,
        }

    def _infer(self, images: list[Path], prompt: str, crop_mode=None,
               max_new_tokens: int | None = None) -> tuple[str, dict]:
        """调 model.infer()（加锁串行）。返回 (纯文本, 度量 dict)。

        eval_mode=False → 解码走流式钩子（实时字数 + 取消中断）；
        钩子子类不打印，控制台保持安静。
        return_dict=True 拿 finish_reason / num_pages_out / expected_pages /
        prefill_time / time（键名以模型源码为准），同时**保留**模型只返回 str
        的兼容分支（旧包装层/自造 stub）：那时度量全空、绝不误报截断。
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
                    return_dict=True,
                )
                if crop_mode is not None:
                    kwargs["crop_mode"] = crop_mode
                if max_new_tokens:
                    kwargs["max_new_tokens"] = max_new_tokens
                with torch.inference_mode():
                    out = self.model.infer(self.tokenizer, **kwargs)
                # 兼容两种返回：dict（return_dict=True）与纯文本（旧实现/桩）
                if isinstance(out, str):
                    text, raw = out, None
                elif isinstance(out, dict):
                    raw = out
                    val = out.get("text")
                    text = "" if val is None else str(val)
                else:                       # 未知类型：按旧行为 str()
                    text, raw = str(out), None
                stats = _summarize_infer(raw)
                self._last_infer = stats
                _log_infer(stats, len(images))
                return text, stats
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
        """释放模型（切换设备/目录时用）。

        清空 ready 与 _loaded_dir：之后再 load() 会真正重建（幂等短路不生效）。
        """
        with self._lock:
            self.model = None
            self.tokenizer = None
            self.ready = False
            self._loaded_dir = None
            self._last_infer = None
            self._vision_orig = {}       # 别吊着旧模型的视觉子模块（~400 MB）
            self._vision_dtype = "bf16"

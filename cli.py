# -*- coding: utf-8 -*-
"""cli.py — OCRMiniTool 无头命令行（路线图 P2 / 提案 E1）

给脚本、CI 与 Agent 用的入口：不开窗口、不起 HTTP server、不 import
pywebview/clr，识别走 `ocr_engine.OcrEngine`。

设计要点（为什么长这样）：

  • **纯函数可单测**：`parse_pages` / `collect_inputs` / `build_arg_parser`
    不碰模型、不碰 torch，`tools/check.py` 与 `tests/test_cli.py` 可放心调；
    `main(argv)` 只返回退出码（0/1/2/3，中断额外 130）。
  • **测试注入缝**：`main(argv, engine_factory=...)` 可换掉引擎（测试用假
    引擎），此时跳过环境自检与真实加载；生产路径 `engine_factory=None` 才
    走 `_preflight()` + `_make_engine()`。
  • **能力探测**：`--max-page-tokens` / `--max-merged-pages` / `--drop-headers`
    已由新引擎支持（A2/A3/E11）；CLI 仍用 `inspect.signature(engine.recognize)`
    探测，以便兼容旧引擎：支持就透传；不支持则 `--max-merged-pages` 在 CLI
    侧分块实现，另外两个只告警不致命（见 docs/cli.md「与引擎能力的关系」）。
  • **crop_mode 口径**：不传（None）= 沿用引擎旧行为（单图 gundam / 多页 base）；
    显式给出时**原样传字符串**——"auto" 交给引擎按长宽比判断（长截图 h/w>3 →
    base），绝不塌缩成 None（否则长截图自动降级失效）。
  • **新增字段一律防御式读取**：`truncated` / `pagesMismatch` / `groups` 用
    `result.get(..., 默认值)`，旧引擎/旧包装层缺字段也不会炸。
  • **文案不走 locales**：CLI 消息表在本文件内（zh/en 两份），因为写域不含
    `webview/locales/*.json`；但仍调用 `i18n.set_lang()`，让引擎冒出的
    `tr()` 文案（EngineError 等）跟着 `--lang`/`OCRMT_LANG` 走。

用法与退出码见 docs/cli.md。不打印 traceback，除非 `--verbose`。
"""
from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

from version import APP_VERSION
import page_range

# ── 退出码（docs/cli.md 的表格与这里保持一致）─────────────────────
EXIT_OK = 0            # 全部成功
EXIT_PARAM = 1         # 参数错误（--input 缺失/不存在、--pages 语法、--out 不可写…）
EXIT_ENV = 2           # 环境/模型错误（模型未下载、torch 组件缺失、加载失败、缺 pymupdf）
EXIT_PARTIAL = 3       # 有文件失败（全部失败也归此类）
EXIT_INTERRUPTED = 130  # 使用者中断（Ctrl+C；与 STTMiniTool 的约定一致）

PDF_DPI = 200          # 与 ocr_engine.pdf_to_images 的默认值保持一致

# 扩展名白名单：与后端一致（ocr_engine.IMAGE_EXTS）+ PDF。
# 直接 import 会把 downloader/i18n 拉进来，故用 try 兜底成字面量——
# 两者必须一致，改一处要同步改另一处（tools/check.py 不检查这条，靠测试兜）。
_FALLBACK_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}
try:                                                    # pragma: no cover
    from ocr_engine import IMAGE_EXTS as _ENGINE_IMAGE_EXTS
except Exception:                                       # pragma: no cover
    _ENGINE_IMAGE_EXTS = _FALLBACK_EXTS
INPUT_EXTS = frozenset(_ENGINE_IMAGE_EXTS) | {".pdf"}


class CliError(Exception):
    """可预期的用户错误（参数/输入问题）；信息已可展示，不需 traceback。"""


# ══════════════════════════════════════════════════════════════════
# 文案表（zh/en）。键名不用 tr() 引用，避免 webview/locales 缺键被
# tools/check.py 拦下（写域不含 locales）。
# ══════════════════════════════════════════════════════════════════
_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "err.inputMissing": "缺少 --input（或子命令）；用法见 --help",
        "err.notFound": "输入不存在：{path}",
        "err.notAFileOrDir": "输入既不是文件也不是目录：{path}",
        "err.unsupported": "不支持的扩展名：{path}（支持 {exts}）",
        "err.noFiles": "目录里没有可识别的文件：{path}",
        "err.outDir": "输出目录不可用：{path}（{err}）",
        "err.badPages": "--pages 格式错误：{spec}（{err}）",
        "err.badNumber": "参数 {name} 必须是 >= 1 的整数，得到：{value}",
        "err.pagesEmpty": "--pages 在 {name} 中未命中任何页（共 {total} 页）",
        "err.modelMissing": "模型文件不完整或未下载：{path}（先在图形界面「模型」页下载）",
        "err.torchMissing": "未检测到推理引擎组件（torch）：先装组件或 pip install torch",
        "err.torchBroken": "推理引擎组件不完整（runtime/torch 存在但安装标记缺失），请重新下载组件",
        "err.pymupdfMissing": "处理 PDF 需要 pymupdf：pip install pymupdf",
        "err.loadFailed": "模型加载失败：{err}",
        "err.notReady": "模型未就绪（engine.ready 为 False）",
        "err.recognizeFailed": "识别失败：{name}：{err}",
        "err.fileUnreadable": "文件不可读：{path}",
        "err.pdfNeedsPdfInput": "可搜索 PDF 只支持「单个 PDF 输入」（--format pdf）",
        "err.pdfNoBlocks": "引擎没有返回版面 blocks，无法生成可搜索 PDF",
        "err.pdfExportFailed": "生成可搜索 PDF 失败：{err}",
        "err.officeNeedsBlocks": "引擎没有返回版面 blocks，无法生成 docx/xlsx（需要结构化结果）",
        "err.officeNoTable": "Excel 只导出表格；当前结果里没有可导出的表格",
        "err.officeExportFailed": "生成 {fmt} 失败：{err}",
        "info.officeOk": "  ✓ {name} → {fmt}：{path}",
        "info.pdfOk": "  ✓ {name} → 可搜索 PDF：{path}",
        "warn.pagesIgnored": "--pages 只对 PDF 生效，已忽略：{name}",
        "warn.noPageTokens": "当前引擎不支持 --max-page-tokens，已忽略",
        "warn.noDropHeaders": "当前引擎不支持 --drop-headers，已忽略",
        "warn.chunk": "引擎不支持 --max-merged-pages，已在 CLI 侧按 {n} 页/次分块",
        "warn.shortPages": "模型返回 {got} 页，期望 {want} 页（可能截断）",
        "info.loading": "加载模型（device={device}）…",
        "info.ready": "模型就绪（{secs:.1f}s）",
        "info.fileStart": "[{i}/{n}] {name}",
        "info.fileOk": "  ✓ {name} → {pages} 页，{secs:.1f}s",
        "info.fileOkNoOut": "  ✓ {name}：{pages} 页，{secs:.1f}s",
        "info.summary": "完成：{ok}/{total} 成功，{failed} 失败，用时 {secs:.1f}s",
        "info.cancelled": "已中断（Ctrl+C）。",
        "status.modelDir": "模型目录：{path}",
        "status.modelPresent": "模型文件：{state}",
        "status.torch": "torch：{state}",
        "status.transformers": "transformers：{state}",
        "status.runtime": "引擎组件：{state}",
        "status.pymupdf": "pymupdf：{state}",
        "status.cuda": "cuda：未探测（status 不加载 torch；识别时自动检测）",
        "status.version": "版本：{ver}",
        "status.yes": "就绪",
        "status.no": "缺失",
        "status.unknown": "未探测",
    },
    "en": {
        "err.inputMissing": "missing --input (or a subcommand); see --help",
        "err.notFound": "input not found: {path}",
        "err.notAFileOrDir": "input is neither a file nor a directory: {path}",
        "err.unsupported": "unsupported extension: {path} (allowed: {exts})",
        "err.noFiles": "no recognisable files in directory: {path}",
        "err.outDir": "output directory unusable: {path} ({err})",
        "err.badPages": "bad --pages spec: {spec} ({err})",
        "err.badNumber": "argument {name} must be an integer >= 1, got: {value}",
        "err.pagesEmpty": "--pages matched no page in {name} (total {total})",
        "err.modelMissing": "model files incomplete or missing: {path} (download on the Model page first)",
        "err.torchMissing": "PyTorch runtime component not found: install the component or `pip install torch`",
        "err.torchBroken": "PyTorch component incomplete (runtime/torch exists without install marker); re-download it",
        "err.pymupdfMissing": "PDF input needs pymupdf: `pip install pymupdf`",
        "err.loadFailed": "model load failed: {err}",
        "err.notReady": "model not ready (engine.ready is False)",
        "err.recognizeFailed": "recognition failed: {name}: {err}",
        "err.fileUnreadable": "file unreadable: {path}",
        "err.pdfNeedsPdfInput": "searchable PDF requires a single PDF input (--format pdf)",
        "err.pdfNoBlocks": "engine returned no layout blocks; cannot build a searchable PDF",
        "err.pdfExportFailed": "searchable PDF export failed: {err}",
        "err.officeNeedsBlocks": "engine returned no layout blocks; docx/xlsx needs structured output",
        "err.officeNoTable": "Excel exports tables only; this result has no tables",
        "err.officeExportFailed": "failed to build {fmt}: {err}",
        "info.officeOk": "  ✓ {name} → {fmt}: {path}",
        "info.pdfOk": "  ✓ {name} → searchable PDF: {path}",
        "warn.pagesIgnored": "--pages only applies to PDF, ignored: {name}",
        "warn.noPageTokens": "current engine has no --max-page-tokens; ignored",
        "warn.noDropHeaders": "current engine has no --drop-headers; ignored",
        "warn.chunk": "engine lacks --max-merged-pages; chunking in CLI at {n} page(s)/call",
        "warn.shortPages": "model returned {got} page(s), expected {want} (possibly truncated)",
        "info.loading": "loading model (device={device})…",
        "info.ready": "model ready ({secs:.1f}s)",
        "info.fileStart": "[{i}/{n}] {name}",
        "info.fileOk": "  ✓ {name} → {pages} page(s), {secs:.1f}s",
        "info.fileOkNoOut": "  ✓ {name}: {pages} page(s), {secs:.1f}s",
        "info.summary": "done: {ok}/{total} ok, {failed} failed, {secs:.1f}s",
        "info.cancelled": "interrupted (Ctrl+C).",
        "status.modelDir": "model dir: {path}",
        "status.modelPresent": "model files: {state}",
        "status.torch": "torch: {state}",
        "status.transformers": "transformers: {state}",
        "status.runtime": "runtime component: {state}",
        "status.pymupdf": "pymupdf: {state}",
        "status.cuda": "cuda: not probed (status avoids importing torch; detection happens at recognition time)",
        "status.version": "version: {ver}",
        "status.yes": "present",
        "status.no": "missing",
        "status.unknown": "unknown",
    },
}


def _msg(lang: str, key: str, **params) -> str:
    """取当前语言文案；缺键回退 zh，再缺回退键名（与 i18n.tr 同策略）。"""
    text = _MESSAGES.get(lang, {}).get(key) or _MESSAGES["zh"].get(key) or key
    if params:
        try:
            return text.format(**params)
        except Exception:
            return text
    return text


# ══════════════════════════════════════════════════════════════════
# 纯函数 1：--pages 解析（真正的实现在 page_range.py，GUI/引擎共用）
# ══════════════════════════════════════════════════════════════════
def parse_pages(spec: str | None, total_pages: int | None = None) -> list[int]:
    """解析 "1-3,5,10-" → 升序去重的 1-based 页号列表。

    • "5" 单页；"1-3" 闭区间；"10-" 开区间（需要 total_pages，否则报错）
    • `total_pages` 给定时**越界夹取**：单页号与区间端点都夹到 [1, total_pages]，
      夹取产生的重复页由去重吸收（"1-3,5" + total=3 → [1,2,3]）
    • 空串/None → []（表示"不筛选"）；语法错误一律 CliError

    薄包装：解析逻辑已迁到 `page_range.parse_pages`（GUI 设置 pdfPageRange 与
    引擎 pdf_to_images 用同一份实现），这里只把 ValueError 转成 CliError，
    错误文案逐字保持历史形态（tests/test_cli.py 依赖）。
    """
    try:
        return page_range.parse_pages(spec, total_pages)
    except ValueError as e:
        raise CliError(str(e)) from None


# ══════════════════════════════════════════════════════════════════
# 纯函数 2：输入枚举
# ══════════════════════════════════════════════════════════════════
def collect_inputs(target, recursive: bool = False,
                   exts: set[str] | None = None, lang: str = "zh") -> list[Path]:
    """把 --input 展开成待识别文件列表（不打开文件、不读内容）。

    • 单文件：扩展名不在白名单 → CliError（参数错误）
    • 目录：默认只扫一层，recursive=True 才递归；按扩展名过滤，空目录返回 []
    • 结果按 (目录, 文件名) 小写排序，保证多文件顺序稳定可复现
    • lang 只影响抛出的错误文案（zh/en），不改变任何行为
    """
    path = Path(target).expanduser()
    if not path.exists():
        raise CliError(_msg(lang, "err.notFound", path=str(path)))
    allowed = {str(e).lower() for e in (exts if exts is not None else INPUT_EXTS)}
    if path.is_file():
        if path.suffix.lower() not in allowed:
            raise CliError(_msg(lang, "err.unsupported",
                                path=str(path), exts=", ".join(sorted(allowed))))
        return [path]
    if not path.is_dir():
        raise CliError(_msg(lang, "err.notAFileOrDir", path=str(path)))
    walker = path.rglob("*") if recursive else path.glob("*")
    found = [p for p in walker if p.is_file() and p.suffix.lower() in allowed]
    return sorted(found, key=lambda p: (str(p.parent).lower(), p.name.lower()))


# ══════════════════════════════════════════════════════════════════
# 纯函数 3：参数解析器
# ══════════════════════════════════════════════════════════════════
def _common_parser(suppress_defaults: bool = False) -> argparse.ArgumentParser:
    """主命令与 status 子命令共用的选项（--json/--quiet/--verbose/--lang）。

    用 parents= 注入，保证 `cli.py status --json` 与 `cli.py --json status`
    都能解析（argparse 的位置参数陷阱，与 STTMiniTool cli_mode 同一手法）。
    `suppress_defaults=True` 供子解析器用：子解析器的默认值会覆盖主解析器
    已解析出的值（`--json status` 会变 False），故子副本一律 SUPPRESS。
    """
    off = argparse.SUPPRESS if suppress_defaults else False
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--json", action="store_true", default=off,
                    help="stdout 只输出一个 JSON 对象（进度/日志走 stderr）")
    ap.add_argument("--quiet", action="store_true", default=off,
                    help="抑制进度与逐文件成功行")
    ap.add_argument("--verbose", action="store_true", default=off,
                    help="失败时打印 traceback")
    ap.add_argument("--lang", default=argparse.SUPPRESS if suppress_defaults else None,
                    help="界面语言（zh/en；默认读 OCRMT_LANG 或 zh）")
    return ap


def build_arg_parser() -> argparse.ArgumentParser:
    """构造 CLI 参数解析器（纯函数：不读文件、不碰模型）。"""
    common = _common_parser()
    ap = argparse.ArgumentParser(
        prog="cli.py",
        parents=[common],
        description="OCRMiniTool 无头命令行：图片/PDF → Markdown/文本/JSON。"
                    "不开窗口、不起 HTTP server。",
        epilog="退出码：0 成功 / 1 参数错误 / 2 环境或模型错误 / 3 有文件失败 / 130 中断",
    )
    ap.add_argument("--input", help="输入文件或目录（目录默认只扫一层）")
    ap.add_argument("--recursive", action="store_true", help="--input 是目录时递归子目录")
    ap.add_argument("--prompt", default="", help="识别指令（留空 = 模型默认文档解析）")
    ap.add_argument("--out", default=None, help="输出目录（默认当前目录）")
    ap.add_argument("--format", choices=("md", "txt", "json", "pdf", "docx", "xlsx"),
                    default=None,
                    help="每份结果的落盘格式（默认 md；pdf=可搜索 PDF，"
                         "docx/xlsx=结构化导出，需单个输入且引擎给 blocks）")
    ap.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto",
                    help="推理设备（默认 auto）")
    ap.add_argument("--max-page-tokens", type=int, default=None,
                    help="单页输出 token 上限（引擎支持时透传）")
    ap.add_argument("--max-merged-pages", type=int, default=None,
                    help="每次请求最多合并多少页（引擎不支持时 CLI 侧分块）")
    ap.add_argument("--crop-mode", choices=("auto", "gundam", "base"), default=None,
                    help="切片策略：缺省=沿用引擎旧行为（单页 gundam/多页 base）；"
                         "显式 auto=按长宽比（长截图 h/w>3 → base，其余 gundam）；"
                         "gundam=强制切片；base=整页")
    ap.add_argument("--drop-headers", action="store_true",
                    help="丢弃页眉/页脚/页码块（引擎支持时透传）")
    ap.add_argument("--pages", default="", help='PDF 页范围，如 "1-3,5,10-"（仅 PDF）')
    ap.add_argument("--version", action="store_true", help="打印版本号后退出")
    sub = ap.add_subparsers(dest="cmd", metavar="command")
    sub.add_parser("status", parents=[_common_parser(suppress_defaults=True)],
                   help="打印模型/环境状态（不加载模型）")
    return ap


def _parse_args(argv: list[str]) -> tuple[argparse.Namespace | None, int | None]:
    """解析 argv，把 argparse 的 SystemExit 归一到本模块的退出码。

    --help/--version 走 argparse 的正常退出（0）；未知参数/错误取值 → 1
    （argparse 默认是 2，但本 CLI 的 2 专指环境/模型错误）。
    """
    try:
        return build_arg_parser().parse_args(argv), None
    except SystemExit as e:
        code = getattr(e, "code", 1)
        return None, EXIT_OK if code in (0, None) else EXIT_PARAM


# ══════════════════════════════════════════════════════════════════
# 小工具：输出、语言、环境自检、引擎构造
# ══════════════════════════════════════════════════════════════════
def _out(text: str = ""):
    print(text, flush=True)


def _err(text: str = ""):
    print(text, file=sys.stderr, flush=True)


def _note(text: str, quiet: bool = False):
    """进度/告警 → stderr（--json 时 stdout 必须干净）。"""
    if not quiet:
        print(text, file=sys.stderr, flush=True)


def _apply_lang(raw: str | None) -> str:
    """--lang 优先，其次环境变量 OCRMT_LANG，最后 zh；交给 i18n.set_lang 归一。"""
    lang = (raw or "").strip() or os.environ.get("OCRMT_LANG", "").strip()
    try:
        from i18n import set_lang
        return set_lang(lang or None)
    except Exception:                                   # pragma: no cover
        return "zh" if not lang else lang.lower()


def _pkg_version(name: str) -> str | None:
    try:
        from importlib.metadata import version as _v
        return _v(name)
    except Exception:
        return None


def _find_spec(name: str):
    try:
        return importlib.util.find_spec(name)
    except Exception:
        return None


def _resolve_model_dir() -> Path:
    """模型目录：settings.json 的 modelDir，空则 <程序目录>/models/Unlimited-OCR-2。"""
    try:
        from settings_store import SettingsStore
        from webview_backend import BASE_DIR, MODEL_SUBDIR, SETTINGS_FILE
        raw = SettingsStore(SETTINGS_FILE).read()
        configured = str(raw.get("modelDir") or "").strip()
        return Path(configured) if configured else Path(BASE_DIR) / MODEL_SUBDIR
    except Exception:
        return Path(__file__).resolve().parent / "models" / "Unlimited-OCR-2"


def _preflight(model_dir: Path, files: list[Path] | None = None,
               lang: str = "zh") -> str | None:
    """加载前的环境自检；返回 None=通过，否则返回可直接打印的错误文案。

    检查顺序刻意从便宜到贵：模型文件清单（纯文件 stat）→ torch 可见性
    （find_spec，零导入）→ runtime 组件标记 → PDF 依赖 pymupdf。
    torch 的「组件存在但标记缺失」陷阱见 ocr_engine.device_info 的注释。
    """
    try:
        from downloader import RUNTIME_DIR, installed_torch_info, quick_check
    except Exception as e:                              # pragma: no cover
        return _msg(lang, "err.loadFailed", err=e)
    if not quick_check(model_dir):
        return _msg(lang, "err.modelMissing", path=str(model_dir))
    spec = _find_spec("torch")
    if spec is None:
        return _msg(lang, "err.torchMissing")
    try:
        origin = str(getattr(spec, "origin", "") or "")
        if str(RUNTIME_DIR) in origin and installed_torch_info() is None:
            return _msg(lang, "err.torchBroken")
    except Exception:                                   # pragma: no cover
        pass
    if any(str(p.suffix).lower() == ".pdf" for p in (files or [])):
        if _find_spec("fitz") is None and _pkg_version("pymupdf") is None:
            return _msg(lang, "err.pymupdfMissing")
    return None


def _make_engine(model_dir: Path):
    """真实引擎工厂（测试可 monkeypatch 本函数或传 engine_factory）。"""
    from ocr_engine import OcrEngine
    return OcrEngine(Path(model_dir))


def _engine_caps(engine) -> set[str]:
    """探测 engine.recognize 接受的参数名（决定透传还是 CLI 侧兜底）。"""
    try:
        sig = inspect.signature(engine.recognize)
    except (TypeError, ValueError):                     # pragma: no cover
        return set()
    return set(sig.parameters)


def _groups_count(value) -> int:
    """把引擎的 groups 字段归一成「分组数」（旧引擎缺字段 → 0，块列表 → 长度）。

    当前引擎返回的是 int（未分组=1）；早期/未来若给块列表，按块数计。
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, (list, tuple)):
        return len(value)
    return 0


# ══════════════════════════════════════════════════════════════════
# PDF 页范围展开（仅 --pages / 分块时才需要）
# ══════════════════════════════════════════════════════════════════
def _expand_pdf(pdf: Path, pages_spec: str, out_dir: Path,
                lang: str = "zh") -> list[tuple[int, Path]]:
    """把 PDF 的指定页渲染成 PNG，返回 [(原页码, 路径), …]（1-based 页码）。

    只在 CLI 需要自己控制页数时使用（--pages / --max-merged-pages）；否则
    交给 engine.recognize 内部展开（行为完全一致，少一次重复光栅化）。
    """
    try:
        import fitz
    except Exception as e:
        raise CliError(_msg(lang, "err.pymupdfMissing")) from e
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(str(pdf))
    try:
        total = int(getattr(doc, "page_count", 0) or 0)
        try:
            numbers = parse_pages(pages_spec, total) if pages_spec else list(range(1, total + 1))
        except CliError as e:
            raise CliError(_msg(lang, "err.badPages", spec=pages_spec, err=e)) from e
        if not numbers:
            raise CliError(_msg(lang, "err.pagesEmpty", name=pdf.name, total=total))
        mat = fitz.Matrix(PDF_DPI / 72, PDF_DPI / 72)
        items: list[tuple[int, Path]] = []
        for n in numbers:
            if n < 1 or n > total:
                continue
            dest = out_dir / f"_ocr_page_{pdf.stem}_{n:04d}.png"
            doc[n - 1].get_pixmap(matrix=mat).save(str(dest))
            items.append((n, dest))
        if not items:
            raise CliError(_msg(lang, "err.pagesEmpty", name=pdf.name, total=total))
        return items
    finally:
        doc.close()


def _chunks(items: list[tuple[int, Path]], size: int | None):
    if not size or size <= 0 or size >= len(items):
        yield items
        return
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ══════════════════════════════════════════════════════════════════
# 识别 + 结果整形
# ══════════════════════════════════════════════════════════════════
def _merge_page_results(parts: list[tuple[dict, list[tuple[int, Path]]]],
                        want_pages: int) -> dict:
    """把一次或多次 engine.recognize 的返回合并成单文件结果。

    parts: [(engine 返回 dict, 该次请求的 [(原页码, 路径)])]
    • pages  按请求顺序拼装；页码优先用「原页号」，否则按 1..n
    • truncated / pagesMismatch 取逻辑或；groups 求和（CLI 分块时是各次之和）
    """
    pages: list[dict] = []
    elapsed = 0.0
    truncated = False
    mismatch = False
    groups = 0
    all_blocks: list[dict] = []
    saw_blocks = False
    for res, page_items in parts:
        res = res if isinstance(res, dict) else {}
        md_pages = res.get("pages")
        if not isinstance(md_pages, list):
            text = res.get("text") or ""
            md_pages = [text] if text else []
        blocks_raw = res.get("blocks")
        if not isinstance(blocks_raw, list):
            blocks_raw = []
        if blocks_raw:
            saw_blocks = True
        if blocks_raw and all(isinstance(b, dict) for b in blocks_raw):
            if any("blocks" in b for b in blocks_raw):
                # 引擎页信封形态 [{page, blocks:[…]}]（E2 的真实形态）
                blocks_by_page = [list(env.get("blocks") or [])
                                  for env in blocks_raw]
            else:
                blocks_by_page = [blocks_raw]        # 单页扁平写法
        else:
            blocks_by_page = [p if isinstance(p, list) else []
                              for p in blocks_raw]
        elapsed += float(res.get("elapsed") or 0.0)
        truncated = truncated or bool(res.get("truncated"))
        mismatch = mismatch or bool(res.get("pagesMismatch"))
        groups += _groups_count(res.get("groups"))
        if page_items and len(md_pages) != len(page_items):
            mismatch = True
        for i, md in enumerate(md_pages):
            number = page_items[i][0] if i < len(page_items) else len(pages) + 1
            page_blocks = blocks_by_page[i] if i < len(blocks_by_page) else []
            pages.append({"page": number,
                          "markdown": md if isinstance(md, str) else str(md),
                          "blocks": page_blocks})
            # 与 HTTP/GUI 同一外壳：`blocks` 是页信封 [{page, blocks}]，
            # 页码沿用原页号（--pages 时可能不是 1..n），下游无需按页数猜。
            all_blocks.append({"page": number, "blocks": page_blocks})
    if want_pages and len(pages) != want_pages:
        # 引擎没自报 pagesMismatch 时，用期望页数兜一层（防旧引擎）
        mismatch = mismatch or len(pages) < want_pages
    merged = {"pages": pages, "elapsed": elapsed, "truncated": truncated,
              "pagesMismatch": mismatch, "groups": groups,
              "expectedPages": want_pages or len(pages)}
    if saw_blocks:
        # 引擎给了 blocks 才带（旧引擎缺省不带，JSON 输出 [] 由 _json_item 兜）
        merged["blocks"] = all_blocks
    return merged


def _recognize_file(engine, path: Path, args, caps: set[str], lang: str,
                    tmp_root: Path) -> dict:
    """识别单个输入文件，返回 {source, pages:[{page,markdown}], elapsed, …}。"""
    t0 = time.time()
    infer_kwargs = {"prompt": args.prompt or ""}
    # crop_mode 口径（与引擎 _resolve_crop_mode 一致）：
    #   不给（None）= 沿用旧行为（单图 gundam / 多页 base）；
    #   给了（"auto"/"gundam"/"base"）= **原样传字符串**——"auto" 由引擎按
    #   长宽比决定（长截图 h/w>3 → base，其余 gundam），不能塌缩成 None。
    if args.crop_mode and "crop_mode" in caps:
        infer_kwargs["crop_mode"] = str(args.crop_mode)
    if args.max_page_tokens and "max_page_tokens" in caps:
        infer_kwargs["max_page_tokens"] = int(args.max_page_tokens)
    if args.max_merged_pages and "max_merged_pages" in caps:
        infer_kwargs["max_merged_pages"] = int(args.max_merged_pages)
    if args.drop_headers and "drop_headers" in caps:
        infer_kwargs["drop_headers"] = True

    suffix = path.suffix.lower()
    is_pdf = suffix == ".pdf"
    if args.pages and not is_pdf:
        _note(_msg(lang, "warn.pagesIgnored", name=path.name), quiet=args.quiet)

    parts: list[tuple[dict, list[tuple[int, Path]]]] = []
    want_pages = 0
    # 什么时候 CLI 自己把 PDF 拆成页图：
    #   ① --pages（引擎没有页范围概念，必须由 CLI 选页）；
    #   ② --max-merged-pages 但**引擎不支持**该参数（旧引擎兜底分块）。
    # 新引擎已原生支持 max_merged_pages，此时直接把 PDF 交给它，避免重复光栅化。
    need_expand = is_pdf and (bool(args.pages) or (
        bool(args.max_merged_pages) and "max_merged_pages" not in caps))
    if need_expand:
        page_dir = Path(tempfile.mkdtemp(prefix="ocrmini_cli_pdf_", dir=str(tmp_root)))
        items = _expand_pdf(path, args.pages, page_dir, lang=lang)
        want_pages = len(items)
        size = int(args.max_merged_pages) if args.max_merged_pages else None
        for chunk in _chunks(items, size):
            res = engine.recognize([p for _n, p in chunk], **infer_kwargs)
            parts.append((res, chunk))
            if getattr(engine, "cancel_event", None) is not None \
                    and engine.cancel_event.is_set():
                raise KeyboardInterrupt
    else:
        res = engine.recognize([path], **infer_kwargs)
        parts.append((res, []))

    merged = _merge_page_results(parts, want_pages)
    merged["source"] = str(path)
    merged["elapsed"] = float(merged.get("elapsed") or (time.time() - t0))
    merged.setdefault("outPath", None)
    merged["error"] = None
    return merged


# 结果 → 落盘文本
def _pages_to_markdown(pages: list[dict]) -> str:
    if not pages:
        return ""
    if len(pages) == 1:
        return pages[0].get("markdown", "")
    return "\n\n".join(f"<!-- page {p.get('page')} -->\n\n{p.get('markdown', '')}"
                       for p in pages)


def _md_to_plain(md: str) -> str:
    """复用后端的纯文本转换（与 GUI 的 txt 导出同一套规则）。"""
    try:
        from webview_backend import WebBackend
        return WebBackend._md_to_plain(md)
    except Exception:                                   # pragma: no cover
        return md


def _sanitize_stem(name: str) -> str:
    try:
        from webview_backend import WebBackend
        return WebBackend._sanitize_name(name)
    except Exception:                                   # pragma: no cover
        banned = '<>:"/\\|?*'
        cleaned = "".join(c for c in str(name) if c not in banned).strip().rstrip(".")
        return (cleaned or "result")[:80]


def _unique_path(target: Path) -> Path:
    """同名不覆盖：a.md → a (1).md → a (2).md（与后端 _save_output 一致）。"""
    if not target.exists():
        return target
    stem, suffix, i = target.stem, target.suffix, 1
    while True:
        cand = target.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
        i += 1


def _build_searchable_pdf(source: Path, blocks: list, target: Path,
                         page_range: str = "") -> Path:
    """E8：调用后端的可搜索 PDF 构建器（懒导入，避免 CLI 导入期拉 webview_backend）。"""
    from webview_backend import build_searchable_pdf
    return build_searchable_pdf(source, blocks, target,
                                dpi=PDF_DPI, page_range=page_range)


def _write_office(target: Path, fmt: str, blocks: list, name: str) -> Path:
    """E9/E10：把 E2 页信封 blocks 写成 docx/xlsx（原子 tmp→replace）。

    office_export 由另一路同事实现（签名固定）；本函数只负责写盘。
    xlsx 无表格时 office_export 抛 ValueError("no tables")，由调用方转成退出码 2。
    """
    from office_export import docx_bytes, xlsx_bytes
    if fmt == "docx":
        data = docx_bytes(blocks, title=name)
    else:
        data = xlsx_bytes(blocks, name=name)
    if not data:
        raise ValueError("empty office bytes")
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_bytes(bytes(data))
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return target


def _write_output(out_dir: Path, source: Path, fmt: str, result: dict) -> Path:
    """每份结果落盘（--out 目录；md/txt/json 三选一）。返回实际路径。"""
    stem = _sanitize_stem(source.stem)
    ext = {"md": ".md", "txt": ".txt", "json": ".json"}.get(fmt, ".md")
    target = _unique_path(Path(out_dir) / f"{stem}{ext}")
    if fmt == "json":
        payload = _json_item(result)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
        return target
    pages = result.get("pages") or []
    text = _pages_to_markdown(pages)
    if fmt == "txt":
        text = "\n\n".join(_md_to_plain(p.get("markdown", "")) for p in pages).strip()
    target.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
    return target


def _json_blocks(result: dict) -> list[dict]:
    """把结果里的 blocks 归一成与 HTTP/GUI 相同的外壳 `[{page, blocks}]`。

    引擎真实形态就是页信封；这里同时兼容三种旧/简化写法，保证 CLI 的
    JSON 契约稳定：
      • `[{page, blocks}]`       → 原样（页码非 1..n 时保留原页号）
      • `[[block, …], …]`        → 每页一个块列表，页号按下标 1..n
      • `[block, …]`             → 单页扁平块列表，归到第 1 页
      • 缺省/非 list             → []
    """
    raw = result.get("blocks") if isinstance(result, dict) else None
    if not isinstance(raw, list) or not raw:
        return []
    if all(isinstance(b, dict) for b in raw):
        if any("blocks" in b for b in raw):
            out = []
            for idx, env in enumerate(raw):
                try:
                    num = int(env.get("page"))
                except (TypeError, ValueError):
                    num = idx + 1
                blocks = env.get("blocks")
                out.append({"page": num,
                            "blocks": list(blocks) if isinstance(blocks, list) else []})
            return out
        return [{"page": 1, "blocks": list(raw)}]
    return [{"page": i + 1,
             "blocks": list(p) if isinstance(p, (list, tuple)) else []}
            for i, p in enumerate(raw)]


def _json_item(result: dict) -> dict:
    """单份结果的 JSON 契约（新增引擎字段一律防御式读取）。

    `blocks`（E2）与 HTTP/GUI 落盘同构：`[{page, blocks:[{type,label,level,
    bbox,text,mergePrev}]}]`。旧引擎没有该字段时输出 []（结构稳定）。
    """
    return {
        "source": result.get("source", ""),
        "pages": result.get("pages") or [],
        "blocks": _json_blocks(result),
        "elapsed": round(float(result.get("elapsed") or 0.0), 3),
        "truncated": bool(result.get("truncated")),
        "pagesMismatch": bool(result.get("pagesMismatch")),
        "groups": _groups_count(result.get("groups")),
        "outPath": result.get("outPath"),
        "error": result.get("error"),
    }


# ══════════════════════════════════════════════════════════════════
# status 子命令（不加载模型、不 import torch）
# ══════════════════════════════════════════════════════════════════
def _cmd_status(args, lang: str) -> int:
    model_dir = _resolve_model_dir()
    present = False
    try:
        from downloader import quick_check
        present = bool(quick_check(model_dir))
    except Exception:                                   # pragma: no cover
        pass
    runtime = None
    try:
        from downloader import installed_torch_info
        runtime = installed_torch_info()
    except Exception:                                   # pragma: no cover
        pass
    torch_ver = _pkg_version("torch") or (runtime or {}).get("torch")
    transformers_ver = _pkg_version("transformers")
    pymupdf_ver = _pkg_version("pymupdf") or (_pkg_version("PyMuPDF"))
    if pymupdf_ver is None and _find_spec("fitz") is not None:
        pymupdf_ver = "present"
    data = {
        "ok": bool(present and torch_ver),
        "version": APP_VERSION,
        "modelDir": str(model_dir),
        "modelPresent": present,
        "runtimeFlavor": (runtime or {}).get("flavor"),
        "torch": torch_ver,
        "transformers": transformers_ver,
        "pymupdf": pymupdf_ver,
        "cuda": None,       # 未加载 torch：CUDA 状态需要 device_info()，status 刻意不探测
    }
    if args.json:
        _out(json.dumps(data, ensure_ascii=False))
        return EXIT_OK
    yes, no = _msg(lang, "status.yes"), _msg(lang, "status.no")
    _out(_msg(lang, "status.version", ver=APP_VERSION))
    _out(_msg(lang, "status.modelDir", path=data["modelDir"]))
    _out(_msg(lang, "status.modelPresent", state=yes if present else no))
    _out(_msg(lang, "status.runtime", state=runtime.get("flavor") if runtime else no))
    _out(_msg(lang, "status.torch", state=torch_ver or no))
    _out(_msg(lang, "status.transformers", state=transformers_ver or no))
    _out(_msg(lang, "status.pymupdf", state=pymupdf_ver or no))
    _out(_msg(lang, "status.cuda"))
    return EXIT_OK


# ══════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════
def _print_human_summary(lang: str, results: list[dict], elapsed: float,
                         quiet: bool):
    ok = sum(1 for r in results if not r.get("error"))
    total = len(results)
    if not quiet:
        _out("")
    _out(_msg(lang, "info.summary", ok=ok, total=total,
              failed=total - ok, secs=elapsed))


def main(argv: list[str] | None = None, engine_factory=None) -> int:
    """CLI 入口。返回退出码（不调用 sys.exit；__main__ 才 sys.exit）。

    engine_factory: 仅测试注入用。给定时跳过环境自检与真实引擎构造，
    但仍调用 engine.load()（假引擎可为 no-op），以便覆盖「加载失败→2」路径。
    """
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args, code = _parse_args(raw_argv)
    if code is not None:
        return code
    if args is None:                                    # pragma: no cover - 防御
        return EXIT_PARAM
    lang = _apply_lang(getattr(args, "lang", None))

    if getattr(args, "version", False):
        _out(APP_VERSION)
        return EXIT_OK
    if getattr(args, "cmd", None) == "status":
        return _cmd_status(args, lang)

    # ── 参数校验（贵操作之前全部前置）─────────────────────────
    if not args.input:
        _err(_msg(lang, "err.inputMissing"))
        return EXIT_PARAM
    try:
        files = collect_inputs(args.input, recursive=args.recursive, lang=lang)
    except CliError as e:
        _err(str(e))
        return EXIT_PARAM
    if not files:
        _err(_msg(lang, "err.noFiles", path=str(Path(args.input).expanduser())))
        return EXIT_PARAM
    for name, value in (("--max-page-tokens", args.max_page_tokens),
                        ("--max-merged-pages", args.max_merged_pages)):
        if value is not None and int(value) < 1:
            _err(_msg(lang, "err.badNumber", name=name, value=value))
            return EXIT_PARAM
    if args.pages:
        try:
            parse_pages(args.pages)
        except CliError as e:
            _err(_msg(lang, "err.badPages", spec=args.pages, err=e))
            return EXIT_PARAM
    # --format 缺省 + --json → 纯管道模式（不落盘）；否则默认 md 落盘
    fmt = args.format
    write_files = True
    if fmt is None:
        fmt = "md"
        write_files = not args.json
    out_dir = Path(args.out).expanduser() if args.out else Path.cwd()
    if fmt == "pdf":
        # E8：可搜索 PDF 需要「原始 PDF + blocks」→ 只支持单个 PDF 输入；
        # 非 PDF → 参数错误(1)；缺 pymupdf → 环境错误(2)；缺 blocks 在识别后判(2)。
        if len(files) != 1 or files[0].suffix.lower() != ".pdf":
            _err(_msg(lang, "err.pdfNeedsPdfInput"))
            return EXIT_PARAM
        if _find_spec("fitz") is None and _find_spec("pymupdf") is None \
                and _pkg_version("pymupdf") is None:
            _err(_msg(lang, "err.pymupdfMissing"))
            return EXIT_ENV
    if write_files:
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            if not out_dir.is_dir():
                raise OSError("not a directory")
        except OSError as e:
            _err(_msg(lang, "err.outDir", path=str(out_dir), err=e))
            return EXIT_PARAM

    # ── 引擎（自检/构造/加载）────────────────────────────────
    if engine_factory is not None:
        engine = engine_factory()
    else:
        model_dir = _resolve_model_dir()
        problem = _preflight(model_dir, files, lang=lang)
        if problem:
            _err(problem)
            return EXIT_ENV
        engine = _make_engine(model_dir)
    _note(_msg(lang, "info.loading", device=args.device), quiet=args.quiet)
    t_load = time.time()
    try:
        engine.load(device_pref=args.device)
    except KeyboardInterrupt:
        _err(_msg(lang, "info.cancelled"))
        return EXIT_INTERRUPTED
    except Exception as e:
        _err(_msg(lang, "err.loadFailed", err=e))
        if args.verbose:
            traceback.print_exc(file=sys.stderr)
        return EXIT_ENV
    if not getattr(engine, "ready", True):
        _err(_msg(lang, "err.notReady"))
        return EXIT_ENV
    _note(_msg(lang, "info.ready", secs=time.time() - t_load), quiet=args.quiet)

    caps = _engine_caps(engine)
    if args.max_page_tokens and "max_page_tokens" not in caps:
        _note(_msg(lang, "warn.noPageTokens"), quiet=args.quiet)
    if args.drop_headers and "drop_headers" not in caps:
        _note(_msg(lang, "warn.noDropHeaders"), quiet=args.quiet)
    chunk_host_side = bool(args.max_merged_pages) and "max_merged_pages" not in caps
    if chunk_host_side:
        _note(_msg(lang, "warn.chunk", n=args.max_merged_pages), quiet=args.quiet)

    # ── 逐文件识别 ───────────────────────────────────────────
    results: list[dict] = []
    t0 = time.time()
    tmp_root = Path(tempfile.mkdtemp(prefix="ocrmini_cli_"))
    try:
        for i, path in enumerate(files, 1):
            if not os.access(path, os.R_OK):
                results.append({"source": str(path), "pages": [], "elapsed": 0.0,
                                "truncated": False, "pagesMismatch": False,
                                "groups": 0, "outPath": None,
                                "error": _msg(lang, "err.fileUnreadable", path=str(path))})
                _err(f"[{i}/{len(files)}] {path.name}: {results[-1]['error']}")
                continue
            _note(_msg(lang, "info.fileStart", i=i, n=len(files), name=path.name),
                  quiet=args.quiet)
            try:
                res = _recognize_file(engine, path, args, caps, lang, tmp_root)
                if res.get("pagesMismatch"):
                    _note(_msg(lang, "warn.shortPages",
                               got=len(res.get("pages") or []),
                               want=res.get("expectedPages") or "?"),
                          quiet=args.quiet)
                if write_files:
                    if fmt == "pdf":
                        # E8：原 PDF 上叠不可见文本层（页范围映射用 --pages）
                        blocks = res.get("blocks")
                        if not blocks:
                            _err(_msg(lang, "err.pdfNoBlocks"))
                            return EXIT_ENV
                        target = _unique_path(
                            out_dir / f"{_sanitize_stem(path.stem)}.pdf")
                        try:
                            _build_searchable_pdf(
                                path, blocks, target,
                                page_range=args.pages or "")
                        except Exception as e:
                            _err(_msg(lang, "err.pdfExportFailed", err=e))
                            if args.verbose:
                                traceback.print_exc(file=sys.stderr)
                            return EXIT_ENV
                        res["outPath"] = str(target)
                    elif fmt in ("docx", "xlsx"):
                        # E9/E10：结构化导出；无 blocks / 无表格 → 退出码 2
                        blocks = res.get("blocks")
                        if not blocks:
                            _err(_msg(lang, "err.officeNeedsBlocks"))
                            return EXIT_ENV
                        stem = _sanitize_stem(path.stem)
                        target = _unique_path(out_dir / f"{stem}.{fmt}")
                        try:
                            _write_office(target, fmt, blocks, stem)
                        except ValueError as e:
                            key = ("err.officeNoTable"
                                   if "no tables" in str(e).lower()
                                   else "err.officeExportFailed")
                            _err(_msg(lang, key, fmt=fmt, err=e))
                            return EXIT_ENV
                        except Exception as e:
                            _err(_msg(lang, "err.officeExportFailed",
                                      fmt=fmt, err=e))
                            if args.verbose:
                                traceback.print_exc(file=sys.stderr)
                            return EXIT_ENV
                        res["outPath"] = str(target)
                        res["savedFormat"] = fmt
                    else:
                        res["outPath"] = str(
                            _write_output(out_dir, path, fmt, res))
                results.append(res)
                if not args.json:
                    if fmt == "pdf":
                        _out(_msg(lang, "info.pdfOk", name=path.name,
                                  path=res.get("outPath")))
                    elif fmt in ("docx", "xlsx"):
                        _out(_msg(lang, "info.officeOk", name=path.name,
                                  fmt=fmt, path=res.get("outPath")))
                    else:
                        key = ("info.fileOk" if res.get("outPath")
                               else "info.fileOkNoOut")
                        _out(_msg(lang, key, name=path.name,
                                  pages=len(res.get("pages") or []),
                                  secs=float(res.get("elapsed") or 0.0)))
            except KeyboardInterrupt:
                raise
            except Exception as e:
                msg = _msg(lang, "err.recognizeFailed", name=path.name, err=e)
                results.append({"source": str(path), "pages": [], "elapsed": 0.0,
                                "truncated": False, "pagesMismatch": False,
                                "groups": 0, "outPath": None, "error": str(e)})
                _err(msg)
                if args.verbose:
                    traceback.print_exc(file=sys.stderr)
    except KeyboardInterrupt:
        try:
            if getattr(engine, "cancel_event", None) is not None:
                engine.cancel_event.set()
        except Exception:                                # pragma: no cover
            pass
        _err(_msg(lang, "info.cancelled"))
        return EXIT_INTERRUPTED
    finally:
        shutil.rmtree(str(tmp_root), ignore_errors=True)

    elapsed = time.time() - t0
    # ── 输出 ─────────────────────────────────────────────────
    payload = {
        "ok": all(not r.get("error") for r in results),
        "results": [_json_item(r) for r in results],
        "summary": {
            "total": len(results),
            "ok": sum(1 for r in results if not r.get("error")),
            "failed": sum(1 for r in results if r.get("error")),
            "elapsed": round(elapsed, 3),
        },
        "error": None,
    }
    # 单输入时把契约字段平铺到顶层（便于 `cli.py --json | jq .pages`）
    if len(results) == 1:
        for key, value in _json_item(results[0]).items():
            payload.setdefault(key, value)
    if args.json:
        _out(json.dumps(payload, ensure_ascii=False))
    else:
        _print_human_summary(lang, results, elapsed, args.quiet)
    return EXIT_PARTIAL if payload["summary"]["failed"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

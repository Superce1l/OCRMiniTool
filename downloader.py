"""downloader.py — Unlimited-OCR-2 模型下载器（HuggingFace + 镜像）

职责：
  • 维护模型必要文件清单（权重分片 + 配置 + trust_remote_code 所需的全部 .py）
  • 逐文件下载：断点续传（Range）、Content-Length 完整性对账、证书验证失败自动
    降级重试（企业/校园代理重签 TLS 的常见环境问题，与 STTMiniTool 同一套手法）
  • 可选镜像站（如 hf-mirror.com）：设置里配置后自动改写 huggingface.co URL
  • progress_cb(pct, msg) 汇报总进度（按字节加权，大文件占大头）

模型仓库：https://huggingface.co/baidu/Unlimited-OCR-2（MIT）
权重约 6.7 GB（2 个 safetensors 分片，BF16，~3.3B 参数）。
"""
from __future__ import annotations

import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from i18n import tr

HF_OFFICIAL = "https://huggingface.co"
REPO_ID = "baidu/Unlimited-OCR-2"
HF_BASE = f"{HF_OFFICIAL}/{REPO_ID}/resolve/main"

_UA = "OCRMiniTool/1.0 (+https://github.com/baidu/Unlimited-OCR)"
_MIRROR_BASE = ""          # set_mirror() 写入；如 https://hf-mirror.com

# ── 模型必要文件清单 ──────────────────────────────────────────────
# trust_remote_code 的动态模块加载器只复制入口模块的**直接**相对导入；
# 二级导入（_sparse_base_impl / dash_attention / packing_patch /
# sparse_gather_*）也要显式列出，缺一个加载时就 ImportError。
WEIGHTS: list[str] = [
    "model-00001-of-00002.safetensors",
    "model-00002-of-00002.safetensors",
]
CONFIG_FILES: list[str] = [
    "config.json",
    "generation_config.json",
    "model.safetensors.index.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
]
CODE_FILES: list[str] = [
    "modeling_unlimitedocr.py",
    "modeling_deepseekv2.py",
    "configuration_deepseek_v2.py",
    "deepencoder.py",
    "conversation.py",
    "dash_attention.py",
    "_sparse_base_impl.py",
    "sparse_gather_triton.py",
    "sparse_gather_e2e_triton.py",
    "packing_patch.py",
]
ALL_FILES = WEIGHTS + CONFIG_FILES + CODE_FILES

# 大文件完整性下限：残档/被代理劫持的 HTML 错误页不该被当完整模型。
MIN_SIZE_BYTES: dict[str, int] = {
    "model-00001-of-00002.safetensors": 1 << 30,   # > 1 GB
    "model-00002-of-00002.safetensors": 1 << 30,   # > 1 GB
    "tokenizer.json":                   1 << 20,   # > 1 MB
    "modeling_unlimitedocr.py":         40_000,
    "_sparse_base_impl.py":             100_000,
}


def set_mirror(base: str | None):
    """设置镜像站（如 hf-mirror.com）；空串恢复官方源。线程安全性由调用方保证。"""
    global _MIRROR_BASE
    base = (base or "").strip().rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        base = "https://" + base
    _MIRROR_BASE = base


def get_mirror() -> str:
    return _MIRROR_BASE


def _apply_mirror(url: str) -> str:
    if _MIRROR_BASE and url.startswith(HF_OFFICIAL):
        return _MIRROR_BASE + url[len(HF_OFFICIAL):]
    return url


# ── SSL Context：certifi → 系统默认 → 降级不验证 ────────────────────
def _ssl_ctx() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        import certifi  # noqa
    except Exception:
        pass
    return ssl.create_default_context()


def _ssl_ctx_unverified() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def file_url(fname: str) -> str:
    return _apply_mirror(f"{HF_BASE}/{fname}")


def quick_check(model_dir: Path) -> bool:
    """所有必要文件都存在且越过大小下限 → True（不联网校验哈希）。"""
    model_dir = Path(model_dir)
    for fname in ALL_FILES:
        p = model_dir / fname
        if not p.is_file():
            return False
        floor = MIN_SIZE_BYTES.get(fname, 1)
        try:
            if p.stat().st_size < floor:
                return False
        except OSError:
            return False
    return True


def missing_files(model_dir: Path) -> list[str]:
    """返回缺失（或残缺）的文件名清单。"""
    model_dir = Path(model_dir)
    out = []
    for fname in ALL_FILES:
        p = model_dir / fname
        ok = p.is_file()
        if ok:
            try:
                ok = p.stat().st_size >= MIN_SIZE_BYTES.get(fname, 1)
            except OSError:
                ok = False
        if not ok:
            out.append(fname)
    return out


def _download_file(url: str, dest: Path, progress_cb=None, cancel_check=None):
    """下载单一文件至 dest，支持断点续传。

    progress_cb(done_bytes, total_bytes)
    cancel_check() 返回 True → 抛 CancelledError（保留 .part，下次续传）。

    完整性校验：下载结束时对账 Content-Length——不匹配（连接中断导致静默
    截断）则删除残档并抛 RuntimeError，绝不把残缺文件留给加载端当完整模型。
    """
    from applog import log_download, log_error

    dest.parent.mkdir(parents=True, exist_ok=True)
    existing = dest.stat().st_size if dest.exists() else 0

    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    if existing > 0:
        req.add_header("Range", f"bytes={existing}-")

    try:
        resp = urllib.request.urlopen(req, timeout=60, context=_ssl_ctx())
    except urllib.error.HTTPError as e:
        if e.code == 416:
            # 416 Range Not Satisfiable = 本地文件已完整，直接视为成功
            return
        raise
    except urllib.error.URLError as e:
        # 证书验证失败（企业代理重签 TLS、系统缺根证书）→ 降级不验证重试一次。
        # 来源为固定的 HF 官方域／其镜像，风险可控；留痕供诊断。
        reason = getattr(e, "reason", None)
        is_cert = isinstance(reason, ssl.SSLCertVerificationError) or \
            "CERTIFICATE_VERIFY_FAILED" in str(reason)
        if not is_cert:
            raise
        from applog import log_warn
        log_warn(f"HTTPS 证书验证失败，降级为不验证下载：{url}")
        resp = urllib.request.urlopen(req, timeout=60,
                                      context=_ssl_ctx_unverified())

    content_length = int(resp.headers.get("Content-Length", 0))

    # 追加写入（resume）或全新写入；total 在 existing 归零后计算：
    # 服务器忽略 Range 返回 200 完整内容时，残档大小不能重复计入。
    mode = "ab" if existing > 0 and resp.status == 206 else "wb"
    if mode == "wb":
        existing = 0
    total = existing + content_length if content_length else 0

    done = existing
    try:
        with open(dest, mode) as f:
            while True:
                if cancel_check is not None and cancel_check():
                    raise CancelledError(dest.name)
                chunk = resp.read(1 << 16)   # 64 KB
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb and total:
                    progress_cb(done, total)
    finally:
        resp.close()

    if total and done != total:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        log_error("下载完整性校验失败（已删除残档）")
        log_download(f"{dest.name}: received {done}/{total} bytes — file removed")
        raise RuntimeError(
            f"下载中断：{dest.name} 只收到 {done / 1_048_576:.0f}/"
            f"{total / 1_048_576:.0f} MB（残档已删除，重试将续传/重下）。")
    log_download(f"{dest.name}: download OK ({done / 1_048_576:.1f} MB)")


class CancelledError(RuntimeError):
    """用户取消下载。"""


# 权重体量参考（用于「预计下载量」提示；实际进度按真实 Content-Length 加权）
APPROX_TOTAL_GB = 6.8


def download_model(model_dir: Path, progress_cb=None, cancel_check=None,
                   mirror: str | None = None):
    """下载全部缺失文件至 model_dir。

    progress_cb(pct: float 0..1, msg: str)
    所有文件齐备时直接返回（幂等，可随时重入续传）。
    """
    from applog import log_download

    if mirror is not None:
        set_mirror(mirror)
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)

    todo = missing_files(model_dir)
    if not todo:
        if progress_cb:
            progress_cb(1.0, tr("dl.allReady"))
        return

    # 按体量加权：权重分片占绝大部分；小文件均摊。先下小配置文件
    # （几秒内可见进展），再下两个大分片。
    ordered = [f for f in CONFIG_FILES + CODE_FILES if f in todo] + \
              [f for f in WEIGHTS if f in todo]
    log_download(f"开始下载 Unlimited-OCR-2：{len(ordered)} 个文件"
                 f"（镜像={'官方' if not _MIRROR_BASE else _MIRROR_BASE}）")

    n = len(ordered)
    for idx, fname in enumerate(ordered):
        if cancel_check is not None and cancel_check():
            raise CancelledError(fname)
        base_pct = idx / n
        span_pct = 1.0 / n
        if progress_cb:
            progress_cb(base_pct, tr("dl.file", name=fname))

        dest = model_dir / fname

        def _file_cb(done: int, total: int, _b=base_pct, _s=span_pct, _f=fname):
            if progress_cb and total > 0:
                mb = done / 1_048_576
                tmb = total / 1_048_576
                speed = ""
                progress_cb(_b + _s * done / total,
                            tr("dl.fileProgress", name=_f,
                               mb=f"{mb:.1f}", tmb=f"{tmb:.1f}", speed=speed))

        # 单文件重试（网络抖动）：3 次退避，断点续传接续
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                _download_file(file_url(fname), dest, progress_cb=_file_cb,
                               cancel_check=cancel_check)
                last_err = None
                break
            except CancelledError:
                raise
            except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                    RuntimeError) as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        if last_err is not None:
            raise last_err

        if progress_cb:
            progress_cb(base_pct + span_pct, tr("dl.fileDone", name=fname))

    if progress_cb:
        progress_cb(1.0, tr("dl.done"))
    log_download("Unlimited-OCR-2 下载完成")


def _safe_print(*args, **kwargs):
    """GBK 等非 Unicode 控制台安全的 print（同 STTMiniTool 手法）。"""
    out = kwargs.get("file") or sys.stdout
    enc = getattr(out, "encoding", None) or "utf-8"
    msg = " ".join(str(a) for a in args)
    try:
        msg.encode(enc, errors="strict")
    except UnicodeEncodeError:
        msg = msg.encode(enc, errors="replace").decode(enc, errors="replace")
    kwargs.pop("file", None)
    print(msg, file=out, **kwargs)


def _cli_bar(pct: float, msg: str):
    filled = int(pct * 40)
    bar = "█" * filled + "░" * (40 - filled)
    _safe_print(f"\r[{bar}] {pct * 100:5.1f}%  {msg:<60}", end="", flush=True)


if __name__ == "__main__":
    # 命令行下载（除错用）：python downloader.py [模型目录]
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "models" / "Unlimited-OCR-2"
    if quick_check(d):
        _safe_print("模型已完整，无需下载")
        sys.exit(0)
    _safe_print(f"下载 Unlimited-OCR-2（约 {APPROX_TOTAL_GB} GB）→ {d}")
    download_model(d, progress_cb=_cli_bar)
    _safe_print("\n完成")

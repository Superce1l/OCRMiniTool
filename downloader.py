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

import hashlib
import json
import os
import re
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

# 下载清单：记录每个文件下载完成时的真实字节数（Content-Length 对账结果）。
# 1 GiB 下限挡不住"下到 2GB 就被关掉"的截断权重（真实权重 4.66GB/1.57GB），
# 那会被 quick_check 当成完整模型、启动自动加载后失败。清单存在时按精确
# 大小比对；旧目录没有清单 → 退回 MIN_SIZE_BYTES 下限（向后兼容）。
MANIFEST_NAME = "_manifest.json"


def _manifest_path(model_dir: Path) -> Path:
    return Path(model_dir) / MANIFEST_NAME


def read_manifest(model_dir: Path) -> dict[str, int]:
    """读下载清单；缺失/损坏/仓库不符一律返回 {}（旧目录照旧走下限）。"""
    try:
        data = json.loads(_manifest_path(model_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("repo") != REPO_ID:
        return {}
    files = data.get("files")
    if not isinstance(files, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in files.items():
        if isinstance(k, str) and isinstance(v, int) and v > 0:
            out[k] = v
    return out


def write_manifest(model_dir: Path, sizes: dict[str, int]):
    """把已知文件大小并入清单（临时文件 + os.replace 原子替换）。

    只应在文件确实下载完成后调用；写失败静默——清单是完整性增强，
    不该因为它而让下载流程失败。
    """
    merged = read_manifest(model_dir)
    merged.update({k: v for k, v in sizes.items()
                   if isinstance(v, int) and v > 0})
    path = _manifest_path(model_dir)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"repo": REPO_ID, "files": merged},
                                  ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _file_size_ok(p: Path, fname: str, manifest: dict[str, int]) -> bool:
    """单个模型文件是否完整：有清单按精确字节数，否则按下限。"""
    try:
        size = p.stat().st_size
    except OSError:
        return False
    want = manifest.get(fname)
    if want:
        return size == want
    return size >= MIN_SIZE_BYTES.get(fname, 1)


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
    """所有必要文件都存在且大小可信 → True（不联网校验哈希）。

    有 `_manifest.json` 时按清单里的精确字节数比对（截断的权重会被识破）；
    没有清单的旧目录退回 MIN_SIZE_BYTES 下限。
    """
    model_dir = Path(model_dir)
    manifest = read_manifest(model_dir)
    for fname in ALL_FILES:
        p = model_dir / fname
        if not p.is_file():
            return False
        if not _file_size_ok(p, fname, manifest):
            return False
    return True


def missing_files(model_dir: Path) -> list[str]:
    """返回缺失（或残缺）的文件名清单（判定口径同 quick_check）。"""
    model_dir = Path(model_dir)
    manifest = read_manifest(model_dir)
    out = []
    for fname in ALL_FILES:
        p = model_dir / fname
        if not p.is_file() or not _file_size_ok(p, fname, manifest):
            out.append(fname)
    return out


def _download_file(url: str, dest: Path, progress_cb=None, cancel_check=None,
                   expected_size: int | None = None) -> int:
    """下载单一文件至 dest，支持断点续传；返回落盘后的字节数。

    progress_cb(done_bytes, total_bytes)
    cancel_check() 返回 True → 抛 CancelledError（保留 .part，下次续传）。

    完整性校验：下载结束时对账 Content-Length——不匹配（连接中断导致静默
    截断）则删除残档并抛 RuntimeError，绝不把残缺文件留给加载端当完整模型。
    expected_size（可选，来自 HEAD）：已知远端大小时，416/落盘后再核对一次，
    大小不符就删掉重下而不是把残档当"已完整"。
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
            # 416 Range Not Satisfiable = 本地已 >= 远端。只有"确实相等"才算
            # 完整；本地比远端还大/大小不符说明残档坏了，删掉重下。
            local = dest.stat().st_size if dest.exists() else 0
            if expected_size is not None and local != expected_size:
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
                raise RuntimeError(
                    f"本地残档大小不符（{local}/{expected_size} 字节），"
                    f"已删除，重试将重下。")
            return local
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
    if expected_size is not None and done != expected_size:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(
            f"下载大小与远端不符：{dest.name} 期望 {expected_size} 字节，"
            f"实得 {done} 字节（残档已删除）。")
    log_download(f"{dest.name}: download OK ({done / 1_048_576:.1f} MB)")
    return done


def _remote_size(url: str, timeout: float = 30.0) -> int | None:
    """HEAD 取远端字节数（校验收到的残档/分卷用）。取不到返回 None。"""
    try:
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ssl_ctx()) as resp:
            n = int(resp.headers.get("Content-Length") or 0)
            return n if n > 0 else None
    except Exception:
        return None


def _download_part(url: str, pdest: Path, progress_cb=None,
                   cancel_check=None) -> int:
    """下载一个分卷到 pdest：字节数对得上才改名，短档不会被永远跳过。

    旧实现是 `if not pdest.exists(): _download_file(...)`：上次被杀/断网留下
    的短分卷会被**永远跳过**，拼接出的 zip 短一截、解压失败（D6）。现在：
      • 期望字节数来自 HEAD（拿不到就退化为"下完即用"）；
      • 先写 `<pdest>.part`，与期望一致才 os.replace 成正式名；
      • 正式名已存在且大小符合期望 → 跳过（幂等；不符则重下）。
    """
    expected = _remote_size(url)
    staging = pdest.with_name(pdest.name + ".part")
    if expected is not None:
        try:
            if pdest.is_file() and pdest.stat().st_size == expected:
                try:
                    staging.unlink(missing_ok=True)   # 清掉上次的暂存残留
                except OSError:
                    pass
                return expected
        except OSError:
            pass
        try:
            if staging.is_file() and staging.stat().st_size == expected:
                os.replace(staging, pdest)
                return expected
        except OSError:
            pass
    got = _download_file(url, staging, progress_cb=progress_cb,
                         cancel_check=cancel_check)
    try:
        got = staging.stat().st_size
    except OSError:
        got = 0
    if expected is not None and got != expected:
        try:
            staging.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(
            f"分卷不完整：{pdest.name} 期望 {expected} 字节，实得 {got} 字节。")
    os.replace(staging, pdest)
    return got


class CancelledError(RuntimeError):
    """用户取消下载。"""


# ── D5：组件包整包 sha256 校验（warn-only / strict 两阶段）────────────
# 下载/拼接完成、**解压之前**校验整包哈希，弥补「只有 Content-Length」的完整性缺口。
# 期望值来源（优先级从高到低）：
#   ① _TORCH_FLAVORS[flavor]["sha256"]（发布后回填，空=跳过）
#   ② 远端 <zip_url>.sha256（纯文本，取第一个 64 位 hex；多写文件名也兼容）
#   ③ 远端 manifest.json（build_components.py 产出；兼容扁平与 {"zips": {...}} 两种结构）
# 模式：默认 warn-only（不一致只记 WARN，继续安装）；环境变量
#       OCRMT_VERIFY_RUNTIME=strict 时删掉 zip 与分卷并抛清晰异常。
# 边界：远端取不到/内容非法/拿不到期望值 → 一律 WARN 不阻断（strict 也不删）。
# 与 D6 的关系：只在拼接/下载完成之后读 final zip，不改变分卷续传/暂存逻辑。
_VERIFY_ENV = "OCRMT_VERIFY_RUNTIME"
_SHA256_HEX_RE = re.compile(r"[0-9a-fA-F]{64}")
_REMOTE_HASH_TIMEOUT = 4.0
# 远端哈希端点本进程内已确认不可用时置 True，后续下载不再为它反复等网络。
_REMOTE_HASH_DOWN = False


def verify_mode() -> str:
    """当前校验模式：'strict' 或 'warn'（默认）。"""
    return "strict" if (os.environ.get(_VERIFY_ENV) or "").strip().lower() == "strict" else "warn"


def normalize_sha256(text) -> str | None:
    """从自由文本里取第一个 64 位 hex（小写）；没有/非法返回 None。"""
    if not text:
        return None
    for token in str(text).split():
        if _SHA256_HEX_RE.fullmatch(token):
            return token.lower()
    return None


def sha256_file(path) -> str:
    """整包 sha256（1 MiB 分块，3.5 GB 也不吃内存）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_text(url: str, timeout: float = _REMOTE_HASH_TIMEOUT) -> str | None:
    """取远端小文本（.sha256 / manifest.json）；任何失败返回 None，绝不抛。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ssl_ctx()) as resp:
            return resp.read(64 * 1024).decode("utf-8", "replace")
    except Exception:
        return None


def _expected_sha256(meta: dict, zip_url: str, zip_name: str,
                     notes: list[str]) -> tuple[str | None, str]:
    """按优先级解析期望哈希 → (hash|None, 来源标签)；失败原因追加到 notes。"""
    global _REMOTE_HASH_DOWN
    a = normalize_sha256(meta.get("sha256"))
    if a:
        return a, "flavor"
    notes.append("flavor.sha256 未配置")

    if _REMOTE_HASH_DOWN:
        notes.append("远端哈希端点本进程内已失败，跳过网络")
        return None, "unavailable"

    text = _fetch_text(zip_url + ".sha256")
    if text is None:
        notes.append("远端 <zip>.sha256 不可用（缺失/网络失败）")
    else:
        b = normalize_sha256(text)
        if b:
            return b, "remote .sha256"
        notes.append("远端 <zip>.sha256 内容不是 64 位 hex")

    mtext = _fetch_text(f"{_TORCH_REL_BASE}/manifest.json")
    if mtext is None:
        notes.append("远端 manifest.json 不可用")
    else:
        try:
            data = json.loads(mtext)
        except Exception:
            data = None
            notes.append("远端 manifest.json 不是合法 JSON")
        if isinstance(data, dict):
            entry = data.get(zip_name)
            if entry is None and isinstance(data.get("zips"), dict):
                entry = data["zips"].get(zip_name)
            if isinstance(entry, dict):
                c = normalize_sha256(entry.get("sha256"))
                if c:
                    return c, "manifest.json"
                notes.append("manifest.json 中该 zip 的 sha256 为空/非法")
            else:
                notes.append("manifest.json 中无该 zip 条目")
    _REMOTE_HASH_DOWN = True      # 两个端点都不可用 → 本进程不再重试
    return None, "unavailable"


def _verify_zip_sha256(flavor: str, zip_path: Path, meta: dict,
                       parts=()) -> None:
    """解压前校验整包 sha256；按 verify_mode() 决定 WARN 还是删包抛错。"""
    from applog import log_download

    mode = verify_mode()
    notes: list[str] = []
    expected, source = _expected_sha256(
        meta, f"{_TORCH_REL_BASE}/{meta['zip']}", meta["zip"], notes)
    if not expected:
        log_download(f"WARN 组件校验跳过：{flavor} 拿不到期望 sha256"
                     f"（{'；'.join(notes)}；mode={mode}）")
        return
    try:
        got = sha256_file(zip_path)
    except OSError as e:
        log_download(f"WARN 组件校验跳过：{flavor} 计算 sha256 失败"
                     f"（{e}；mode={mode}）")
        return
    if got == expected:
        log_download(f"组件校验通过：{flavor} sha256={got}"
                     f"（source={source}, mode={mode}）")
        return

    detail = f"期望 {expected}（source={source}），实得 {got}"
    if mode != "strict":
        log_download(f"WARN 组件 sha256 不匹配：{flavor} {detail}；"
                     "warn-only，继续安装（mode=warn）")
        return

    # strict：删除下载产物（zip + 分卷 + 可能的暂存/拼接残档）再抛错
    candidates = [Path(zip_path)]
    candidates += [Path(p) for p in parts]
    candidates += [Path(str(zip_path) + ".part"),
                   Path(str(zip_path) + ".concat.part")]
    removed: list[str] = []
    for p in candidates:
        try:
            if p.exists():
                p.unlink()
                removed.append(p.name)
        except OSError:
            pass
    log_download(f"组件 sha256 不匹配：{flavor} {detail}；"
                 f"strict 模式已删除 {removed}（mode=strict）")
    raise RuntimeError(
        f"组件包校验失败：{meta['zip']} sha256 不匹配"
        f"（期望 {expected[:16]}…，实得 {got[:16]}…，来源 {source}）。"
        "已删除下载文件；请检查网络/镜像后重试。")


# ── 推理引擎组件（PyTorch 运行时）：CPU / CUDA 按需下载 ─────────────
# 安装包本体**不含 torch**（v1.1.2 曾把构建机的 CPU 版 torch 照单打进包，
# 用户有 N 卡也永远检测不到——issue #3；反过来想，干脆谁都 不带，安装包从
# 4.4G 缩到 ~0.3G）。使用者在「模型」页按自己硬件二选一下载，解压到
# RUNTIME_DIR（torch/、torchvision/ 顶层目录），ocr_engine 启动时把该目录
# 插到 sys.path 最前，import torch 即命中外部组件。
#
# 组件 zip 由 build_components.py 从官方 wheel（pip install --target）打包
# 并输出 sha256，发布到本仓库 GitHub Release（tag：runtime-torch<版本>）。
# GitHub 直连不走 HF 镜像（set_mirror 只改写 huggingface.co URL）。
# 版本必须与 transformers==4.52.1 兼容：torch 2.11 起移除了
# torch.nn.functional._model_output_flatten 等内部符号，transformers
# 4.52.1 的 generation.utils 仍在引用 → GenerationMixin/AutoTokenizer 全体
# 占位化（NameError 实录）。2.8.0+cu128 是 AutoDL 真机验证过的组合。
_TORCH_VERSION = "2.8.0"
_TORCHVISION_VERSION = "0.23.0"
_TORCH_TAG = f"runtime-torch{_TORCH_VERSION}"
# OCRMIT_RUNTIME_BASE：组件下载基址覆盖（测试/内网镜像用），形如
# http://host/prefix；缺省 = 本仓库 GitHub Release（runtime-torch<版本> tag）
_TORCH_REL_BASE = (os.environ.get("OCRMIT_RUNTIME_BASE").rstrip("/")
                   if os.environ.get("OCRMIT_RUNTIME_BASE")
                   else ("https://github.com/Superce1l/OCRMiniTool/releases/"
                         f"download/{_TORCH_TAG}"))

# cu128 wheel 要求的最低 NVIDIA 驱动（CUDA 12.8 → 570 系列；取整 570.0）
_CU128_MIN_DRIVER = 570.0


def _runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


RUNTIME_DIR = _runtime_base_dir() / "runtime"
_TORCH_MARKER = "torch_build.json"

# flavor → 组件元数据。label/desc 走 i18n key（界面语言跟随设置，英文用户
# 看英文文案——与 STT 的纯中文注册表不同，OCR 的用户一半在墙外）。
_TORCH_FLAVORS: dict[str, dict] = {
    "cpu": {
        "zip": f"ocrmini-runtime-torch{_TORCH_VERSION}-cpu.zip",
        "size_mb": 259,
        "label_key": "env.flavor.cpu",
        "desc_key": "env.flavor.cpuDesc",
        # D5：整包 sha256。发布后从 build_components.py 产出的 `<zip>.sha256`
        # 回填到这里（可选）；空串 = 交给远端 .sha256 / manifest.json 兜底。
        "sha256": "",
    },
    "cu128": {
        "zip": f"ocrmini-runtime-torch{_TORCH_VERSION}-cu128.zip",
        "size_mb": 3300,
        # GitHub Release 单资产上限 2GB → CUDA 包按定长分卷发布
        # （.zip.part01-of-2 / .part02-of-2）；下载端逐卷断点续传后二进制
        # 拼接（内容即原 zip，sha256 对账不变）。
        "parts": 2,
        "label_key": "env.flavor.cu128",
        "desc_key": "env.flavor.cu128Desc",
        "sha256": "",
    },
}


def torch_flavors() -> dict[str, dict]:
    """flavor 注册表唯读副本（供 UI 列菜单）。"""
    return {k: dict(v) for k, v in _TORCH_FLAVORS.items()}


def installed_torch_info(runtime_dir: Path | None = None) -> dict | None:
    """读安装标记 torch_build.json；None＝尚未安装任何推理引擎组件。"""
    d = runtime_dir or RUNTIME_DIR
    try:
        data = json.loads((d / _TORCH_MARKER).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("flavor") else None
    except Exception:
        return None


def quick_check_torch(flavor: str, runtime_dir: Path | None = None) -> bool:
    """推理引擎组件是否就绪：标记在 + torch 可见 + 关键 DLL 在位。

    cu128 额外要求 torch_cuda.dll（CPU zip 覆盖安装后残留标记不一致的兜底）。
    """
    d = runtime_dir or RUNTIME_DIR
    info = installed_torch_info(d)
    if not info or info.get("flavor") != flavor:
        return False
    if info.get("torch") != _TORCH_VERSION:
        return False
    if not (d / "torch" / "__init__.py").is_file():
        return False
    if flavor == "cu128" and not (d / "torch" / "lib" / "torch_cuda.dll").is_file():
        return False
    return True


def detect_hardware() -> dict:
    """检测显卡与 NVIDIA 驱动版本（Windows：WMI + nvidia-smi，零依赖）。

    从 STTMiniTool 同名函数移植。必须在组件下载前就能跑（先有鸡先有蛋：
    torch 还没装，不能用 torch.cuda 探测）。
    """
    info = {"gpus": [], "vendors": [], "nvidia_driver": None,
            "has_discrete": False, "error": None}
    if sys.platform != "win32":
        # 非 Windows（开发/远程调试）也给出最小判定，不弹错误
        info["vendors"] = []
        return info

    import json as _json
    import subprocess

    def _run_hidden(cmd: list[str], timeout: int = 12) -> str:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            r = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=timeout, creationflags=0x08000000,
                               startupinfo=si)
            return r.stdout or ""
        except Exception:
            return ""

    ps = ("Get-CimInstance Win32_VideoController | "
          "Select-Object Name,AdapterCompatibility,DriverVersion | "
          "ConvertTo-Json -Compress")
    out = _run_hidden(["powershell", "-NoProfile", "-NonInteractive",
                       "-Command", ps])
    try:
        data = _json.loads(out) if out.strip() else []
        if isinstance(data, dict):
            data = [data]
        for d in data:
            name = (d.get("Name") or "").strip()
            compat = (d.get("AdapterCompatibility") or "").strip()
            s = f"{compat} {name}".lower()
            if "nvidia" in s:
                vendor = "nvidia"
            elif "amd" in s or "advanced micro" in s or "ati " in s:
                vendor = "amd"
            elif "intel" in s:
                vendor = "intel"
            else:
                vendor = "other" if name else ""
            if name and vendor:
                info["gpus"].append({"vendor": vendor, "name": name,
                                     "driver": (d.get("DriverVersion") or "").strip()})
    except Exception as e:
        info["error"] = f"显卡列举失败：{e}"

    info["vendors"] = sorted({g["vendor"] for g in info["gpus"]})
    info["has_discrete"] = "nvidia" in info["vendors"]

    if "nvidia" in info["vendors"]:
        smi = _run_hidden(["nvidia-smi", "--query-gpu=driver_version",
                           "--format=csv,noheader"])
        for line in smi.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parts = line.split(".")
                info["nvidia_driver"] = float(
                    f"{parts[0]}.{parts[1]}" if len(parts) > 1 else parts[0])
            except Exception:
                pass
            break
    return info


def recommend_torch_flavor(hw: dict | None = None) -> dict:
    """依硬件推荐推理引擎组件，返回 UI 可直接渲染的整份清单。

    返回 {"recommended", "reason", "hardware", "options":
          [{"key","label","size_mb","desc","recommended","suitable","note"}…]}
    推荐政策：有 NVIDIA → cu128（驱动低于 CUDA 12.8 要求仍推荐但注明需升级
    驱动）；其余（AMD/Intel/无独显）→ cpu。
    """
    hw = hw or detect_hardware()
    has_nv = "nvidia" in hw.get("vendors", [])
    drv = hw.get("nvidia_driver")
    if has_nv:
        recommended = "cu128"
        reason = tr("env.recommendNv", gpu=(hw["gpus"][0]["name"] if hw["gpus"] else "NVIDIA"))
    else:
        recommended = "cpu"
        reason = tr("env.recommendCpu")

    options = []
    for key, meta in _TORCH_FLAVORS.items():
        note = ""
        suitable = True
        if key == "cu128":
            if not has_nv:
                suitable = False
                note = tr("env.noteNoNv")
            elif drv is not None and drv < _CU128_MIN_DRIVER:
                note = tr("env.noteOldDriver", driver=f"{drv:g}", min="570")
        options.append({
            "key": key,
            "label": tr(meta["label_key"], ver=_TORCH_VERSION),
            "size_mb": meta["size_mb"],
            "desc": tr(meta["desc_key"]),
            "recommended": key == recommended,
            "suitable": suitable,
            "note": note,
        })
    return {"recommended": recommended, "reason": reason,
            "hardware": hw, "options": options}


def _extract_zip_checked(zip_path: Path, dest: Path,
                         progress_cb=None, cancel_check=None):
    """把组件 zip 解压到 dest（带 zip-slip 防护与取消检查）。

    组件 zip 由 build_components.py 打包，顶层只含 torch/、torchvision/ 等
    wheel 顶级目录；这里仍按不可信归档对待：拒绝绝对路径与 .. 上跳成员。
    progress_cb(done_files, total_files)。
    """
    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        total = len(names)
        for i, name in enumerate(names):
            if cancel_check is not None and cancel_check():
                raise CancelledError(name)
            target = (dest / name).resolve()
            if dest.resolve() not in target.parents and target != dest.resolve():
                raise RuntimeError(f"组件 zip 含非法路径成员：{name}")
            zf.extract(name, dest)
            if progress_cb and i % 25 == 0:
                progress_cb(i, total)
        if progress_cb:
            progress_cb(total, total)


def download_torch(flavor: str, progress_cb=None, cancel_check=None,
                   force: bool = False) -> dict:
    """下载并安装推理引擎组件（断点续传下载 → sha256 校验 → 解压 → 安装标记）。

    校验（D5）：解压前对整包做 sha256 比对，期望值优先级 flavor.sha256 →
    远端 <zip>.sha256 → 远端 manifest.json；默认 warn-only，设
    OCRMT_VERIFY_RUNTIME=strict 时不一致会删包并抛 RuntimeError。

    progress_cb(pct: float, msg: str)（0~1，下载段占 0~0.9、解压段 0.9~1）。
    已装同版本同 flavor 时直接返回标记（force=True 强制重装）。
    安装中途取消：zip 留 .part 可续传；已解压文件被下次安装覆盖，无半成品
    标记（标记最后才写）→ 不会出现「标记说有、文件缺失」的坏状态。
    """
    meta = _TORCH_FLAVORS.get(flavor)
    if not meta:
        raise RuntimeError(f"未知的推理引擎组件：{flavor}")
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if not force:
        cur = installed_torch_info()
        if cur and cur.get("flavor") == flavor and cur.get("torch") == _TORCH_VERSION \
                and quick_check_torch(flavor):
            return cur

    url = f"{_TORCH_REL_BASE}/{meta['zip']}"
    final_zip = RUNTIME_DIR / meta["zip"]
    n_parts = meta.get("parts", 1)
    downloaded_parts: list[Path] = []

    if n_parts == 1:
        # 单文件：.part 断点续传，完成后改正式名
        zip_path = RUNTIME_DIR / (meta["zip"] + ".part")
        expected = _remote_size(url)
        if final_zip.exists() and not zip_path.exists():
            # 上次已完整下载但未装完：大小对得上才复用，否则当残档重下
            reuse = True
            if expected is not None:
                try:
                    reuse = final_zip.stat().st_size == expected
                except OSError:
                    reuse = False
            if reuse:
                zip_path = final_zip
            else:
                try:
                    final_zip.unlink(missing_ok=True)
                except OSError:
                    pass
        was_part = zip_path.suffix == ".part"
        got = _download_file(
            url, zip_path,
            progress_cb=lambda done, total: progress_cb(
                0.9 * done / total, tr("env.dlProgress",
                                       done=done // 1_048_576,
                                       total=max(total // 1_048_576, 1)))
            if (progress_cb and total) else None,
            cancel_check=cancel_check, expected_size=expected)
        if expected is not None and got != expected:
            try:
                zip_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuntimeError(
                f"组件包大小不符：{zip_path.name} 期望 {expected} 字节，"
                f"实得 {got} 字节（残档已删除）。")
        if was_part:
            zip_path.rename(final_zip)   # .part 残档 → 正式名（重命名文件本身）
    else:
        # 分卷资产（GitHub 单资产上限 2GB）：逐卷断点续传 → 二进制拼接。
        # 每卷先写 <part>.part 暂存、HEAD 字节数对得上才改名；短分卷因此会
        # 重下而不是被 `if not exists` 永远跳过（D6）。
        part_files = []
        for i in range(1, n_parts + 1):
            pname = f"{meta['zip']}.part{i:02d}-of-{n_parts}"
            pdest = RUNTIME_DIR / pname
            _download_part(f"{_TORCH_REL_BASE}/{pname}", pdest,
                           progress_cb=lambda done, total, _i=i: progress_cb(
                               0.9 * ((_i - 1) + done / total) / n_parts,
                               tr("env.dlPartProgress",
                                  part=_i, total=n_parts,
                                  done=done // 1_048_576,
                                  tmb=max(total // 1_048_576, 1)))
                           if (progress_cb and total) else None,
                           cancel_check=cancel_check)
            part_files.append(pdest)
        downloaded_parts = list(part_files)
        concat = RUNTIME_DIR / (meta["zip"] + ".concat.part")
        if progress_cb:
            progress_cb(0.9, tr("env.concatProgress"))
        with open(concat, "wb") as out:
            done = 0
            for pf in part_files:
                with open(pf, "rb") as src:
                    while True:
                        if cancel_check is not None and cancel_check():
                            raise CancelledError(pf.name)
                        chunk = src.read(1 << 23)      # 8 MB
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if progress_cb:
                            progress_cb(0.9 + 0.05 * done / max(meta["size_mb"] * 1_048_576, 1),
                                        tr("env.concatProgress"))
        concat.rename(final_zip)
        for pf in part_files:
            try:
                pf.unlink(missing_ok=True)             # 拼接完删分卷（共 3GB+ 不留盘）
            except OSError:
                pass
    zip_path = final_zip

    # D5：解压前做整包 sha256 校验（分卷此时已拼接成 final_zip，与发布产物一致）。
    # 默认 warn-only；OCRMT_VERIFY_RUNTIME=strict 才会删包抛错。
    _verify_zip_sha256(flavor, zip_path, meta, downloaded_parts)

    if progress_cb:
        progress_cb(0.9, tr("env.extractProgress"))
    try:
        _extract_zip_checked(zip_path, RUNTIME_DIR,
                             cancel_check=cancel_check,
                             progress_cb=lambda d, t: progress_cb(
                                 0.9 + 0.1 * d / max(t, 1), tr("env.extractProgress"))
                             if progress_cb else None)
    except Exception:
        # 解压/安装失败不留安装包：真机实测「覆盖被占用的 pyd」失败后会白留
        # 3.5 GB 的 zip，且 marker 仍是旧组件，用户看不出发生了什么。
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        zip_path.unlink(missing_ok=True)     # 装完即删安装包（3GB 不留盘）
    except OSError:
        pass

    marker = {"torch": _TORCH_VERSION, "torchvision": _TORCHVISION_VERSION,
              "flavor": flavor, "installed_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    (RUNTIME_DIR / _TORCH_MARKER).write_text(
        json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")
    # 组件是进程中途装的 → 让 importlib 重新扫描 sys.path（否则 find_spec
    # 看不到新目录，transformers 的 is_torch_available 也拿旧结果）
    import importlib
    importlib.invalidate_caches()
    from applog import log_download
    log_download(f"推理引擎组件安装完成：{flavor}（torch {_TORCH_VERSION}）")
    return marker


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

        # 记进清单（精确字节数）：下次启动 quick_check 就能识破被截断的文件，
        # 而不是拿 1 GiB 下限放行后在加载阶段炸。
        try:
            size = dest.stat().st_size
        except OSError:
            size = 0
        if size > 0:
            write_manifest(model_dir, {fname: size})

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

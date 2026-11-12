"""build_components.py — 制作推理引擎组件 zip（cpu / cu128）

安装包本体不含 torch（见 OCRMiniTool.spec 顶部注释）；本脚本从官方 wheel
（uv pip install --target 到临时 stage 目录）打包出两个组件 zip，供发布到
GitHub Release（tag 必须与 downloader._TORCH_TAG 一致，如 runtime-torch2.11.0）：

  ocrmini-runtime-torch<版本>-cpu.zip     ~250 MB
  ocrmini-runtime-torch<版本>-cu128.zip   ~2.8 GB

用法：
  uv run python build_components.py            # 两个都做
  uv run python build_components.py cpu        # 只做 CPU
  uv run python build_components.py cu128      # 只做 CUDA

产物（dist_components/）：
  *.zip              组件包本体
  manifest.json      每个 zip 的 flavor/torch/size/sha256（downloader 的哈希来源③）
  <zip>.sha256       "<hex>  <文件名>"（downloader 的哈希来源②）
  SHA256SUMS         所有 zip + manifest.json 的 sha256 清单

发布 Release（tag 与 downloader._TORCH_TAG 一致）时上传：
  - 各 flavor 的 zip；cu128 超过 GitHub 单资产 2GB 上限，按 1900 MiB 切成
    `<zip>.part01-of-2` / `<zip>.part02-of-2` 两卷上传
  - `<zip>.sha256`、`SHA256SUMS`、`manifest.json`（三者都是可选的校验来源，
    但建议全传：应用会在解压前按优先级挑一个核验整包 sha256）
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# 与 downloader._TORCH_VERSION / _TORCHVISION_VERSION 保持一致（改动时两处同步）。
# torch 必须 <2.9：2.10+ 移除 _model_output_flatten 等内部符号，
# transformers==4.52.1 的 generation.utils 仍在引用（NameError 实录）。
TORCH_VER = "2.8.0"
TV_VER = "0.23.0"
CU128_INDEX = "https://download.pytorch.org/whl/cu128"

OUT_DIR = Path(__file__).resolve().parent / "dist_components"

# 组件 zip 收录 stage 顶层**全部**内容（torch/torchvision/torchgen/functorch
# + 纯 Python 依赖 sympy/networkx/jinja2/filelock/fsspec/mpmath/… +
# *.dist-info 元数据）。dist-info **必须带上**：transformers 用
# importlib.metadata 探测 torch（_is_package_available 走包元数据而非
# import spec），没有 dist-info → 装了 torch 也判「不可用」（实测翻车：
# "None of PyTorch … have been found"→ GenerationMixin 全体占位化）。
# 安装包本体已排除 torch 及其依赖，PyInstaller 不会收集它们，缺一个
# import torch 就会 ImportError（另一个实测翻车：缺 sympy）。
# 仅排除 __pycache__ 与 bin（Linux 平台脚本）。


def build(flavor: str) -> Path:
    index = [ "--index-url", CU128_INDEX] if flavor == "cu128" else []
    pin = (f"torch=={TORCH_VER}" if flavor == "cpu"
           else f"torch=={TORCH_VER}+cu128")
    with tempfile.TemporaryDirectory(prefix=f"ocrmt_{flavor}_") as tmp:
        stage = Path(tmp) / "stage"
        cmd = ["uv", "pip", "install", "--python", sys.executable,
               "--target", str(stage), pin, f"torchvision=={TV_VER}"] + index
        print(f"[{flavor}] 安装 wheel → {stage}")
        subprocess.run(cmd, check=True)
        tops = sorted(
            p.name for p in stage.iterdir()
            if p.name != "__pycache__" and p.name != "bin")
        print(f"[{flavor}] 顶层条目：{tops}")
        if "torch" not in tops or "torchvision" not in tops:
            raise SystemExit(f"[{flavor}] wheel 内容不全：{tops}")

        OUT_DIR.mkdir(exist_ok=True)
        out = OUT_DIR / f"ocrmini-runtime-torch{TORCH_VER}-{flavor}.zip"
        print(f"[{flavor}] 打包 → {out}")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            n = 0
            for top in tops:
                for p in sorted((stage / top).rglob("*")):
                    if p.is_file() and "__pycache__" not in p.parts:
                        zf.write(p, p.relative_to(stage).as_posix())
                        n += 1
                        if n % 500 == 0:
                            print(f"[{flavor}] …{n} files")
        return out


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_sidecars(out_dir: Path, manifest: dict) -> list[Path]:
    """为每个已有 zip 写 `<zip>.sha256`，并写 SHA256SUMS（含 manifest.json）。

    manifest 结构保持不变（顶层按 zip 名索引），只额外产出旁路文件；
    downloader 的期望哈希来源②/③ 直接吃这两个产物。
    """
    out_dir = Path(out_dir)
    written: list[Path] = []
    for name in sorted(manifest):
        zpath = out_dir / name
        sha = (manifest[name] or {}).get("sha256") or ""
        if zpath.is_file() and sha:
            p = out_dir / (name + ".sha256")
            p.write_text(f"{sha}  {name}\n", encoding="utf-8")
            written.append(p)
    lines = [f"{manifest[name]['sha256']}  {name}"
             for name in sorted(manifest)
             if (out_dir / name).is_file() and manifest[name].get("sha256")]
    man = out_dir / "manifest.json"
    if man.is_file():
        lines.append(f"{sha256_of(man)}  manifest.json")
    sums = out_dir / "SHA256SUMS"
    sums.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    written.append(sums)
    return written


def main():
    wants = sys.argv[1:] or ["cpu", "cu128"]
    bad = [w for w in wants if w not in ("cpu", "cu128")]
    if bad:
        raise SystemExit(f"未知 flavor：{bad}（可选 cpu / cu128）")
    manifest = {}
    old = OUT_DIR / "manifest.json"
    if old.exists():
        manifest = json.loads(old.read_text(encoding="utf-8"))
    for flavor in wants:
        out = build(flavor)
        size = out.stat().st_size
        manifest[out.name] = {"flavor": flavor, "torch": TORCH_VER,
                              "torchvision": TV_VER, "size": size,
                              "size_mb": size // 1_048_576,
                              "sha256": sha256_of(out)}
        print(f"[{flavor}] {size / 1_048_576:.0f} MB  sha256={manifest[out.name]['sha256'][:16]}…")
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    sidecars = write_sidecars(OUT_DIR, manifest)
    print(f"manifest → {OUT_DIR / 'manifest.json'}")
    for p in sidecars:
        print(f"校验清单 → {p}")
    print(f"发布：GitHub Release tag 为 downloader._TORCH_TAG（"
          f"runtime-torch{TORCH_VER}），上传以下资产：")
    print("  - *.zip（cu128 按 1900 MiB 切成 .part01-of-2 / .part02-of-2 两卷）")
    print("  - <zip>.sha256、SHA256SUMS、manifest.json（校验来源②/③）")


def _print_help() -> None:
    print(__doc__.strip())
    print("\n当前参数：")
    print("  （无参数）   同时构建 cpu 与 cu128")
    print("  cpu          只构建 CPU 组件")
    print("  cu128        只构建 CUDA 12.8 组件")
    print("  -h, --help   显示本帮助")


if __name__ == "__main__":
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        _print_help()
        raise SystemExit(0)
    main()

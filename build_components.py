# -*- coding: utf-8 -*-
"""build_components.py — 制作推理引擎组件 zip（cpu / cu128）

安装包本体不含 torch（见 OCRMiniTool.spec 顶部注释）；本脚本从官方 wheel
（uv pip install --target 到临时 stage 目录）打包出组件 zip，供发布到
GitHub Release（tag 必须与 downloader._TORCH_TAG 一致）：

  ocrmini-runtime-torch<版本>-cpu.zip     ~250 MB
  ocrmini-runtime-torch<版本>-cu128.zip   ~3.3 GB

用法：
  uv run python build_components.py                 # 两个都做
  uv run python build_components.py cpu             # 只做 CPU
  uv run python build_components.py cu128           # 只做 CUDA
  uv run python build_components.py cu128 --split 1900
  uv run python build_components.py --no-deps --pins "sympy==1.14.0,networkx==3.7"

分卷（D7）：
  --split MIB 按字节把 zip 切成 `<zip>.part01-of-<N>`（downloader 期望的命名），
  默认 cu128=1900 MiB（GitHub 单资产 2GB 上限）、cpu=0（不切）。**切完保留原 zip**
  （sidecar/整包 sha256 仍基于原 zip；发布只上传 parts）。

版本单一来源：
  TORCH_VER / TV_VER 必须与 downloader._TORCH_VERSION / _TORCHVISION_VERSION 一致，
  main 启动先校验（version_mismatch），不一致直接拒绝构建。

产物（dist_components/）：
  *.zip              组件包本体
  *.part??-of-N      分卷（默认只 cu128）
  manifest.json      每个 zip 的 flavor/torch/size/sha256 + tops + parts[...]
  <zip>.sha256       "<hex>  <文件名>"（整包哈希；downloader 哈希来源②）
  SHA256SUMS         所有 zip + 各分卷 + manifest.json 的 sha256 清单

发布 Release（tag 与 downloader._TORCH_TAG 一致）时上传：
  - 各 flavor 的 zip；cu128 若分卷则上传 `<zip>.part01-of-N … partNN-of-N`
  - `<zip>.sha256`、`SHA256SUMS`、`manifest.json`（校验来源②/③，建议全传）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# 与 downloader._TORCH_VERSION / _TORCHVISION_VERSION 必须一致（main 会校验）。
# torch 必须 <2.9：2.10+ 移除 _model_output_flatten 等内部符号，
# transformers==4.52.1 的 generation.utils 仍在引用（NameError 实录）。
TORCH_VER = "2.8.0"
TV_VER = "0.23.0"
CU128_INDEX = "https://download.pytorch.org/whl/cu128"

OUT_DIR = Path(__file__).resolve().parent / "dist_components"

FLAVORS = ("cpu", "cu128")
# 分卷默认值（MiB）：cu128 超过 GitHub 单资产 2GB → 1900；cpu 不切。
DEFAULT_SPLIT_MIB = {"cpu": 0, "cu128": 1900}
# downloader.download_torch 里的分卷命名：`{zip}.part{i:02d}-of-{n_parts}`
# 注意 N **不补零**（下载端是 f"-of-{n_parts}"），否则拼不出下载 URL。
PART_NAME_FMT = "{zip}.part{i:02d}-of-{n}"


# ── 版本单一来源 ────────────────────────────────────────────────────
def downloader_versions() -> tuple[str, str]:
    """读 downloader.py 的组件版本常量（唯一事实来源）。"""
    import downloader          # 延迟导入：脚本被单测导入时不强依赖仓库其它模块
    return (str(downloader._TORCH_VERSION),
            str(downloader._TORCHVISION_VERSION))


def version_mismatch(torch_ver: str | None = None,
                     tv_ver: str | None = None) -> str | None:
    """build_components 与 downloader 的版本是否不一致；一致返回 None。"""
    if torch_ver is None or tv_ver is None:
        d_torch, d_tv = downloader_versions()
        torch_ver = torch_ver if torch_ver is not None else d_torch
        tv_ver = tv_ver if tv_ver is not None else d_tv
    problems: list[str] = []
    if torch_ver != TORCH_VER:
        problems.append(f"torch: build_components={TORCH_VER} ≠ downloader={torch_ver}")
    if tv_ver != TV_VER:
        problems.append(f"torchvision: build_components={TV_VER} ≠ downloader={tv_ver}")
    return "；".join(problems) if problems else None


# ── 哈希 / 分卷 ─────────────────────────────────────────────────────
def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def split_file(path: Path, part_mib: int) -> list[Path]:
    """按 part_mib（MiB）把 path 切成 downloader 期望的分卷；返回 parts。

    - `part_mib <= 0` → 不切，返回 []；
    - 命名 `{zip}.part{i:02d}-of-{n}`（N 不补零，与 downloader 拼接的 URL 一致）；
    - **保留原 zip**（整包 sha256/发布说明仍以它为准）。
    """
    path = Path(path)
    try:
        size_mib = int(part_mib)
    except (TypeError, ValueError):
        return []
    if size_mib <= 0 or not path.is_file():
        return []
    size = path.stat().st_size
    part_bytes = size_mib * 1024 * 1024
    n = max(1, math.ceil(size / part_bytes))
    if n > 99:
        raise SystemExit(f"分卷数过多（{n} > 99）：请调大 --split MIB")
    parts: list[Path] = []
    with open(path, "rb") as src:
        for i in range(1, n + 1):
            name = PART_NAME_FMT.format(zip=path.name, i=i, n=n)
            dst = path.with_name(name)
            remaining = min(part_bytes, size - (i - 1) * part_bytes)
            with open(dst, "wb") as out:
                while remaining > 0:
                    chunk = src.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    remaining -= len(chunk)
            parts.append(dst)
    print(f"[split] {path.name} → {n} 卷（{size_mib} MiB/卷）")
    return parts


# ── manifest ───────────────────────────────────────────────────────
def manifest_entry(flavor: str, zip_path: Path, tops: list[str],
                   parts: list[Path] | None = None) -> dict:
    """单个 zip 的 manifest 条目（含 D7 新增的 parts/tops）。"""
    zip_path = Path(zip_path)
    size = zip_path.stat().st_size
    return {
        "flavor": flavor,
        "torch": TORCH_VER,
        "torchvision": TV_VER,
        "size": size,
        "size_mb": size // 1_048_576,
        "sha256": sha256_of(zip_path),
        "tops": list(tops or []),
        "parts": [{"name": p.name, "size": p.stat().st_size,
                   "sha256": sha256_of(p)} for p in (parts or [])],
    }


def write_sidecars(out_dir: Path, manifest: dict) -> list[Path]:
    """写 `<zip>.sha256` 与 SHA256SUMS（含 zip、parts、manifest.json 行）。

    manifest 顶层仍按 zip 名索引（向后兼容）；缺 tops/parts 的旧条目也能处理。
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
    lines: list[str] = []
    for name in sorted(manifest):
        entry = manifest[name] or {}
        if (out_dir / name).is_file() and entry.get("sha256"):
            lines.append(f"{entry['sha256']}  {name}")
        for part in entry.get("parts") or []:
            pname, psha = part.get("name"), part.get("sha256")
            if pname and psha and (out_dir / pname).is_file():
                lines.append(f"{psha}  {pname}")
    man = out_dir / "manifest.json"
    if man.is_file():
        lines.append(f"{sha256_of(man)}  manifest.json")
    sums = out_dir / "SHA256SUMS"
    sums.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    written.append(sums)
    return written


# ── 构建 ────────────────────────────────────────────────────────────
def build_flavor(flavor: str, *, split_mib: int | None = None,
                 no_deps: bool = False, pins: list[str] | None = None,
                 runner=None) -> dict:
    """构建一个 flavor 的组件 zip（可选分卷）。

    返回 {"zip": Path, "tops": [顶层目录…], "parts": [Path…]}。
    runner(cmd, check=True) 可注入（默认 subprocess.run），单测用假 stage。
    """
    if flavor not in FLAVORS:
        raise SystemExit(f"未知 flavor：{flavor}（可选 {' / '.join(FLAVORS)}）")
    run = runner or subprocess.run
    pins = list(pins or [])
    if split_mib is None:
        split_mib = DEFAULT_SPLIT_MIB.get(flavor, 0)

    index = ["--index-url", CU128_INDEX] if flavor == "cu128" else []
    pin = (f"torch=={TORCH_VER}" if flavor == "cpu"
           else f"torch=={TORCH_VER}+cu128")
    with tempfile.TemporaryDirectory(prefix=f"ocrmt_{flavor}_") as tmp:
        stage = Path(tmp) / "stage"
        cmd = ["uv", "pip", "install", "--python", sys.executable,
               "--target", str(stage)]
        if no_deps:
            cmd.append("--no-deps")
        cmd += [pin, f"torchvision=={TV_VER}"] + index + pins
        print(f"[{flavor}] 安装 wheel → {stage}")
        run(cmd, check=True)
        tops = sorted(
            p.name for p in stage.iterdir()
            if p.name not in ("__pycache__", "bin"))
        print(f"[{flavor}] 顶层条目：{tops}")
        if "torch" not in tops or "torchvision" not in tops:
            raise SystemExit(f"[{flavor}] wheel 内容不全：{tops}")

        OUT_DIR.mkdir(parents=True, exist_ok=True)
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
        parts = split_file(out, split_mib)
        return {"zip": out, "tops": tops, "parts": parts}


def build(flavor: str) -> Path:
    """向后兼容的旧入口：返回 zip 路径（分卷用 build_flavor）。"""
    return build_flavor(flavor)["zip"]


# ── CLI ─────────────────────────────────────────────────────────────
def _parse_pins(text: str | None) -> list[str]:
    if not text:
        return []
    return [tok.strip() for tok in str(text).split(",") if tok.strip()]


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="构建推理引擎组件 zip（cpu / cu128）",
        epilog="发布：GitHub Release tag 必须与 downloader._TORCH_TAG 一致。")
    ap.add_argument("flavors", nargs="*", default=None,
                    help="要构建的 flavor（默认 cpu cu128）")
    ap.add_argument("--split", type=int, default=None, metavar="MIB",
                    help="分卷大小 MiB（默认 cu128=1900、cpu=0 不切；0=不切）")
    ap.add_argument("--no-deps", action="store_true",
                    help="给 uv 传 --no-deps（必须同时给 --pins，否则退出 2）")
    ap.add_argument("--pins", default=None,
                    help='显式依赖版本，逗号分隔，如 "sympy==1.14.0,networkx==3.7"')
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_argparser().parse_args(argv)

    mismatch = version_mismatch()
    if mismatch:
        raise SystemExit(f"版本不一致：{mismatch}（先同步 downloader.py 与 "
                         "build_components.py 的 _TORCH_VERSION/_TORCHVISION_VERSION）")

    flavors = list(args.flavors or FLAVORS)
    bad = [f for f in flavors if f not in FLAVORS]
    if bad:
        print(f"[FAIL] 未知 flavor：{bad}（可选 {' / '.join(FLAVORS)}）",
              file=sys.stderr)
        return 2

    pins = _parse_pins(args.pins)
    if args.no_deps and not pins:
        print("[FAIL] --no-deps 需要同时给出 --pins "
              '"pkg==ver,pkg2==ver2"（缺 pins 会装出缺依赖的组件包）',
              file=sys.stderr)
        return 2
    # 显式给 pins 时自动走 --no-deps（pins 就是完整依赖集）；默认仍由 uv 解析
    no_deps = bool(args.no_deps or pins)

    manifest: dict = {}
    old = OUT_DIR / "manifest.json"
    if old.exists():
        try:
            manifest = json.loads(old.read_text(encoding="utf-8"))
        except Exception as e:                       # 坏 manifest 不阻断重建
            print(f"[WARN] 旧 manifest 解析失败，重新生成：{e}", file=sys.stderr)
            manifest = {}

    for flavor in flavors:
        split_mib = (args.split if args.split is not None
                     else DEFAULT_SPLIT_MIB.get(flavor, 0))
        print(f"[{flavor}] split={split_mib} MiB  no_deps={no_deps}  "
              f"pins={pins or '（uv 解析）'}")
        res = build_flavor(flavor, split_mib=split_mib, no_deps=no_deps,
                           pins=pins)
        entry = manifest_entry(flavor, res["zip"], res["tops"], res["parts"])
        manifest[res["zip"].name] = entry
        print(f"[{flavor}] {entry['size'] / 1_048_576:.0f} MB  "
              f"sha256={entry['sha256'][:16]}…  "
              f"parts={[p['name'] for p in entry['parts']]}")

    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    sidecars = write_sidecars(OUT_DIR, manifest)
    print(f"manifest → {OUT_DIR / 'manifest.json'}")
    for p in sidecars:
        print(f"校验清单 → {p}")
    print(f"发布：GitHub Release tag 为 downloader._TORCH_TAG（"
          f"runtime-torch{TORCH_VER}），上传以下资产：")
    print("  - 各 flavor 的 zip；分卷 flavor 上传 <zip>.part01-of-N … partNN-of-N")
    print("  - <zip>.sha256、SHA256SUMS、manifest.json（校验来源②/③）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

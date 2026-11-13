# -*- coding: utf-8 -*-
"""tools/release_check.py — 发布前检查（tag↔版本↔工作区↔tag↔远端↔发行 zip）

用途：在 `tools/build_exe.py --zip` 之后、上传 GitHub Release 之前跑一遍，
把「版本漂移 / 脏工作区 / tag 不在 HEAD / 远端没有 tag / zip 名不对」这类
发布事故挡在最后一步。

用法
----
    python tools/release_check.py                 # 默认 tag=v<APP_VERSION>，zip=dist/OCRMiniTool-v<ver>-windows-x64.zip
    python tools/release_check.py --tag v1.6.0
    python tools/release_check.py --zip dist/xxx.zip
    python tools/release_check.py --offline       # 跳过远端 tag 查询（离线/无 origin）
    python tools/release_check.py --write-sums    # 写 SHA256SUMS.txt（<hex>  <basename>）

检查项（逐项 ✓/✗）：
  a. version.py 的 APP_VERSION 与 tag（去 v）一致，且 tag 形如 vX.Y.Z
  b. 工作区干净（git status --porcelain 为空）
  c. tag 存在且指向 HEAD
  d. 远端 origin 上有该 tag（--offline 跳过）
  e. zip 存在时：文件名含版本号、算 sha256 并在末尾打印

退出码：0 全过；1 有失败项；2 参数/环境错误（非 git 仓库、tag 格式非法、
读不到 version.py、--write-sums 但 zip 不存在）。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = "version.py"
ZIP_TEMPLATE = "OCRMiniTool-v{ver}-windows-x64.zip"
SUMS_NAME = "SHA256SUMS.txt"

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_APP_VERSION_RE = re.compile(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']')


class ReleaseError(RuntimeError):
    """发布检查可预期的环境/参数错误（对应退出码 2）。"""


# ── 纯函数（便于单测）────────────────────────────────────────────────
def parse_version(text: str) -> str | None:
    """从 version.py 文本里取 APP_VERSION。"""
    m = _APP_VERSION_RE.search(text or "")
    return m.group(1).strip() if m else None


def read_version(root: Path = ROOT) -> str:
    path = Path(root) / VERSION_FILE
    if not path.is_file():
        raise ReleaseError(f"找不到版本文件：{path}")
    version = parse_version(path.read_text(encoding="utf-8", errors="replace"))
    if not version:
        raise ReleaseError(f"{VERSION_FILE} 里没有 APP_VERSION")
    return version


def derive_tag(version: str) -> str:
    return f"v{version}"


def is_valid_tag(tag: str) -> bool:
    return bool(_TAG_RE.match(tag or ""))


def version_from_tag(tag: str) -> str | None:
    m = _TAG_RE.match(tag or "")
    return f"{m.group(1)}.{m.group(2)}.{m.group(3)}" if m else None


def zip_name(version: str) -> str:
    return ZIP_TEMPLATE.format(ver=version)


def zip_name_matches(zip_path: Path, version: str) -> bool:
    """发行 zip 文件名是否含版本号且是 .zip。"""
    name = Path(zip_path).name
    return name.endswith(".zip") and f"-v{version}-" in f"-{name}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Check:
    """一条检查结果。"""

    def __init__(self, key: str, ok: bool, detail: str = ""):
        self.key = key
        self.ok = bool(ok)
        self.detail = detail

    def line(self) -> str:
        mark = "✓" if self.ok else "✗"
        return f"  {mark} {self.key}：{self.detail}" if self.detail else f"  {mark} {self.key}"

    def __repr__(self) -> str:      # 便于测试失败信息
        return f"Check({self.key!r}, ok={self.ok}, detail={self.detail!r})"


# ── git（统一入口，测试里 mock 这一个）──────────────────────────────
def _git(args: list[str], cwd: Path = ROOT) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=30)
        return r.returncode, (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def is_git_repo(root: Path = ROOT) -> bool:
    rc, out = _git(["rev-parse", "--is-inside-work-tree"], root)
    return rc == 0 and out == "true"


def head_commit(root: Path = ROOT) -> str | None:
    rc, out = _git(["rev-parse", "HEAD"], root)
    return out if rc == 0 and out else None


def git_dirty(root: Path = ROOT) -> list[str] | None:
    """未提交改动列表；None = 读不到（非 git 仓库/git 不可用）。"""
    rc, out = _git(["status", "--porcelain", "-uall"], root)
    if rc != 0:
        return None
    return [ln for ln in out.splitlines() if ln.strip()]


def tag_commit(root: Path, tag: str) -> str | None:
    rc, out = _git(["rev-parse", f"{tag}^{{commit}}"], root)
    return out if rc == 0 and out else None


def remote_has_tag(root: Path, tag: str) -> bool | None:
    """origin 上是否有该 tag；None = 查询失败（无 origin/网络问题）。"""
    rc, out = _git(["ls-remote", "--tags", "origin", f"refs/tags/{tag}"], root)
    if rc != 0:
        return None
    return bool(out)


# ── 检查集合 ────────────────────────────────────────────────────────
def collect_checks(root: Path = ROOT, *, version: str, tag: str,
                   zip_path: Path, offline: bool = False
                   ) -> tuple[list[Check], str | None]:
    """跑全部检查，返回 (checks, zip_sha256|None)。"""
    checks: list[Check] = []

    # a. 版本 ↔ tag
    tag_ver = version_from_tag(tag)
    checks.append(Check(
        "版本 ↔ tag", tag_ver == version,
        f"APP_VERSION={version}，tag={tag}（v{version} 期望）"))

    # b. 工作区干净
    dirty = git_dirty(root)
    if dirty is None:
        checks.append(Check("工作区干净", False, "无法读取 git status（非 git 仓库？）"))
    elif dirty:
        head = "、".join(x[:50] for x in dirty[:5])
        checks.append(Check("工作区干净", False,
                            f"{len(dirty)} 项未提交（{head}{'…' if len(dirty) > 5 else ''}）"))
    else:
        checks.append(Check("工作区干净", True, "干净"))

    # c. tag 存在且指向 HEAD
    tcommit = tag_commit(root, tag)
    checks.append(Check("tag 存在", tcommit is not None,
                        f"{tag} → {tcommit[:12]}" if tcommit else f"{tag} 不存在"))
    head = head_commit(root)
    at_head = bool(tcommit and head and tcommit == head)
    checks.append(Check(
        "tag 指向 HEAD", at_head,
        f"tag={tcommit[:12] if tcommit else '—'}，HEAD={head[:12] if head else '—'}"))

    # d. 远端 tag
    if offline:
        checks.append(Check("远端 tag", True, "已跳过（--offline）"))
    else:
        found = remote_has_tag(root, tag)
        if found is None:
            checks.append(Check("远端 tag", False,
                                "无法查询 origin（无 origin/网络失败）；确认后可用 --offline 跳过"))
        else:
            checks.append(Check("远端 tag", found,
                                f"origin {'有' if found else '没有'} {tag}"))

    # e. 发行 zip
    sha: str | None = None
    zp = Path(zip_path)
    if not zp.exists():
        checks.append(Check("发行 zip", True, f"未找到 {zp.name}（跳过）"))
    else:
        checks.append(Check("zip 文件名含版本", zip_name_matches(zp, version), zp.name))
        try:
            sha = sha256_file(zp)
            checks.append(Check("zip 可读", True, f"{zp.stat().st_size} 字节"))
        except OSError as e:
            checks.append(Check("zip 可读", False, f"读取失败：{e}"))
    return checks, sha


def write_sums(zip_path: Path, sha: str, sums_path: Path | None = None) -> Path:
    """写 SHA256SUMS.txt：`<hex>  <basename>`（LF）。"""
    zp = Path(zip_path)
    out = Path(sums_path) if sums_path else zp.with_name(SUMS_NAME)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"{sha}  {zp.name}\n", encoding="utf-8", newline="\n")
    return out


# ── CLI ─────────────────────────────────────────────────────────────
def _say(msg: str = ""):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="发布前检查（版本/tag/工作区/远端/发行 zip）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("检查项")[-1].strip())
    ap.add_argument("--tag", default=None,
                    help="要发布的 tag（默认 v<APP_VERSION>）")
    ap.add_argument("--zip", default=None,
                    help="发行 zip 路径（默认 dist/OCRMiniTool-v<ver>-windows-x64.zip）")
    ap.add_argument("--offline", action="store_true",
                    help="跳过远端 tag 查询（离线/无 origin）")
    ap.add_argument("--write-sums", action="store_true",
                    help="写 SHA256SUMS.txt（与 zip 同目录）")
    return ap


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    _configure_stdio()
    opts = build_argparser().parse_args(argv)
    root = Path(root).resolve()

    try:
        version = read_version(root)
    except ReleaseError as e:
        _say(f"[FAIL] {e}")
        return EXIT_USAGE

    tag = (opts.tag or derive_tag(version)).strip()
    if not is_valid_tag(tag):
        _say(f"[FAIL] tag 格式非法：{tag!r}（应形如 v{version}）")
        return EXIT_USAGE
    if not is_git_repo(root):
        _say(f"[FAIL] 不是 git 工作区：{root}（发布检查需要 git）")
        return EXIT_USAGE

    zip_path = (Path(opts.zip) if opts.zip
                else root / "dist" / zip_name(version))
    if opts.write_sums and not zip_path.exists():
        _say(f"[FAIL] --write-sums 需要已存在的发行 zip，但没找到：{zip_path}")
        return EXIT_USAGE

    _say(f"发布前检查 — {root}")
    _say(f"  版本 {version} / tag {tag} / zip {zip_path}")
    checks, sha = collect_checks(root, version=version, tag=tag,
                                 zip_path=zip_path, offline=opts.offline)
    for c in checks:
        _say(c.line())

    if sha:
        _say(f"\n  SHA256：{sha}  {zip_path.name}")
    if opts.write_sums and sha:
        sums = write_sums(zip_path, sha)
        _say(f"  SHA256SUMS：{sums}")

    failed = [c for c in checks if not c.ok]
    _say(f"\n结果：{'全部通过' if not failed else f'{len(failed)} 项失败'}"
         f"（共 {len(checks)} 项）")
    return EXIT_FAIL if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

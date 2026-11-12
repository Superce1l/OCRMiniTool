# -*- coding: utf-8 -*-
"""tools/normalize_eol.py — 统一仓库行尾（一次性维护脚本，可反复执行）

背景：工作区源码曾被编辑器整批改成 CRLF，而 git index 里是 LF，导致
`git status` 恒显示 20 个文件"全量修改"，真实改动完全淹没在噪声里。

约定（与 .gitattributes 一致）：
  • 一律 LF：.py .js .css .html .json .md .txt .spec .toml .yml .cfg，
    以及 .gitignore / LICENSE 等无后缀文本文件
  • 保留 CRLF：.bat / .cmd（cmd.exe 对 LF 批处理的 goto/label 有历史坑）

用法：
    python tools/normalize_eol.py --dry-run   # 只看会改哪些文件
    python tools/normalize_eol.py            # 就地统一为 LF
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

LF_SUFFIXES = {".py", ".js", ".css", ".html", ".json", ".md", ".txt",
               ".spec", ".toml", ".yml", ".yaml", ".cfg", ".ini", ".ps1"}
LF_NAMES = {".gitignore", ".gitattributes", "LICENSE", "PYINSTALLER"}
KEEP_CRLF = {".bat", ".cmd"}
SKIP_DIRS = {".venv", "venv", "build", "dist", "node_modules", "__pycache__",
             "models", "outputs", "logs", "_hf_cache", "runtime", "_work",
             ".git"}


def candidates():
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if p.suffix.lower() in KEEP_CRLF:
            continue
        if p.suffix.lower() in LF_SUFFIXES or p.name in LF_NAMES:
            yield p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="统一仓库行尾为 LF")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写盘")
    args = ap.parse_args(argv)

    changed, skipped, total = [], [], 0
    for p in candidates():
        total += 1
        try:
            raw = p.read_bytes()
        except OSError:
            skipped.append(p)
            continue
        if b"\0" in raw[:4096]:          # 二进制伪装成文本后缀，跳过
            skipped.append(p)
            continue
        if b"\r\n" not in raw:
            continue
        new = raw.replace(b"\r\n", b"\n")
        changed.append((p.relative_to(ROOT).as_posix(),
                        raw.count(b"\r\n"), len(raw), len(new)))
        if not args.dry_run:
            p.write_bytes(new)

    print(f"扫描文本文件 {total} 个，需要统一行尾 {len(changed)} 个"
          + ("（dry-run，未写盘）" if args.dry_run else ""))
    for name, n, a, b in changed:
        print(f"  {name}: CRLF {n} 处，{a} → {b} 字节")
    if skipped:
        print(f"跳过 {len(skipped)} 个（读取失败或疑似二进制）："
              + ", ".join(x.name for x in skipped[:5]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

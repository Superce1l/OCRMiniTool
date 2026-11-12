# -*- coding: utf-8 -*-
"""tools/check.py — OCRMiniTool 静态体检（零第三方依赖，CI 可直接跑）

用途：改代码后一条命令确认「没把项目改坏」。只做静态检查，不启动界面、
不加载模型、不联网，因此可在任何机器上跑（不需要 torch/transformers）。

检查项：
  1. Python 语法：全部 *.py 编译通过（不落 __pycache__）
  2. JS 语法：webview/ 下 *.js 通过 node --check（无 node 时降级为跳过并告警）
  3. JSON：webview/locales/*.json 与已知配置文件可解析
  4. 语言包对齐：所有 locales 的扁平键集合必须与 zh.json 完全一致
  5. i18n 键引用：后端 tr("k") / 前端 t("k")、data-i18n="k" 引用的键必须存在
  6. 版本口径：version.py 的 APP_VERSION 与 spec / README 中出现的版本号是否一致
  7. 行尾一致性：源码不应混用 CRLF/LF（仓库以 LF 为准，*.bat 允许 CRLF）

退出码：0 = 无错误；1 = 有错误（告警不影响退出码，除非 --strict）

用法：
    python tools/check.py            # 全量体检
    python tools/check.py --strict   # 告警也算失败
    python tools/check.py --quiet    # 只打印失败项
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import warnings as _warnings
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", "venv", "build", "dist", "node_modules", "__pycache__",
             "models", "outputs", "logs", "_hf_cache", "runtime", "_work"}
BAT_OK_CRLF = {".bat", ".cmd"}

errors: list[str] = []
warnings: list[str] = []


def err(msg: str):
    errors.append(msg)


def warn(msg: str):
    warnings.append(msg)


def iter_files(suffixes: tuple[str, ...]):
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        if p.suffix.lower() in suffixes:
            yield p


def rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


# ── 1. Python 语法 ──────────────────────────────────────────────
def check_python():
    n = 0
    for p in iter_files((".py",)):
        n += 1
        try:
            src = p.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            err(f"[py] {rel(p)} 不是 UTF-8：{e}")
            continue
        try:
            # SyntaxWarning 也当错误：`\P` 这类无效转义在 3.12 只是警告，
            # 后续 Python 版本会升级成 SyntaxError（实测 motw_unblock.py 中招）
            with _warnings.catch_warnings(record=True) as caught:
                _warnings.simplefilter("always")
                compile(src, str(p), "exec")
            for w in caught:
                if issubclass(w.category, SyntaxWarning):
                    loc = f":{w.lineno}" if getattr(w, "lineno", None) else ""
                    err(f"[py] {rel(p)}{loc} 潜在缺陷（{w.category.__name__}）："
                        f"{w.message}")
        except SyntaxError as e:
            err(f"[py] {rel(p)}:{e.lineno} 语法错误：{e.msg}")
    if n == 0:
        warn("[py] 未找到任何 .py 文件")
    return n


# ── 2. JS 语法 ──────────────────────────────────────────────────
def check_js():
    files = sorted(iter_files((".js",)))
    node = shutil.which("node")
    if not node:
        for cand in (Path("C:/Users/admin/.dsh/dsh-runtimes/dsh-primary-runtime/"
                          "dependencies/node/bin/node.exe"),):
            if cand.is_file():
                node = str(cand)
                break
    if not node:
        warn(f"[js] 未找到 node，跳过 {len(files)} 个 JS 文件的语法检查")
        return 0
    for p in files:
        r = subprocess.run([node, "--check", str(p)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip().splitlines()
            err(f"[js] {rel(p)} 语法错误：{tail[0] if tail else '未知'}")
    return len(files)


# ── 3. JSON 可解析 ──────────────────────────────────────────────
def load_locales() -> dict[str, dict]:
    out: dict[str, dict] = {}
    ldir = ROOT / "webview" / "locales"
    if not ldir.is_dir():
        err("[json] 缺少 webview/locales/ 目录")
        return out
    for p in sorted(ldir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            err(f"[json] {rel(p)} 解析失败：{e}")
            continue
        if not isinstance(data, dict):
            err(f"[json] {rel(p)} 顶层必须是对象")
            continue
        out[p.stem] = data
    return out


def flatten(d: dict, prefix: str = "") -> dict[str, object]:
    out = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out


def check_json_locales(locales: dict[str, dict]) -> dict[str, dict]:
    for extra in ("settings.json", "prompt_template.json"):
        p = ROOT / extra
        if p.is_file():
            try:
                json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                err(f"[json] {extra} 解析失败：{e}")
    return {k: flatten(v) for k, v in locales.items()}


# ── 4. 语言包键对齐 ─────────────────────────────────────────────
def check_locale_parity(flat: dict[str, dict]):
    if "zh" not in flat:
        err("[i18n] 缺少基准语言包 zh.json")
        return
    base = set(flat["zh"])
    if "meta.nativeName" not in flat["zh"]:
        err("[i18n] zh.json 缺少 meta.nativeName（设置页语言下拉依赖它）")
    for code, keys in flat.items():
        if code == "zh":
            continue
        missing = sorted(base - set(keys))
        extra = sorted(set(keys) - base)
        if missing:
            err(f"[i18n] {code}.json 缺少 {len(missing)} 个键，例如：{', '.join(missing[:5])}")
        if extra:
            warn(f"[i18n] {code}.json 多出 {len(extra)} 个键，例如：{', '.join(extra[:5])}")
        if "meta.nativeName" not in keys:
            err(f"[i18n] {code}.json 缺少 meta.nativeName")


# ── 5. i18n 键引用 ──────────────────────────────────────────────
TR_CALL_RE = re.compile(r"""\btr\(\s*["']([A-Za-z0-9_.\-]+)["']""")
T_CALL_RE = re.compile(r"""\b(?:t|i18n\.t)\(\s*["']([A-Za-z0-9_.\-]+)["']""")
DATA_I18N_RE = re.compile(r"""data-i18n(?:-[\w-]+)?\s*=\s*["']([A-Za-z0-9_.\-]+)["']""")


def check_i18n_usage(flat: dict[str, dict]):
    known = set(flat.get("zh", {}))
    if not known:
        return
    missing: dict[str, list[str]] = {}

    def add(key: str, where: str):
        if key and key not in known:
            missing.setdefault(where, []).append(key)

    # tests/ 与 tools/ 自身会故意引用不存在的键（测回退行为），跳过
    def owned(p: Path) -> bool:
        parts = p.relative_to(ROOT).parts
        return bool(parts) and parts[0] not in {"tests", "tools"}

    for p in iter_files((".py",)):
        if not owned(p):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for k in TR_CALL_RE.findall(text):
            add(k, rel(p))
    for p in iter_files((".js", ".html")):
        if not owned(p) or "locales" in p.as_posix():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for k in T_CALL_RE.findall(text):
            add(k, rel(p))
        for k in DATA_I18N_RE.findall(text):
            add(k, rel(p))
    for where, keys in sorted(missing.items()):
        uniq = sorted(set(keys))
        err(f"[i18n] {where} 引用了 {len(uniq)} 个不存在的键：{', '.join(uniq[:5])}")


# ── 6. 版本口径 ─────────────────────────────────────────────────
def check_version():
    vpath = ROOT / "version.py"
    if not vpath.is_file():
        err("[ver] 缺少 version.py（版本单一事实来源）")
        return
    m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']',
                  vpath.read_text(encoding="utf-8"))
    if not m:
        err("[ver] version.py 中找不到 APP_VERSION")
        return
    ver = m.group(1)
    # 只统计"版本号用法"（v1.2.3 / 版本 1.2.3 / -v1.2.3-），避免把
    # pythonnet==3.1.0 这类依赖版本当成应用版本误报
    VER_USE_RE = re.compile(r"(?:^|[\s(（\[\-_,，]|版本\s*)v?(\d+\.\d+\.\d+)(?=[\s)\]）\-,，.]|$)")
    # 历史锚点（"v1.3.1 起" / "v1.1.2 之前" / "历史版本 v1.0.0"）是合法的旧版本引用，
    # 不该被当成版本漂移
    HIST_RE = re.compile(r"起|之前|以前|以来|历史|旧版|自\s*v?\d")
    for extra in ("README.md", "OCRMiniTool.spec"):
        p = ROOT / extra
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if extra.endswith(".spec"):
            # spec 的注释里大量引用历史版本（"2.2.9 式改造（v1.1.2）"），那是
            # 变更记录不是构建口径；只检查真正会被执行的代码行。
            text = "\n".join(
                ln for ln in text.splitlines()
                if not ln.lstrip().startswith("#"))
        found = set()
        for mm in VER_USE_RE.finditer(text):
            ctx = text[max(0, mm.start() - 8):mm.end() + 6]
            if HIST_RE.search(ctx):
                continue
            found.add(mm.group(1))
        stale = sorted(x for x in found if x != ver)
        if stale:
            warn(f"[ver] {extra} 中出现与 APP_VERSION={ver} 不同的版本号：{', '.join(stale)}")


# ── 7. 行尾一致性 ───────────────────────────────────────────────
def check_eol():
    mixed = []
    for p in iter_files((".py", ".js", ".css", ".html", ".json", ".md",
                         ".txt", ".spec", ".bat")):
        # settings.json 是运行时用户数据（.gitignore 已忽略），由程序反复改写；
        # 旧版本程序用 write_text 会写成 CRLF，不该让体检因此报警。
        if p.name == "settings.json" and p.parent == ROOT:
            continue
        try:
            b = p.read_bytes()
        except OSError:
            continue
        crlf = b.count(b"\r\n")
        lf = b.count(b"\n") - crlf
        if crlf and lf:
            mixed.append(f"{rel(p)}（CRLF={crlf}, LF={lf}）")
        elif crlf and p.suffix.lower() not in BAT_OK_CRLF:
            warn(f"[eol] {rel(p)} 全 CRLF，仓库以 LF 为准")
    if mixed:
        warn(f"[eol] {len(mixed)} 个文件混用行尾：" + "；".join(mixed[:5]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OCRMiniTool 静态体检")
    ap.add_argument("--strict", action="store_true", help="告警也算失败")
    ap.add_argument("--quiet", action="store_true", help="只打印失败项")
    args = ap.parse_args(argv)

    counts = {}

    def stage(name, fn):
        try:
            counts[name] = fn()
        except Exception as e:                      # 体检器自身不许炸
            err(f"[{name}] 检查器异常：{type(e).__name__}: {e}")

    stage("py", check_python)
    stage("js", check_js)
    locales = load_locales()
    flat = check_json_locales(locales)
    check_locale_parity(flat)
    check_i18n_usage(flat)
    check_version()
    check_eol()
    counts["locales"] = len(locales)

    if not args.quiet:
        print(f"OCRMiniTool check — {ROOT}")
        print(f"  扫描：py {counts.get('py', 0)} 个，js {counts.get('js', 0)} 个，"
              f"语言包 {counts.get('locales', 0)} 份")
    for w in warnings:
        if not args.quiet:
            print(f"  WARN  {w}")
    for e in errors:
        print(f"  ERROR {e}")

    ok = not errors and not (args.strict and warnings)
    if not args.quiet:
        print(f"结果：{'通过' if ok else '失败'} "
              f"（错误 {len(errors)}，告警 {len(warnings)}）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

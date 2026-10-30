"""i18n.py — UI 多语言（前端与后端共用 webview/locales/*.json）

语言文件为扁平 key + {param} 占位符的 JSON，前端（webview/js/i18n.js）与
后端（本模块）读同一份，保证两侧文案一致。后端负责所有经 SSE 推给前端的
进度/错误文案，以及原生对话框（退出确认、选择文件夹）。

语言存于 settings.json 的 "lang"（en | zh | es；缺省 en）。WebBackend 在
构造时和 set_settings 时调用 set_lang 同步。auto 值按系统 UI 语言解析
（zh* → zh，es* → es，其余 → en）。

frozen（PyInstaller onefile）时语言文件在 _MEIPASS/webview_assets/locales
（webview/ 整目录被打包为 webview_assets，见 *.spec datas）。
"""
from __future__ import annotations

import json
import re
import sys
import threading
from pathlib import Path

FALLBACK_LANG = "zh"                   # 默认语言：中文
AVAILABLE_LANGS = ("zh", "en", "es")   # 内置保证项；更多语言丢一份 JSON 即接入   # 内置保证项；更多语言丢一份 JSON 即接入

_LOCALES_DIR_CANDIDATES = (
    # PyInstaller 冻结：webview/ → webview_assets/
    Path(getattr(sys, "_MEIPASS", "")) / "webview_assets" / "locales"
    if getattr(sys, "_MEIPASS", "") else None,
    # 源码运行：本文件旁的 webview/locales/
    Path(__file__).resolve().parent / "webview" / "locales",
)

_lock = threading.Lock()
_lang = FALLBACK_LANG
_dicts: dict[str, dict] = {}


def _locales_dir() -> Path | None:
    for cand in _LOCALES_DIR_CANDIDATES:
        if cand and cand.is_dir():
            return cand
    return None


def _load(lang: str) -> dict:
    with _lock:
        if lang in _dicts:
            return _dicts[lang]
        d = _locales_dir()
        path = d / f"{lang}.json" if d else None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        _dicts[lang] = data
        return data


class _SafeParams(dict):
    """缺参时保留 {name} 原样（便于发现漏传参数），而不是抛 KeyError。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def detect_lang() -> str:
    """系统 UI 语言 → 支持的语言（zh* → zh，es* → es，其余 → en）。"""
    code = ""
    try:
        if sys.platform == "win32":
            import ctypes
            # GetUserDefaultUILanguage 返回 LCID；LCIDToLocaleName 转成
            # "zh-CN" 这类 BCP-47 标签再按前缀归一。
            buf = ctypes.create_unicode_buffer(16)
            ctypes.windll.kernel32.LCIDToLocaleName(
                ctypes.windll.kernel32.GetUserDefaultUILanguage(),
                buf, len(buf), 0)
            code = buf.value
        else:
            import locale
            code = locale.getdefaultlocale()[0] or ""
    except Exception:
        code = ""
    code = (code or "").lower()
    if code.startswith("zh"):
        return "zh"
    if code.startswith("es"):
        return "es"
    return "en"


def known_langs() -> set[str]:
    """可用语言 = 内置 AVAILABLE_LANGS ∪ locales 目录下实际存在的 *.json。

    接入新语言只需在 webview/locales/ 放一份 <code>.json（含 meta.nativeName）。
    语言代码限定字母/数字/连字符，防止把目录里其它 json 误当语言包。
    """
    found: set[str] = set()
    d = _locales_dir()
    if d:
        try:
            for p in d.glob("*.json"):
                if re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", p.stem):
                    found.add(p.stem.lower())
        except OSError:
            pass
    return set(AVAILABLE_LANGS) | found


def available_languages() -> list[dict]:
    """[{code, name}] 供设置页语言下拉框动态填充。

    name 取语言包的 meta.nativeName（各语言以母语显示自身，如 en→English、
    zh→中文）；缺失时回退大写代码。en（默认语言）排最前，其余按代码排序。
    """
    langs = []
    for code in sorted(known_langs(), key=lambda c: (c != FALLBACK_LANG, c)):
        name = _load(code).get("meta.nativeName")
        langs.append({"code": code, "name": str(name) if name else code.upper()})
    return langs


def set_lang(lang: str | None) -> str:
    """设置当前语言（en | zh | es | locales/ 里的任意语言；auto 按系统语言解析）。

    未知语言（目录里也没有对应 JSON）回退 en。
    """
    global _lang
    lang = (lang or "").strip().lower()
    if lang == "auto":
        lang = detect_lang()
    if lang not in known_langs():
        lang = FALLBACK_LANG
    with _lock:
        _lang = lang
    _load(lang)          # 预热，避免首个 tr() 落盘读文件
    return lang


def get_lang() -> str:
    with _lock:
        return _lang


def tr(key: str, **params) -> str:
    """取当前语言的文案；{param} 占位符用 params 填充。

    当前语言缺 key 时回退 en，再缺则原样返回 key（便于发现漏翻）。
    注意：文案里不要出现字面 { }（str.format 约束）。
    """
    text = _load(get_lang()).get(key)
    if text is None:
        text = _load(FALLBACK_LANG).get(key)
    if text is None:
        return key
    if params:
        try:
            return text.format_map(_SafeParams(params))
        except Exception:
            return text
    return text

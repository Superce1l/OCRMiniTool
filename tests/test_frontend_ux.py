# -*- coding: utf-8 -*-
"""tests/test_frontend_ux.py — 前端交互修复的静态回归（B2–B15）

这些改动大多只能靠「真机点界面」验证，CI 里退化为源码契约断言：
把每项修复的关键锚点固定下来，防止后续重构悄悄退回旧行为。
（消毒器 / CSP 这类安全项在 test_frontend_security.py 里真跑 Node。）

覆盖：
  B2  本地字体栈（不再外链 Google Fonts）
  B3  传输层不等 window load；mock 模式有可见横幅
  B4  界面缩放走 zoom（--ui-zoom），并保留不可缩放时的字号回退
  B5  模型下载/加载可取消（复用 /api/cancel）
  B6  设置保存合并同一 debounce 窗口内的多次 patch
  B7  bridge 失败返回结构化结果；调用点不再裸解构/裸访问
  B10 移除「卸载模型」假按钮
  B12 结果区字数、历史项时间
  B14 文档级拖放兜底
  B15 retranslate 覆盖 Python 探测结果；删除 shutdown / view-ctx 死代码
"""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "webview"
APP_JS = WEB / "js" / "app.js"
BRIDGE_JS = WEB / "js" / "bridge.js"
INDEX = WEB / "index.html"
CSS = WEB / "css" / "app.css"
ZH = WEB / "locales" / "zh.json"
EN = WEB / "locales" / "en.json"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def locale(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


class FontStackTest(unittest.TestCase):
    def test_local_font_stack(self):
        css = read(CSS)
        self.assertIn("Segoe UI", css)
        self.assertIn("Microsoft YaHei UI", css)
        self.assertIn("Cascadia Mono", css)


class TransportBootstrapTest(unittest.TestCase):
    def test_bridge_settles_on_dom_ready_not_load(self):
        bridge = read(BRIDGE_JS)
        self.assertIn("DOMContentLoaded", bridge)
        self.assertNotIn('addEventListener("load"', bridge)

    def test_offline_banner_wired(self):
        self.assertIn('id="mode-banner"', read(INDEX))
        app = read(APP_JS)
        self.assertIn('$("#mode-banner")', app)
        self.assertIn('API.mode !== "web"', app)


class ScaleTest(unittest.TestCase):
    def test_zoom_scale_implemented(self):
        app = read(APP_JS)
        self.assertIn("--ui-zoom", app)
        self.assertIn("CAN_ZOOM", app)
        self.assertIn("syncAppHeight", app)
        self.assertIn("zoom: var(--ui-zoom", read(CSS))

    def test_font_size_fallback_kept(self):
        """老 WebView2 没有 zoom：必须还有字号回退，不能什么都不做。"""
        self.assertIn("root.style.fontSize", read(APP_JS))

    def test_scale_options_cover_range(self):
        html = read(INDEX)
        self.assertIn('value="25"', html)
        self.assertIn('value="200"', html)


class ModelCancelTest(unittest.TestCase):
    def test_cancel_button_exists_and_wired(self):
        self.assertIn('id="btn-model-cancel"', read(INDEX))
        app = read(APP_JS)
        self.assertIn('$("#btn-model-cancel").addEventListener("click"', app)
        self.assertIn("API.cancel()", app)

    def test_cancel_hides_on_terminal_progress(self):
        app = read(APP_JS)
        self.assertIn('$("#btn-model-cancel").hidden = true', app)


class SettingsSaveTest(unittest.TestCase):
    def test_patch_accumulation(self):
        app = read(APP_JS)
        self.assertIn("pendingSettings", app)
        self.assertIn("Object.assign(pendingSettings, patch", app)
        self.assertIn("flushSettings", app)

    def test_save_failure_is_visible(self):
        self.assertIn('msg.genericFailed', read(APP_JS))


class BridgeFailureTest(unittest.TestCase):
    def test_history_never_destructures_raw(self):
        app = read(APP_JS)
        self.assertNotIn("const { items } = await API.listHistory()", app)
        self.assertIn("(r && r.items) || []", app)

    def test_browse_calls_guard_object(self):
        app = read(APP_JS)
        self.assertIn("r && r.ok && r.path", app)

    def test_list_history_returns_structured_failure(self):
        bridge = read(BRIDGE_JS)
        self.assertIn("return { items: [], error: String(e) }", bridge)


class DeadCodeTest(unittest.TestCase):
    def test_unload_button_removed(self):
        self.assertNotIn("btn-model-unload", read(INDEX))
        self.assertNotIn("btn-model-unload", read(APP_JS))

    def test_shutdown_api_removed(self):
        self.assertNotIn("shutdown", read(BRIDGE_JS))

    def test_view_ctx_removed(self):
        self.assertNotIn("view-ctx", read(INDEX))
        self.assertNotIn(".topbar .ctx", read(CSS))


class ResultMetaTest(unittest.TestCase):
    def test_result_meta_has_chars(self):
        app = read(APP_JS)
        self.assertIn("result.historyMeta", app)
        self.assertIn("chars:", app)

    def test_history_shows_time(self):
        app = read(APP_JS)
        self.assertIn("fmtDateTime", app)
        self.assertIn("it.mtime", app)

    def test_history_load_marks_elapsed_null(self):
        self.assertIn("elapsed: null", read(APP_JS))


class DragDropTest(unittest.TestCase):
    def test_document_level_guard(self):
        app = read(APP_JS)
        self.assertIn('document.addEventListener("dragover"', app)
        self.assertIn('document.addEventListener("drop"', app)
        self.assertIn("msg.dropOutside", app)


class RetranslateTest(unittest.TestCase):
    def test_probe_result_retranslated(self):
        app = read(APP_JS)
        self.assertIn("pyUI.lastProbe", app)
        self.assertIn("renderProbeResult(pyUI.lastProbe)", app)


class LocaleKeysTest(unittest.TestCase):
    NEW_KEYS = (
        "result.historyMeta", "msg.genericFailed", "msg.dropOutside",
        "mode.offlineBanner",
    )

    def test_new_keys_present_in_both_locales(self):
        zh, en = locale(ZH), locale(EN)
        for key in self.NEW_KEYS:
            self.assertIn(key, zh, f"zh.json 缺少 {key}")
            self.assertIn(key, en, f"en.json 缺少 {key}")

    def test_page_meta_carries_chars(self):
        for p in (ZH, EN):
            self.assertIn("{chars}", locale(p)["result.pageMeta"], f"{p.name} 缺 {{chars}}")

    def test_new_keys_referenced_by_frontend(self):
        app = read(APP_JS)
        self.assertIn('data-i18n="mode.offlineBanner"', read(INDEX))
        self.assertIn('t("msg.dropOutside"', app)
        self.assertIn('t("result.historyMeta"', app)


if __name__ == "__main__":
    unittest.main()

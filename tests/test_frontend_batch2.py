# -*- coding: utf-8 -*-
"""tests/test_frontend_batch2.py — 前端第二批静态回归

覆盖（B13 键盘/aria、E6 粘贴、E7/E12 设置控件、E14 表格告警、C2 前端兜底）：
  B13  Ctrl/Cmd+Enter 开始、Esc 取消、#drop / #model-status 可聚焦、
       历史删除支持 Enter/Space、三个进度条 aria-valuenow 同步、
       #ocr-log / #result-meta aria-live
  E6   document paste 取 image/* → addFiles，输入框内不拦截
  E7   #set-pdf-pages 读写 pdfPageRange（input debounce + change 立即提交）
  E12  #set-join-lines / #set-cjk-spacing 读写 joinLines / cjkSpacing
  E14  tableWarnings → result.tableWarn（单条与批量），retryHint 只跟截断/页数
  C2   关键 API 调用点均有 try/catch 或 .catch（409 由 bridge 抛 Error）

断言只依赖锚点片段（不写死行号）；HTML 结构断言是例外。
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "webview"
APP_JS = WEB / "js" / "app.js"
INDEX = WEB / "index.html"
ZH = WEB / "locales" / "zh.json"
EN = WEB / "locales" / "en.json"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def chunk(src: str, start: str, end: str) -> str:
    i = src.index(start)
    j = src.index(end, i + len(start))
    return src[i:j]


def tag_of(html: str, tag: str, elem_id: str) -> str:
    m = re.search(rf"<{tag}\b[^>]*\bid=\"{re.escape(elem_id)}\"[^>]*>", html)
    if not m:
        raise AssertionError(f'index.html 中找不到 <{tag} id="{elem_id}">')
    return m.group(0)


class KeyboardTest(unittest.TestCase):
    """B13：最小键盘集（Ctrl/Cmd+Enter / Esc / Enter-Space 激活 role=button）。"""

    def _global_handler(self) -> str:
        app = read(APP_JS)
        return chunk(app, 'document.addEventListener("keydown"', 'activateOnKey($("#drop")')

    def test_global_keydown_starts_with_ctrl_or_cmd_enter(self):
        body = self._global_handler()
        self.assertIn('(e.ctrlKey || e.metaKey) && e.key === "Enter"', body)
        self.assertIn("runOcr()", body)
        self.assertIn("if (state.running) return", body)
        self.assertIn("btn.disabled", body)

    def test_escape_only_cancels_while_running(self):
        body = self._global_handler()
        self.assertIn('e.key === "Escape" && state.running', body)
        self.assertIn("API.cancel()", body)
        self.assertIn('t("msg.cancelRequested")', body)

    def test_drop_and_model_status_are_focusable_role_buttons(self):
        html = read(INDEX)
        drop = tag_of(html, "div", "drop")
        self.assertIn('role="button"', drop)
        self.assertIn('tabindex="0"', drop)
        self.assertIn('data-i18n-aria="aria.dropZone"', drop)
        status = tag_of(html, "div", "model-status")
        self.assertIn('role="button"', status)
        self.assertIn('tabindex="0"', status)
        self.assertIn('data-i18n-aria="aria.modelStatus"', status)

    def test_activate_on_key_ignores_inner_controls(self):
        app = read(APP_JS)
        body = chunk(app, "function activateOnKey", 'document.addEventListener("keydown"')
        self.assertIn('e.key !== "Enter"', body)
        self.assertIn('" "', body)
        # 子按钮自己处理键盘激活，父容器不能抢
        self.assertIn('e.target.closest("button, a, input, select, textarea")', body)
        self.assertIn('activateOnKey($("#drop")', app)
        self.assertIn('activateOnKey($("#model-status")', app)

    def test_history_delete_supports_keyboard(self):
        app = read(APP_JS)
        body = chunk(app, '$("#history-list").addEventListener("keydown"',
                     "/* ── 模型页")
        self.assertIn('closest(".hx")', body)
        self.assertIn("e.preventDefault()", body)
        self.assertIn("e.stopPropagation()", body)
        self.assertIn("deleteHistoryItem(", body)
        self.assertIn("async function deleteHistoryItem", app)


class AriaTest(unittest.TestCase):
    """F12：进度条/日志/结果概要对读屏有语义，且数值真的在同步。"""

    def test_progress_containers_are_progressbars(self):
        html = read(INDEX)
        for elem_id in ("progress", "model-progress", "flavor-progress"):
            tag = tag_of(html, "div", elem_id)
            self.assertIn('role="progressbar"', tag, elem_id)
            self.assertIn('aria-valuemin="0"', tag, elem_id)
            self.assertIn('aria-valuemax="100"', tag, elem_id)

    def test_progress_aria_valuenow_is_synced_in_js(self):
        app = read(APP_JS)
        cases = {
            "function setProgress": ("/* ── E3", '$("#progress").setAttribute('),
            "function setModelProgress": ("/* ── 推理引擎组件卡片",
                                          '$("#model-progress").setAttribute('),
            "function setFlavorProgress": ('API.on("env-progress"',
                                           '$("#flavor-progress").setAttribute('),
        }
        for fn, (end, want) in cases.items():
            body = chunk(app, fn, end)
            self.assertIn(want, body, fn)
            self.assertIn('"aria-valuenow"', body, fn)

    def test_live_regions(self):
        html = read(INDEX)
        self.assertIn('aria-live="polite"', tag_of(html, "div", "ocr-log"))
        self.assertIn('aria-live="polite"', tag_of(html, "span", "result-meta"))


class PasteTest(unittest.TestCase):
    """E6：非输入区 Ctrl+V 图片 → addFiles；输入框交给默认粘贴。"""

    def _handler(self) -> str:
        return chunk(read(APP_JS), 'document.addEventListener("paste"', "/* F7")

    def test_paste_handler_filters_images_and_skips_inputs(self):
        body = self._handler()
        self.assertIn("isTextEntry(e.target)", body)
        self.assertIn("isTextEntry(document.activeElement)", body)
        self.assertIn('startsWith("image/")', body)
        self.assertIn("getAsFile", body)
        self.assertIn("pasted-", body)
        self.assertIn("new File([raw]", body)

    def test_paste_reports_counts_without_nagging_on_text(self):
        body = self._handler()
        self.assertIn('t("msg.pasted", { n: added })', body)
        self.assertIn('t("msg.pastedNone")', body)
        self.assertIn("if (!hasText)", body)
        self.assertIn("if (!files.length)", body)

    def test_paste_does_not_intercept_when_running(self):
        body = self._handler()
        self.assertIn('t("msg.runningLocked")', body)


class SettingsControlsTest(unittest.TestCase):
    """E7/E12：三个新设置的 HTML、回填与提交链路。"""

    def test_index_has_new_setting_controls(self):
        html = read(INDEX)
        pages = tag_of(html, "input", "set-pdf-pages")
        self.assertIn('placeholder="1-3,5,10-"', pages)
        self.assertIn('id="set-pdf-pages"', pages)
        for i18n_key in ("set.pdfPages", "set.pdfPagesDesc"):
            self.assertIn(i18n_key, html)
        for elem_id, i18n_key in (("set-join-lines", "set.joinLines"),
                                  ("set-cjk-spacing", "set.cjkSpacing")):
            tag = tag_of(html, "input", elem_id)
            self.assertIn('type="checkbox"', tag, elem_id)
            self.assertIn(f'data-i18n-aria="{i18n_key}"', tag, elem_id)

    def test_fill_settings_restores_new_values_without_clobbering_input(self):
        app = read(APP_JS)
        self.assertIn('setTextSetting(s, "pdfPageRange", "#set-pdf-pages")', app)
        self.assertIn('setBoolSetting(s, "joinLines", "#set-join-lines")', app)
        self.assertIn('setBoolSetting(s, "cjkSpacing", "#set-cjk-spacing")', app)
        # 键缺失（旧后端）时不许把用户输入清空
        body = chunk(app, "function setTextSetting", "function setBoolSetting")
        self.assertIn("hasOwnProperty.call(s, key)", body)
        self.assertNotIn('el.value = ""', body)

    def test_new_settings_are_pushed(self):
        app = read(APP_JS)
        self.assertIn("pushSettings({ pdfPageRange: v })", app)
        self.assertIn('pushSettings({ pdfPageRange: $("#set-pdf-pages").value.trim() })', app)
        self.assertIn('pushSettings({ joinLines: $("#set-join-lines").checked })', app)
        self.assertIn('pushSettings({ cjkSpacing: $("#set-cjk-spacing").checked })', app)
        # 输入中（结尾逗号）不发，避免每敲一个字符弹一次校验错误
        body = chunk(app, '$("#set-pdf-pages").addEventListener("input"',
                     '$("#set-pdf-pages").addEventListener("change"')
        self.assertIn("setTimeout", body)
        self.assertIn(r"/,+\s*$/", body)


class TableWarnTest(unittest.TestCase):
    """E14：tableWarnings 进结果提示条（与截断提示同一 banner）。"""

    def test_truncation_info_carries_table_warnings(self):
        app = read(APP_JS)
        body = chunk(app, "function truncationInfo", "function resultWarnLines")
        self.assertIn("tableWarnings", body)
        self.assertIn("r.table_warnings", body)
        self.assertIn("!(tableWarnings > 0)", body)

    def test_result_lines_include_table_warn_and_gate_retry_hint(self):
        app = read(APP_JS)
        body = chunk(app, "function resultWarnLines", "function batchWarnLines")
        self.assertIn('t("result.tableWarn", { n: info.tableWarnings })', body)
        self.assertIn("if (info.truncated || info.mismatch) lines.push(t(\"warn.retryHint\"))",
                      body)

    def test_batch_lines_aggregate_table_warnings(self):
        app = read(APP_JS)
        body = chunk(app, "function batchWarnLines", "function renderResultWarn")
        self.assertIn("tableN", body)
        self.assertIn('t("result.tableWarn", { n: tableN })', body)


class ErrorPathTest(unittest.TestCase):
    """C2：HTTP 409 等错误由 bridge 抛 Error，调用点必须有兜底，禁止裸 await。"""

    def test_cancel_and_aux_handlers_have_catch(self):
        app = read(APP_JS)
        for anchor, end in (
            ('$("#btn-model-cancel").addEventListener("click"', "function setModelProgress"),
            ('$("#btn-flavor-cancel").addEventListener("click"', "function setFlavorProgress"),
            ('$("#btn-model-dir-browse").addEventListener("click"', '$("#set-mirror")'),
            ('$("#btn-python-browse").addEventListener("click"', "// 手动改路径"),
            ('$("#btn-python-apply").addEventListener("click"', '$("#btn-python-reset")'),
        ):
            body = chunk(app, anchor, end)
            self.assertTrue("try {" in body or ".catch(" in body,
                            f"{anchor} 缺少 try/catch 兜底")

    def test_history_and_refresh_have_catch(self):
        app = read(APP_JS)
        body = chunk(app, "async function refreshHistory", "/* B13：历史删除")
        self.assertIn("try {", body)
        self.assertIn("catch (e)", body)
        body = chunk(app, "async function deleteHistoryItem",
                     '$("#history-list").addEventListener("click"')
        self.assertIn("try {", body)
        self.assertIn("catch (e)", body)
        self.assertIn("flashError", body)

    def test_open_output_dir_promise_is_handled(self):
        app = read(APP_JS)
        body = chunk(app, '$("#btn-open-dir").addEventListener("click"', "/* ── 最近识别")
        self.assertIn(".catch(", body)


class I18nKeysTest(unittest.TestCase):
    """新增文案必须双语齐备，且在 js/html 里真的被引用。"""

    KEYS = [
        "aria.dropZone", "aria.progress", "aria.resultMeta", "aria.log",
        "aria.modelStatus", "msg.pasted", "msg.pastedNone",
        "set.pdfPages", "set.pdfPagesDesc", "set.joinLines", "set.joinLinesDesc",
        "set.cjkSpacing", "set.cjkSpacingDesc", "result.tableWarn",
    ]

    def test_keys_exist_in_both_locales(self):
        zh = json.loads(read(ZH))
        en = json.loads(read(EN))
        for key in self.KEYS:
            self.assertIn(key, zh, f"zh 缺少 {key}")
            self.assertIn(key, en, f"en 缺少 {key}")

    def test_keys_are_referenced(self):
        app = read(APP_JS)
        html = read(INDEX)
        for key in self.KEYS:
            self.assertTrue(key in app or key in html, f"{key} 没有被引用")


if __name__ == "__main__":
    unittest.main()

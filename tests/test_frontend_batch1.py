# -*- coding: utf-8 -*-
"""tests/test_frontend_batch1.py — 前端第一批的静态回归（B9 / B11 / F7 / F15）

这批改动只能在真机界面上点，CI 里退化为源码契约断言：把每项的关键锚点
固定下来，防止后续重构悄悄把行为改回去。断言只依赖代码片段，不写死行号；
HTML 结构断言是例外（结构本身就是契约）。

覆盖：
  B9  结果可编辑：#md-raw 可编辑、逐页回写、多页 text 重拼、批量同步、「全部」只读
  B11 错误置顶：#alert-banner / showAlert / flashError，错误路径不再只进底部日志
  F7  文件去重：name|size|lastModified，重复时提示 msg.duplicateFiles
  F15 运行中锁定：prompt/选择/清空/删除/拖放全部拦截，提示 msg.runningLocked
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "webview"
APP_JS = WEB / "js" / "app.js"
INDEX = WEB / "index.html"
CSS = WEB / "css" / "app.css"
ZH = WEB / "locales" / "zh.json"
EN = WEB / "locales" / "en.json"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def chunk(src: str, start: str, end: str) -> str:
    """按首尾锚点截取一段源码（比行号稳定）。"""
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


class ResultEditableTest(unittest.TestCase):
    """B9：结果可编辑，且编辑必须回到 state（复制/保存才拿得到修订）。"""

    def test_md_raw_is_editable(self):
        html = read(INDEX)
        tag = re.search(r'<textarea[^>]*id="md-raw"[^>]*>', html)
        self.assertIsNotNone(tag, "index.html 中找不到 #md-raw")
        self.assertNotIn("readonly", tag.group(0),
                         "#md-raw 仍带 readonly，B9 的可编辑等于没做")

    def test_input_listener_feeds_apply_result_edit(self):
        app = read(APP_JS)
        self.assertIn('$("#md-raw").addEventListener("input"', app)
        self.assertIn("applyResultEdit", app)

    def test_page_edit_writes_current_page_and_rebuilds_text(self):
        app = read(APP_JS)
        body = chunk(app, "function applyResultEdit", '$("#md-raw").addEventListener')
        self.assertIn("pages[state.curPage] = value", body)
        self.assertIn("rebuildMergedText(pages)", body)
        self.assertIn("r.edited = true", body)

    def test_merged_text_keeps_backend_page_markers(self):
        """多页 text 必须与后端同格式：`<!-- page N -->` + 空行连接。"""
        app = read(APP_JS)
        body = chunk(app, "function rebuildMergedText", "function applyResultEdit")
        self.assertIn(r"`<!-- page ${i + 1} -->\n${txt}`", body)
        self.assertIn(r'.join("\n\n")', body)

    def test_single_page_writes_pages_and_text(self):
        app = read(APP_JS)
        body = chunk(app, "function applyResultEdit", '$("#md-raw").addEventListener')
        self.assertIn("r.pages = [value]", body)
        self.assertIn("r.text = value", body)

    def test_all_pages_view_is_read_only(self):
        app = read(APP_JS)
        self.assertIn("const readOnlyAll = multi && state.curPage === -1", app)
        self.assertIn("raw.readOnly = readOnlyAll", app)
        self.assertIn('raw.title = readOnlyAll ? t("result.editAllHint")', app)
        # 「全部」视图即使收到 input 也不能写回
        body = chunk(app, "function applyResultEdit", '$("#md-raw").addEventListener')
        self.assertIn("pages.length > 1 && state.curPage === -1", body)

    def test_batch_items_are_synced(self):
        app = read(APP_JS)
        body = chunk(app, "function applyResultEdit", '$("#md-raw").addEventListener')
        self.assertIn("state.batchItems[state.viewIndex]", body)
        self.assertIn("it.edited = true", body)
        # 切回条目时要还原编辑态
        self.assertIn("edited: !!it.edited", app)

    def test_edited_chip_exists_and_toggles(self):
        self.assertIn('id="result-edited"', read(INDEX))
        self.assertIn('data-i18n="result.edited"', read(INDEX))
        app = read(APP_JS)
        self.assertIn('$("#result-edited")', app)
        self.assertIn("chip.hidden = !(state.lastResult && state.lastResult.edited)", app)
        # 批量失败项没有正文：chip 不能挂在上一次的编辑态上
        self.assertIn('$("#result-edited").hidden = true', app)


class AlertBannerTest(unittest.TestCase):
    """B11：错误必须出现在顶部，而不是只写页面最底部的日志区。"""

    def test_markup_position_and_a11y(self):
        html = read(INDEX)
        self.assertIn('id="alert-banner"', html)
        self.assertIn('role="alert"', html)
        self.assertIn('id="alert-text"', html)
        self.assertIn('id="alert-close"', html)
        self.assertIn('data-i18n-title="alert.dismiss"', html)
        # 必须位于 .view-host 之前（跨视图常驻，不随识别页滚动）
        self.assertLess(html.index('id="alert-banner"'), html.index('class="view-host"'))

    def test_show_alert_api(self):
        app = read(APP_JS)
        body = chunk(app, "function showAlert", "function flashError")
        self.assertIn('$("#alert-text").textContent = text', body)
        self.assertIn("box.hidden = false", body)
        self.assertIn("box.hidden = true", body)     # 空消息 = 收起

    def test_flash_error_keeps_log_and_shows_banner(self):
        app = read(APP_JS)
        body = chunk(app, "function flashError", "$(\"#alert-close\")")
        self.assertIn("flash(msg);", body)
        self.assertIn("showAlert(msg);", body)
        self.assertEqual(body.count("logLine"), 0,
                         "flashError 不能绕过 flash 自己写日志")

    def test_close_button_wired(self):
        app = read(APP_JS)
        self.assertIn('$("#alert-close")', app)
        # 允许 `?.addEventListener` 的防御写法；关键是点击后收起横幅
        self.assertIn('addEventListener("click", hideAlert)', app)

    def test_recognition_failure_goes_to_banner(self):
        app = read(APP_JS)
        body = chunk(app, "async function runOcr", "function baseNameOf")
        self.assertIn('flashError(t("msg.failed"', body)

    def test_other_error_paths_go_to_banner(self):
        app = read(APP_JS)
        for anchor in (
            'flashError(t("msg.genericFailed"',          # 重试/设置/环境切换/复制保存
            'flashError((r && r.error) || t("msg.cannotDownload")',
            'flashError((r && r.error) || t("msg.cannotLoad")',
            'flashError((s && s.error)',
        ):
            self.assertIn(anchor, app)
        self.assertGreaterEqual(app.count("flashError("), 8,
                                "错误路径应统一走 flashError")

    def test_new_run_hides_previous_alert(self):
        app = read(APP_JS)
        body = chunk(app, "async function runOcr", "function baseNameOf")
        self.assertIn("hideAlert()", body)

    def test_banner_css(self):
        css = read(CSS)
        self.assertIn(".alert-banner", css)
        self.assertIn("#alert-text", css)
        self.assertIn(".alert-close", css)


class FileDedupeTest(unittest.TestCase):
    """F7：同名同大小同修改时间的文件不能重复入列。"""

    def test_dedupe_key_uses_name_size_mtime(self):
        app = read(APP_JS)
        body = chunk(app, "function fileKey", "function addFiles")
        self.assertIn("f.name", body)
        self.assertIn("f.size", body)
        self.assertIn("f.lastModified", body)

    def test_compares_against_existing_files(self):
        app = read(APP_JS)
        body = chunk(app, "function addFiles", "function clipName")
        self.assertIn("new Set(state.files.map(fileKey))", body)
        self.assertIn("seen.has(key)", body)

    def test_duplicate_notice(self):
        app = read(APP_JS)
        body = chunk(app, "function addFiles", "function clipName")
        self.assertIn('t("msg.duplicateFiles"', body)
        self.assertIn("{ n: duplicated }", body)


class RunningLockTest(unittest.TestCase):
    """F15：识别运行中，输入区/选择/清空/删除/拖放都要被锁住并给出提示。"""

    def test_controls_disabled_while_running(self):
        app = read(APP_JS)
        body = chunk(app, "function renderRunButtons", "/* ── 识别页：文件选择")
        for sel in ('$("#prompt-box").disabled = state.running',
                    '$("#btn-pick").disabled = state.running',
                    '$("#btn-pick-dir").disabled = state.running',
                    '$("#btn-clear-files").disabled = state.running'):
            self.assertIn(sel, body)

    def test_add_files_guard(self):
        app = read(APP_JS)
        body = chunk(app, "function addFiles", "function clipName")
        self.assertIn('t("msg.runningLocked")', body)
        self.assertIn("return;", body)

    def test_delete_clear_and_pick_guards(self):
        app = read(APP_JS)
        for start, end in (
            ('$("#file-list").addEventListener', '$("#btn-clear-files").addEventListener'),
            ('$("#btn-clear-files").addEventListener', "/* ── 目录识别"),
        ):
            self.assertIn('t("msg.runningLocked")', chunk(app, start, end))
        body = chunk(app, "async function pickDirectory", "/* ── 识别模式")
        self.assertIn('t("msg.runningLocked")', body)

    def test_drop_zone_guard(self):
        app = read(APP_JS)
        body = chunk(app, "drop.addEventListener(\"click\"", "function fileKey")
        self.assertIn('t("msg.runningLocked")', body)


class Batch1LocaleKeysTest(unittest.TestCase):
    NEW_KEYS = ("result.edited", "result.editAllHint", "msg.duplicateFiles",
                "msg.runningLocked", "alert.dismiss")

    def test_keys_present_in_both_locales(self):
        zh = json.loads(read(ZH))
        en = json.loads(read(EN))
        for key in self.NEW_KEYS:
            self.assertIn(key, zh, f"zh.json 缺少 {key}")
            self.assertIn(key, en, f"en.json 缺少 {key}")

    def test_keys_referenced_by_frontend(self):
        html = read(INDEX)
        app = read(APP_JS)
        self.assertIn('data-i18n="result.edited"', html)
        self.assertIn('data-i18n-title="alert.dismiss"', html)
        self.assertIn('t("result.editAllHint")', app)
        self.assertIn('t("msg.duplicateFiles"', app)
        self.assertIn('t("msg.runningLocked")', app)


if __name__ == "__main__":
    unittest.main()

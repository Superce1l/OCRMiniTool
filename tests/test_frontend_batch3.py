# -*- coding: utf-8 -*-
"""tests/test_frontend_batch3.py — 第三批前端：E2 JSON 导出 / E17 查找替换 / 组件进度条

纯静态契约测试（不依赖 GUI）。逐项锁住：
  A) 输出格式 JSON 选项；保存按钮 json 分支（schema/文件名/blocks 缺失提示）；批量 blocks 透传
  B) 查找/替换工具栏结构、四个动作绑定、Enter 行为、替换走 applyResultEdit、
     作用范围（当前页/整篇，多页「全部」只读）、匹配计数与重置
  C) #flavor-progress 恢复 .progress > .bar > i 结构，setFlavorProgress 宽度/aria 同步
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "webview"
APP_JS = WEB / "js" / "app.js"
INDEX = WEB / "index.html"
CSS = WEB / "css" / "app.css"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def chunk(text: str, start: str, end: str) -> str:
    i = text.find(start)
    assert i >= 0, f"未找到起点：{start}"
    j = text.find(end, i + len(start))
    assert j > i, f"未找到终点：{end}"
    return text[i:j]


class JsonExportTest(unittest.TestCase):
    """A) E2：输出格式 = json 时保存结构化 blocks。"""

    def test_outfmt_has_json_option(self):
        html = read(INDEX)
        seg = chunk(html, 'id="set-outfmt"', "</div>")
        self.assertIn('data-v="json"', seg)
        self.assertIn('data-i18n="set.outfmtJson"', seg)

    def test_fillsettings_toggles_outfmt_generically(self):
        app = read(APP_JS)
        body = chunk(app, "$$(\"#set-outfmt button\")", "\n")
        self.assertIn("b.dataset.v", body)
        self.assertIn("s.outFormat", body)

    def test_save_json_branch_schema_and_filename(self):
        app = read(APP_JS)
        body = chunk(app, '$("#btn-save").addEventListener("click"',
                     '$("#btn-open-dir").addEventListener')
        self.assertIn('outFormat === "json"', body)
        self.assertIn("version: 1", body)
        self.assertIn("pages: r.blocks", body)
        self.assertIn("JSON.stringify(payload, null, 2)", body)
        self.assertIn('"application/json;charset=utf-8"', body)
        self.assertIn('stem + ".json"', body)
        self.assertIn('rawBase.replace(/\\.(md|txt|json)$/i', body)

    def test_save_json_requires_blocks(self):
        app = read(APP_JS)
        body = chunk(app, '$("#btn-save").addEventListener("click"',
                     '$("#btn-open-dir").addEventListener')
        self.assertIn("!Array.isArray(r.blocks)", body)
        self.assertIn('flashError(t("result.jsonUnavailable"))', body)
        # 缺 blocks 时必须先 return，不能落到 md/txt 导出空内容
        guard = body[body.find("!Array.isArray(r.blocks)"):]
        self.assertIn("return;", guard)

    def test_save_md_txt_branch_kept(self):
        app = read(APP_JS)
        body = chunk(app, '$("#btn-save").addEventListener("click"',
                     '$("#btn-open-dir").addEventListener')
        self.assertIn('outFormat === "txt" ? "txt" : "md"', body)
        self.assertIn("mdToPlain(text)", body)

    def test_batch_item_passes_blocks_into_last_result(self):
        app = read(APP_JS)
        body = chunk(app, "function viewBatchItem", "/* 复用 showResult")
        self.assertIn("blocks: it.blocks", body)

    def test_save_uses_settings_outFormat(self):
        app = read(APP_JS)
        body = chunk(app, "function fillSettings", "let saveTimer")
        self.assertIn("state.settings = s", body)
        self.assertIn('"#set-outfmt button"', body)


class FindReplaceTest(unittest.TestCase):
    """B) E17：结果内查找/替换。"""

    def test_find_bar_markup(self):
        html = read(INDEX)
        bar = chunk(html, 'id="find-bar"', "</div>")
        for elem in ("find-text", "replace-text", "find-info", "find-prev",
                     "find-next", "find-replace", "find-replace-all"):
            self.assertIn(f'id="{elem}"', bar, elem)
        self.assertIn('data-i18n-placeholder="result.search"', bar)
        self.assertIn('data-i18n-placeholder="result.replaceWith"', bar)
        self.assertIn('data-i18n="result.searchPrev"', bar)
        self.assertIn('data-i18n="result.searchNext"', bar)
        self.assertIn('data-i18n="result.replace"', bar)
        self.assertIn('data-i18n="result.replaceAll"', bar)
        self.assertIn("hidden", chunk(html, 'id="find-bar"', ">"))

    def test_find_buttons_are_bound(self):
        app = read(APP_JS)
        for sel in ("#find-next", "#find-prev", "#find-replace", "#find-replace-all"):
            self.assertIn(f'$("{sel}").addEventListener("click"', app, sel)

    def test_match_count_uses_indexof_only(self):
        app = read(APP_JS)
        body = chunk(app, "function findMatches", "function showFindBar")
        self.assertIn("indexOf(query", body)
        self.assertNotIn("new RegExp", body)
        self.assertIn('t("result.matchInfo", { i: findState.index + 1, n: matches.length })',
                      read(APP_JS))

    def test_replace_reuses_apply_result_edit(self):
        app = read(APP_JS)
        body = chunk(app, "function replaceCurrentMatch", "$(\"#find-text\").addEventListener")
        self.assertIn("applyResultEdit(", body)
        self.assertIn('t("msg.replaced", { n: 1 })', body)
        self.assertIn('t("msg.replaced", { n })', body)
        self.assertIn('t("msg.noMatch")', body)

    def test_scope_current_page_vs_whole_and_readonly_all(self):
        app = read(APP_JS)
        body = chunk(app, "function findTarget", "function findMatches")
        self.assertIn("pages.length > 1", body)
        self.assertIn("state.curPage >= 0", body)
        self.assertIn("editable:", body)
        self.assertIn("state.curPage === -1", body)
        # 只读「全部」视图替换时给出提示而不是静默失败
        self.assertIn('t("result.editAllHint")', app)

    def test_enter_moves_next_without_stealing_ctrl_enter(self):
        app = read(APP_JS)
        body = chunk(app, '$("#find-text").addEventListener("keydown"',
                     '$("#replace-text").addEventListener("keydown"')
        self.assertIn("e.ctrlKey || e.metaKey", body)
        self.assertIn('e.key === "Enter"', body)
        self.assertIn("findNext(e.shiftKey ? -1 : 1)", body)
        self.assertIn('e.key === "Escape"', body)

    def test_next_navigation_selects_in_raw_textarea(self):
        app = read(APP_JS)
        body = chunk(app, "function focusFindMatch", "function findNext")
        self.assertIn("ensureRawMode()", body)
        self.assertIn("setSelectionRange(at, at + q.length)", body)
        self.assertIn("function ensureRawMode", app)

    def test_render_and_result_changes_recount(self):
        app = read(APP_JS)
        self.assertIn("updateFindInfo();   // E17：切页/换结果后重新计算匹配计数",
                      read(APP_JS))
        body = chunk(app, "function renderResultText", "/* ── B9")
        self.assertIn("updateFindInfo()", body)
        show = chunk(app, "function showResult(result)", "function renderResultText")
        self.assertIn("showFindBar(true)", show)
        self.assertIn("resetFind(true)", show)
        batch = chunk(app, "function viewBatchItem", "/* 复用 showResult")
        self.assertIn("resetFind(false)", batch)
        self.assertIn("showFindBar(false)", batch)

    def test_match_info_is_aria_live(self):
        html = read(INDEX)
        info = html[html.find('id="find-info"') - 120:html.find('id="find-info"') + 120]
        self.assertIn('aria-live="polite"', info)


class FlavorProgressTest(unittest.TestCase):
    """C) 组件下载进度条与 .progress > .bar > i 结构统一。"""

    def test_flavor_progress_uses_track_and_fill(self):
        html = read(INDEX)
        body = chunk(html, 'id="flavor-progress"', "</div>\n          </div>")
        self.assertIn('<div class="bar"><i id="flavor-bar"', body)
        # 旧写法（把轨道当成填充条）必须消失
        self.assertNotIn('<div class="bar" id="flavor-bar">', html)

    def test_flavor_progress_aria_and_width_synced(self):
        app = read(APP_JS)
        body = chunk(app, "function setFlavorProgress", 'API.on("env-progress"')
        self.assertIn('$("#flavor-bar").style.width', body)
        self.assertIn('$("#flavor-progress").setAttribute("aria-valuenow"', body)
        self.assertIn("Math.max(0, Math.min(100", body)

    def test_progress_containers_consistent(self):
        html = read(INDEX)
        self.assertIn('role="progressbar"', html)
        self.assertEqual(html.count('role="progressbar"'), 3)
        self.assertIn('<div class="bar"><i id="prog-bar"', html)
        self.assertIn('<div class="bar"><i id="model-prog-bar"', html)


if __name__ == "__main__":
    unittest.main()

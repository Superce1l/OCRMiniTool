# -*- coding: utf-8 -*-
"""tests/test_frontend_batch4.py — 第四批前端：E19 输出目录 UI + E16 批量合并导出

静态契约测试（不依赖 GUI）：
  A) outputDir 控件/浏览按钮/回填；input 400ms debounce、change 立即提交；
     后端拒绝（ok:false / 抛错）时 flashError 并把界面回填成后端实际值
  B) 批量合并导出：按钮存在与可见性（≥1 个 ok）、仅合并 ok 项、
     合并格式 `# <name>\\n\\n<text>` + `\\n\\n---\\n\\n` 分隔、
     文件名 `<首个成功项 stem>-全部.md`、空/全失败提示、复用 downloadBlob
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "webview"
APP_JS = WEB / "js" / "app.js"
INDEX = WEB / "index.html"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def chunk(text: str, start: str, end: str) -> str:
    i = text.find(start)
    assert i >= 0, f"未找到起点：{start}"
    j = text.find(end, i + len(start))
    assert j > i, f"未找到终点：{end}"
    return text[i:j]


class OutputDirUiTest(unittest.TestCase):
    """A) E19：设置页输出目录。"""

    def test_controls_exist_and_follow_outfmt_row(self):
        html = read(INDEX)
        row = chunk(html, 'data-i18n="set.outDir"', 'data-i18n="set.theme"')
        self.assertIn('data-i18n="set.outDirDesc"', row)
        self.assertIn('id="set-out-dir"', row)
        self.assertIn('id="btn-out-dir-browse"', row)
        self.assertIn('data-i18n="btn.browse"', row)
        self.assertIn('class="input mono"', row)
        # 必须排在「输出格式」之后
        self.assertGreater(html.find('id="set-out-dir"'), html.find('id="set-outfmt"'))

    def test_empty_placeholder_means_default_outputs(self):
        html = read(INDEX)
        tag = html[html.find('id="set-out-dir"') - 160:html.find('id="set-out-dir"') + 160]
        self.assertIn('placeholder=""', tag)
        self.assertIn('data-i18n="set.outDirDesc"',
                      chunk(html, 'data-i18n="set.outDir"', 'data-i18n="set.theme"'))

    def test_fillsettings_backfills_output_dir(self):
        app = read(APP_JS)
        body = chunk(app, "function fillSettings", "let saveTimer")
        self.assertIn('$("#set-out-dir").value = s.outputDir || ""', body)

    def test_input_debounce_400ms_and_change_immediate(self):
        app = read(APP_JS)
        body = chunk(app, '$("#set-out-dir").addEventListener("input"',
                     '$("#set-out-dir").addEventListener("change"')
        self.assertIn("clearTimeout(outDirTimer)", body)
        self.assertIn("setTimeout(", body)
        self.assertIn("400", body)
        change = chunk(app, '$("#set-out-dir").addEventListener("change"',
                       '$("#btn-out-dir-browse").addEventListener')
        self.assertIn("commitOutDir", change)
        commit = chunk(app, "function commitOutDir", "$(\"#set-out-dir\").addEventListener")
        self.assertIn('pushSettings({ outputDir: $("#set-out-dir").value.trim() })', commit)

    def test_browse_button_uses_api_and_submits(self):
        app = read(APP_JS)
        body = chunk(app, '$("#btn-out-dir-browse").addEventListener',
                     "/* ── 设置页：Python 解释器")
        self.assertIn("API.browseFolder(", body)
        self.assertIn("r.ok && r.path", body)
        self.assertIn("commitOutDir()", body)
        self.assertIn("flashError(", body)

    def test_rejected_settings_revert_to_server_value(self):
        app = read(APP_JS)
        body = chunk(app, "async function revertSettingsFromServer",
                     '$("#set-model-dir").addEventListener')
        self.assertIn("API.getSettings()", body)
        self.assertIn("fillSettings(cur)", body)
        flush = chunk(app, "async function flushSettings",
                      '$("#set-model-dir").addEventListener')
        self.assertIn("await revertSettingsFromServer()", flush)
        self.assertEqual(flush.count("await revertSettingsFromServer()"), 2,
                         "ok:false 与异常两条路径都要回填")

    def test_open_output_dir_handler_unchanged(self):
        app = read(APP_JS)
        self.assertIn('$("#btn-open-dir").addEventListener("click", () => {', app)
        body = chunk(app, '$("#btn-open-dir").addEventListener',
                     "/* ── 最近识别")
        self.assertIn("API.openOutputDir()", body)


class ExportAllTest(unittest.TestCase):
    """B) E16：批量成功项合并导出。"""

    def test_button_in_batch_actions(self):
        html = read(INDEX)
        actions = chunk(html, 'id="batch-actions"', "</div>")
        self.assertIn('id="btn-export-all"', actions)
        self.assertIn('data-i18n="btn.exportAll"', actions)

    def test_visibility_requires_at_least_one_ok(self):
        app = read(APP_JS)
        body = chunk(app, "function updateBatchActions", "// 文件名 stem")
        self.assertIn("hasOk", body)
        self.assertIn("hasFail", body)
        self.assertIn('exportBtn.hidden = !hasOk', body)
        self.assertIn('retry.hidden = !hasFail', body)
        self.assertIn("box.hidden = !(hasOk || hasFail)", body)

    def test_show_batch_result_recomputes_actions(self):
        app = read(APP_JS)
        body = chunk(app, "function showBatchResult", "function showBatchError")
        self.assertIn("updateBatchActions(items)", body)

    def test_only_ok_items_and_merge_format(self):
        app = read(APP_JS)
        body = chunk(app, "function exportAllBatch", '$("#btn-export-all").addEventListener')
        self.assertIn('const okItems = batchOkItems();', body)
        self.assertIn('.filter(it => it && it.ok)', chunk(app, "function batchOkItems",
                                                          "// 批量动作栏可见性"))
        self.assertIn('`# ${it.name}\\n\\n${it.text || ""}`', body)
        self.assertIn('join("\\n\\n---\\n\\n")', body)

    def test_filename_from_first_ok_stem(self):
        app = read(APP_JS)
        body = chunk(app, "function exportAllBatch", '$("#btn-export-all").addEventListener')
        self.assertIn("batchStem(okItems[0])", body)
        self.assertIn('+ "-全部.md"', body)
        stem = chunk(app, "function batchStem", "function exportAllBatch")
        self.assertIn("outPath", stem)
        self.assertIn('replace(/\\.(md|txt|json)$/i, "")', stem)

    def test_empty_or_all_failed_flashes_error(self):
        app = read(APP_JS)
        body = chunk(app, "function exportAllBatch", '$("#btn-export-all").addEventListener')
        self.assertIn("!okItems.length", body)
        self.assertIn('flashError(t("msg.exportAllEmpty"))', body)
        done = re.search(r'flash\(t\("msg\.exportAllDone", \{ n: okItems\.length, name: filename \}\)\)', body)
        self.assertIsNotNone(done, "成功后要 flash 导出条数与文件名")

    def test_batch_stem_falls_back_to_item_name(self):
        app = read(APP_JS)
        stem = chunk(app, "function batchStem", "function exportAllBatch")
        self.assertIn('|| (it && it.name) || "result"', stem)

    def test_uses_download_blob_and_is_bound(self):
        app = read(APP_JS)
        body = chunk(app, "function exportAllBatch", '$("#btn-export-all").addEventListener')
        self.assertIn("downloadBlob(", body)
        self.assertIn('"text/markdown;charset=utf-8"', body)
        self.assertIn('$("#btn-export-all").addEventListener("click", exportAllBatch)', app)


import re  # noqa: E402  （放在末尾避免与上面的静态断言混淆）

if __name__ == "__main__":
    unittest.main()

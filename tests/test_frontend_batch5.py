# -*- coding: utf-8 -*-
"""tests/test_frontend_batch5.py — 第五批前端：E8 可搜索 PDF 导出按钮 + bridge 端点

静态契约测试（不依赖 GUI）：
  • #btn-export-pdf 存在、默认 hidden、文案/aria/title
  • bridge.exportPdf() → POST /api/export-pdf（空 body），失败返回 {ok:false,error}，mock 明确失败
  • 可见性只看 state.lastResult.pdfAvailable（前端只读，不自行构造）
  • 点击禁用 + msg.pdfExporting；成功 msg.pdfExported{path}；失败 flashError
  • finally 按可见性重算并恢复可点；批量失败项强制隐藏
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "webview"
APP_JS = WEB / "js" / "app.js"
BRIDGE_JS = WEB / "js" / "bridge.js"
INDEX = WEB / "index.html"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def chunk(text: str, start: str, end: str) -> str:
    i = text.find(start)
    assert i >= 0, f"未找到起点：{start}"
    j = text.find(end, i + len(start))
    assert j > i, f"未找到终点：{end}"
    return text[i:j]


class PdfButtonMarkupTest(unittest.TestCase):
    """按钮本体与无障碍/提示。"""

    def test_button_exists_hidden_and_localized(self):
        html = read(INDEX)
        block = chunk(html, '<button class="btn btn-ghost" id="btn-export-pdf"', "</button>")
        tag = block[:block.find(">") + 1]
        self.assertIn("hidden", tag)
        self.assertIn('data-i18n="btn.exportPdf"', block)
        self.assertIn('class="btn btn-ghost"', tag)

    def test_button_sits_between_save_and_open_dir(self):
        html = read(INDEX)
        save = html.find('id="btn-save"')
        pdf = html.find('id="btn-export-pdf"')
        open_dir = html.find('id="btn-open-dir"')
        self.assertGreater(pdf, save)
        self.assertLess(pdf, open_dir)

    def test_title_mentions_original_result(self):
        html = read(INDEX)
        tag = chunk(html, 'id="btn-export-pdf"', ">")
        self.assertIn("title=", tag)
        self.assertIn("原始识别结果", tag)
        self.assertIn('data-i18n-aria="btn.exportPdf"', tag)


class BridgeExportPdfTest(unittest.TestCase):
    """bridge 端点和错误口径。"""

    def test_bridge_exports_via_export_pdf_endpoint(self):
        bridge = read(BRIDGE_JS)
        body = chunk(bridge, "async exportPdf()", "async startDownload()")
        self.assertIn('apiPost("/api/export-pdf", {})', body)

    def test_bridge_failure_returns_structured_error(self):
        bridge = read(BRIDGE_JS)
        body = chunk(bridge, "async exportPdf()", "async startDownload()")
        self.assertIn("return { ok: false, error: String(e) };", body)
        self.assertIn('return { ok: false, error: "mock" };', body)


class PdfVisibilityTest(unittest.TestCase):
    """可见性：只看 pdfAvailable；各处结果切换都重算。"""

    def test_visibility_reads_pdf_available_flag(self):
        app = read(APP_JS)
        body = chunk(app, "function updatePdfButton", '$("#btn-export-pdf").addEventListener')
        self.assertIn("r.pdfAvailable === true", body)
        self.assertIn("btn.hidden =", body)
        self.assertIn("forceHide", body)

    def test_result_switches_recompute(self):
        app = read(APP_JS)
        self.assertIn("updatePdfButton();   // E8：切页/换结果后按 pdfAvailable 重算导出按钮", app)
        pages = chunk(app, "function showResultPages", '$("#batch-list").addEventListener')
        self.assertIn("updatePdfButton();", pages)
        failed = chunk(app, "function viewBatchItem", "/* 复用 showResult")
        self.assertIn("updatePdfButton(true);", failed)

    def test_batch_item_copies_pdf_available(self):
        app = read(APP_JS)
        body = chunk(app, "function viewBatchItem", "/* 复用 showResult")
        self.assertIn("pdfAvailable: !!it.pdfAvailable", body)

    def test_frontend_never_invents_pdf_available(self):
        app = read(APP_JS)
        self.assertNotIn("pdfAvailable = true", app)
        self.assertNotIn("pdfAvailable: true", app)


class PdfClickBehaviourTest(unittest.TestCase):
    """点击流程：禁用 → 提示 → 成功/失败 → finally 恢复。"""

    def test_click_disables_and_flashes_exporting(self):
        app = read(APP_JS)
        body = chunk(app, '$("#btn-export-pdf").addEventListener("click"',
                     "});\n\n$(\"#btn-save\")")
        self.assertIn("btn.disabled = true", body)
        self.assertIn('flash(t("msg.pdfExporting"))', body)

    def test_success_and_failure_feedback(self):
        app = read(APP_JS)
        body = chunk(app, '$("#btn-export-pdf").addEventListener("click"',
                     "});\n\n$(\"#btn-save\")")
        self.assertIn('flash(t("msg.pdfExported", { path: r.path || "" }))', body)
        self.assertIn('flashError((r && r.error) || t("err.pdfUnavailable"))', body)
        self.assertIn('API.exportPdf()', body)

    def test_finally_restores_by_visibility(self):
        app = read(APP_JS)
        body = chunk(app, '$("#btn-export-pdf").addEventListener("click"',
                     "});\n\n$(\"#btn-save\")")
        finally_body = body[body.find("finally"):]
        self.assertIn("updatePdfButton();", finally_body)
        self.assertIn("btn.disabled = false", finally_body)

    def test_no_modal_added(self):
        app = read(APP_JS)
        body = chunk(app, '$("#btn-export-pdf").addEventListener("click"',
                     "});\n\n$(\"#btn-save\")")
        for forbidden in ("confirm(", "alert(", "<dialog"):
            self.assertNotIn(forbidden, body)


class PdfKeysTest(unittest.TestCase):
    """文案走已就绪的 locale 键（不新增键）。"""

    def test_all_pdf_keys_referenced(self):
        app = read(APP_JS)
        for key in ("msg.pdfExporting", "msg.pdfExported", "err.pdfUnavailable"):
            self.assertIn(f'"{key}"', app, key)
        html = read(INDEX)
        self.assertIn('data-i18n="btn.exportPdf"', html)


if __name__ == "__main__":
    unittest.main()

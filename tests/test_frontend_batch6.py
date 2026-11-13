# -*- coding: utf-8 -*-
"""tests/test_frontend_batch6.py — 第六批前端：E9/E10 输出 Word/Excel（后端生成 + base64 下载）

静态契约测试（不依赖 GUI / 不实际导出 Office 文件）：
  • 设置页输出格式新增 docx / xlsx（data-v + data-i18n），fillSettings 通用回填
  • bridge.exportOffice(fmt) → POST /api/export-office {fmt}，失败/ mock 口径
  • #btn-save 对 docx/xlsx 走后端：成功 base64ToBlob(MIME) → downloadBlob(name) → msg.officeExported
  • 失败 flashError(error 或 err.officeUnavailable)，不产空文件；md/txt/json 分支保持不变
"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "webview"
APP_JS = WEB / "js" / "app.js"
BRIDGE_JS = WEB / "js" / "bridge.js"
INDEX = WEB / "index.html"

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def chunk(text: str, start: str, end: str) -> str:
    i = text.find(start)
    assert i >= 0, f"未找到起点：{start}"
    j = text.find(end, i + len(start))
    assert j > i, f"未找到终点：{end}"
    return text[i:j]


def save_handler() -> str:
    return chunk(read(APP_JS), '$("#btn-save").addEventListener("click"',
                 '$("#btn-open-dir").addEventListener')


class OutputFormatOptionsTest(unittest.TestCase):
    """输出格式新增 Word / Excel。"""

    def test_docx_and_xlsx_buttons(self):
        html = read(INDEX)
        seg = chunk(html, 'id="set-outfmt"', "</div>")
        self.assertIn('data-v="docx"', seg)
        self.assertIn('data-i18n="set.outfmtDocx"', seg)
        self.assertIn('data-v="xlsx"', seg)
        self.assertIn('data-i18n="set.outfmtXlsx"', seg)
        # 与既有 md/txt/json 同款（seg 内按钮），不新起控件类型
        self.assertIn('data-v="md"', seg)
        self.assertIn('data-v="json"', seg)

    def test_fillsettings_toggles_by_data_v(self):
        app = read(APP_JS)
        body = chunk(app, "function fillSettings", "let saveTimer")
        self.assertIn('$$("#set-outfmt button")', body)
        self.assertIn("b.dataset.v === (s.outFormat || \"md\")", body)


class BridgeExportOfficeTest(unittest.TestCase):
    """bridge 端点与错误口径。"""

    def test_endpoint_and_payload(self):
        bridge = read(BRIDGE_JS)
        body = chunk(bridge, "async exportOffice(fmt)", "async startDownload()")
        self.assertIn('apiPost("/api/export-office", { fmt })', body)

    def test_failure_and_mock_paths(self):
        bridge = read(BRIDGE_JS)
        body = chunk(bridge, "async exportOffice(fmt)", "async startDownload()")
        self.assertIn("return { ok: false, error: String(e) };", body)
        self.assertIn('return { ok: false, error: "mock" };', body)


class Base64HelperTest(unittest.TestCase):
    """base64ToBlob：整块转换 + 正确 MIME。"""

    def test_base64_to_blob_uses_atob_and_uint8array(self):
        app = read(APP_JS)
        body = chunk(app, "function base64ToBlob", '$("#btn-save").addEventListener')
        self.assertIn("atob(clean)", body)
        self.assertIn("new Uint8Array(len)", body)
        self.assertIn("new Blob([bytes]", body)
        # 大文件性能：不能逐字符 push 到普通数组
        self.assertNotIn(".push(", body)

    def test_office_mime_types_exact(self):
        app = read(APP_JS)
        body = chunk(app, "const OFFICE_MIME", "function base64ToBlob")
        self.assertIn(DOCX_MIME, body)
        self.assertIn(XLSX_MIME, body)


class SaveOfficeBranchTest(unittest.TestCase):
    """保存按钮的 docx/xlsx 分支。"""

    def test_save_handler_is_async(self):
        app = read(APP_JS)
        self.assertIn('$("#btn-save").addEventListener("click", async () => {', app)

    def test_branch_uses_export_office(self):
        body = save_handler()
        self.assertIn('outFormat === "docx" || outFormat === "xlsx"', body)
        self.assertIn("API.exportOffice(outFormat)", body)
        # office 分支必须在 json 之前，且成功后直接 return
        self.assertLess(body.find('outFormat === "docx"'), body.find('outFormat === "json"'))

    def test_success_downloads_named_blob_and_flashes(self):
        body = save_handler()
        self.assertIn("downloadBlob(base64ToBlob(res.data_b64, mime), String(res.name))", body)
        self.assertIn('flash(t("msg.officeExported", { name: res.name }))', body)
        self.assertIn("OFFICE_MIME[outFormat]", body)

    def test_missing_payload_is_failure_not_empty_file(self):
        body = save_handler()
        self.assertIn("!res || !res.ok || !res.name || !res.data_b64", body)
        self.assertIn('flashError((res && res.error) || t("err.officeUnavailable"))', body)
        guard = body[body.find("!res || !res.ok"):]
        guard = guard[:guard.find("downloadBlob(")]
        self.assertIn("return;", guard, "失败必须先 return，不能继续生成空文件")

    def test_no_frontend_byte_construction(self):
        """docx/xlsx 必须走后端；前端不得自己拼 blocks 字节。"""
        body = save_handler()
        office = body[body.find('outFormat === "docx"'):body.find("const mime")]
        self.assertNotIn("r.blocks", office)
        self.assertNotIn("docx", office.replace('outFormat === "docx"', ""))


class ExistingBranchesTest(unittest.TestCase):
    """md/txt/json 既有行为不被破坏。"""

    def test_json_branch_kept(self):
        body = save_handler()
        self.assertIn("version: 1", body)
        self.assertIn("pages: r.blocks", body)
        self.assertIn('"application/json;charset=utf-8"', body)

    def test_md_txt_branch_kept(self):
        body = save_handler()
        self.assertIn('outFormat === "txt" ? "txt" : "md"', body)
        self.assertIn("mdToPlain(text)", body)
        self.assertIn('"text/markdown;charset=utf-8"', body)

    def test_save_has_no_batch_special_case(self):
        """批量/历史结果统一交给后端判错，前端不另拼字节。"""
        body = save_handler()
        self.assertNotIn("batchItems", body)
        self.assertIn("err.officeUnavailable", save_handler())


if __name__ == "__main__":
    unittest.main()

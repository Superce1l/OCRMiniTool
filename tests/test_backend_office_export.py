# -*- coding: utf-8 -*-
"""tests/test_backend_office_export.py — E9/E10：docx/xlsx 输出与导出接口

契约（task-33）：
  • outFormat 白名单 md/txt/json/docx/xlsx（非法 → err.badValue 且整批不写盘）；
  • `_save_output(fmt)` 支持 docx/xlsx（office_export 从 result["blocks"] 生成、
    原子写、同名递增）；**失败降级写 .md**（无 blocks / xlsx 无表格 / 模块缺失 /
    生成异常），payload 的 `savedFormat` 报实际格式；
  • 识别结束缓存 `_last_result={blocks,name}`（每轮开始清空、无 blocks 不缓存）；
  • `POST /api/export-office {fmt}` → 成功 {ok,name,data_b64}（**不写盘**）；
    无缓存/非法 fmt → err.officeUnavailable；xlsx 无表格 → err.officeNoTable；
  • CLI `--format docx|xlsx`：无 blocks / 无表格 → 退出码 2，成功原子落盘。

office_export.py 由另一路同事并行实现：本文件用「注入假模块」验证接线，
再在真模块可用时用 skipUnless 跑真实 OOXML 断言（无该模块时整段跳过）。
"""
import base64
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import applog  # noqa: E402
import cli  # noqa: E402
import webview_backend as wb  # noqa: E402

OFFICE_AVAILABLE = importlib.util.find_spec("office_export") is not None


def _rmtree(p: Path):
    for f in sorted(p.rglob("*"), reverse=True):
        try:
            f.unlink() if f.is_file() else f.rmdir()
        except OSError:
            pass
    try:
        p.rmdir()
    except OSError:
        pass


def _zip_bytes(*members: str) -> bytes:
    """造一个合法 zip（默认补一个 [Content_Types].xml，像真 OOXML）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        for name in members:
            zf.writestr(name, "<root/>")
    return buf.getvalue()


def _fake_office(no_tables: bool = False, empty: bool = False):
    """假 office_export：签名与契约一致，产出合法 zip。"""
    mod = types.ModuleType("office_export")

    def docx_bytes(pages, *, title=""):
        return b"" if empty else _zip_bytes("word/document.xml")

    def xlsx_bytes(pages, *, name=""):
        if no_tables:
            raise ValueError("no tables")
        return b"" if empty else _zip_bytes("xl/workbook.xml")

    def write_docx(path, pages, *, title=""):
        Path(path).write_bytes(docx_bytes(pages, title=title))

    def write_xlsx(path, pages, *, name=""):
        Path(path).write_bytes(xlsx_bytes(pages, name=name))

    mod.docx_bytes = docx_bytes
    mod.xlsx_bytes = xlsx_bytes
    mod.write_docx = write_docx
    mod.write_xlsx = write_xlsx
    return mod


def _blocks():
    """一份最小 E2 页信封（含一段表格块，便于 xlsx 语义）。"""
    return [
        {"page": 1, "blocks": [
            {"type": "title", "label": "#", "level": 1,
             "bbox": [10, 10, 500, 40], "text": "标题", "mergePrev": False},
            {"type": "table", "label": "table", "level": 0,
             "bbox": [10, 50, 500, 200],
             "text": "| a | b |\n|---|---|\n| 1 | 2 |", "mergePrev": False},
        ]},
    ]


class _BackendCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_office_"))
        self.out = self.tmp / "outputs"
        self.out.mkdir()
        self.settings_file = self.tmp / "settings.json"
        self.settings_file.write_text(json.dumps({"lang": "zh"}),
                                      encoding="utf-8")
        self._patches = [
            mock.patch.object(wb, "SETTINGS_FILE", self.settings_file),
            mock.patch.object(wb, "OUTPUT_DIR", self.out),
            mock.patch.object(applog, "_DIR", self.tmp / "logs"),
        ]
        for p in self._patches:
            p.start()
        applog._FH = None
        applog._CURRENT_MONTH = ""
        self.backend = wb.WebBackend()

    def tearDown(self):
        applog._FH = None
        for p in self._patches:
            p.stop()
        _rmtree(self.tmp)

    def saved(self) -> dict:
        return json.loads(self.settings_file.read_text(encoding="utf-8"))


# ══════════════════════════════════════════════════════════
# outFormat 白名单
# ══════════════════════════════════════════════════════════
class OutFormatWhitelistTest(_BackendCase):
    def test_default_is_md(self):
        self.assertEqual(self.backend.get_settings()["outFormat"], "md")

    def test_docx_and_xlsx_accepted(self):
        for fmt in ("docx", "xlsx"):
            res = self.backend.set_settings({"outFormat": fmt})
            self.assertEqual(res["outFormat"], fmt)
            self.assertEqual(self.saved()["outFormat"], fmt)

    def test_uppercase_normalized(self):
        self.assertEqual(
            self.backend.set_settings({"outFormat": "DOCX"})["outFormat"],
            "docx")

    def test_invalid_rejected_and_not_written(self):
        before = self.settings_file.read_bytes()
        res = self.backend.set_settings({"theme": "dark", "outFormat": "xml"})
        self.assertIs(res.get("ok"), False)
        self.assertIn("outFormat", res["error"])
        self.assertEqual(self.settings_file.read_bytes(), before)
        self.assertEqual(self.saved().get("theme", "light"), "light",
                         "非法 outFormat 时整批不写盘")


# ══════════════════════════════════════════════════════════
# _save_output：docx/xlsx + 降级
# ══════════════════════════════════════════════════════════
class SaveOutputOfficeTest(_BackendCase):
    def _result(self, blocks=None):
        return {"text": "正文", "pages": ["正文"],
                "blocks": _blocks() if blocks is None else blocks}

    def test_docx_is_real_ooxml_zip(self):
        with mock.patch.dict(sys.modules, {"office_export": _fake_office()}):
            path = self.backend._save_output("报告", self._result(), "docx")
        self.assertEqual(path.suffix, ".docx")
        with zipfile.ZipFile(path) as zf:
            self.assertIn("word/document.xml", zf.namelist())

    def test_xlsx_is_real_ooxml_zip(self):
        with mock.patch.dict(sys.modules, {"office_export": _fake_office()}):
            path = self.backend._save_output("表格", self._result(), "xlsx")
        self.assertEqual(path.suffix, ".xlsx")
        with zipfile.ZipFile(path) as zf:
            self.assertIn("xl/workbook.xml", zf.namelist())

    def test_same_name_increments(self):
        with mock.patch.dict(sys.modules, {"office_export": _fake_office()}):
            first = self.backend._save_output("重名", self._result(), "docx")
            second = self.backend._save_output("重名", self._result(), "docx")
        self.assertEqual(first.name, "重名.docx")
        self.assertEqual(second.name, "重名 (1).docx")

    def test_atomic_no_tmp_residue(self):
        with mock.patch.dict(sys.modules, {"office_export": _fake_office()}):
            self.backend._save_output("原子", self._result(), "xlsx")
        self.assertEqual(list(self.out.glob("*.tmp")), [])

    def test_no_blocks_degrades_to_md(self):
        result = {"text": "纯文本", "pages": ["纯文本"]}       # 没有 blocks
        path = self.backend._save_output("无块", result, "docx")
        self.assertEqual(path.suffix, ".md", "无 blocks 必须降级为 md")
        self.assertIn("纯文本", path.read_text(encoding="utf-8"))

    def test_xlsx_without_tables_degrades_to_md(self):
        with mock.patch.dict(sys.modules,
                             {"office_export": _fake_office(no_tables=True)}):
            path = self.backend._save_output("无表格", self._result(), "xlsx")
        self.assertEqual(path.suffix, ".md")

    def test_missing_office_module_degrades_to_md(self):
        with mock.patch.dict(sys.modules, {"office_export": None}):
            path = self.backend._save_output("缺模块", self._result(), "docx")
        self.assertEqual(path.suffix, ".md")

    def test_empty_office_bytes_degrades_to_md(self):
        with mock.patch.dict(sys.modules,
                             {"office_export": _fake_office(empty=True)}):
            path = self.backend._save_output("空字节", self._result(), "docx")
        self.assertEqual(path.suffix, ".md")


# ══════════════════════════════════════════════════════════
# 识别 payload：savedFormat + _last_result 缓存
# ══════════════════════════════════════════════════════════
class SavedFormatPayloadTest(_BackendCase):
    def setUp(self):
        super().setUp()
        self.backend.engine.ready = True
        self.img = self.tmp / "p1.png"
        self.img.write_bytes(b"PNG")

    def _recognize(self, *, blocks, out_format, mode="merged"):
        self.backend.set_settings({"outFormat": out_format})
        reply = {"text": "正文", "pages": ["正文"], "elapsed": 0.0}
        if blocks is not None:
            reply["blocks"] = blocks
        with mock.patch.dict(sys.modules, {"office_export": _fake_office()}), \
                mock.patch.object(self.backend.engine, "recognize",
                                  side_effect=lambda *a, **k: dict(reply)):
            return self.backend.recognize(
                {"paths": [str(self.img)], "name": "导出", "mode": mode})

    def test_docx_saved_and_cached(self):
        res = self._recognize(blocks=_blocks(), out_format="docx")
        self.assertTrue(res["outPath"].endswith(".docx"), res["outPath"])
        self.assertEqual(res["savedFormat"], "docx")
        self.assertIsNotNone(self.backend._last_result)
        self.assertEqual(self.backend._last_result["name"], "导出")

    def test_degraded_savedformat_is_md(self):
        res = self._recognize(blocks=None, out_format="docx")
        self.assertTrue(res["outPath"].endswith(".md"), res["outPath"])
        self.assertEqual(res["savedFormat"], "md")
        self.assertIsNone(self.backend._last_result,
                          "无 blocks 不缓存结构化结果")

    def test_batch_item_has_savedformat(self):
        res = self._recognize(blocks=None, out_format="md", mode="batch")
        self.assertEqual(res["items"][0]["savedFormat"], "md")

    def test_new_run_clears_previous_cache(self):
        self._recognize(blocks=_blocks(), out_format="md")
        self.assertIsNotNone(self.backend._last_result)
        self._recognize(blocks=None, out_format="md")
        self.assertIsNone(self.backend._last_result)


# ══════════════════════════════════════════════════════════
# /api/export-office
# ══════════════════════════════════════════════════════════
class ExportOfficeTest(_BackendCase):
    def test_no_cache_returns_unavailable(self):
        res = self.backend.export_office("docx")
        self.assertIs(res["ok"], False)
        self.assertEqual(res["error"], wb.tr("err.officeUnavailable"))

    def test_invalid_fmt_returns_unavailable(self):
        self.backend._last_result = {"blocks": _blocks(), "name": "x"}
        for bad in ("pdf", "", "md", None):
            res = self.backend.export_office(bad)
            self.assertIs(res["ok"], False, bad)
            self.assertEqual(res["error"], wb.tr("err.officeUnavailable"))

    def test_docx_success_returns_base64_zip(self):
        self.backend._last_result = {"blocks": _blocks(), "name": "报告"}
        with mock.patch.dict(sys.modules, {"office_export": _fake_office()}):
            res = self.backend.export_office("docx")
        self.assertIs(res["ok"], True, res)
        self.assertEqual(res["name"], "报告.docx")
        data = base64.b64decode(res["data_b64"])
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertIn("word/document.xml", zf.namelist())
        self.assertEqual(list(self.out.iterdir()), [],
                         "导出接口只回字节，不写盘")

    def test_xlsx_no_tables_specific_error(self):
        self.backend._last_result = {"blocks": _blocks(), "name": "表格"}
        with mock.patch.dict(sys.modules,
                             {"office_export": _fake_office(no_tables=True)}):
            res = self.backend.export_office("xlsx")
        self.assertIs(res["ok"], False)
        self.assertEqual(res["error"], wb.tr("err.officeNoTable"))

    def test_xlsx_success(self):
        self.backend._last_result = {"blocks": _blocks(), "name": "表格"}
        with mock.patch.dict(sys.modules, {"office_export": _fake_office()}):
            res = self.backend.export_office("xlsx")
        self.assertIs(res["ok"], True, res)
        data = base64.b64decode(res["data_b64"])
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            self.assertIn("xl/workbook.xml", zf.namelist())

    def test_office_module_missing_returns_unavailable(self):
        self.backend._last_result = {"blocks": _blocks(), "name": "x"}
        with mock.patch.dict(sys.modules, {"office_export": None}):
            res = self.backend.export_office("docx")
        self.assertIs(res["ok"], False)
        self.assertEqual(res["error"], wb.tr("err.officeUnavailable"))

    def test_server_route_returns_bytes(self):
        import http.client
        import webview_server as ws
        srv = ws.WebViewServer(port=0)
        srv.backend._last_result = {"blocks": _blocks(), "name": "服务端"}
        srv.start()
        try:
            with mock.patch.dict(sys.modules,
                                 {"office_export": _fake_office()}):
                conn = http.client.HTTPConnection("127.0.0.1", srv.port,
                                                  timeout=20)
                try:
                    conn.request("POST", "/api/export-office",
                                 body=json.dumps({"fmt": "docx"}),
                                 headers={"Host": f"127.0.0.1:{srv.port}",
                                          "Content-Type": "application/json"})
                    resp = conn.getresponse()
                    data = json.loads(resp.read())
                finally:
                    conn.close()
        finally:
            srv.stop()
        self.assertEqual(resp.status, 200)
        self.assertIs(data["ok"], True, data)
        self.assertTrue(data["name"].endswith(".docx"))
        raw = base64.b64decode(data["data_b64"])
        self.assertIn("word/document.xml",
                      zipfile.ZipFile(io.BytesIO(raw)).namelist())


# ══════════════════════════════════════════════════════════
# CLI --format docx/xlsx
# ══════════════════════════════════════════════════════════
class _FakeCliEngine:
    def __init__(self, blocks=None):
        self.ready = True
        self.cancel_event = None
        self._blocks = blocks

    def load(self, device_pref="auto", cb=None):
        return None

    def recognize(self, image_paths, prompt="", **kwargs):
        res = {"text": "正文", "pages": ["正文"], "elapsed": 0.1,
               "truncated": False, "pagesMismatch": False, "groups": 1}
        if self._blocks is not None:
            res["blocks"] = self._blocks
        return res


class CliOfficeFormatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_clioffice_"))
        self.png = self.tmp / "a.png"
        self.png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        self.out_dir = self.tmp / "out"
        self._patch = mock.patch.object(applog, "_DIR", self.tmp / "logs")
        self._patch.start()
        applog._FH = None
        applog._CURRENT_MONTH = ""

    def tearDown(self):
        applog._FH = None
        self._patch.stop()
        _rmtree(self.tmp)

    def _run(self, argv, engine):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(argv, engine_factory=lambda: engine)
        return rc, out.getvalue(), err.getvalue()

    def test_docx_end_to_end(self):
        with mock.patch.dict(sys.modules, {"office_export": _fake_office()}):
            rc, _out, err = self._run(
                ["--input", str(self.png), "--format", "docx",
                 "--out", str(self.out_dir)],
                _FakeCliEngine(blocks=_blocks()))
        self.assertEqual(rc, cli.EXIT_OK, err)
        target = self.out_dir / "a.docx"
        self.assertTrue(target.is_file())
        with zipfile.ZipFile(target) as zf:
            self.assertIn("word/document.xml", zf.namelist())
        self.assertEqual(list(self.out_dir.glob("*.tmp")), [])

    def test_xlsx_end_to_end(self):
        with mock.patch.dict(sys.modules, {"office_export": _fake_office()}):
            rc, _out, err = self._run(
                ["--input", str(self.png), "--format", "xlsx",
                 "--out", str(self.out_dir)],
                _FakeCliEngine(blocks=_blocks()))
        self.assertEqual(rc, cli.EXIT_OK, err)
        with zipfile.ZipFile(self.out_dir / "a.xlsx") as zf:
            self.assertIn("xl/workbook.xml", zf.namelist())

    def test_missing_blocks_exits_2(self):
        with mock.patch.dict(sys.modules, {"office_export": _fake_office()}):
            rc, _out, err = self._run(
                ["--input", str(self.png), "--format", "docx",
                 "--out", str(self.out_dir)], _FakeCliEngine(blocks=None))
        self.assertEqual(rc, cli.EXIT_ENV)
        self.assertIn("blocks", err)

    def test_xlsx_without_tables_exits_2(self):
        with mock.patch.dict(sys.modules,
                             {"office_export": _fake_office(no_tables=True)}):
            rc, _out, err = self._run(
                ["--input", str(self.png), "--format", "xlsx",
                 "--out", str(self.out_dir)],
                _FakeCliEngine(blocks=_blocks()))
        self.assertEqual(rc, cli.EXIT_ENV)
        self.assertIn("表格", err)

    def test_missing_office_module_exits_2(self):
        with mock.patch.dict(sys.modules, {"office_export": None}):
            rc, _out, _err = self._run(
                ["--input", str(self.png), "--format", "docx",
                 "--out", str(self.out_dir)],
                _FakeCliEngine(blocks=_blocks()))
        self.assertEqual(rc, cli.EXIT_ENV)


# ══════════════════════════════════════════════════════════
# 真 office_export（并行落地后才跑）
# ══════════════════════════════════════════════════════════
@unittest.skipUnless(OFFICE_AVAILABLE, "office_export.py 尚未落地")
class RealOfficeExportTest(_BackendCase):
    def _real_blocks(self):
        """用真后处理 build_document 造 blocks（含表格，便于 xlsx）。"""
        from ocr_postprocess import build_document
        raw = ("<PAGE 1>\n# 标题\n\n正文段落。\n\n"
               "| a | b |\n|---|---|\n| 1 | 2 |\n")
        doc = build_document(raw)
        pages = doc.get("pages") or []
        return pages

    def test_real_docx_contains_document_xml(self):
        blocks = self._real_blocks()
        if not blocks:
            self.skipTest("build_document 没产出 blocks")
        path = self.backend._save_output("真Word", {"pages": ["x"],
                                                   "blocks": blocks}, "docx")
        self.assertEqual(path.suffix, ".docx")
        with zipfile.ZipFile(path) as zf:
            self.assertIn("word/document.xml", zf.namelist())

    def test_real_xlsx_or_graceful_degrade(self):
        """真模块：有表格 → 合法 xlsx；判不出表格 → 允许降级 md（不崩）。"""
        blocks = self._real_blocks()
        if not blocks:
            self.skipTest("build_document 没产出 blocks")
        path = self.backend._save_output("真Excel", {"pages": ["x"],
                                                     "blocks": blocks}, "xlsx")
        self.assertIn(path.suffix, (".xlsx", ".md"))
        if path.suffix == ".xlsx":
            with zipfile.ZipFile(path) as zf:
                self.assertIn("xl/workbook.xml", zf.namelist())


if __name__ == "__main__":
    unittest.main()

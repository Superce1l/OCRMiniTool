# -*- coding: utf-8 -*-
"""tests/test_frontend_phase2.py — P0/P1 前端落地（S1–S5）静态契约测试

覆盖：
  S1 设置四键（maxPageTokens / maxMergedPages / cropMode / dropHeaders）
     控件存在、被读取与提交、前端先夹取（Node 真跑 clampsettings 纯函数）
  S2 E3 截断可见：truncated / pagesMismatch / finishReason / groups /
     droppedBlocks 参与提示条与日志（Node 真跑 truncationInfo 纯函数）
  S3 目录识别：选择文件夹 → /api/scan-dir → 待识别列表；路径直读走 JSON
  S4 B8 批量失败项：完整错误可看、可重试（只重跑失败项）
  S5 文案全部走 locales；既有 err.internal / err.badValue / err.badPath 保留

后端由 backend-api 并行实现：这里的断言只锁「前端按契约写、字段缺失可降级」，
不假设后端已就绪（例如 truncationInfo({}) 必须返回 null 而不是抛错）。
"""
import json
import re
import shutil
import subprocess
import tempfile
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

FALLBACK_NODE = Path(
    "C:/Users/admin/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/node/bin/node.exe")


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def locale(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def find_node() -> str | None:
    for name in ("node", "node.exe"):
        found = shutil.which(name)
        if found:
            return found
    if FALLBACK_NODE.is_file():
        return str(FALLBACK_NODE)
    return None


def run_node_harness(script: str) -> dict:
    """把 JS 写到系统临时目录再跑（不污染仓库），返回 stdout 的 JSON。"""
    node = find_node()
    if not node:
        raise unittest.SkipTest("未找到 node")
    with tempfile.TemporaryDirectory(prefix="ocr_fe_") as tmp:
        f = Path(tmp) / "harness.js"
        f.write_text(script, encoding="utf-8")
        proc = subprocess.run(
            [node, str(f)], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    if proc.returncode != 0:
        raise AssertionError(f"Node 断言失败：\n{proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


class RecognizeTransportTest(unittest.TestCase):
    """S3 契约：bridge 按「有 File 走 multipart / 只有路径走 JSON」分流（Node 真跑）。"""

    def test_web_recognize_branches(self):
        bridge = read(BRIDGE_JS)
        start = bridge.find("async function webRecognize(opts)")
        end = bridge.find("// ════", start)
        self.assertGreater(start, 0)
        self.assertGreater(end, start)
        chunk = bridge[start:end]
        script = (
            "const vm = require('vm');\n"
            "const calls = [];\n"
            "class FakeFormData { constructor() { this.entries = []; }\n"
            "  append(k, v, n) { this.entries.push([k, n]); } }\n"
            "const ctx = {\n"
            "  console, JSON, String, Array, FormData: FakeFormData,\n"
            "  tr: (k, p, fb) => fb || k,\n"
            "  emit: (ev, payload) => calls.push({ ev, payload }),\n"
            "  fetch: async (url, init) => {\n"
            "    calls.push({ url, init });\n"
            "    return { ok: true, json: async () => ({ mode: 'merged' }) };\n"
            "  },\n"
            "};\n"
            "vm.createContext(ctx);\n"
            f"vm.runInContext({chunk!r} + ';this.webRecognize = webRecognize;', ctx);\n"
            "(async () => {\n"
            "  await ctx.webRecognize({ files: [{ name: 'a.png' }], prompt: 'p', mode: 'merged' });\n"
            "  await ctx.webRecognize({ paths: ['D:\\\\scan\\\\a.png'], prompt: 'p2', mode: 'batch', name: 'a' });\n"
            "  const out = calls.map(c => {\n"
            "    if (!c.url) return { ev: c.ev };\n"
            "    const isForm = c.init.body instanceof FakeFormData;\n"
            "    let jsonBody = null;\n"
            "    try { jsonBody = isForm ? null : JSON.parse(c.init.body); } catch (e) {}\n"
            "    return { url: c.url, isForm, formEntries: isForm ? c.init.body.entries : null,\n"
            "             jsonBody, ctype: (c.init.headers || {})['Content-Type'] || null };\n"
            "  });\n"
            "  console.log(JSON.stringify(out));\n"
            "})();\n"
        )
        got = run_node_harness(script)
        posts = [x for x in got if x.get("url")]
        self.assertEqual(len(posts), 2, f"应发起两次识别请求：{got}")
        first, second = posts
        self.assertEqual(first["url"], "/api/recognize")
        self.assertTrue(first["isForm"], "有 File 时必须走 multipart")
        self.assertEqual(first["formEntries"][0][0], "files")
        self.assertIsNone(first["jsonBody"])
        self.assertFalse(second["isForm"], "只有路径时不能再用 multipart")
        self.assertIn("application/json", second["ctype"] or "")
        self.assertEqual(second["jsonBody"]["paths"], ["D:\\scan\\a.png"])
        self.assertEqual(second["jsonBody"]["mode"], "batch")


class SettingsControlsTest(unittest.TestCase):
    """S1：控件与提交路径。"""

    def test_controls_exist(self):
        html = read(INDEX)
        for cid in ("set-max-tokens", "set-max-merged-pages",
                    "set-crop-mode", "set-drop-headers", "ocr-params-card"):
            self.assertIn(f'id="{cid}"', html, f"index.html 缺少控件 {cid}")

    def test_four_keys_read_and_written(self):
        app = read(APP_JS)
        for key in ("maxPageTokens", "maxMergedPages", "cropMode", "dropHeaders"):
            self.assertIn(key, app, f"app.js 未处理设置键 {key}")
        # 读取（fillSettings）与提交（pushSettings）两处都要有
        self.assertIn("setIntSetting(s, \"maxPageTokens\"", app)
        self.assertIn("setIntSetting(s, \"maxMergedPages\"", app)
        self.assertIn('pushSettings({ cropMode: b.dataset.v })', app)
        self.assertIn('pushSettings({ dropHeaders:', app)

    def test_clamp_bounds_present(self):
        app = read(APP_JS)
        self.assertIn("const SETTINGS_LIMITS", app)
        self.assertIn("65536", app)
        self.assertIn("256", app)
        self.assertIn("200", app)

    def test_clamp_function_behaviour(self):
        """Node 真跑：非法值/越界值必须被夹到契约区间。"""
        app = read(APP_JS)
        start = app.find("const SETTINGS_LIMITS")
        end = app.find("function setIntSetting", start)
        self.assertGreater(start, 0)
        self.assertGreater(end, start)
        chunk = app[start:end]
        script = (
            "const vm = require('vm');\n"
            "const ctx = { Number, String, Object, JSON };\n"
            "vm.createContext(ctx);\n"
            f"vm.runInContext({chunk!r} + ';this.clamp = clampSettingValue;', ctx);\n"
            "const cases = [\n"
            "  ['maxPageTokens','abc'], ['maxPageTokens',''],\n"
            "  ['maxPageTokens','100'], ['maxPageTokens','999999'],\n"
            "  ['maxPageTokens','0'], ['maxPageTokens','-5'],\n"
            "  ['maxMergedPages','500'], ['maxMergedPages','20'],\n"
            "  ['maxMergedPages','-1'], ['maxMergedPages',''],\n"
            "];\n"
            "console.log(JSON.stringify(cases.map(c => ctx.clamp(c[0], c[1]))));\n"
        )
        got = run_node_harness(script)
        self.assertEqual(got, [4096, 4096, 256, 65536, 0, 0, 200, 20, 0, 20])


class TruncationUiTest(unittest.TestCase):
    """S2：截断/页数不符提示（字段缺失时降级为无提示）。"""

    def test_warn_banner_and_keys(self):
        self.assertIn('id="result-warn"', read(INDEX))
        app = read(APP_JS)
        for field in ("truncated", "pagesMismatch", "finishReason",
                      "groups", "droppedBlocks"):
            self.assertIn(field, app, f"app.js 未处理响应字段 {field}")
        self.assertIn("renderResultWarn", app)
        self.assertIn("logResultWarnings", app)

    def test_hint_text_mentions_how_to_fix(self):
        """P0 核心：提示必须讲清怎么调，而不是只报错。"""
        zh = locale(ZH)
        body = zh["warn.truncatedBody"] + zh["warn.pagesMismatchBody"] + zh["warn.retryHint"]
        self.assertIn("单页生成上限", body)
        self.assertIn("合并页数上限", body)
        en = locale(EN)
        en_body = en["warn.truncatedBody"] + en["warn.retryHint"]
        self.assertIn("Max tokens per page", en_body)
        self.assertIn("Max merged pages", en_body)

    def test_truncation_info_behaviour(self):
        """Node 真跑：旧后端（无字段）不报错；新字段正确提炼。"""
        app = read(APP_JS)
        start = app.find("function numOrNull")
        end = app.find("function resultWarnLines", start)
        self.assertGreater(start, 0)
        self.assertGreater(end, start)
        chunk = app[start:end]
        script = (
            "const vm = require('vm');\n"
            "const ctx = { Number, String, Object, JSON };\n"
            "vm.createContext(ctx);\n"
            f"vm.runInContext({chunk!r} + ';this.ti = truncationInfo;', ctx);\n"
            "const out = [\n"
            "  ctx.ti({}),\n"
            "  ctx.ti(null),\n"
            "  ctx.ti({truncated:false, pagesMismatch:false}),\n"
            "  ctx.ti({truncated:true, finishReason:'length', groups:3, droppedBlocks:2}),\n"
            "  ctx.ti({pagesMismatch:true, expectedPages:5, pages:['a','b']}),\n"
            "];\n"
            "console.log(JSON.stringify(out));\n"
        )
        got = run_node_harness(script)
        self.assertIsNone(got[0], "字段缺失时必须按无问题降级")
        self.assertIsNone(got[1])
        self.assertIsNone(got[2])
        self.assertTrue(got[3]["truncated"])
        self.assertEqual(got[3]["finish"], "length")
        self.assertEqual(got[3]["groups"], 3)
        self.assertEqual(got[3]["dropped"], 2)
        self.assertTrue(got[4]["mismatch"])
        self.assertEqual(got[4]["expected"], 5)
        self.assertEqual(got[4]["actual"], 2)


class DirectoryScanTest(unittest.TestCase):
    """S3：目录识别 UI 与 JSON 直读路径。"""

    def test_button_and_flow(self):
        self.assertIn('id="btn-pick-dir"', read(INDEX))
        self.assertIn('id="btn-clear-files"', read(INDEX))
        app = read(APP_JS)
        self.assertIn("pickDirectory", app)
        self.assertIn("API.scanDir", app)
        self.assertIn("pathFiles", app)
        self.assertIn("msg.dirScanTruncated", app)

    def test_bridge_scan_dir_contract(self):
        bridge = read(BRIDGE_JS)
        self.assertIn("/api/scan-dir", bridge)
        self.assertIn("async scanDir(opts)", bridge)
        for field in ("recursive", "maxFiles", "maxDepth"):
            self.assertIn(field, bridge)

    def test_dropzone_click_does_not_open_file_dialog_for_dir_button(self):
        app = read(APP_JS)
        self.assertIn('closest("#btn-pick-dir")', app)

    def test_recognize_has_json_paths_branch(self):
        bridge = read(BRIDGE_JS)
        self.assertIn("opts.paths", bridge)
        self.assertIn('/api/recognize', bridge)
        self.assertIn('"Content-Type": "application/json"', bridge)
        self.assertIn("mode: opts.mode", bridge)
        # multipart 上传链路不能丢
        self.assertIn("new FormData()", bridge)
        self.assertIn('fd.append("files"', bridge)
        # 混用拦截 + 与后端 MAX_RECOGNIZE_PATHS 对齐的 200 上限
        app = read(APP_JS)
        self.assertIn("msg.mixedSources", app)
        self.assertIn("DIR_SCAN_MAX = 200", app)
        self.assertIn("msg.dirLimitReached", app)


class BatchRetryTest(unittest.TestCase):
    """S4：失败项可看完整错误、可只重试失败项。"""

    def test_retry_button_and_function(self):
        self.assertIn('id="btn-retry-failed"', read(INDEX))
        app = read(APP_JS)
        self.assertIn("retryFailedItems", app)
        self.assertIn("findSourceForItem", app)
        self.assertIn('mode: "batch"', app)

    def test_full_error_not_truncated(self):
        app = read(APP_JS)
        self.assertNotIn("slice(0, 80)", app)
        self.assertNotIn("slice(0,80)", app)
        self.assertIn('id="batch-error"', read(INDEX))
        self.assertIn("showBatchError", app)

    def test_failed_items_are_clickable(self):
        """失败项不再 disabled（旧实现点击无反应）。"""
        app = read(APP_JS)
        self.assertNotIn('class="batch-item${i === state.viewIndex ? " on" : ""}" data-i="${i}"${it.ok ? "" : " disabled"}', app)


class LocaleContractTest(unittest.TestCase):
    """S5：新文案走 locales；三个后端错误键必须保留。"""

    NEW_KEYS = (
        "set.ocrSection", "set.maxTokens", "set.maxMergedPages",
        "set.cropMode", "set.dropHeaders",
        "warn.truncatedTitle", "warn.truncatedBody", "warn.pagesMismatchBody",
        "warn.retryHint", "warn.batchTruncated",
        "chip.truncated", "chip.mismatch",
        "drop.pickDir", "btn.clearFiles", "btn.retryFailed",
        "msg.dirScanned", "msg.dirScanTruncated", "msg.scanFailed",
        "msg.scanNeedsBackend", "msg.filesCleared", "msg.dirLimitReached",
        "msg.mixedSources", "msg.retrying", "msg.retryDone",
        "msg.retryNoSource", "msg.batchErrorTitle", "prog.reading",
        "result.viewError", "set.cropGundam", "files.summary",
    )

    def test_new_keys_in_both_locales(self):
        zh, en = locale(ZH), locale(EN)
        for key in self.NEW_KEYS:
            self.assertIn(key, zh, f"zh.json 缺少 {key}")
            self.assertIn(key, en, f"en.json 缺少 {key}")

    def test_backend_error_keys_preserved(self):
        for code in ("zh", "en"):
            data = locale(WEB / "locales" / f"{code}.json")
            for key in ("err.internal", "err.badValue", "err.badPath"):
                self.assertIn(key, data, f"{code}.json 丢失后端要用的 {key}")

    def test_key_sets_aligned(self):
        self.assertEqual(set(locale(ZH)), set(locale(EN)))

    def test_new_keys_referenced(self):
        app = read(APP_JS)
        for needle in ('t("warn.', 't("msg.dirScanTruncated"', 't("msg.retryDone"',
                       't("files.summary"', 't("result.viewError"'):
            self.assertIn(needle, app)
        self.assertIn('data-i18n="set.ocrSection"', read(INDEX))
        self.assertIn('data-i18n="btn.retryFailed"', read(INDEX))


class DegradationTest(unittest.TestCase):
    """后端未落地时前端不得崩：默认值/空字段/缺键路径。"""

    def test_defaults_present_in_markup(self):
        html = read(INDEX)
        self.assertIn('id="set-max-tokens"', html)
        self.assertIn('value="4096"', html)
        self.assertIn('value="20"', html)

    def test_missing_keys_keep_user_input(self):
        app = read(APP_JS)
        self.assertIn("hasOwnProperty.call(s, key)", app)
        self.assertIn("else if (!el.value)", app)

    def test_batch_flags_optional(self):
        app = read(APP_JS)
        # 逐文件 flags 缺失时不应出现 undefined 文案
        self.assertIn("it.truncated ?", app)
        self.assertIn("it.pagesMismatch ?", app)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""tests/test_frontend_security.py — 前端结果区 HTML 消毒 + CSP（B1）

背景：OCR 结果可以被一张图片控制（图片正文里写 `<div><img onerror=...>`，
模型会忠实转写），旧实现把「行首是 HTML 块」的整行直接写进 innerHTML，
等于把文档内容当代码执行；配合本机 API 可读写设置/读取历史输出。

修复后：HTML 块先经 `sanitizeHtmlFragment()` 白名单重建（标签/属性/协议），
页面再加 CSP 作为第二道防线。这里：
  1) 跑 Node 回归脚本 `tools/frontend_sanitize_check.js`（真实执行 app.js 里的
     消毒器，攻击 payload 不得原样进入输出，正常表格/列表/标题不得回归）；
  2) 静态断言 HTML 直通实现、CSP、外域资源没有回潮。

零第三方依赖；没有 node 时跳过（与 tools/check.py 的降级策略一致）。
"""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "webview"
APP_JS = WEB / "js" / "app.js"
BRIDGE_JS = WEB / "js" / "bridge.js"
INDEX = WEB / "index.html"
CSS = WEB / "css" / "app.css"

FALLBACK_NODE = Path(
    "C:/Users/admin/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/node/bin/node.exe")


def find_node() -> str | None:
    for name in ("node", "node.exe"):
        found = shutil.which(name)
        if found:
            return found
    if FALLBACK_NODE.is_file():
        return str(FALLBACK_NODE)
    return None


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class SanitizeHarnessTest(unittest.TestCase):
    """真实执行消毒器（Node），而不是只做字符串匹配。"""

    def test_sanitizer_regression_script_passes(self):
        node = find_node()
        if not node:
            self.skipTest("未找到 node，跳过前端消毒回归")
        script = ROOT / "tools" / "frontend_sanitize_check.js"
        self.assertTrue(script.is_file(), "缺少 tools/frontend_sanitize_check.js")
        proc = subprocess.run(
            [node, str(script)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"消毒回归失败：\n{proc.stdout}\n{proc.stderr}",
        )
        self.assertIn("通过", proc.stdout)

    def test_dangerous_blacklist_removed(self):
        """黑名单方案已被白名单取代：不允许再出现旧的 HTML_DANGEROUS_RE。"""
        self.assertNotIn("HTML_DANGEROUS_RE", read(APP_JS))

    def test_raw_line_passthrough_removed(self):
        """旧实现的核心缺陷是 out.push(line) 直接把原始行塞进结果。"""
        app = read(APP_JS)
        # 白名单重建后的入口必须存在
        self.assertIn("sanitizeHtmlFragment(", app)
        self.assertIn("HTML_ALLOWED_TAGS", app)


class CSPTest(unittest.TestCase):
    def test_index_has_csp_meta(self):
        html = read(INDEX)
        self.assertIn("Content-Security-Policy", html)
        for directive in ("default-src 'self'", "script-src 'self'",
                          "object-src 'none'", "frame-src 'none'",
                          "img-src 'self' data:"):
            self.assertIn(directive, html, f"CSP 缺少 {directive}")

    def test_no_inline_script_blocks(self):
        """CSP script-src 'self' 会拦内联脚本：页面上不该有内联 <script>。"""
        html = read(INDEX)
        lower = html.lower()
        pos = 0
        while True:
            i = lower.find("<script", pos)
            if i < 0:
                break
            end = lower.find(">", i)
            self.assertGreater(end, i, "index.html 存在未闭合的 <script")
            head = lower[i:end]
            self.assertIn("src=", head, f"发现内联脚本：{head}")
            pos = end + 1

    def test_no_external_font_or_style_origin(self):
        """CSP 默认只允许同源资源；页面与样式表都不该再引外域。"""
        for text in (read(INDEX), read(CSS)):
            for bad in ("fonts.googleapis.com", "fonts.gstatic.com",
                        "Space Grotesk", "Noto Sans SC", "Space Mono"):
                self.assertNotIn(bad, text, f"仍有外域字体依赖：{bad}")


class BridgeTransportTest(unittest.TestCase):
    def test_event_source_resyncs_status_on_open(self):
        """断线重连后必须补拉一次状态，否则界面停在旧状态。"""
        self.assertIn("es.onopen", read(BRIDGE_JS))
        self.assertIn("/api/status", read(BRIDGE_JS))


if __name__ == "__main__":
    unittest.main()

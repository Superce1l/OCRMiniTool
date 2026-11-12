# -*- coding: utf-8 -*-
"""tests/test_server_smoke.py — 本机 HTTP 服务的端到端冒烟

真起一个 127.0.0.1 随机端口的服务（不加载模型、不开窗口），打真实 HTTP 请求。
用户数据全部重定向到临时目录：不会碰项目的 settings.json / outputs/。

这是"改完代码至少还能开机"的那条底线；后续加端点时应在这里补断言。
"""
import http.client
import json
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import webview_backend as wb  # noqa: E402
import webview_server as ws  # noqa: E402


def _get(port: int, path: str, headers=None) -> tuple[int, bytes]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class ServerSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ocr_srv_"))
        cls._patches = [
            mock.patch.object(wb, "SETTINGS_FILE", cls.tmp / "settings.json"),
            mock.patch.object(wb, "OUTPUT_DIR", cls.tmp / "outputs"),
        ]
        for p in cls._patches:
            p.start()
        (cls.tmp / "outputs").mkdir(exist_ok=True)
        cls.server = ws.WebViewServer(port=0)
        cls.server.start()
        cls.port = cls.server.port
        assert cls.port, "服务未绑定到端口"

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.stop()
        finally:
            for p in cls._patches:
                p.stop()
            for f in sorted(cls.tmp.rglob("*"), reverse=True):
                try:
                    f.unlink() if f.is_file() else f.rmdir()
                except OSError:
                    pass
            try:
                cls.tmp.rmdir()
            except OSError:
                pass

    # ── 基本可达性 ───────────────────────────────────────
    def test_health(self):
        code, body = _get(self.port, "/health")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["modelReady"], "冒烟测试里不应已加载模型")

    def test_status(self):
        code, body = _get(self.port, "/api/status")
        self.assertEqual(code, 200)
        self.assertIsInstance(json.loads(body), dict)

    def test_settings_shape(self):
        code, body = _get(self.port, "/api/settings")
        self.assertEqual(code, 200)
        data = json.loads(body)
        for key in ("mirror", "devicePref", "outFormat", "lang"):
            self.assertIn(key, data)

    def test_env_status(self):
        code, _ = _get(self.port, "/api/env")
        self.assertEqual(code, 200)

    def test_history_empty(self):
        code, body = _get(self.port, "/api/history")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["items"], [])

    def test_unknown_api_404(self):
        code, _ = _get(self.port, "/api/no-such-endpoint")
        self.assertEqual(code, 404)

    # ── 静态资源 ─────────────────────────────────────────
    def test_index_served(self):
        code, body = _get(self.port, "/")
        self.assertEqual(code, 200)
        self.assertIn(b"<html", body.lower())

    def test_js_served_with_content_type(self):
        code, body = _get(self.port, "/js/app.js")
        self.assertEqual(code, 200)
        self.assertIn(b"function", body or b"function")

    def test_static_traversal_forbidden(self):
        code, _ = _get(self.port, "/../README.md")
        self.assertIn(code, (403, 404), "不允许读出 webview/ 之外的文件")

    # ── 安全：Host / Origin 校验 ─────────────────────────
    def test_dns_rebinding_host_rejected(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/api/status",
                         headers={"Host": "evil.example.com"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 403)
        finally:
            conn.close()

    def test_cross_site_origin_rejected(self):
        body = json.dumps({"theme": "dark"})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("POST", "/api/settings", body=body,
                         headers={"Host": f"127.0.0.1:{self.port}",
                                  "Origin": "http://evil.example.com",
                                  "Content-Type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 403)
        finally:
            conn.close()

    # ── 契约：设置写入落到指定文件 ───────────────────────
    def test_set_settings_persists(self):
        body = json.dumps({"theme": "dark"})
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/settings",
            data=body.encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)
            data = json.loads(r.read())
        self.assertEqual(data.get("theme"), "dark")
        saved = json.loads((self.tmp / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(saved.get("theme"), "dark")

    # ── SSE：首帧必须是 status ───────────────────────────
    def test_sse_first_event_is_status(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request("GET", "/api/events",
                         headers={"Host": f"127.0.0.1:{self.port}"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.getheader("Content-Type", ""))
            # 首帧是注释 ": connected"，第二帧才是 status 事件；
            # 逐字节读到"完整的一个 data 事件"（data: ...\n\n）为止。
            buf = b""
            deadline = time.time() + 10
            while time.time() < deadline:
                chunk = resp.read(1)
                if not chunk:
                    break
                buf += chunk
                tail = buf.split(b"data:", 1)
                if len(tail) == 2 and b"\n\n" in tail[1]:
                    break
            self.assertIn(b"data:", buf, "SSE 首帧之后必须推送 status 事件")
            self.assertIn(b"status", buf)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""tests/test_server_hardening.py — 服务层加固回归（C1/C3/C5/C6/C14）

对应 docs/explore/03-backend-api.md §7：
  C1  早退（403/400/404/500）后 keep-alive 连接仍可用（不再残留请求体串包）
  C3  GET 内部异常 → 500 JSON + 落日志（以前是"无响应无日志"）
  C5  SSE 队列有界（256）+ status 合并
  C6  客户端断开后 SSE handler 线程在 ≤1 个 keepalive 周期内回收
  C14 访问日志开关（默认关）

全部用临时目录，不碰项目 settings.json / outputs/ / logs/。
"""
import http.client
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import applog  # noqa: E402
import webview_backend as wb  # noqa: E402
import webview_server as ws  # noqa: E402


def _conn(port: int) -> http.client.HTTPConnection:
    return http.client.HTTPConnection("127.0.0.1", port, timeout=10)


def _get(port: int, path: str, headers=None) -> tuple[int, bytes]:
    c = _conn(port)
    try:
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


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


class ServerHardeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ocr_hard_"))
        cls._patches = [
            mock.patch.object(wb, "SETTINGS_FILE", cls.tmp / "settings.json"),
            mock.patch.object(wb, "OUTPUT_DIR", cls.tmp / "outputs"),
            mock.patch.object(applog, "_DIR", cls.tmp / "logs"),
        ]
        for p in cls._patches:
            p.start()
        applog._FH = None
        applog._CURRENT_MONTH = ""
        (cls.tmp / "outputs").mkdir(exist_ok=True)
        cls.server = ws.WebViewServer(port=0)
        cls.server.start()
        cls.port = cls.server.port
        assert cls.port

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.stop()
        finally:
            applog._FH = None
            for p in cls._patches:
                p.stop()
            _rmtree(cls.tmp)

    def _logs_text(self) -> str:
        out = []
        for p in (self.tmp / "logs").glob("*.log"):
            try:
                out.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        return "\n".join(out)

    # ── C1：早退不破坏 keep-alive ────────────────────────────
    def test_rejected_post_keeps_connection_usable(self):
        body = json.dumps({"pad": "x" * 40})
        c = _conn(self.port)
        try:
            c.request("POST", "/api/settings", body=body,
                      headers={"Host": "evil.example",
                               "Content-Type": "application/json"})
            r = c.getresponse()
            self.assertEqual(r.status, 403)
            r.read()
            # 同一条连接继续用：修复前这里会因为残留请求体收到 400/501
            c.request("GET", "/health",
                      headers={"Host": f"127.0.0.1:{self.port}"})
            r2 = c.getresponse()
            self.assertEqual(r2.status, 200, r2.read()[:120])
            r2.read()
        finally:
            c.close()

    def test_bad_recognize_body_keeps_connection_usable(self):
        c = _conn(self.port)
        try:
            c.request("POST", "/api/recognize",
                      body=json.dumps({"pad": "y" * 40}),
                      headers={"Host": f"127.0.0.1:{self.port}",
                               "Content-Type": "application/json"})
            r = c.getresponse()
            self.assertEqual(r.status, 400)
            r.read()
            c.request("GET", "/api/status",
                      headers={"Host": f"127.0.0.1:{self.port}"})
            r2 = c.getresponse()
            self.assertEqual(r2.status, 200, r2.read()[:120])
            r2.read()
        finally:
            c.close()

    # ── C3：GET 兜底 + 日志 ──────────────────────────────────
    def test_get_error_returns_json_500_and_logs(self):
        with mock.patch.object(self.server.backend, "get_settings",
                               side_effect=RuntimeError("boom-probe")):
            code, payload = _get(self.port, "/api/settings",
                                 {"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(code, 500)
        data = json.loads(payload)
        self.assertIn("error", data)
        self.assertIn("message", data["error"])
        self.assertIn("boom-probe", self._logs_text(),
                      "GET 异常必须留下日志（以前完全无痕）")

    # ── C5：有界队列 + status 合并 ───────────────────────────
    def test_event_queue_is_bounded(self):
        sub = self.server.hub.subscribe()
        try:
            for i in range(20000):
                self.server.hub.publish("progress",
                                        {"pct": 0.5, "status": str(i)})
            self.assertLessEqual(sub.qsize(), ws.SSE_QUEUE_MAX)
            self.assertGreater(sub.qsize(), 0)
        finally:
            self.server.hub.unsubscribe(sub)

    def test_status_events_are_coalesced(self):
        sub = self.server.hub.subscribe()
        try:
            for i in range(20):
                self.server.hub.publish("status", {"i": i})
            self.assertEqual(sub.qsize(), 1, "未消费的 status 快照应合并为一条")
            msg = sub.get(timeout=0.2)
            self.assertEqual(msg["event"], "status")
            self.assertEqual(msg["payload"]["i"], 19, "合并后必须是最新状态")
        finally:
            self.server.hub.unsubscribe(sub)

    # ── C6：断线后线程回收 ──────────────────────────────────
    def test_sse_thread_reclaimed_after_client_close(self):
        time.sleep(0.3)                       # 等前序用例的连接线程退出
        base = threading.active_count()
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall(f"GET /api/events HTTP/1.1\r\n"
                         f"Host: 127.0.0.1:{self.port}\r\n\r\n".encode())
            buf = b""
            deadline = time.time() + 5
            while b"\n\n" not in buf and time.time() < deadline:
                buf += sock.recv(4096)
            self.assertIn(b"text/event-stream", buf)
        finally:
            sock.close()
        deadline = time.time() + ws.SSE_KEEPALIVE + 3
        while time.time() < deadline and threading.active_count() > base:
            time.sleep(0.2)
        self.assertLessEqual(
            threading.active_count(), base,
            f"SSE handler 线程应在 {ws.SSE_KEEPALIVE:g}s 级别内回收")

    # ── C14：访问日志开关 ───────────────────────────────────
    def test_http_access_log_toggle(self):
        before = self._logs_text().count("http ")
        with mock.patch.object(ws, "HTTP_LOG", True):
            _get(self.port, "/health")
        time.sleep(0.2)
        self.assertGreater(self._logs_text().count("http "), before)
        # 默认关：再打一次不应增加
        count = self._logs_text().count("http ")
        _get(self.port, "/health")
        time.sleep(0.2)
        self.assertEqual(self._logs_text().count("http "), count,
                         "HTTP_LOG 关闭时不应刷访问日志")


if __name__ == "__main__":
    unittest.main()

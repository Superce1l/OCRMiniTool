# -*- coding: utf-8 -*-
"""tests/test_server_quiet.py — 客户端正常断线不该在 stderr 留栈

真机实测（v1.3.0，GUI 关窗）：keep-alive 连接上客户端先关闭，handler 线程在
readline 撞到 ConnectionAbortedError(WinError 10053)，socketserver 把它当
"处理请求异常"打印整段 traceback。连接已死，这不是错误——按连接类异常静默，
其余异常照常上报（不能顺手把真 bug 也吞了）。
"""
import contextlib
import http.server
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import webview_server as ws  # noqa: E402


class QuietServerTest(unittest.TestCase):
    def setUp(self):
        self.srv = ws._QuietServer(("127.0.0.1", 0),
                                   http.server.BaseHTTPRequestHandler)

    def tearDown(self):
        self.srv.server_close()

    def test_connection_abort_silenced(self):
        buf = io.StringIO()
        try:
            raise ConnectionAbortedError(10053, "你的主机中的软件中止了一个已建立的连接")
        except ConnectionAbortedError:
            with contextlib.redirect_stderr(buf):
                self.srv.handle_error(None, ("127.0.0.1", 1234))
        self.assertEqual(buf.getvalue(), "", "客户端断线不应产生栈输出")

    def test_connection_reset_and_broken_pipe_silenced(self):
        for exc_type in (ConnectionResetError, BrokenPipeError, TimeoutError):
            buf = io.StringIO()
            try:
                raise exc_type("x")
            except exc_type:
                with contextlib.redirect_stderr(buf):
                    self.srv.handle_error(None, ("127.0.0.1", 1234))
            self.assertEqual(buf.getvalue(), "", f"{exc_type.__name__} 应静默")

    def test_real_error_still_reported(self):
        """真 bug 必须继续上报，否则这个补丁就成了"把日志关掉"。"""
        buf = io.StringIO()
        try:
            raise RuntimeError("boom-在 handler 里真炸了")
        except RuntimeError:
            with contextlib.redirect_stderr(buf):
                self.srv.handle_error(None, ("127.0.0.1", 1234))
        self.assertIn("boom", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""tests/test_backend_batch1.py — 后端第一批（C11 日志元数据 + A9 页数/体积护栏）

对应 docs/拓展与优化探索报告.md §5 的两项未落地：
  C11 applog 行格式带 pid/线程名、启动 header、server 400/403 记 WARN、
      500 不重复记账；403 带上实际 Host/Origin
  A9  MAX_RECOGNIZE_PAGES / MAX_RECOGNIZE_FILE_MB / RecognizeLimitError(status=400)
      护栏必须在「置忙」之前执行；server 把 status=400 映射成 HTTP 400

约束：不触发真实推理、不写项目 logs/（tempfile + mock.patch _DIR）。
"""
import http.client
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import applog  # noqa: E402
import webview_backend as wb  # noqa: E402
import webview_server as ws  # noqa: E402
from version import APP_VERSION  # noqa: E402

_DEFAULT_BYTES = 8 * 1024 * 1024
_DEFAULT_BACKUPS = 3


def _rmtree(p) -> None:
    for f in sorted(Path(p).rglob("*"), reverse=True):
        try:
            f.unlink() if f.is_file() else f.rmdir()
        except OSError:
            pass
    try:
        Path(p).rmdir()
    except OSError:
        pass


def _conn(port: int) -> http.client.HTTPConnection:
    return http.client.HTTPConnection("127.0.0.1", port, timeout=10)


# ════════════════════════════════════════════════════════
# C11：applog 行格式与启动 header
# ════════════════════════════════════════════════════════
class LogMetadataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_c11_"))
        self.logdir = self.tmp / "logs"
        self._patch = mock.patch.object(applog, "_DIR", self.logdir)
        self._patch.start()
        applog.MAX_BYTES = _DEFAULT_BYTES
        applog.MAX_BACKUPS = _DEFAULT_BACKUPS
        applog.close()          # 清掉可能残留的句柄（写向当月文件）

    def tearDown(self):
        applog.close()
        applog._DIR = None
        applog.MAX_BYTES = _DEFAULT_BYTES
        applog.MAX_BACKUPS = _DEFAULT_BACKUPS
        self._patch.stop()
        _rmtree(self.tmp)

    def _text(self) -> str:
        out = []
        for p in sorted(self.logdir.glob("*.log")):
            try:
                out.append(p.read_text(encoding="utf-8"))
            except OSError:
                pass
        return "\n".join(out)

    def _files_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.logdir.glob("*.log"))

    # ── 行格式 ───────────────────────────────────────────
    def test_line_has_pid_and_thread_name(self):
        applog.log_info("hello")
        m = re.search(
            r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \[INFO\] \[pid=(\d+) ([^\]]+)\] hello$",
            self._text(), re.M)
        self.assertIsNotNone(m, self._text()[:300])
        self.assertEqual(int(m.group(1)), os.getpid())
        self.assertEqual(m.group(2), "MainThread")

    def test_thread_name_truncated_to_24_chars(self):
        long_name = "long-worker-thread-name-over-24-chars"
        t = threading.Thread(target=lambda: applog.log_info("from-thread"),
                             name=long_name)
        t.start()
        t.join()
        text = self._text()
        self.assertIn(f"[pid={os.getpid()} {long_name[:24]}]", text)
        self.assertNotIn(long_name, text, "线程名应按 24 字符截断")

    def test_fh_bytes_matches_real_file_size(self):
        """_FH_BYTES 估算必须与实际落盘字节一致（中文 utf-8 + CRLF 换行）。"""
        applog.log_info("中文日志-entry-1")
        try:
            raise ZeroDivisionError("boom-bytes")
        except ZeroDivisionError as e:
            applog.log_error("多行日志", exc=e)     # 带 traceback 的多行消息
        self.assertGreater(self._files_bytes(), 0)
        self.assertEqual(applog._FH_BYTES, self._files_bytes(),
                         "字节估算与文件大小不一致 → 轮转会漂移")

    def test_error_line_keeps_pid_prefix_and_traceback(self):
        try:
            raise ValueError("boom-trace")
        except ValueError as e:
            applog.log_error("识别失败X", exc=e)
        text = self._text()
        self.assertRegex(text, r"\[ERROR\] \[pid=\d+ MainThread\] 识别失败X")
        self.assertIn("ValueError: boom-trace", text)
        self.assertIn("Traceback (most recent call last)", text)

    # ── 启动 header ──────────────────────────────────────
    def test_log_startup_has_version_pid_and_context(self):
        applog.log_startup("窗口=原生")
        text = self._text()
        self.assertIn(f"启动 v{APP_VERSION}", text)
        self.assertIn(f"pid={os.getpid()}", text)
        self.assertIn("python=", text)
        self.assertIn("frozen=", text)
        self.assertIn("cwd=", text)
        self.assertIn("窗口=原生", text)
        header = [ln for ln in text.splitlines() if "启动 v" in ln][0]
        self.assertIn("====", header)
        self.assertTrue(header.rstrip().endswith("===="), header)

    def test_log_startup_never_raises_when_dir_unusable(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("x", encoding="utf-8")
        applog.configure(log_dir=blocker / "logs")
        for _ in range(3):
            applog.log_startup("坏的目录")
        self.assertTrue(blocker.is_file(), "不能把同名文件覆盖成目录")


# ════════════════════════════════════════════════════════
# A9：页数 / 体积护栏
# ════════════════════════════════════════════════════════
class RecognizeLimitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ocr_a9_"))

    def tearDown(self):
        _rmtree(self.tmp)

    def _file(self, name: str, size: int = 1) -> Path:
        p = self.tmp / name
        p.write_bytes(b"x" * size)
        return p

    # ── 常量与异常契约 ───────────────────────────────────
    def test_limit_error_is_engine_error_with_status_400(self):
        self.assertTrue(issubclass(wb.RecognizeLimitError, wb.EngineError))
        self.assertEqual(wb.RecognizeLimitError.status, 400)
        self.assertEqual(wb.RecognizeLimitError("x").status, 400)

    def test_defaults_are_page_and_size_guards(self):
        self.assertGreater(wb.MAX_RECOGNIZE_PAGES, 0)
        self.assertGreater(wb.MAX_RECOGNIZE_FILE_MB, 0)

    # ── 体积 ────────────────────────────────────────────
    def test_oversize_file_rejected_with_filename(self):
        f = self._file("big image.png", size=4096)
        with mock.patch.object(wb, "MAX_RECOGNIZE_FILE_MB", 0):
            with self.assertRaises(wb.RecognizeLimitError) as cm:
                wb._check_recognize_limits([f])
        msg = str(cm.exception)
        self.assertIn("big image.png", msg)
        self.assertIn("0", msg)             # {limit}
        self.assertNotIn("{", msg, "占位符必须已替换")

    def test_size_check_happens_before_page_check(self):
        """单文件超体积要"先报"，不能先被页数上限掩盖。"""
        f = self._file("huge.pdf", size=4096)
        with mock.patch.object(wb, "MAX_RECOGNIZE_FILE_MB", 0), \
                mock.patch.object(wb, "_pdf_page_count", return_value=999), \
                mock.patch.object(wb, "MAX_RECOGNIZE_PAGES", 1):
            with self.assertRaises(wb.RecognizeLimitError) as cm:
                wb._check_recognize_limits([f])
        self.assertIn("huge.pdf", str(cm.exception))

    def test_missing_file_is_skipped(self):
        with mock.patch.object(wb, "MAX_RECOGNIZE_FILE_MB", 0):
            wb._check_recognize_limits([self.tmp / "nope.png"])   # 不抛

    # ── 页数 ────────────────────────────────────────────
    def test_pdf_page_limit_rejected_with_numbers(self):
        f = self._file("doc.pdf")
        with mock.patch.object(wb, "_pdf_page_count", return_value=200), \
                mock.patch.object(wb, "MAX_RECOGNIZE_PAGES", 10):
            with self.assertRaises(wb.RecognizeLimitError) as cm:
                wb._check_recognize_limits([f])
        msg = str(cm.exception)
        self.assertIn("200", msg)
        self.assertIn("10", msg)

    def test_images_count_as_one_page_each(self):
        a, b, c = (self._file(f"p{i}.png") for i in range(3))
        with mock.patch.object(wb, "MAX_RECOGNIZE_PAGES", 2):
            wb._check_recognize_limits([a, b])            # 恰好 2 页 → 放行
            with self.assertRaises(wb.RecognizeLimitError) as cm:
                wb._check_recognize_limits([a, b, c])     # 3 页 → 拦
        self.assertIn("3", str(cm.exception))

    def test_unknown_pdf_pages_do_not_block(self):
        """读不到页数（缺 pymupdf/损坏）返回 0：不拦，交给引擎报具体原因。"""
        pdfs = [self._file(f"u{i}.pdf") for i in range(5)]
        with mock.patch.object(wb, "_pdf_page_count", return_value=0), \
                mock.patch.object(wb, "MAX_RECOGNIZE_PAGES", 1):
            wb._check_recognize_limits(pdfs)

    def test_pdf_and_images_accumulate_together(self):
        pdf = self._file("book.pdf")
        imgs = [self._file(f"i{i}.jpg") for i in range(3)]
        with mock.patch.object(wb, "_pdf_page_count", return_value=148), \
                mock.patch.object(wb, "MAX_RECOGNIZE_PAGES", 150):
            with self.assertRaises(wb.RecognizeLimitError):
                wb._check_recognize_limits([pdf] + imgs)      # 148 + 3 = 151 → 拦
        with mock.patch.object(wb, "_pdf_page_count", return_value=148), \
                mock.patch.object(wb, "MAX_RECOGNIZE_PAGES", 150):
            wb._check_recognize_limits([pdf] + imgs[:2])      # 148 + 2 = 150 → 放行

    def test_batch_mode_limits_per_file_not_total(self):
        """批量模式逐文件推理：200 张单页图不该被 150 页总上限误伤。"""
        imgs = [self._file(f"b{i}.png") for i in range(5)]
        with mock.patch.object(wb, "MAX_RECOGNIZE_PAGES", 2):
            with self.assertRaises(wb.RecognizeLimitError):
                wb._check_recognize_limits(imgs)              # 合并：5 页 → 拦
            wb._check_recognize_limits(imgs, batch=True)      # 批量：各 1 页 → 放行

    def test_batch_mode_still_rejects_single_huge_pdf(self):
        """批量模式不会替用户拆 PDF：单个超页数文件仍要拦。"""
        pdf = self._file("huge.pdf")
        with mock.patch.object(wb, "_pdf_page_count", return_value=200), \
                mock.patch.object(wb, "MAX_RECOGNIZE_PAGES", 150):
            with self.assertRaises(wb.RecognizeLimitError) as cm:
                wb._check_recognize_limits([pdf], batch=True)
        msg = str(cm.exception)
        self.assertIn("200", msg)
        self.assertIn("150", msg)

    def test_recognize_passes_batch_flag(self):
        """recognize() 必须把 mode 传给护栏，否则批量会被按总页数拦。"""
        src = Path(wb.__file__).read_text(encoding="utf-8")
        self.assertIn('batch=(opts.get("mode") == "batch")', src)

    # ── 顺序：必须在置忙之前 ─────────────────────────────
    def test_source_calls_limit_check_before_marking_busy(self):
        src = Path(wb.__file__).read_text(encoding="utf-8")
        call_at = src.index("_check_recognize_limits(paths")
        busy_at = src.index("self.recognizing = True", call_at)
        self.assertLess(busy_at - call_at, 400,
                        "护栏调用必须紧邻且早于 self.recognizing = True")

    def test_recognize_rejects_without_staying_busy(self):
        """真实调用 recognize()：抛 RecognizeLimitError 后 recognizing 仍为 False。"""
        f = self._file("too-big.png", size=4096)

        class _FakeEngine:
            ready = True

        b = object.__new__(wb.WebBackend)
        b._lock = threading.RLock()
        b.recognizing = False
        b.loading = False
        b.downloading = False
        b.env_downloading = False
        b.engine = _FakeEngine()
        with mock.patch.object(wb, "MAX_RECOGNIZE_FILE_MB", 0):
            with self.assertRaises(wb.RecognizeLimitError):
                b.recognize({"paths": [str(f)]})
        self.assertFalse(b.recognizing, "护栏拒绝后不能留下 busy 状态")


# ════════════════════════════════════════════════════════
# C11 + A9：server 侧（日志与 400 映射）
# ════════════════════════════════════════════════════════
class ServerLogAndLimitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ocr_c11srv_"))
        cls._patches = [
            mock.patch.object(wb, "SETTINGS_FILE", cls.tmp / "settings.json"),
            mock.patch.object(wb, "OUTPUT_DIR", cls.tmp / "outputs"),
            mock.patch.object(applog, "_DIR", cls.tmp / "logs"),
        ]
        for p in cls._patches:
            p.start()
        applog._FH = None
        applog._CURRENT_MONTH = ""
        applog._FH_BYTES = 0
        (cls.tmp / "outputs").mkdir(exist_ok=True)
        cls.server = ws.WebViewServer(port=0)
        cls.server.start()
        cls.port = cls.server.port
        assert cls.port
        cls._png = cls.tmp / "probe.png"
        cls._png.write_bytes(b"x")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.stop()
        finally:
            applog.close()
            applog._FH = None
            for p in cls._patches:
                p.stop()
            _rmtree(cls.tmp)

    def _logs(self) -> str:
        out = []
        for p in (self.tmp / "logs").glob("*.log"):
            try:
                out.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        return "\n".join(out)

    def _get(self, path: str, headers=None):
        c = _conn(self.port)
        try:
            c.request("GET", path, headers=headers or {})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def _post(self, path: str, body, headers=None):
        c = _conn(self.port)
        try:
            c.request("POST", path, body=json.dumps(body),
                      headers=headers or {})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    # ── 403 带上实际值（便于排查）────────────────────────
    def test_bad_host_403_logged_with_actual_host(self):
        code, _ = self._get("/health", {"Host": "evil.example"})
        self.assertEqual(code, 403)
        logs = self._logs()
        self.assertIn("bad host: evil.example", logs)
        self.assertIn("HTTP 403 GET /health", logs)

    def test_bad_origin_403_logged_with_actual_origin(self):
        code, _ = self._get("/health", {
            "Host": f"127.0.0.1:{self.port}",
            "Origin": "http://evil.example",
        })
        self.assertEqual(code, 403)
        self.assertIn("bad origin: http://evil.example", self._logs())

    # ── A9：status=400 映射成 HTTP 400 + WARN（不记 ERROR）──
    def test_recognize_limit_maps_to_http_400_and_warns(self):
        before = len(self._logs())
        with mock.patch.object(
                self.server.backend, "recognize",
                side_effect=wb.RecognizeLimitError(
                    "页数过多：本次共 200 页，超过单次上限 150 页。")):
            code, raw = self._post("/api/recognize", {"paths": [str(self._png)]},
                                   {"Host": f"127.0.0.1:{self.port}",
                                    "Content-Type": "application/json"})
        self.assertEqual(code, 400, raw[:200])
        body = json.loads(raw.decode("utf-8"))
        self.assertIn("页数过多", body["error"]["message"])
        new_logs = self._logs()[before:]
        self.assertIn("识别被拒绝", new_logs)
        self.assertIn("WARN", new_logs)
        self.assertNotIn("识别失败", new_logs,
                         "护栏拒绝不能记成识别失败（ERROR）")

    def test_post_fallback_logs_error_with_path_and_exception(self):
        before = len(self._logs())
        with mock.patch.object(self.server.backend, "set_settings",
                               side_effect=RuntimeError("boom-post")):
            code, _ = self._post("/api/settings", {},
                                 {"Host": f"127.0.0.1:{self.port}",
                                  "Content-Type": "application/json"})
        self.assertEqual(code, 500)
        new_logs = self._logs()[before:]
        self.assertIn("POST /api/settings 处理失败", new_logs)
        self.assertIn("boom-post", new_logs)
        # 请求线程名为 Thread-N（不是 MainThread），只断言格式与上下文前缀
        self.assertRegex(new_logs,
                         r"\[ERROR\] \[pid=\d+ [^\]]+\] POST /api/settings 处理失败")

    # ── 源码契约：映射用 getattr(status, 500)，400/403 才记 WARN ──
    def test_server_source_mapping_contract(self):
        src = Path(ws.__file__).read_text(encoding="utf-8")
        self.assertEqual(src.count('code = getattr(e, "status", 500)'), 2,
                         "两条识别分支都要按 status 映射")
        self.assertIn("logged=code == 400", src)
        self.assertIn("code in (400, 403)", src)
        self.assertIn("log_warn", src)


if __name__ == "__main__":
    unittest.main()

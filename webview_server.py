"""webview_server.py — OCRMiniTool 的本机 HTTP 服务器（纯标准库）

与 STTMiniTool 同一架构：webview/ 静态前端 + /api/* 业务端点由同一个
stdlib HTTP 服务器提供，进度/状态以 SSE（/api/events）主动推给前端。
零第三方依赖，方便 PyInstaller 打包。

安全：只绑 127.0.0.1（loopback），外部连不到、不触发防火墙提示；
Host/Origin 校验挡 DNS rebinding / 跨站 POST。

路由：
  GET  /                    → webview/index.html
  GET  /css/* /js/* …       → webview/ 静态档
  GET  /health              → {"status","modelReady"}
  GET  /api/status          → 后端状态（模型状态机 + 设备信息）
  GET  /api/settings        → 设置；POST 改设置（json patch）
  GET  /api/history         → outputs/ 最近识别结果
  GET  /api/events          → SSE：data: {"event","payload"} 进度/状态推送
  POST /api/download        → 开始下载模型（进度走 SSE dl-progress）
  POST /api/load            → 加载模型（进度走 SSE load-progress）
  POST /api/recognize       → multipart 多文件上传识别，或 JSON {"paths":[…]}
                              直读本机路径（不复制文件），回 {text,pages,outPath}
  POST /api/scan-dir        → 列目录下的可识别文件（只回路径，不回传内容）
  POST /api/cancel          → 请求取消当前任务
  POST /api/history-read    → 读取一条历史结果的文本
  POST /api/history-delete  → 删除一条历史结果（移入回收站）
  POST /api/browse-file     → 原生「选择文件」对话框
  POST /api/browse-folder   → 原生「选择文件夹」对话框
  POST /api/probe-python    → 探测指定解释器的 torch/CUDA/transformers
  POST /api/apply-python    → 保存 pythonPath 并以新解释器重启应用
  POST /api/open-output     → 打开输出文件夹
  POST /api/shutdown        → 结束进程
"""
from __future__ import annotations

import json
import os
import queue
import select
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from applog import log_error, log_info
from i18n import tr
from webview_backend import WebBackend, normalize_recognize_paths

# SSE keepalive 周期（秒）。同时是"客户端已断开"的检测粒度：写已关连接在
# Windows 上首次可能成功，靠 _peer_gone() + 这个周期把线程回收压到 ≤5s。
SSE_KEEPALIVE = 5.0

# 每个 SSE 连接的缓冲上限：客户端读得慢/假死时丢最旧，不再无限吃内存。
SSE_QUEUE_MAX = 256

# 访问日志：默认关闭（SSE 与状态轮询会刷屏）。排查时设环境变量
# OCRMT_HTTP_LOG=1，或进程内把本模块的 _HTTP_LOG 置 True。
HTTP_LOG = (os.environ.get("OCRMT_HTTP_LOG") or "").strip().lower() not in (
    "", "0", "false", "no", "off")


def resolve_web_dir() -> Path:
    """前端资产目录：源码用项目 webview/；PyInstaller 冻结时用 _MEIPASS。"""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        for name in ("webview_assets", "webview"):
            d = base / name
            if (d / "index.html").is_file():
                return d
        return base / "webview_assets"
    return Path(__file__).resolve().parent / "webview"


WEB_DIR = resolve_web_dir()


def _parse_multipart_stream(rfile, boundary: bytes, out_dir: Path,
                            content_length: int, chunk_size: int = 1 << 20):
    """流式解析 multipart/form-data，文件部件直接落盘（不全量进内存）。

    与 STTMiniTool 同名函数同源；唯一差别：同名部件收集成列表
    （files: dict[name, list[(filename, Path)]]），支持「files」多文件上传。
    """
    fields: dict[str, str] = {}
    files: dict[str, list] = {}
    marker = b"--" + boundary
    delim = b"\r\n" + marker
    remaining = content_length

    def _read(n: int) -> bytes:
        nonlocal remaining
        if remaining <= 0:
            return b""
        n = min(n, remaining)
        data = rfile.read(n)
        remaining -= len(data)
        return data

    buf = b""
    keep = len(marker) - 1
    while True:
        i = buf.find(marker)
        if i >= 0:
            buf = buf[i + len(marker):]
            break
        if len(buf) > keep:
            buf = buf[-keep:]
        chunk = _read(chunk_size)
        if not chunk:
            return fields, files
        buf += chunk

    while True:
        while len(buf) < 2:
            chunk = _read(chunk_size)
            if not chunk:
                return fields, files
            buf += chunk
        if buf[:2] == b"--":
            return fields, files
        buf = buf[2:]

        while b"\r\n\r\n" not in buf:
            chunk = _read(chunk_size)
            if not chunk:
                return fields, files
            buf += chunk
        header_blob, _, buf = buf.partition(b"\r\n\r\n")
        name = filename = None
        for line in header_blob.decode("utf-8", errors="replace").split("\r\n"):
            key, _, val = line.partition(":")
            key, val = key.strip().lower(), val.strip()
            if key == "content-disposition":
                for tok in val.split(";"):
                    tok = tok.strip()
                    if tok.startswith("name="):
                        name = tok[5:].strip('"')
                    elif tok.startswith("filename="):
                        filename = tok[9:].strip('"')
        if name is None:
            name = ""

        sink = None
        acc: bytearray | None = None
        if filename is not None:
            ext = "".join(c for c in Path(filename).suffix
                          if c.isalnum() or c == ".")[:16]
            dest = out_dir / f"part_{sum(len(v) for v in files.values())}_upload{ext or '.bin'}"
            sink = open(dest, "wb")
            files.setdefault(name, []).append((filename, dest))
        else:
            acc = bytearray()

        def _emit(piece: bytes):
            if sink is not None:
                sink.write(piece)
            elif acc is not None:
                acc.extend(piece)

        keep = len(delim) - 1
        search_from = 0
        try:
            while True:
                i = buf.find(delim, search_from)
                if i >= 0:
                    nxt = buf[i + len(delim): i + len(delim) + 2]
                    if len(nxt) < 2:
                        chunk = _read(chunk_size)
                        if chunk:
                            buf += chunk
                            continue
                    if nxt[:2] in (b"\r\n", b"--"):
                        _emit(buf[:i])
                        buf = buf[i + len(delim):]
                        break
                    search_from = i + 1
                    continue
                if len(buf) > keep:
                    _emit(buf[:-keep])
                    buf = buf[-keep:]
                    search_from = 0
                chunk = _read(chunk_size)
                if not chunk:
                    _emit(buf)
                    buf = b""
                    break
                buf += chunk
        finally:
            if sink is not None:
                sink.close()
        if acc is not None:
            fields[name] = bytes(acc).decode("utf-8", errors="replace")


_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class _Subscriber:
    """一条 SSE 连接的事件缓冲（有界 + status 合并）。

    • 有界（SSE_QUEUE_MAX）：客户端读得慢或假死时丢最旧，内存不随事件数增长；
    • status 合并：未消费的 status 只占队里一个"占位符"，真实快照存在
      `_status` 槽里并不断被最新值覆盖——合并后前端拿到的始终是**最新**状态。
    """

    __slots__ = ("_q", "_lock", "_status", "_status_queued")

    def __init__(self, maxsize: int = SSE_QUEUE_MAX):
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._status: dict | None = None
        self._status_queued = False

    def put(self, msg: dict):
        with self._lock:
            if msg.get("event") == "status":
                self._status = msg                 # 快照：后到的覆盖先到的
                if self._status_queued:
                    return                         # 队里已有占位符，不必再入队
                if self._enqueue_locked(_STATUS_TOKEN):
                    self._status_queued = True
                return
            self._enqueue_locked(msg)

    def _enqueue_locked(self, item) -> bool:
        try:
            self._q.put_nowait(item)
            return True
        except queue.Full:
            try:
                self._q.get_nowait()               # 丢最旧
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(item)
                return True
            except queue.Full:
                return False

    def get(self, timeout: float | None = None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if deadline is None:
                item = self._q.get()
            else:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise queue.Empty
                item = self._q.get(timeout=left)
            if item is not _STATUS_TOKEN:
                return item
            with self._lock:
                msg = self._status
                self._status = None
                self._status_queued = False
            if msg is not None:
                return msg
            # 极小概率：占位符还在但快照已被取走 → 继续取下一条

    def qsize(self) -> int:
        return self._q.qsize()


# status 占位符（只用于唤醒消费者；真实快照在 _Subscriber._status）
_STATUS_TOKEN = object()


class _EventHub:
    """SSE 广播：每个连接一个 _Subscriber，publish() 推给全部订阅者。"""

    def __init__(self):
        self._subs: set[_Subscriber] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> _Subscriber:
        sub = _Subscriber()
        with self._lock:
            self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: _Subscriber):
        with self._lock:
            self._subs.discard(sub)

    def publish(self, event: str, payload: dict):
        msg = {"event": event, "payload": payload}
        with self._lock:
            subs = list(self._subs)
        for sub in subs:
            try:
                sub.put(msg)
            except Exception:
                pass


class _QuietServer(ThreadingHTTPServer):
    """客户端正常断线（关页面/刷新）不应该在日志里留下一整段栈。

    实测（真机 GUI 关窗、浏览器刷新）：keep-alive 连接复用中客户端先关闭，
    server 的 handler 线程在 readline 上撞到 ConnectionAbortedError
    (WinError 10053)，socketserver 会把它当"处理请求异常"打印整段 traceback。
    连接已经死了，这不是错误；按连接类异常静默掉，其余异常照常上报。
    """

    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class WebViewServer:
    """本机前端服务器。持有 WebBackend + EventHub。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.hub = _EventHub()
        self.backend = WebBackend(on_event=self.hub.publish)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._want_port = port
        self.port = port

    def start(self):
        if self._httpd:
            return
        server = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            _body_read = 0          # 本次请求已从 rfile 读掉的字节数（早退兜底用）

            def log_message(self, fmt, *args):
                """访问日志（默认关）。打开后每条一行：客户端 请求行 状态码。"""
                if not HTTP_LOG:
                    return
                try:
                    log_info(f"http {self.address_string()} "
                             f"{(fmt % args) if args else fmt}")
                except Exception:
                    pass

            def _json(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _err(self, code, msg):
                self._json({"error": {"message": msg}}, code)

            def _read_json_body(self) -> dict:
                try:
                    n = int(self.headers.get("Content-Length", 0))
                except (TypeError, ValueError):
                    self._body_read = 0
                    raise
                if not n:
                    return {}
                try:
                    raw = self.rfile.read(n)
                    self._body_read = len(raw)
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    return {}

            def _drain_body(self, limit: int = 1 << 20) -> bool:
                """早退（403/400/404/500）前把没读的请求体读掉。

                HTTP/1.1 keep-alive 下，残留的请求体会被当成下一条请求的起始
                行 → 客户端下一个请求收到 400/501（实测复现）。返回 False 表示
                体太大或读失败，此时置 close_connection，宁可断开也不串包。
                """
                # 只有"带实体"的方法才排空：GET/HEAD 声称有 body 一律当异常，
                # 直接关连接（不去读一个可能永远不来的 body，避免卡住 handler）。
                if self.command not in ("POST", "PUT", "PATCH"):
                    try:
                        declared = int(self.headers.get("Content-Length") or 0)
                    except (TypeError, ValueError):
                        declared = 0
                    if declared > 0:
                        self.close_connection = True
                    return False
                try:
                    declared = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    declared = 0
                left = declared - getattr(self, "_body_read", 0)
                if left <= 0:
                    return True
                if left > limit:
                    self.close_connection = True
                    return False
                try:
                    while left > 0:
                        chunk = self.rfile.read1(min(left, 1 << 16))
                        if not chunk:
                            self.close_connection = True
                            return False
                        left -= len(chunk)
                        self._body_read = self._body_read + len(chunk)
                except Exception:
                    self.close_connection = True
                    return False
                return True

            def _check_host(self) -> bool:
                allowed = {f"127.0.0.1:{server.port}", f"localhost:{server.port}"}
                if self.headers.get("Host", "") not in allowed:
                    self._drain_body()
                    self._err(403, "bad host")
                    return False
                origin = self.headers.get("Origin")
                if origin and origin not in {f"http://{a}" for a in allowed}:
                    self._drain_body()
                    self._err(403, "bad origin")
                    return False
                return True

            # ── GET ───────────────────────────────────────────
            def do_GET(self):
                path = urlparse(self.path).path
                if (path.startswith("/api/") or path == "/health") and not self._check_host():
                    return
                try:
                    return self._route_get(path)
                except (BrokenPipeError, ConnectionResetError):
                    return
                except Exception as e:
                    # 读路径同样要兜底：以前 GET 内抛异常 = 客户端只看到
                    # RemoteDisconnected、日志里一个字都没有（实测）。
                    log_error(f"GET {path} 处理失败", exc=e)
                    self._drain_body()
                    try:
                        return self._err(500, tr("err.internal"))
                    except Exception:
                        self.close_connection = True
                        return

            def _route_get(self, path: str):
                if path == "/health":
                    return self._json({"status": "ok",
                                       "modelReady": bool(server.backend.engine.ready)})
                if path == "/api/status":
                    return self._json(server.backend.get_status())
                if path == "/api/env":
                    return self._json(server.backend.env_status())
                if path == "/api/settings":
                    return self._json(server.backend.get_settings())
                if path == "/api/history":
                    return self._json(server.backend.list_history())
                if path == "/api/events":
                    return self._sse()
                if path.startswith("/api/"):
                    return self._err(404, "unknown api")
                return self._static(path)

            # ── POST ──────────────────────────────────────────
            def do_POST(self):
                path = urlparse(self.path).path
                if not self._check_host():
                    return
                try:
                    if path == "/api/settings":
                        return self._json(server.backend.set_settings(self._read_json_body()))
                    if path == "/api/download":
                        return self._json(server.backend.start_download())
                    if path == "/api/env/download":
                        body = self._read_json_body()
                        return self._json(server.backend.start_env_download(
                            body.get("flavor", "")))
                    if path == "/api/env/set":
                        body = self._read_json_body()
                        return self._json(server.backend.set_env_flavor(
                            body.get("flavor", "")))
                    if path == "/api/env/cancel":
                        return self._json({"ok": server.backend.cancel_env_download()})
                    if path == "/api/load":
                        return self._json(server.backend.start_load())
                    if path == "/api/recognize":
                        return self._recognize()
                    if path == "/api/cancel":
                        return self._json({"ok": server.backend.cancel()})
                    if path == "/api/history-read":
                        body = self._read_json_body()
                        return self._json(server.backend.history_read(body.get("name", "")))
                    if path == "/api/history-delete":
                        body = self._read_json_body()
                        return self._json(server.backend.history_delete(body.get("name", "")))
                    if path == "/api/browse-folder":
                        body = self._read_json_body()
                        # 标题空 → 后端按当前语言兜底（i18n.tr）
                        return self._json(server.backend.browse_folder(
                            body.get("start", ""), body.get("title", "")))
                    if path == "/api/browse-file":
                        body = self._read_json_body()
                        return self._json(server.backend.browse_file(
                            body.get("start", ""), body.get("title", "")))
                    if path == "/api/probe-python":
                        body = self._read_json_body()
                        return self._json(server.backend.probe_python(
                            body.get("path", "")))
                    if path == "/api/apply-python":
                        body = self._read_json_body()
                        return self._json(server.backend.apply_python(
                            body.get("path", "")))
                    if path == "/api/scan-dir":
                        body = self._read_json_body()
                        return self._json(server.backend.scan_dir(
                            body.get("path", ""),
                            recursive=bool(body.get("recursive", True)),
                            max_files=body.get("maxFiles", 500),
                            max_depth=body.get("maxDepth", 8)))
                    if path == "/api/open-output":
                        return self._json({"ok": server.backend.open_output_dir()})
                    if path == "/api/shutdown":
                        return self._shutdown()
                    self._drain_body()
                    return self._err(404, "unknown api")
                except Exception as e:
                    # 兜底：请求体可能没读完（keep-alive 残留体会串成下一个
                    # 请求的起始行），先排空再回应。
                    self._drain_body()
                    return self._err(500, str(e))

            def _shutdown(self):
                import threading as _t

                def _kill():
                    try:
                        self._json({"ok": True})
                    except Exception:
                        pass
                    # C9：先请求取消并给任务最多 2s 收尾（下载留可续传残档、
                    # 识别不静默丢结果），再停服；最后仍是 os._exit 硬退出
                    # （torch/DLL 正常卸载有卡死实录，不能改回 sys.exit）。
                    try:
                        server.backend.cancel_all()
                    except Exception:
                        pass
                    try:
                        server.backend.wait_for_tasks(2.0)
                    except Exception:
                        pass
                    try:
                        server.stop()
                    except Exception:
                        pass
                    os._exit(0)

                _t.Thread(target=_kill, daemon=True).start()
                return None

            def _static(self, path):
                if path in ("", "/"):
                    path = "/index.html"
                target = (WEB_DIR / path.lstrip("/")).resolve()
                if WEB_DIR.resolve() not in target.parents and target != WEB_DIR.resolve():
                    return self._err(403, "forbidden")
                if not target.is_file():
                    return self._err(404, "not found")
                data = target.read_bytes()
                ctype = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)

            def _peer_gone(self) -> bool:
                """客户端是否已断开（SSE 客户端从不发数据：可读且 peek 到空 = FIN）。

                Windows 上向已关闭连接写第一次可能仍"成功"，只靠写失败回收要等
                两个 keepalive 周期；这里用 0 超时 select + MSG_PEEK 精确判断。
                """
                try:
                    readable, _, _ = select.select([self.connection], [], [], 0)
                    if not readable:
                        return False
                    return self.connection.recv(1, socket.MSG_PEEK) == b""
                except OSError:
                    return True

            def _sse(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                sub = server.hub.subscribe()
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    init = {"event": "status",
                            "payload": server.backend.get_status()}
                    self.wfile.write(f"data: {json.dumps(init, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    while not self._peer_gone():
                        try:
                            msg = sub.get(timeout=SSE_KEEPALIVE)
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        self.wfile.write(f"data: {json.dumps(msg, ensure_ascii=False)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    server.hub.unsubscribe(sub)

            # ── 识别（multipart 上传 / JSON 路径直读）──────────
            def _recognize(self):
                ctype = self.headers.get("Content-Type", "")
                if "application/json" in ctype:
                    # 前端分流：有 File 走 multipart；只有路径走 JSON 直读
                    # （不落上传临时目录，校验与 scan-dir 同一套边界）
                    return self._recognize_paths()
                if "multipart/form-data" not in ctype or "boundary=" not in ctype:
                    self._drain_body()
                    return self._err(400, tr("err.needMultipart"))
                boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
                try:
                    n = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    self._drain_body()
                    return self._err(400, tr("err.noContentLength"))
                if n <= 0:
                    return self._err(400, tr("err.emptyBody"))

                tmp_dir = Path(tempfile.mkdtemp(prefix="ocr_web_"))
                # 解析、校验、识别任何一步退出都要清掉临时目录
                try:
                    fields, files = _parse_multipart_stream(
                        self.rfile, boundary, tmp_dir, n)
                    self._body_read = n          # 解析器已把整个体读完
                    uploads = files.get("files") or files.get("file") or []
                    if not uploads:
                        return self._err(400, tr("err.noFilesPart"))
                    for _fname, p in uploads:
                        if not p.is_file() or p.stat().st_size == 0:
                            return self._err(400, tr("err.emptyUpload"))

                    # 保持前端给出的页序
                    paths = [str(p) for _fn, p in uploads]
                    primary_name = Path(uploads[0][0] or "result").stem or "result"
                    # 多文件用公共前缀（同名不同扩展）或「前缀 等 N 个文件」
                    if len(uploads) > 1:
                        stems = {Path(fn or "").stem for fn, _ in uploads if fn}
                        primary_name = (sorted(stems)[0] if stems else "result") + \
                                       (f" 等{len(uploads)}个" if len(stems) > 1 else "")
                    opts = {
                        "paths": paths,
                        # 原始文件名（与 paths 一一对应）：批量模式的条目名/产物命名用
                        "names": [fn or "" for fn, _p in uploads],
                        "name": primary_name,
                        "prompt": fields.get("prompt", ""),
                        "mode": (fields.get("mode") or "").strip(),
                    }
                    try:
                        result = server.backend.recognize(
                            opts, progress_cb=lambda pct, status:
                            server.hub.publish("progress", {"pct": pct, "status": status}))
                        return self._json(result)
                    except Exception as e:
                        log_error(f"识别失败：{primary_name}", exc=e)
                        head = str(e).splitlines()[0][:300] if str(e) else type(e).__name__
                        return self._err(500, head)
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)

            def _recognize_paths(self):
                """`{"paths": [...]}` 直读：不复制文件，校验后直接用原路径识别。"""
                body = self._read_json_body()
                paths, bad = normalize_recognize_paths(body.get("paths"))
                if bad is not None:
                    return self._json(bad, 400)
                opts = {
                    "paths": [str(p) for p in paths],
                    "names": [p.name for p in paths],
                    "name": (paths[0].stem if len(paths) == 1
                             else f"{paths[0].stem} 等{len(paths)}个"),
                    "prompt": body.get("prompt", ""),
                    "mode": (body.get("mode") or "").strip(),
                }
                primary_name = opts["name"]
                try:
                    result = server.backend.recognize(
                        opts, progress_cb=lambda pct, status:
                        server.hub.publish("progress",
                                           {"pct": pct, "status": status}))
                    return self._json(result)
                except Exception as e:
                    log_error(f"识别失败：{primary_name}", exc=e)
                    head = str(e).splitlines()[0][:300] if str(e) else \
                        type(e).__name__
                    return self._err(500, head)

        self._httpd = _QuietServer((self.host, self._want_port), _Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self.port

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


if __name__ == "__main__":
    # 独立启动（除错用）：起 server，打印网址，不自动开窗口。
    srv = WebViewServer(port=8770)
    srv.start()
    print(f"WebView server: {srv.url}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.stop()
